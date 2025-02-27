import copy
import logging
import pickle
import time
from ast import List
from pathlib import Path

import numpy as np
import torch
import torchvision
from basic.dataset import get_dataloader, get_dataset_subset
from basic.fast_train import (
    NetworkEMA,
    fast_train_fun,
    get_cifar10_data,
    logging_columns_list,
    make_net,
    print_training_details,
)
from basic.models import get_model
from sklearn.model_selection import train_test_split
from torch import nn
from basic.train import inference, train
from basic.util import get_split, load_models_by_conditions, load_models_by_model_idx

from privacy_meter import audit_report
from privacy_meter.audit import MetricEnum
from privacy_meter.audit_report import ROCCurveReport, SignalHistogramReport
from privacy_meter.constants import InferenceGame
from privacy_meter.dataset import Dataset
from privacy_meter.hypothesis_test import linear_itp_threshold_func
from privacy_meter.information_source import InformationSource
from privacy_meter.information_source_signal import (
    ModelLoss,
    ModelNegativeRescaledLogits,
)
from privacy_meter.metric import PopulationMetric, ReferenceMetric
from privacy_meter.model import PytorchModelTensor


def load_existing_target_model(
    dataset_size: int, model_metadata_dict: dict, configs: dict
):
    """Return a list of model's index that matches the configuration.

    Args:
        dataset_size (int): Size of the whole training dataset.
        model_metadata_dict (dict): Model metedata dict.
        configs (dict): Training target models configuration.

    Returns:
        matched_idx: List of target model index which matches the conditions or model_idx specified in configs['train']['model_idx']
    """
    assert isinstance(model_metadata_dict, dict)
    assert "model_metadata" in model_metadata_dict

    if "model_idx" in configs["train"]:
        matched_idx_list = load_models_by_model_idx(
            model_metadata_dict, [configs["train"]["model_idx"]]
        )
    else:
        num_target_models = configs["train"].get("num_target_model", 1)
        conditions = {
            "optimizer": configs["train"]["optimizer"],
            "batch_size": configs["train"]["batch_size"],
            "model_name": configs["train"]["model_name"],
            "epochs": configs["train"]["epochs"],
            "learning_rate": configs["train"]["learning_rate"],
            "weight_decay": configs["train"]["weight_decay"],
            "num_train": int(dataset_size * configs["data"]["f_train"]),
            "dataset": configs["data"]["dataset"],
        }
        matched_idx_list = load_models_by_conditions(
            model_metadata_dict, conditions, num_target_models
        )
    return matched_idx_list


def load_existing_reference_models(
    model_metadata_dict: dict, configs: dict, target_idx: int
):
    """Return a list of reference model's index that matches the configuration.

    Args:
        model_metadata_dict (dict): Model metedata dict.
        configs (dict): Training target models configuration.
        target_idx (int): Target model.

    Returns:
        List(int): List of reference model index which matches the conditions
    """
    if configs["algorithm"] != "reference_out":
        return []
    number_models = configs.get("num_reference_models", 10)
    num_audit_train_data = int(
        model_metadata_dict["model_metadata"][target_idx]["num_train"]
        * configs["f_reference_dataset"]
    )
    conditions = {
        "optimizer": configs["optimizer"],
        "batch_size": configs["batch_size"],
        "epochs": configs["epochs"],
        "learning_rate": configs["learning_rate"],
        "weight_decay": configs["weight_decay"],
        "num_train": num_audit_train_data,
    }

    reference_matched_idx = load_models_by_conditions(
        model_metadata_dict, conditions, number_models, [target_idx]
    )

    return reference_matched_idx


