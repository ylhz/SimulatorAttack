import os

import argparse
from collections import defaultdict

import matplotlib.pyplot as plt
import numpy as np
import json

import re



def get_all_json_data(file_dir_path, ablation_key):
    pattern = re.compile(".*{}[@|_](\d+)_.*".format(ablation_key))
    data = {}
    for filename in os.listdir(file_dir_path):
        if filename.endswith(".json"):
            ma = pattern.match(filename)
            key = int(ma.group(1))
            with open(file_dir_path + "/" + filename, "r") as file_obj:
                data_txt = file_obj.read().replace('"not_done_loss": NaN, "not_done_prob": NaN,', "")
                json_data = json.loads(data_txt.strip())
            data[key] = {"success_rate": (1-json_data["avg_not_done"]) * 100, "mean_query": round(json_data["mean_query"]),
                         "median_query":round(json_data["median_query"])}
    return data


def draw_meta_predict_interval_curve_figure(ablation_key, json_key, dump_file_path, xlabel, ylabel):

    targeted_data = {}
    for targeted in ["untargeted", "targeted"]:
        if targeted == "targeted":
            file_dir = "AblationStudy_{}@CIFAR-10-cw_loss-l2-targeted".format(ablation_key)
        else:
            file_dir = "AblationStudy_{}@CIFAR-10-cw_loss-l2-untargeted-mse".format(ablation_key)

        data = get_all_json_data("/home1/machen/meta_perturbations_black_box_attack/logs/" + file_dir, ablation_key)

        x = []
        y = []
        for key, json_val in sorted(data.items(), key=lambda e: int(e[0])):
            x.append(key)
            y.append(json_val[json_key])
        x = np.array(x)
        y = np.array(y)
        targeted_data[targeted] = (x,y)
    plt.style.use('seaborn-whitegrid')
    plt.figure(figsize=(10, 8))

    colors = ['b', 'r',  'c', 'm', 'y', 'k', 'w']
    markers = [".",",","o","^","s","p","x"]
    max_x = 0
    min_x = 0
    for idx,targeted in enumerate(["untargeted", "targeted"]):
        x,y = targeted_data[targeted]
        line, = plt.plot(x, y, marker="o",
                         label=r"$\ell_2$ norm {} attack on WRN-28 model".format(targeted), color=colors[idx], linestyle="-")
    plt.xlim(0, 90)
    plt.ylim(0, 101)
    plt.gcf().subplots_adjust(bottom=0.15)
    xtick = [0,3,5,7,10,20, 30, 40, 50, 60, 70, 80, 90]
    # xtick = [0, 5000, 10000]
    plt.xticks(xtick, fontsize=15)
    plt.yticks([0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100], fontsize=15)
    plt.xlabel(xlabel, fontsize=18)
    plt.ylabel(ylabel, fontsize=18)
    plt.legend(loc='lower right', prop={'size': 13})
    plt.savefig(dump_file_path, dpi=200)


def draw_other_curve_figure(ablation_key, json_key, dump_file_path):
    targeted_data = {}
    for targeted in ["untargeted", "targeted"]:
        target_str = "untargeted-mse" if targeted == "untargeted" else "targeted_increment-mse"
        file_dir = "AblationStudy_{}@CIFAR-10-cw_loss-l2-{}".format(ablation_key, target_str)
        data = get_all_json_data("/home1/machen/meta_perturbations_black_box_attack/logs/" + file_dir, ablation_key)
        x = []
        y = []
        for key, json_val in sorted(data.items(), key=lambda e: int(e[0])):
            x.append(key)
            y.append(json_val[json_key])
        x = np.array(x)
        y = np.array(y)
        targeted_data[targeted] = (x,y)
    plt.style.use('seaborn-whitegrid')
    plt.figure(figsize=(10, 8))
    if ablation_key == "meta_seq_len":
        label = r"deque $\mathbb{D}$'s maximum capacity"
    elif ablation_key == "warm_up":
        label = "warm-up iterations"
    color = {"untargeted": "b", "targeted":"r"}
    for idx,targeted in enumerate(["untargeted", "targeted"]):
        x,y = targeted_data[targeted]
        line, = plt.plot(x, y, marker="o", label=r"$\ell_2$ norm {} attack on WRN-28 model".format(targeted), color=color[targeted], linestyle="-")
    plt.xlim(0, max(x.tolist()))
    plt.ylim(0, 660)
    plt.gcf().subplots_adjust(bottom=0.15)
    # xtick = [0, 5000, 10000]
    plt.xticks([0] + x.tolist(), fontsize=15)
    plt.yticks(np.arange(0,661,20).tolist(), fontsize=15)
    plt.xlabel(label, fontsize=18)
    plt.ylabel("Avg. Query", fontsize=18)
    if ablation_key == "meta_seq_len":
        plt.legend(prop={'size': 13},loc='upper right')
    else:
        plt.legend(prop={'size': 13})
    plt.savefig(dump_file_path, dpi=200)


