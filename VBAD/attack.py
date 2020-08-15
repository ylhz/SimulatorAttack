import argparse
import collections
import logging
import json
import sys
import os.path as osp
import glob
import numpy as np
import os

import random
import torch
from torch.nn import functional as F
from VBAD.group_generator import EquallySplitGrouping
from VBAD.models import ResNetFeatureExtractor, DensenetFeatureExtractor
from VBAD.tentative_perturbation_generator import TentativePerturbationGenerator
from config import CLASS_NUM, MODELS_TEST_STANDARD, PY_ROOT
from dataset.dataset_loader_maker import DataLoaderMaker
import glog as log
from dataset.standard_model import StandardModel
from dataset.defensive_model import DefensiveModel
from torchvision import models

class VBADAttack(object):

    def __init__(self, args, directions_generator):
        self.rank_transform = not args.no_rank_transform
        self.random_mask = args.random_mask

        self.image_split = args.image_split
        self.sub_num_sample = args.sub_num_sample
        self.sigma = args.sigma
        self.starting_eps = args.starting_eps
        self.eps = args.epsilon
        self.sample_per_draw = args.sample_per_draw
        self.directions_generator = directions_generator
        self.max_iter = args.max_queries
        self.delta_eps = args.delta_eps
        self.max_lr = args.max_lr
        self.min_lr = args.min_lr

        self.dataset_loader = DataLoaderMaker.get_test_attacked_data(args.dataset, 1)
        self.total_images = len(self.dataset_loader.dataset)
        self.query_all = torch.zeros(self.total_images)
        self.correct_all = torch.zeros_like(self.query_all)  # number of images
        self.not_done_all = torch.zeros_like(self.query_all)  # always set to 0 if the original image is misclassified
        self.success_all = torch.zeros_like(self.query_all)
        self.success_query_all = torch.zeros_like(self.query_all)
        self.not_done_prob_all = torch.zeros_like(self.query_all)
        self.dataset_name = args.dataset
        if self.dataset_name == "TinyImageNet":
            self.candidate_loader = DataLoaderMaker.get_candidate_attacked_data(self.dataset_name, 1)
            self.dataset = self.candidate_loader.dataset
        else:
            self.dataset = self.dataset_loader.dataset
        self.label_data_index_dict = self.get_label_dataset(self.dataset)

    def get_label_dataset(self, dataset):
        label_index = collections.defaultdict(list)
        for index, (*_, label) in enumerate(dataset):  # 保证这个data_loader没有shuffle过
            label_index[label].append(index)
        return label_index

    def get_image_of_class(self, target_labels, dataset, target_model):
        images = []
        for label in target_labels:  # length of target_labels is 1
            index = random.choice(self.label_data_index_dict[label.item()])
            image, label_ = dataset[index]
            with torch.no_grad():
                logits = target_model(image.unsqueeze(0).cuda())
            while logits.max(1)[1].item() != label.item():
                index = random.choice(self.label_data_index_dict[label.item()])
                image, label_ = dataset[index]
                with torch.no_grad():
                    logits = target_model(image.unsqueeze(0).cuda())

            assert label_ == label.item()
            images.append(image)
        return torch.stack(images).cuda()  # B,C,H,W

    def get_image_of_class_ImageNet(self, target_labels, dataset, target_model):
        images = []
        for label in target_labels:  # length of target_labels is 1
            index = random.choice(self.label_data_index_dict[label.item()])
            image_small, image_big, label_ = dataset[index]
            if target_model.input_size[-1] >= 299:
                image, true_labels = image_big, label
            else:
                image, true_labels = image_small, label
            if image.size(-1) != target_model.input_size[-1]:
                image = F.interpolate(image.unsqueeze(0), size=target_model.input_size[-1], mode='bilinear',align_corners=True)
            with torch.no_grad():
                logits = target_model(image.cuda())
            while logits.max(1)[1].item() != label.item():
                index = random.choice(self.label_data_index_dict[label.item()])
                image_small, image_big, label = dataset[index]
                if target_model.input_size[-1] >= 299:
                    image, true_labels = image_big, label
                else:
                    image, true_labels = image_small, label
                if image.size(-1) != target_model.input_size[-1]:
                    image = F.interpolate(image.unsqueeze(0), size=target_model.input_size[-1], mode='bilinear', align_corners=True)
                with torch.no_grad():
                    logits = target_model(image.cuda())

            assert label_ == label.item()
            images.append(torch.squeeze(image))
        return torch.stack(images).cuda()  # B,C,H,W

    def sim_rectification_vector(self, model, adv_images, tentative_directions, n, sigma, target_class, rank_transform,
                                 sub_num, group_gen, untargeted):

        # 此处的frame_number只能选择1
        with torch.no_grad():
            grads = torch.zeros(len(group_gen), device='cuda') # len(group_len) = frame_number * patch_number of a image
            count_in = 0
            loss_total = 0
            log.info('sampling....')
            batch_loss = []
            batch_noise = []
            batch_idx = []

            assert n % sub_num == 0 and sub_num % 2 == 0
            for _ in range(n // sub_num):
                adv_vid_rs = adv_images.repeat((sub_num,) + (1,) * len(adv_images.size())) # shape = (sub_num, frame_number, C, H, W)
                noise_list = torch.randn((sub_num // 2,) + grads.size(), device='cuda') * sigma  # shape = (sub_num//2, frame_number * patch number * patch_number)
                all_noise = torch.cat([noise_list, -noise_list], 0)   # shape = (sub_num, frame_number * patch number * patch_number)
                perturbation_sample = group_gen.apply_group_change(tentative_directions, all_noise)  # 1 个patch一个扰动数值
                adv_vid_rs += perturbation_sample  # shape = (sub_num, frame_number, C, H, W)
                del perturbation_sample
                top_val, top_idx, logits = self.output_top_values(model, adv_vid_rs)  # logits shape = (sub_num,)
                # top_val & top_idx shape = (sub_num,1),
                if untargeted:
                    loss = -torch.max(logits, 1)[0]
                else:
                    loss = F.cross_entropy(logits, torch.tensor(target_class, dtype=torch.long,
                                                               device='cuda').repeat(sub_num), reduction='none')
                batch_loss.append(loss)
                batch_idx.append(top_idx)
                batch_noise.append(all_noise)
            batch_noise = torch.cat(batch_noise, 0)
            batch_idx = torch.cat(batch_idx)  # n, 1
            batch_loss = torch.cat(batch_loss)  # n

            # Apply rank-based loss transformation
            if rank_transform:
                good_idx = torch.sum(batch_idx == target_class, 1).byte()  # good_idx shape = (n,)
                changed_loss = torch.where(good_idx, batch_loss, torch.tensor(1000., device='cuda'))
                loss_order = torch.zeros(changed_loss.size(0), device='cuda')
                sort_index = changed_loss.sort()[1]
                loss_order[sort_index] = torch.arange(0, changed_loss.size(0), device='cuda', dtype=torch.float)
                available_number = torch.sum(good_idx).item()
                count_in += available_number
                unavailable_number = n - available_number
                unavailable_weight = torch.sum(torch.where(good_idx, torch.tensor(0., device='cuda'),
                                                           loss_order)) / unavailable_number if unavailable_number else torch.tensor(
                    0., device='cuda')
                rank_weight = torch.where(good_idx, loss_order, unavailable_weight) / (n - 1)
                grads += torch.sum(batch_noise / sigma * (rank_weight.view((-1,) + (1,) * (len(batch_noise.size()) - 1))), 0)
            else:
                idxs = (batch_idx == target_class).nonzero()
                valid_idxs = idxs[:, 0]
                valid_loss = torch.index_select(batch_loss, 0, valid_idxs)

                loss_total += torch.mean(valid_loss).item()
                count_in += valid_loss.size(0)
                noise_select = torch.index_select(batch_noise, 0, valid_idxs)
                grads += torch.sum(noise_select / sigma * (valid_loss.view((-1,) + (1,) * (len(noise_select.size()) - 1))),0)

            if count_in == 0:
                return None, None
            log.info('count in: {}'.format(count_in))
            return loss_total / count_in, grads


    def output_top_values(self, model, images, k=1):
        if images.dim() == 5:
            images = images.squeeze(1)  #   (sub_num, frame_number, C, H, W) -> (sub_num, C, H, W)
        assert images.dim() == 4
        with torch.no_grad():
            out = model(images)
            top_val, top_idx = torch.topk(F.softmax(out,1), k, dim=-1)  # top_val是最大logits的那个位置的概率，top_idx是对应class id
        return top_val, top_idx, out

    # Input images should be torch.tensor and its shape should be [b, c, w, h].
    # The input should be normalized to [0, 1]
    # target_labels shape = (batch_size,)
    def targeted_attack(self, target_model, images, target_class_images, target_labels):
        assert target_labels.size(0) == 1
        target_class = target_labels[0].item()

        delta_eps = self.delta_eps
        adv_images = target_class_images.clone()
        query_num = torch.zeros(adv_images.size(0))  # shape = 1
        cur_eps = self.starting_eps

        explore_succ = collections.deque(maxlen=5)
        reduce_eps_fail = 0
        cur_min_lr = self.min_lr
        cur_max_lr = self.max_lr

        delta_eps_schedule = [0.01, 0.003, 0.001, 0]
        update_steps = [1, 10, 100, 100]
        update_weight = [2, 1.5, 1.5, 1.5]
        cur_eps_period = 0

        group_gen = EquallySplitGrouping(self.image_split)

        while query_num.min().item() < self.max_iter:
            top_val, top_idx, _ = self.output_top_values(target_model, adv_images)  # shape = (frame_number,1)
            query_num += 1

            tentative_directions = self.directions_generator(adv_images).cuda()  # shape = (frame_number, C, H, W)
            group_gen.initialize(tentative_directions)

            l, g = self.sim_rectification_vector(target_model, adv_images, tentative_directions, self.sample_per_draw, self.sigma,
                                                 target_class, self.rank_transform, self.sub_num_sample, group_gen, untargeted=False)
            query_num += self.sample_per_draw
            if l is None and g is None:
                log.info('nes sim fails, try again....')
                continue

            # Rectify tentative perturabtions
            assert g.size(0) == len(group_gen), 'rectification vector size error!'
            rectified_directions = group_gen.apply_group_change(tentative_directions, torch.sign(g))

            if target_class == top_idx[0][0] and cur_eps <= self.eps:
                log.info('early stop at iterartion {}'.format(query_num))
                return True, query_num, adv_images
            idx = (top_idx == target_class).nonzero()
            pre_score = top_val[0][idx[0][1]]
            log.info('cur target prediction: {}'.format(pre_score))
            log.info('cur eps: {}'.format(cur_eps))

            cur_lr = cur_max_lr
            prop_de = delta_eps

            while True:

                proposed_adv_images = adv_images.clone()  # 1,C,H,W

                assert proposed_adv_images.size() == rectified_directions.size(), 'rectification error!'
                # PGD
                proposed_adv_images -= cur_lr * rectified_directions
                proposed_eps = max(cur_eps - prop_de, self.eps)
                bottom_bounded_adv = torch.where((images - proposed_eps) > proposed_adv_images, images - proposed_eps,
                                                 proposed_adv_images)
                bounded_adv = torch.where((images + proposed_eps) < bottom_bounded_adv, images + proposed_eps, bottom_bounded_adv)
                clip_frame = torch.clamp(bounded_adv, 0., 1.)
                proposed_adv_images = clip_frame.clone()

                top_val, top_idx, _ = self.output_top_values(target_model, proposed_adv_images)
                query_num += 1
                if target_class in top_idx[0]:  # top_idx shape = (1, 1) 取第一帧，看看是否== target_class
                    log.info('update with delta eps: {}'.format(prop_de))
                    if prop_de > 0:
                        cur_max_lr = self.max_lr
                        cur_min_lr = self.min_lr
                        explore_succ.clear()
                        reduce_eps_fail = 0
                    else:
                        explore_succ.append(True)
                        reduce_eps_fail += 1

                    adv_images = proposed_adv_images.clone()
                    cur_eps = max(cur_eps - prop_de, self.eps)
                    break
                # Adjust the learning rate
                elif cur_lr >= cur_min_lr * 2:
                    cur_lr = cur_lr / 2
                else:
                    if prop_de == 0:
                        explore_succ.append(False)
                        reduce_eps_fail += 1
                        logging.info('Trying to eval grad again.....')
                        break
                    prop_de = 0
                    cur_lr = cur_max_lr

            # Adjust delta eps
            if reduce_eps_fail >= update_steps[cur_eps_period]:
                delta_eps = max(delta_eps / update_weight[cur_eps_period], delta_eps_schedule[cur_eps_period])
                log.info('Success rate of reducing eps is too low. Decrease delta eps to {}'.format(delta_eps))
                if delta_eps <= delta_eps_schedule[cur_eps_period]:
                    cur_eps_period += 1
                if delta_eps < 1e-5:
                    log.info('fail to converge at query number {} with eps {}'.format(query_num, cur_eps))
                    return False, query_num, adv_images
                reduce_eps_fail = 0

            # Adjust the max lr and min lr
            if len(explore_succ) == explore_succ.maxlen and cur_min_lr > 1e-7:
                succ_p = np.mean(explore_succ)
                if succ_p < 0.5:
                    cur_min_lr /= 2
                    cur_max_lr /= 2
                    explore_succ.clear()
                    log.info('explore succ rate too low. increase lr scope [{}, {}]'.format(cur_min_lr, cur_max_lr))
            log.info('step {} : loss {} | lr {}'.format(query_num, l, cur_lr))
        return False, query_num, adv_images


    # Input video should be torch.tensor and its shape should be [num_frames, c, w, h]
    # The input should be normalized to [0, 1], orig_class只能有一个
    # 这段函数要好好考虑下怎么改，因为ori_class
    def untargeted_attack(self, target_model, images, true_labels):
        assert true_labels.size(0) == 1
        ori_class = true_labels[0].item()
        adv_images = torch.clamp(images.clone() + (torch.rand_like(images) * 2 - 1) * self.eps, 0., 1.)
        query_num = torch.zeros(adv_images.size(0))  # shape = 1
        cur_lr = self.max_lr
        last_p = []
        last_score = []

        group_gen = EquallySplitGrouping(self.image_split)

        while query_num.min().item() < self.max_iter:
            top_val, top_idx, _ = self.output_top_values(target_model, adv_images)
            query_num += 1
            if ori_class != top_idx[0][0]:
                log.info('early stop at iterartion {}'.format(query_num[0].item()))
                return True, query_num, adv_images
            idx = (top_idx == ori_class).nonzero()
            pre_score = top_val[0][idx[0][1]]
            log.info('cur target prediction: {}'.format(pre_score))

            last_score.append(pre_score)
            last_score = last_score[-200:]
            if last_score[-1] >= last_score[0] and len(last_score) == 200:
                log.info('FAIL: No Descent, Stop iteration')
                return False, query_num, adv_images

            # Annealing max learning rate
            last_p.append(pre_score)
            last_p = last_p[-20:]
            if last_p[-1] <= last_p[0] and len(last_p) == 20:
                if cur_lr > self.min_lr:
                    print("[log] Annealing max_lr")
                    cur_lr = max(cur_lr / 2., self.min_lr)
                last_p = []

            tentative_directions = self.directions_generator(adv_images).cuda()
            group_gen.initialize(tentative_directions)

            l, g = self.sim_rectification_vector(target_model, adv_images, tentative_directions, self.sample_per_draw, self.sigma,
                                            ori_class, self.rank_transform, self.sub_num_sample, group_gen, untargeted=True)
            query_num += self.sample_per_draw
            if l is None and g is None:
                log.info('nes sim fails, try again....')
                continue

            # Rectify tentative perturabtions
            assert g.size(0) == len(group_gen), 'rectification vector size error!'
            rectified_directions = group_gen.apply_group_change(tentative_directions, torch.sign(g))
            proposed_adv_vid = adv_images

            assert proposed_adv_vid.size() == rectified_directions.size(), 'rectification error!'
            # PGD
            proposed_adv_vid += cur_lr * rectified_directions
            bottom_bounded_adv = torch.where((images - self.eps) > proposed_adv_vid, images - self.eps,
                                             proposed_adv_vid)
            bounded_adv = torch.where((images + self.eps) < bottom_bounded_adv, images + self.eps, bottom_bounded_adv)
            clip_frame = torch.clamp(bounded_adv, 0., 1.)
            adv_images = clip_frame.clone()
            log.info('step {} : loss {} | lr {}'.format(query_num[0].item(), l, cur_lr))
        return False, query_num, adv_images

    def attack_all_images(self, args, arch_name, target_model, result_dump_path):
        while target_model.input_size[-1] % args.image_split != 0:
            args.image_split = args.image_split + 1
            self.image_split = args.image_split
        for batch_index, data_tuple in enumerate(self.dataset_loader):
            selected = torch.arange(batch_index,
                                    min(batch_index + 1, self.total_images))
            if args.dataset == "ImageNet":
                if target_model.input_size[-1] >= 299:
                    images, true_labels = data_tuple[1], data_tuple[2]
                else:
                    images, true_labels = data_tuple[0], data_tuple[2]
            else:
                images, true_labels = data_tuple[0], data_tuple[1]
            if images.size(-1) != target_model.input_size[-1]:
                images = F.interpolate(images, size=target_model.input_size[-1], mode='bilinear',align_corners=True)
            images = images.cuda()
            true_labels = true_labels.cuda()
            if args.targeted:
                if args.target_type == 'random':
                    target_labels = torch.randint(low=0, high=CLASS_NUM[args.dataset],
                                                  size=true_labels.size()).long().cuda()
                    invalid_target_index = target_labels.eq(true_labels)
                    while invalid_target_index.sum().item() > 0:
                        target_labels[invalid_target_index] = torch.randint(low=0, high=CLASS_NUM[args.dataset],
                                                                            size=target_labels[
                                                                                invalid_target_index].shape).long().cuda()
                        invalid_target_index = target_labels.eq(true_labels)
                elif args.target_type == 'least_likely':
                    logit = target_model(images)
                    target_labels = logit.argmin(dim=1)
                elif args.target_type == "increment":
                    target_labels = torch.fmod(true_labels + 1, CLASS_NUM[args.dataset])
                else:
                    raise NotImplementedError('Unknown target_type: {}'.format(args.target_type))
            else:
                target_labels = None  # target_labels shape = (mini_batch_size,)
            with torch.no_grad():
                logit = target_model(images)
            pred = logit.argmax(dim=1)
            correct = pred.eq(true_labels).float()  # shape = (batch_size,)
            not_done = correct.clone()
            if args.targeted:
                if self.dataset_name == "ImageNet":
                    target_class_images = self.get_image_of_class_ImageNet(target_labels, self.dataset, target_model)
                else:
                    target_class_images = self.get_image_of_class(target_labels, self.dataset, target_model)
                target_class_images = target_class_images.cuda()
                self.directions_generator.set_targeted_params(target_class_images, self.random_mask)
                is_success, query, adv_images = self.targeted_attack(target_model, images, target_class_images, target_labels)
            else:
                self.directions_generator.set_untargeted_params(images, self.random_mask, scale=5.)
                is_success, query, adv_images =self.untargeted_attack(target_model, images, true_labels)

            with torch.no_grad():
                adv_logit = target_model(adv_images)
            adv_pred = adv_logit.argmax(dim=1)
            adv_prob = F.softmax(adv_logit, dim=1)
            if args.targeted:
                not_done = not_done * (1 - adv_pred.eq(target_labels).float()).float()  # not_done初始化为 correct, shape = (batch_size,)
            else:
                not_done = not_done * adv_pred.eq(true_labels).float()
            success = (1 - not_done) * correct
            success_query = success * query.cuda()
            not_done_prob = adv_prob[torch.arange(1), true_labels] * not_done

            log.info('Attacking image {} - {} / {}, query {}'.format(
                batch_index, batch_index + 1, self.total_images,  int(query.min().item())
            ))
            log.info('        correct: {:.4f}'.format(correct.mean().item()))
            log.info('       not_done: {:.4f}'.format(not_done[correct.byte()].mean().item()))
            if success.sum().item() > 0:
                log.info('     mean_query: {:.4f}'.format(success_query[success.byte()].mean().item()))
                log.info('   median_query: {:.4f}'.format(success_query[success.byte()].median().item()))
            if not_done.sum().item() > 0:
                log.info('  not_done_prob: {:.4f}'.format(not_done_prob[not_done.byte()].mean().item()))

            for key in ['query', 'correct', 'not_done',
                        'success', 'success_query', 'not_done_prob']:
                value_all = getattr(self, key + "_all")
                value = eval(key)
                value_all[selected] = value.detach().float().cpu()

        log.info('{} is attacked finished ({} images)'.format(arch_name, self.total_images))
        log.info('        avg correct: {:.4f}'.format(self.correct_all.mean().item()))
        log.info('       avg not_done: {:.4f}'.format(self.not_done_all.mean().item()))  # 有多少图没做完
        if self.success_all.sum().item() > 0:
            log.info('     avg mean_query: {:.4f}'.format(self.success_query_all[self.success_all.byte()].mean().item()))
            log.info('   avg median_query: {:.4f}'.format(self.success_query_all[self.success_all.byte()].median().item()))
            log.info('     max query: {}'.format(self.success_query_all[self.success_all.byte()].max().item()))
        if self.not_done_all.sum().item() > 0:
            log.info('  avg not_done_prob: {:.4f}'.format(self.not_done_prob_all[self.not_done_all.byte()].mean().item()))
        log.info('Saving results to {}'.format(result_dump_path))
        meta_info_dict = {"avg_correct": self.correct_all.mean().item(),
                          "avg_not_done": self.not_done_all[self.correct_all.byte()].mean().item(),
                          "mean_query": self.success_query_all[self.success_all.byte()].mean().item(),
                          "median_query": self.success_query_all[self.success_all.byte()].median().item(),
                          "max_query": self.success_query_all[self.success_all.byte()].max().item(),
                          "correct_all": self.correct_all.detach().cpu().numpy().astype(np.int32).tolist(),
                          "not_done_all": self.not_done_all.detach().cpu().numpy().astype(np.int32).tolist(),
                          "query_all": self.query_all.detach().cpu().numpy().astype(np.int32).tolist(),
                          "not_done_prob": self.not_done_prob_all[self.not_done_all.byte()].mean().item(),
                          "args":vars(args)}
        with open(result_dump_path, "w") as result_file_obj:
            json.dump(meta_info_dict, result_file_obj, sort_keys=True)
        log.info("done, write stats info to {}".format(result_dump_path))


def get_args_parse():
    parser = argparse.ArgumentParser()
    parser.add_argument('--targeted', action='store_true')
    parser.add_argument('--target_type', type=str, default='increment', choices=['random', 'least_likely', "increment"])
    parser.add_argument('--arch', type=str, required=True)
    parser.add_argument('--test_archs', action='store_true')
    parser.add_argument('--surrogate_arch',type=str,required=True, choices=['resnet50','densenet121','densenet169'])
    parser.add_argument('--epsilon',type=float, default=0.05)
    parser.add_argument('--delta_eps',type=float,default=0.5)
    parser.add_argument('--max_lr',type=float,default=1e-2)
    parser.add_argument('--min_lr',type=float,default=1e-3)
    parser.add_argument('--starting_eps',type=float,default=1.0)
    parser.add_argument('--random_mask', default=0.9, type=float)
    parser.add_argument('--no_rank_transform', action='store_true')
    parser.add_argument('--max_queries',type=int, default=10000)
    parser.add_argument('--sigma', type=float, default=1e-6)
    parser.add_argument('--sample_per_draw', type=int, default=48, help='Number of samples used for NES')
    parser.add_argument('--image_split', type=int, default=8)
    parser.add_argument('--sub_num_sample', type=int, default=12,
                        help='Number of samples processed each time. Adjust this number if the gpu memory is limited.'
                             'This number should be even and sample_per_draw can be divisible by it.')
    parser.add_argument('--attack_defense', action="store_true")
    parser.add_argument('--defense_model', type=str, default=None)
    parser.add_argument('--dataset',type=str, required=True)
    parser.add_argument('--gpu', type=int, required=True)
    parser.add_argument('--exp-dir', default='logs', type=str, help='directory to save results and logs')
    args = parser.parse_args()
    return args


def get_exp_dir_name(dataset, norm, targeted, target_type, args):
    target_str = "untargeted" if not targeted else "targeted_{}".format(target_type)
    if args.attack_defense:
        dirname = 'VBAD_attack_on_defensive_model-{}-{}-{}'.format(dataset, norm, target_str)
    else:
        dirname = 'VBAD_attack-{}-{}-{}'.format(dataset, norm, target_str)
    return dirname

def set_log_file(fname):
    import subprocess
    tee = subprocess.Popen(['tee', fname], stdin=subprocess.PIPE)
    os.dup2(tee.stdin.fileno(), sys.stdout.fileno())
    os.dup2(tee.stdin.fileno(), sys.stderr.fileno())

def print_args(args):
    keys = sorted(vars(args).keys())
    max_len = max([len(key) for key in keys])
    for key in keys:
        prefix = ' ' * (max_len + 1 - len(key)) + key
        log.info('{:s}: {}'.format(prefix, args.__getattribute__(key)))


def get_model_names(args):
    archs = []
    if args.test_archs:
        if args.dataset == "CIFAR-10" or args.dataset == "CIFAR-100":
            for arch in MODELS_TEST_STANDARD[args.dataset]:
                test_model_path = "{}/train_pytorch_model/real_image_model/{}-pretrained/{}/checkpoint.pth.tar".format(PY_ROOT,
                                                                                        args.dataset,  arch)
                if os.path.exists(test_model_path):
                    archs.append(arch)
                else:
                    log.info(test_model_path + " does not exists!")
        elif args.dataset == "TinyImageNet":
            for arch in MODELS_TEST_STANDARD[args.dataset]:
                test_model_list_path = "{root}/train_pytorch_model/real_image_model/{dataset}@{arch}*.pth.tar".format(
                    root=PY_ROOT, dataset=args.dataset, arch=arch)
                test_model_path = list(glob.glob(test_model_list_path))
                if test_model_path and os.path.exists(test_model_path[0]):
                    archs.append(arch)
        else:
            for arch in MODELS_TEST_STANDARD[args.dataset]:
                test_model_list_path = "{}/train_pytorch_model/real_image_model/{}-pretrained/checkpoints/{}*.pth".format(
                    PY_ROOT,
                    args.dataset, arch)
                test_model_list_path = list(glob.glob(test_model_list_path))
                if len(test_model_list_path) == 0:  # this arch does not exists in args.dataset
                    continue
                archs.append(arch)
    else:
        assert args.arch is not None
        archs = [args.arch]
    return archs

def main():
    args = get_args_parse()
    os.environ["TORCH_HOME"] = "/home1/machen/meta_perturbations_black_box_attack/train_pytorch_model/real_image_model/ImageNet-pretrained"
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)
    args.exp_dir = osp.join(args.exp_dir, get_exp_dir_name(args.dataset, "linf", args.targeted, args.target_type, args))  # 随机产生一个目录用于实验
    if args.test_archs:
        if args.attack_defense:
            log_file_path = osp.join(args.exp_dir, 'run_defense_{}.log'.format(args.defense_model))
        else:
            log_file_path = osp.join(args.exp_dir, 'run.log')
    elif args.arch is not None:
        if args.attack_defense:
            log_file_path = osp.join(args.exp_dir, 'run_defense_{}_{}.log'.format(args.arch, args.defense_model))
        else:
            log_file_path = osp.join(args.exp_dir, 'run_{}.log'.format(args.arch))
    set_log_file(log_file_path)
    archs = get_model_names(args)
    args.arch = ", ".join(archs)

    log.info('Command line is: {}'.format(' '.join(sys.argv)))
    log.info("Log file is written in {}".format(log_file_path))
    log.info('Called with args:')
    print_args(args)
    layer = ['fc']
    extractors = []
    if args.surrogate_arch == "resnet50":
        resnet50 = models.resnet50(pretrained=True).eval()
        resnet50_extractor = ResNetFeatureExtractor(resnet50, layer).eval().cuda()
        extractors.append(resnet50_extractor)
    elif args.surrogate_arch == "densenet121":
        densenet121 = models.densenet121(pretrained=True).eval()
        densenet121_extractor = DensenetFeatureExtractor(densenet121, layer).eval().cuda()
        extractors.append(densenet121_extractor)
    elif args.surrogate_arch == "densenet169":
        densenet169 = models.densenet169(pretrained=True).eval()
        densenet169_extractor = DensenetFeatureExtractor(densenet169, layer).eval().cuda()
        extractors.append(densenet169_extractor)

    directions_generator = TentativePerturbationGenerator(extractors, part_size=32, preprocess=True)
    attacker = VBADAttack(args, directions_generator)
    for arch in archs:
        if args.attack_defense:
            save_result_path = args.exp_dir + "/{}_{}_result.json".format(arch, args.defense_model)
        else:
            save_result_path = args.exp_dir + "/{}_result.json".format(arch)
        if os.path.exists(save_result_path):
            continue
        if args.attack_defense:
            model = DefensiveModel(args.dataset, arch, no_grad=True, defense_model=args.defense_model)
        else:
            model = StandardModel(args.dataset, arch, no_grad=True)
        model.cuda()
        model.eval()
        attacker.attack_all_images(args, arch, model, save_result_path)
        model.cpu()



if __name__ == '__main__':
    main()