def check_reference_model_dataset(
    model_metadata_dict: dict,
    reference_matched_idx: List(int),
    target_idx: int,
    splitting_method: str,
    data_idx=None,
):
    """Filter out the reference models that does not satisfy the splitting method.

    Args:
        model_metadata_dict (dict): Model metedata dict.
        reference_matched_idx (List): List of reference model index we want to filter.
        target_idx (int): Target model index.
        splitting_method (str): Splitting method. Take value from ['no_overlapping', 'uniform', 'leave_one_out'].
        data_idx (int, optional): Data index, used for check splitting_method='leave_one_out'. Defaults to None.
    Returns:
        List (int): List of reference model index which matches the splitting method.
    """
    meta_data_dict = model_metadata_dict["model_metadata"]
    target_train_split = meta_data_dict[target_idx]["train_split"]
    if splitting_method == "no_overlapping":
        return [
            idx
            for idx in reference_matched_idx
            if set(meta_data_dict[idx]["train_split"]).isdisjoint(target_train_split)
        ]
    elif splitting_method == "uniform":
        return reference_matched_idx
    elif splitting_method == "leave_one_out":
        assert data_idx is not None
        filter_reference_idx = []
        for idx in reference_matched_idx:
            diff = set(meta_data_dict[idx]["train_split"]).symmetric_difference(
                target_train_split
            )
            if set(diff) == set([data_idx]):
                filter_reference_idx.append(idx)
        return filter_reference_idx
    else:
        raise ValueError(
            f"{splitting_method} is not a valid splitting method. Take value from ['no_overlapping', 'uniform', 'leave_one_out']"
        )


def load_existing_models(
    model_metadata_dict: dict,
    matched_idx: List(int),
    model_name: str,
    dataset: torchvision.datasets,
    dataset_name: str,
    device="cuda",
):
    """Load existing models from dicks for matched_idx.

    Args:
        model_metadata_dict (dict): Model metedata dict.
        matched_idx (List): List of model index we want to load.
        model_name (str): Model name.
        dataset_list (List): Dataset List.
        dataset (torchvision.datasets): Dataset.
        dataset_name (str): Dataset name.
    Returns:
        List (nn.Module): List of models.
    """
    model_list = []
    if len(matched_idx) > 0:
        for metadata_idx in matched_idx:
            metadata = model_metadata_dict["model_metadata"][metadata_idx]
            if model_name != "speedyresnet":
                model = get_model(model_name, dataset_name)
            else:
                data = get_cifar10_data(dataset, [0], [0], device=device)
                model = NetworkEMA(make_net(data, device=device))
            with open(f"{metadata['model_path']}", "rb") as file:
                model_weight = pickle.load(file)
            model.load_state_dict(model_weight)
            model_list.append(model)
        return model_list
    else:
        return []


def load_dataset_for_existing_models(
    dataset_size: int, model_metadata_dict: dict, matched_idx: List(int), configs: dict
):
    """Load the dataset index list for the existing models.

    Args:
        dataset_size (int): Dataset size.
        model_metadata_dict (dict): Model metadata dict.
        matched_idx (List(int)): List of matched model index.
        configs (dict): Training configuration.
    Returns:
        List(dict): List of dataset splits for each model, including train, test, and audit.
    """
    assert isinstance(matched_idx, list)
    all_index = np.arange(dataset_size)
    test_size = int(configs["f_test"] * dataset_size)
    audit_size = int(configs["f_audit"] * dataset_size)
    index_list = []
    for metadata_idx in matched_idx:
        metadata = model_metadata_dict["model_metadata"][metadata_idx]
        # Check if the existing target data has the test split.
        if "test_split" in metadata:
            index_list.append(
                {
                    "train": metadata["train_split"],
                    "test": metadata["test_split"],
                    "audit": metadata["audit_split"],
                }
            )
        else:
            all_index = np.arange(dataset_size)
            test_index = get_split(
                all_index, metadata["train_split"], test_size, "no_overlapping"
            )
            used_index = np.concatenate([metadata["train_split"], test_index])
            audit_index = get_split(
                all_index,
                used_index,
                size=audit_size,
                split_method=configs["split_method"],
            )
            index_list.append(
                {
                    "train": metadata["train_split"],
                    "test": test_index,
                    "audit": audit_index,
                }
            )
    return index_list