def draw_meta_or_not_curve_figure(dump_file_path):
    data_dict = defaultdict(dict)
    for mode in ["meta", "deep"]:
        json_file_path = "/home1/machen/meta_perturbations_black_box_attack/logs/AblationStudy_meta_or_not@CIFAR-10-cw_loss-l2-untargeted-mse/meta_mode_{}_WRN-28-10-drop_result.json".format(mode)

        with open(json_file_path, "r") as file_obj:
            data_txt = file_obj.read().replace('"not_done_loss": NaN, "not_done_prob": NaN,', "")
            json_data = json.loads(data_txt.strip())
            data_dict[mode]["is_finetune"] = [is_finetune for iter, is_finetune in sorted(json_data["logits_error_finetune_iteration"].items(),
                                                               key=lambda e:int(e[0]))]
            data_dict[mode]["MSE_error"] =  [MSE_error for iter, MSE_error in sorted(json_data["logits_error_iteration"].items(),
                                                               key=lambda e:int(e[0]))]
    # plt.style.use('seaborn-whitegrid')
    plt.figure(figsize=(10, 8))
    colors = {"deep":'m', "meta":'r', "uninitial": 'y'}
    for mode,  data_info in data_dict.items():
        x  = np.arange(125) + 1
        y  = np.array(data_info["MSE_error"][:125])
        is_finetune_list = data_info["is_finetune"][:125]
        if mode == "meta":
            simulator_name = "MetaSimulator"
        elif mode == "deep":
            simulator_name = "Simulator$_{vanilla}$"
        else:
            simulator_name = "Simulator$_{rnd}$"
        line, = plt.plot(x, y, label=r"$\ell_2$ norm untargeted attack result of {}".format(simulator_name),
                         color=colors[mode], linestyle="-")
    first_finetune = True
    for x_, is_finetune in enumerate(is_finetune_list):
        if is_finetune == 1:
            if first_finetune:
                plt.axvline(x=x_+1, color='#778899', linestyle='--', linewidth=1, label="fine-tune iterations")
                first_finetune = False
            else:
                plt.axvline(x=x_ + 1, color='#778899', linestyle='--', linewidth=1)

    plt.xlim(min(x.tolist()), max(x.tolist()))
    plt.ylim(0, 30)
    plt.gcf().subplots_adjust(bottom=0.15)
    # xtick = [0, 5000, 10000]
    plt.xticks([1,10, 25,50,75,100,125], fontsize=15)
    plt.yticks([0, 5, 10,15, 20,25,30], fontsize=15)
    plt.xlabel("attack iterations", fontsize=18)
    plt.ylabel("MSE between outputs of simulator and target model", fontsize=18)
    legend = plt.legend(loc='upper right', prop={'size': 15}, shadow=True, facecolor="white")
    # legend.get_frame().set_facecolor('#E6E6FA')
    plt.savefig(dump_file_path, dpi=200)

def parse_args():
    parser = argparse.ArgumentParser(description='PyTorch Meta Model Training')
    parser.add_argument("--fig_type", type=str, choices=["query_success_rate_dict", "query_threshold_success_rate_dict",
                                                         "success_rate_to_avg_query"])
    parser.add_argument("--targeted", action="store_true", help="Does it train on the data of targeted attack?")
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = parse_args()

    # x_label = "meta-predict interval"
    # y_label = "Attack Success Rate (%)"
    # dump_folder = "/home1/machen/meta_perturbations_black_box_attack/figures/ablation_study_meta_predict_steps/"
    # os.makedirs(dump_folder, exist_ok=True)
    # file_path = dump_folder + "meta_predict_steps.png"
    # draw_meta_predict_interval_curve_figure("meta_predict_steps", "success_rate", file_path, x_label, y_label)
    # print("written to {}".format(file_path))


    # dump_folder = "/home1/machen/meta_perturbations_black_box_attack/figures/ablation_study/"
    # os.makedirs(dump_folder, exist_ok=True)
    # file_path = dump_folder + "warm_up.png"
    # draw_other_curve_figure("warm_up", "mean_query", file_path)
    # print("written to {}".format(file_path))
    #
    # dump_folder = "/home1/machen/meta_perturbations_black_box_attack/figures/ablation_study/"
    # os.makedirs(dump_folder, exist_ok=True)
    # file_path = dump_folder + "deque_length.png"
    # draw_other_curve_figure("meta_seq_len", "mean_query", file_path)
    # print("written to {}".format(file_path))

    dump_folder = "/home1/machen/meta_perturbations_black_box_attack/figures/ablation_study/"
    os.makedirs(dump_folder, exist_ok=True)
    file_path = dump_folder + "meta_or_not.png"
    draw_meta_or_not_curve_figure(file_path)
    # print("written to {}".format(file_path))