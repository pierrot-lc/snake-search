from pathlib import Path

import hydra
import torch
import torch.nn as nn
import torch.optim as optim
from hydra.utils import to_absolute_path
from omegaconf import DictConfig, OmegaConf
from torch.utils.data import DataLoader
from torch.utils.data.sampler import RandomSampler
from torchinfo import summary

from src.dataset import CelebADataset, NeedleDataset, StandardDataset
from src.model import GRUPolicy
from src.reinforce import Reinforce


def init_celeba_datasets(config: DictConfig) -> tuple[CelebADataset, CelebADataset]:
    # Make sure the data is downloaded to the same location every runs.
    root_path = Path(to_absolute_path(config.data.path))
    train_dataset = CelebADataset("train", root_path)
    test_dataset = CelebADataset("test", root_path)
    return train_dataset, test_dataset


def init_standard_datasets(config: DictConfig) -> tuple[StandardDataset, NeedleDataset]:
    root_path = Path(to_absolute_path(config.data.path))
    train_dataset, test_dataset = StandardDataset.load_from_dir(root_path)
    return train_dataset, test_dataset


def init_datasets(config: DictConfig) -> tuple[NeedleDataset, NeedleDataset]:
    """Initialize the train and test datasets.

    ---
    Args:
    config: The Hydra configuration.

    ---
    Returns:
        train_dataset: The train dataset.
        test_dataset: The test dataset.
    """
    match config.data.dataset:
        case "celeba":
            train_dataset, test_dataset = init_celeba_datasets(config)
        case "standard":
            train_dataset, test_dataset = init_standard_datasets(config)
        case _:
            raise ValueError(f"Unknown dataset: {config.data.dataset}")

    train_dataset = NeedleDataset(train_dataset)
    test_dataset = NeedleDataset(test_dataset)
    return train_dataset, test_dataset


def init_dataloaders(
    config: DictConfig, train_dataset: NeedleDataset, test_dataset: NeedleDataset
) -> tuple[DataLoader, DataLoader]:
    train_loader = DataLoader(
        train_dataset,
        batch_size=config.data.batch_size,
        collate_fn=lambda b: NeedleDataset.collate_fn(b, config.env.patch_size),
        num_workers=config.data.num_workers,
        sampler=RandomSampler(train_dataset, replacement=True, num_samples=int(1e10)),
        shuffle=False,
        pin_memory=True,
    )
    test_loader = DataLoader(
        test_dataset,
        batch_size=config.data.batch_size,
        collate_fn=lambda b: NeedleDataset.collate_fn(b, config.env.patch_size),
        num_workers=config.data.num_workers,
        sampler=RandomSampler(test_dataset, replacement=True, num_samples=int(1e10)),
        shuffle=False,
        pin_memory=True,
    )
    return train_loader, test_loader


def init_model(config: DictConfig, dataset: NeedleDataset) -> GRUPolicy:
    model = GRUPolicy(
        n_channels=config.model.n_channels,
        kernels=config.model.kernels,
        maxpools=config.model.maxpools,
        embedding_size=config.model.embedding_size,
        n_layers_mlp=config.model.n_layers_mlp,
        gru_hidden_size=config.model.gru_hidden_size,
        gru_num_layers=config.model.gru_num_layers,
        maximum_steps=config.model.maximum_steps,
    )
    image, _ = dataset[0]
    n_channels = image.shape[0]
    patches = torch.zeros((1, n_channels, config.env.patch_size, config.env.patch_size))
    actions = torch.zeros((1, 4), dtype=torch.long)
    summary(
        model,
        input_data=[patches, actions],
    )
    return model


def init_optimizer(config: DictConfig, model: nn.Module) -> optim.Optimizer:
    optimizer = optim.AdamW(model.parameters(), lr=config.optimizer.learning_rate)
    return optimizer


@hydra.main(version_base="1.3", config_path="configs", config_name="base")
def main(config: DictConfig):
    if config.device == "auto":
        config.device = "cuda" if torch.cuda.is_available() else "cpu"

    train_dataset, test_dataset = init_datasets(config)
    train_lodaer, test_loader = init_dataloaders(config, train_dataset, test_dataset)
    model = init_model(config, train_dataset)
    optimizer = init_optimizer(config, model)
    reinforce = Reinforce(
        model=model,
        optimizer=optimizer,
        train_loader=train_lodaer,
        test_loader=test_loader,
        patch_size=config.env.patch_size,
        max_ep_len=config.env.max_ep_len,
        n_iterations=config.reinforce.n_iterations,
        log_every=config.reinforce.log_every,
        plot_every=config.reinforce.plot_every,
        device=config.device,
    )
    reinforce.launch_training(
        config.group, OmegaConf.to_container(config, resolve=True)
    )


if __name__ == "__main__":
    # Launch the hydra app.
    main()