def prepare_datasets(dataset_size: int, num_datasets: int, configs: dict):
    """Prepare the dataset for training the target models when the training data are sampled uniformly from the distribution (pool of all possible data).

    Args:
        dataset_size (int): Size of the whole dataset
        num_datasets (int): Number of datasets we should generate
        configs (dict): Data split configuration

    Returns:
        dict: Data split information which saves the information of training points index and test points index for all target models.
    """

    # The index_list will save all the information about the train, test and auit for each target model.
    index_list = []
    all_index = np.arange(dataset_size)
    train_size = int(configs["f_train"] * dataset_size)
    test_size = int(configs["f_test"] * dataset_size)
    audit_size = int(configs["f_audit"] * dataset_size)
    for _ in range(num_datasets):
        selected_index = np.random.choice(
            all_index, train_size + test_size, replace=False
        )
        train_index, test_index = train_test_split(selected_index, test_size=test_size)
        audit_index = get_split(
            all_index,
            selected_index,
            size=audit_size,
            split_method=configs["split_method"],
        )
        index_list.append(
            {"train": train_index, "test": test_index, "audit": audit_index}
        )

    dataset_splits = {"split": index_list, "split_method": configs["split_method"]}
    return dataset_splits


def prepare_datasets_for_sample_privacy_risk(
    dataset_size: int,
    num_models: int,
    data_idx: int,
    configs: dict,
    data_type: str,
    split_method: str,
    model_metadata_dict: dict,
    matched_in_idx: List(int) = None,
):
    """Prepare the datasets for auditing the priavcy risk for a data point. We prepare the dataset with or without the target point for training a set of models with or without the target point.

    Args:
        dataset_size (int): Size of the whole dataset
        num_models (int): Number of additional target models
        data_idx (int): Data index of the target point
        configs (dict): Data split configuration
        data_type (str): Indicate whether we want to include the target point or exclude the data point (takes value from 'include' and 'exclude' )
        split_method (str): Indicate how to sample the rest of the data points. Take value from uniform and no_overlapping.
        model_metadata_dict (dict): Metadata for existing models.
        matched_in_idx (List(int), optional): List of model index which are trained on the data points for generating leave_one_out dataset.

    Returns:
        dict: Data split information.
    """
    all_index = np.arange(dataset_size)
    all_index_exclude_z = np.array([i for i in all_index if i != data_idx])
    index_list = []

    # Indicate how to sample the rest of the dataset.
    if split_method == "uniform":
        # Placeholder for the existing models
        for _ in range(num_models):
            if data_type == "include":
                train_index = np.random.choice(
                    all_index_exclude_z,
                    int((configs["f_train"]) * dataset_size) - 1,
                    replace=False,
                )
                index_list.append(
                    {
                        "train": np.append(train_index, data_idx),
                        "test": all_index,
                        "audit": all_index,
                    }
                )
            elif data_type == "exclude":
                train_index = np.random.choice(
                    all_index_exclude_z,
                    int((configs["f_train"]) * dataset_size),
                    replace=False,
                )
                index_list.append(
                    {"train": train_index, "test": all_index, "audit": all_index}
                )
            else:
                raise ValueError(
                    f"{data_type} is not supported. Please use the value include or exclude to indicate whether you want to generate a set of dataset with or without the target point."
                )

    # We generate a list of dataset which is the same as the training dataset of the models indicated by the matched_in_idx but excluding the target point.
    elif split_method == "leave_one_out" and data_type == "include":
        train_index = np.random.choice(
            all_index_exclude_z,
            int((configs["f_train"]) * dataset_size) - 1,
            replace=False,
        )
        for _ in range(num_models):
            index_list.append(
                {
                    "train": np.append(train_index, data_idx),
                    "test": all_index,
                    "audit": all_index,
                }
            )

    elif split_method == "leave_one_out" and data_type == "exclude":
        assert (
            matched_in_idx is not None
        ), "Please indicate the in-world model metdadata"
        assert (
            len(matched_in_idx) >= num_models
        ), "Input enough in-world (with the target point z) to generate the out world"

        index_list = []  # List of data split
        all_index = np.arange(dataset_size)
        all_index_exclude_z = np.array([i for i in all_index if i != data_idx])

        for metadata_idx in matched_in_idx:
            metadata = model_metadata_dict["model_metadata"][metadata_idx]
            train_index = np.delete(
                metadata["train_split"],
                np.where(metadata["train_split"] == data_idx)[0],
            )
            # Note: Since we are intested in the individual privacy risk, we consider the whole dataset as the test and audit dataset
            index_list.append(
                {
                    "train": train_index,
                    "test": [
                        i for i in all_index if i not in train_index and i != data_idx
                    ],
                    "audit": [
                        i for i in all_index if i not in train_index and i != data_idx
                    ],
                }
            )

    else:
        raise ValueError(
            f"{split_method} is not supported. Please use uniform or leave_one_out splitting method."
        )

    dataset_splits = {"split": index_list, "split_method": split_method}
    return dataset_splits


def prepare_datasets_for_reference_in_attack(
    all_dataset_size: int,
    dataset_size: int,
    num_models: int,
    keep_ratio: float,
    is_uniform: bool,
):
    """Prepare the datasets for reference_in attacks. Each data point will be randomly chosen by half of the models with probability keep_ratio and the rest of the models will be trained on the rest of the dataset.
    The partioning method is from https://github.com/tensorflow/privacy/blob/master/research/mi_lira_2021/train.py
    Args:
        all_dataset_size (int): Size of the whole dataset
        dataset_size (int): Size of the whole dataset used for training the models
        num_models (int): Number of additional target models
        keep_ratio (float): Indicate the probability of keeping the target point for training the model.
        is_uniform (bool): Indicate whether to perform the splitting in a uniform way.
    Returns:
        dict: Data split information.
        list: List of boolean indicating whether the model is trained on the target point.
        list: List of target data index on which the adversary wants to infer the membership.
    """
    index_list = []
    all_index = np.random.choice(all_dataset_size, dataset_size, replace=False)
    left_index = np.setdiff1d(np.arange(all_dataset_size), all_index)
    if is_uniform:
        keep = np.random.uniform(0, 1, size=(num_models, dataset_size)) <= keep_ratio
    else:
        selected_matrix = np.random.uniform(0, 1, size=(num_models, dataset_size))
        order = selected_matrix.argsort(0)
        keep = order < int(keep_ratio * num_models)
    for i in range(num_models):
        if np.sum(~keep[i]) % 2 == 0:
            # This is for speedyresnet
            index_list.append(
                {
                    "train": all_index[keep[i]],
                    "test": all_index[~keep[i]],
                    "audit": left_index,
                }
            )
        else:
            train_index = all_index[keep[i]]
            test_index = all_index[~keep[i]]
            index_list.append(
                {
                    "train": np.append(train_index, test_index[0]),
                    "test": test_index[1:],
                    "audit": left_index,
                }
            )

    dataset_splits = {"split": index_list, "split_method": f"random_{keep_ratio}"}
    return dataset_splits, keep, all_index


def prepare_models(
    log_dir: str,
    dataset: torchvision.datasets,
    data_split: dict,
    configs: dict,
    model_metadata_dict: dict,
    dataset_name: str,
):
    """Train models based on the dataset split information.

    Args:
        log_dir (str): Log directory that saved all the information, including the models.
        dataset (torchvision.datasets): The whole dataset
        data_split (dict): Data split information. 'split' contains a list of dict, each of which has the train, test and audit information. 'split_method' indicates the how the dataset is generated.
        configs (dict): Indicate the traininig information
        model_metadata_dict (dict): Metadata information about the existing models.
        matched_idx (List, optional): Index list of existing models that matchs configuration. Defaults to None.
        dataset_name (str): Name of the dataset
    Returns:
        nn.Module: List of trained models
        dict: Updated Metadata of the existing models
        List(int): Updated index list that matches the target model configurations.
    """
    # Initialize the model list
    model_list = []
    target_model_idx_list = []
    # Train the additional target models based on the dataset split
    for split in range(len(data_split["split"])):
        meta_data = {}
        baseline_time = time.time()

        print(50 * "-")
        print(
            f"Training the {split}-th model: ",
            f"Train size {len(data_split['split'][split]['train'])}, Test size {len(data_split['split'][split]['test'])}",
        )

        if configs["model_name"] != "speedyresnet":
            train_loader = get_dataloader(
                torch.utils.data.Subset(dataset, data_split["split"][split]["train"]),
                batch_size=configs["batch_size"],
                shuffle=True,
            )
            test_loader = get_dataloader(
                torch.utils.data.Subset(dataset, data_split["split"][split]["test"]),
                batch_size=configs["test_batch_size"],
            )

            # Train the target model based on the configurations.
            model = train(
                get_model(configs["model_name"], dataset_name),
                train_loader,
                configs,
                test_loader,
            )
            # Test performance on the training dataset and test dataset
            test_loss, test_acc = inference(model, test_loader, configs["device"])
            train_loss, train_acc = inference(model, train_loader, configs["device"])
            print(f"Train accuracy {train_acc}, Train Loss {train_loss}")
            print(f"Test accuracy {test_acc}, Test Loss {test_loss}")

        elif configs["model_name"] == "speedyresnet" and dataset_name == "cifar10":
            data = get_cifar10_data(
                dataset,
                data_split["split"][split]["train"],
                data_split["split"][split]["test"][: configs["num_test_size"]],
                device=configs["device"],
            )
            print_training_details(logging_columns_list, column_heads_only=True)
            model, train_acc, train_loss, test_acc, test_loss = fast_train_fun(
                data,
                make_net(data, device=configs["device"]),
                eval_batchsize=configs["test_batch_size"],
                device=configs["device"],
            )

        else:
            raise ValueError(
                f"The {configs['model_name']} is not supported for the {dataset_name}"
            )

        model_list.append(copy.deepcopy(model))
        logging.info(
            "Prepare %s-th target model costs %s seconds ",
            split,
            time.time() - baseline_time,
        )

        print(50 * "-")

        # Update the model metadata and save the model
        model_idx = model_metadata_dict["current_idx"]
        model_metadata_dict["current_idx"] += 1
        with open(f"{log_dir}/model_{model_idx}.pkl", "wb") as f:
            pickle.dump(model.state_dict(), f)
        meta_data["train_split"] = data_split["split"][split]["train"]
        meta_data["test_split"] = data_split["split"][split]["test"]
        meta_data["audit_split"] = data_split["split"][split]["audit"]
        meta_data["num_train"] = len(data_split["split"][split]["train"])
        meta_data["optimizer"] = configs["optimizer"]
        meta_data["batch_size"] = configs["batch_size"]
        meta_data["epochs"] = configs["epochs"]
        meta_data["model_name"] = configs["model_name"]
        meta_data["split_method"] = data_split["split_method"]
        meta_data["model_idx"] = model_idx
        meta_data["learning_rate"] = configs["learning_rate"]
        meta_data["weight_decay"] = configs["weight_decay"]
        meta_data["model_path"] = f"{log_dir}/model_{model_idx}.pkl"
        meta_data["train_acc"] = train_acc
        meta_data["test_acc"] = test_acc
        meta_data["train_loss"] = train_loss
        meta_data["test_loss"] = test_loss
        meta_data["dataset"] = dataset_name

        model_metadata_dict["model_metadata"][model_idx] = meta_data
        with open(f"{log_dir}/models_metadata.pkl", "wb") as f:
            pickle.dump(model_metadata_dict, f)
        target_model_idx_list.append(model_idx)
    return model_list, model_metadata_dict, target_model_idx_list


def get_info_source_population_attack(
    dataset: torchvision.datasets,
    data_split: dict,
    model: nn.Module,
    configs: dict,
    model_name: str,
):
    """Prepare the information source for calling the core of Privacy Meter for the population attack

    Args:
        dataset(torchvision.datasets): The whole dataset
        data_split (dict): Data split information. 'split' contains a list of dict, each of which has the train, test and audit information. 'split_method' indicates the how the dataset is generated.
        model (nn.Module): Target Model.
        configs (dict): Auditing configuration
        model_name (str): Target model name
    Returns:
        List(Dataset): List of target dataset on which we want to infer the membership
        List(Dataset):  List of auditing datasets we use for launch the attack
        List(nn.Module): List of target models we want to audit
        List(nn.Module): List of reference models (which is the target model based on population attack)
    """
    train_data, train_targets = get_dataset_subset(
        dataset, data_split["train"], model_name, device=configs["device"]
    )
    test_data, test_targets = get_dataset_subset(
        dataset, data_split["test"], model_name, device=configs["device"]
    )
    audit_data, audit_targets = get_dataset_subset(
        dataset, data_split["audit"], model_name, device=configs["device"]
    )
    target_dataset = Dataset(
        data_dict={
            "train": {"x": train_data, "y": train_targets},
            "test": {"x": test_data, "y": test_targets},
        },
        default_input="x",
        default_output="y",
    )

    audit_dataset = Dataset(
        data_dict={"train": {"x": audit_data, "y": audit_targets}},
        default_input="x",
        default_output="y",
    )
    target_model = PytorchModelTensor(
        model_obj=model,
        loss_fn=nn.CrossEntropyLoss(),
        device=configs["device"],
        batch_size=configs["audit_batch_size"],
    )
    return [target_dataset], [audit_dataset], [target_model], [target_model]


def get_info_source_reference_attack(
    log_dir: str,
    dataset: torchvision.datasets,
    data_split: dict,
    model: nn.Module,
    configs: dict,
    model_metadata_dict: dict,
    target_model_idx: int,
    model_name: str,
    dataset_name: str,
):
    """Prepare the information source for the reference attacks

     Args:
        log_dir(str): Log directory that saved all the information, including the models.
        dataset(torchvision.datasets): The whole dataset.
        data_split (dict): Data split information. 'split' contains a list of dict, each of which has the train, test and audit information. 'split_method' indicates the how the dataset is generated.
        model (model): Target Model.
        configs (dict): Auditing configuration.
        model_metadata_dict (dict): Model metedata dict.
        target_model_idx (int): target model index.
        model_name (str): target model name.
        dataset_name (str): name of the dataset.

    Returns:
        target_dataset: List of target dataset on which we want to infer the membership.
        audit_dataset:  List of auditing datasets we use for launch the attack (which is the target dataset based on reference attack)
        target_model: List of target models we want to audit.
        reference_model: List of reference models.
        model_metadata_dict: Updated metadata for the trained model.

    """

    # Construct the target dataset and target models

    train_data, train_targets = get_dataset_subset(
        dataset, data_split["train"], model_name, device=configs["device"]
    )
    test_data, test_targets = get_dataset_subset(
        dataset, data_split["test"], model_name, device=configs["device"]
    )
    target_dataset = Dataset(
        data_dict={
            "train": {"x": train_data, "y": train_targets},
            "test": {"x": test_data, "y": test_targets},
        },
        default_input="x",
        default_output="y",
    )
    target_model = PytorchModelTensor(
        model_obj=model,
        loss_fn=nn.CrossEntropyLoss(),
        device=configs["device"],
        batch_size=configs["audit_batch_size"],
    )
    # Load existing reference models from disk using searching
    reference_idx = load_existing_reference_models(
        model_metadata_dict, configs, target_model_idx
    )

    reference_idx = check_reference_model_dataset(
        model_metadata_dict, reference_idx, target_model_idx, configs["split_method"]
    )
    print(f"Load existing {len(reference_idx)} reference models")
    existing_reference_models = load_existing_models(
        model_metadata_dict,
        reference_idx,
        configs["model_name"],
        dataset,
        dataset_name,
    )
    reference_models = [
        PytorchModelTensor(
            model_obj=model,
            loss_fn=nn.CrossEntropyLoss(),
            device=configs["device"],
            batch_size=configs["audit_batch_size"],
        )
        for model in existing_reference_models
    ]

    # Train additional reference models
    num_reference_models = configs["num_reference_models"] - len(reference_models)
    for reference_idx in range(num_reference_models):
        reference_data_idx = get_split(
            data_split["audit"],
            None,
            size=int(configs["f_reference_dataset"] * len(train_data)),
            split_method=configs["split_method"],
        )

        print(f"Training  {reference_idx}-th reference model")
        start_time = time.time()

        if configs["model_name"] != "speedyresnet":
            reference_loader = get_dataloader(
                torch.utils.data.Subset(dataset, reference_data_idx),
                batch_size=configs["batch_size"],
                shuffle=True,
            )

            reference_model = get_model(configs["model_name"], dataset_name)
            reference_model = train(reference_model, reference_loader, configs)
            # Test performance on the training dataset and test dataset
            train_loss, train_acc = inference(
                model, reference_loader, configs["device"]
            )
        else:
            data = get_cifar10_data(
                dataset,
                reference_data_idx,
                reference_data_idx[: configs["num_test_size"]],
                device=configs["device"],
            )
            print_training_details(
                logging_columns_list, column_heads_only=True
            )  ## print out the training column heads before we print the actual content for each run.
            reference_model, train_acc, train_loss, _, _ = fast_train_fun(
                data,
                make_net(data, device=configs["device"]),
                eval_batchsize=configs["test_batch_size"],
                device=configs["device"],
            )

        logging.info(
            f"Prepare {reference_idx}-th reference model costs {time.time()-start_time} seconds: Train accuracy (on auditing dataset) {train_acc}, Train Loss {train_loss}"
        )

        model_idx = model_metadata_dict["current_idx"]
        model_metadata_dict["current_idx"] += 1
        with open(f"{log_dir}/model_{model_idx}.pkl", "wb") as f:
            pickle.dump(reference_model.state_dict(), f)

        meta_data = {}
        meta_data["train_split"] = reference_data_idx
        meta_data["num_train"] = len(reference_data_idx)
        meta_data["optimizer"] = configs["optimizer"]
        meta_data["batch_size"] = configs["batch_size"]
        meta_data["epochs"] = configs["epochs"]
        meta_data["split_method"] = configs["split_method"]
        meta_data["model_idx"] = model_idx
        meta_data["learning_rate"] = configs["learning_rate"]
        meta_data["weight_decay"] = configs["weight_decay"]
        meta_data["model_name"] = configs["model_name"]
        meta_data["model_path"] = f"{log_dir}/model_{model_idx}.pkl"
        meta_data["dataset"] = dataset_name
        model_metadata_dict["model_metadata"][model_idx] = meta_data
        reference_models.append(
            PytorchModelTensor(
                model_obj=reference_model,
                loss_fn=nn.CrossEntropyLoss(),
                device=configs["device"],
                batch_size=configs["audit_batch_size"],
            )
        )

        # Save the updated metadata
        with open(f"{log_dir}/models_metadata.pkl", "wb") as f:
            pickle.dump(model_metadata_dict, f)

    return (
        [target_dataset],
        [target_dataset],
        [target_model],
        reference_models,
        model_metadata_dict,
    )


def get_signal_and_hypothesis_test_func(configs):
    """Return the attack and way to find the threshold

    Args:
        configs (dict): Auditing configuration.
    """
    signals = []

    if configs["signal"] == "loss":
        signals.append(ModelLoss())
    elif configs["signal"] == "rescaled_logits":
        signals.append(ModelNegativeRescaledLogits())
    else:
        raise ValueError(
            f"{configs['signal']} is not supported. Please use loss or rescaled_logits as the signal."
        )

    hypothesis_test_func = linear_itp_threshold_func
    return signals, hypothesis_test_func


def prepare_information_source(
    log_dir: str,
    dataset: torchvision.datasets,
    data_split: dict,
    model_list: List(nn.Module),
    configs: dict,
    model_metadata_dict: dict,
    target_model_idx_list: List(int) = None,
    model_name: str = None,
    dataset_name: str = None,
):
    """Prepare the information source for calling the core of the Privacy Meter
    Args:
        log_dir (str): Log directory that saved all the information, including the models.
        dataset (torchvision.datasets): The whole dataset
        data_split (dict): Data split information. 'split' contains a list of dict, each of which has the train, test and audit information. 'split_method' indicates the how the dataset is generated.
        model_list (List): List of target models.
        configs (dict): Auditing configuration.
        model_metadata_dict (dict): Model metedata dict.
        model_name str: target model name
        dataset_name (str): name of the dataset

    Returns:
        List(InformationSource): target information source list.
        List(InformationSource): reference information source list.
        List: List of metrics used for each target models.
        List(str): List of directory to save the Privacy Meter results for each target model.
        dict: Updated metadata for the trained model.
    """
    reference_info_source_list = []
    target_info_source_list = []
    metric_list = []
    log_dir_list = []

    # Prepare the information source for each target model
    for split in range(len(model_list)):
        print(f"preparing information sources for {split}-th target model")
        log_dir_path = f"{log_dir}/{configs['report_log']}/signal_{split}"
        signals, hypothesis_test_func = get_signal_and_hypothesis_test_func(configs)
        if configs["algorithm"] == "population":
            (
                target_dataset,
                audit_dataset,
                target_model,
                audit_models,
            ) = get_info_source_population_attack(
                dataset,
                data_split["split"][split],
                model_list[split],
                configs,
                model_name,
            )
            target_info_source = InformationSource(
                models=target_model, datasets=target_dataset
            )
            reference_info_source = InformationSource(
                models=audit_models, datasets=audit_dataset
            )
            metrics = PopulationMetric(
                target_info_source=target_info_source,
                reference_info_source=reference_info_source,
                signals=signals,
                hypothesis_test_func=hypothesis_test_func,
                logs_dirname=log_dir_path,
            )
        elif configs["algorithm"] == "reference_out":
            # Check if there are existing reference models
            (
                target_dataset,
                audit_dataset,
                target_model,
                audit_models,
                model_metadata_dict,
            ) = get_info_source_reference_attack(
                log_dir,
                dataset,
                data_split["split"][split],
                model_list[split],
                configs,
                model_metadata_dict,
                target_model_idx_list[split],
                model_name,
                dataset_name,
            )
            target_info_source = InformationSource(
                models=target_model, datasets=target_dataset
            )
            reference_info_source = InformationSource(
                models=audit_models, datasets=audit_dataset
            )
            metrics = ReferenceMetric(
                target_info_source=target_info_source,
                reference_info_source=reference_info_source,
                signals=signals,
                hypothesis_test_func=hypothesis_test_func,
                logs_dirname=log_dir_path,
            )
        metric_list.append(metrics)

        reference_info_source = InformationSource(
            models=audit_models, datasets=audit_dataset
        )
        reference_info_source_list.append(reference_info_source)
        target_info_source_list.append(target_info_source)

        # Save the log_dir for attacking different target model
        Path(log_dir_path).mkdir(parents=True, exist_ok=True)
        log_dir_list.append(log_dir_path)

    return (
        target_info_source_list,
        reference_info_source_list,
        metric_list,
        log_dir_list,
        model_metadata_dict,
    )


def prepare_priavcy_risk_report(
    log_dir: str, audit_results: List, configs: dict, save_path: str = None
):
    """Generate privacy risk report based on the auditing report

    Args:
        log_dir(str): Log directory that saved all the information, including the models.
        audit_results(List): Privacy meter results.
        configs (dict): Auditing configuration.
        save_path (str, optional): Report path. Defaults to None.

    Raises:
        NotImplementedError: Check if the report for the privacy game is implemented.

    """
    audit_report.REPORT_FILES_DIR = "privacy_meter/report_files"
    if save_path is None:
        save_path = log_dir

    if configs["privacy_game"] in [
        "privacy_loss_model",
        "avg_privacy_loss_training_algo",
    ]:
        # Generate privacy risk report for auditing the model
        if len(audit_results) == 1 and configs["privacy_game"] == "privacy_loss_model":
            ROCCurveReport.generate_report(
                metric_result=audit_results[0],
                inference_game_type=InferenceGame.PRIVACY_LOSS_MODEL,
                save=True,
                filename=f"{save_path}/ROC.png",
            )
            SignalHistogramReport.generate_report(
                metric_result=audit_results[0][0],
                inference_game_type=InferenceGame.PRIVACY_LOSS_MODEL,
                save=True,
                filename=f"{save_path}/Histogram.png",
            )
        # Generate privacy risk report for auditing the training algorithm
        elif (
            len(audit_results) > 1
            and configs["privacy_game"] == "avg_privacy_loss_training_algo"
        ):
            ROCCurveReport.generate_report(
                metric_result=audit_results,
                inference_game_type=InferenceGame.AVG_PRIVACY_LOSS_TRAINING_ALGO,
                save=True,
                filename=f"{save_path}/ROC.png",
            )
        else:
            raise ValueError(
                f"{len(audit_results)} results are not enough for {configs['privacy_game']})"
            )
    else:
        raise NotImplementedError(f"{configs['privacy_game']} is not implemented yet")
