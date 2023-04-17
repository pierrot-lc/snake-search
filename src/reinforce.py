from typing import Any, Iterator

import torch
from torch.distributions import Categorical
from torch.optim import Optimizer
from torch.utils.data import DataLoader
from tqdm import tqdm

import wandb

from .env import NeedleEnv
from .model import GRUPolicy
from .plot import plot_trajectory


class Reinforce:
    """The REINFORCE algorithm.
    It used to train the model, on a batched environment.
    """

    def __init__(
        self,
        model: GRUPolicy,
        optimizer: Optimizer,
        train_loader: DataLoader,
        test_loader: DataLoader,
        patch_size: int,
        max_ep_len: int,
        n_iterations: int,
        plot_every: int,
        device: str,
    ):
        self.model = model
        self.optimizer = optimizer
        self.train_loader = train_loader
        self.test_loader = test_loader
        self.patch_size = patch_size
        self.max_ep_len = max_ep_len
        self.n_iterations = n_iterations
        self.plot_every = plot_every
        self.device = device

    def rollout(self, env: NeedleEnv) -> dict[str, torch.Tensor]:
        """Do a rollout on the given environment.
        Returns the rewards, returns and logprobs of the rollout.
        """
        rewards = torch.zeros(
            (env.batch_size, env.max_ep_len),
            device=self.device,
        )
        logprobs = torch.zeros(
            (env.batch_size, env.max_ep_len),
            device=self.device,
        )
        masks = torch.zeros(
            (env.batch_size, env.max_ep_len),
            dtype=torch.bool,
            device=self.device,
        )
        percentages = torch.zeros(
            (env.batch_size,),
            device=self.device,
        )

        memory = None
        actions = torch.zeros((env.batch_size,), dtype=torch.long, device=self.device)
        patches, _ = env.reset()

        for step_id in range(env.max_ep_len):
            logits, memory = self.model(patches, actions, memory)
            categorical = Categorical(logits=logits)
            actions = categorical.sample()

            patches, step_rewards, terminated, truncated, infos = env.step(actions)

            rewards[:, step_id] = step_rewards
            logprobs[:, step_id] = categorical.log_prob(actions)
            masks[:, step_id] = ~terminated
            percentages = infos["percentages"]

            if torch.all(terminated | truncated):
                # All environments are done.
                break

        # The last terminated state is not counted in the masks,
        # so we need to shift the masks by 1 to make sure we include id.
        masks = torch.roll(masks, shifts=1, dims=(1,))
        masks[:, 0] = True

        rewards = torch.flip(rewards, dims=(1,))
        masks = torch.flip(masks, dims=(1,))
        cumulated_returns = torch.cumsum(rewards * masks, dim=1)
        cumulated_returns = torch.flip(cumulated_returns, dims=(1,))
        rewards = torch.flip(rewards, dims=(1,))
        masks = torch.flip(masks, dims=(1,))

        return {
            "rewards": rewards,
            "returns": cumulated_returns,
            "logprobs": logprobs,
            "masks": masks,
            "percentages": percentages,
        }

    def compute_metrics(
        self, rollout: dict[str, torch.Tensor]
    ) -> dict[str, torch.Tensor]:
        """Compute the metrics of the given rollout.

        ---
        Args:
            rollout: A dictionary containing the rewards, returns and logprobs.

        ---
        Returns:
            The metrics, containing the loss.
        """
        metrics = dict()
        returns = rollout["returns"]
        masks = rollout["masks"]

        advantages = (returns - returns.mean(dim=0, keepdim=True)) / (
            returns.std(dim=0, keepdim=True) + 1e-8
        )
        metrics["loss"] = (
            -(rollout["logprobs"] * advantages * masks).sum() / masks.sum()
        )
        metrics["returns"] = (rollout["rewards"] * masks).sum(dim=1).mean()
        metrics["percentages"] = rollout["percentages"].mean()
        metrics["episode-length"] = masks.sum(dim=1).float().mean()
        return metrics

    def launch_training(self, group: str, config: dict[str, Any]):
        """Train the model using REINFORCE.
        The logs are sent to Weights & Biases.

        ---
        Args:
            group: The name of the group of experiments.
            config: A dictionary of hyperparameters.
        """
        print(f"Launching REINFORCE on device {self.device}")
        self.model.to(self.device)

        train_iter = iter(self.train_loader)
        test_iter = iter(self.test_loader)

        with wandb.init(
            project="snake-search",
            entity="pierrotlc",
            group=group,
            config=config,
        ) as run:
            # Log gradients and model parameters.
            run.watch(self.model)

            for step_id in tqdm(range(self.n_iterations)):
                self.model.train()
                images, bboxes = next(train_iter)
                images = images.to(self.device)
                env = NeedleEnv(images, bboxes, self.patch_size, self.max_ep_len)

                rollout = self.rollout(env)
                metrics = self.compute_metrics(rollout)

                self.optimizer.zero_grad()
                metrics["loss"].backward()
                self.optimizer.step()

                if step_id % self.plot_every == 0:
                    # Log the trajectories on a batch of images.
                    plots = self.get_predictions(train_iter)
                    metrics["trajectories/train"] = wandb.Image(plots)
                    plots = self.get_predictions(test_iter)
                    metrics["trajectories/test"] = wandb.Image(plots)

                run.log(metrics)

    def get_predictions(
        self, iter_loader: Iterator, n_predictions: int = 16
    ) -> torch.Tensor:
        """Sample from the loader and make predictions on the sampled images."""
        images, bboxes = next(iter_loader)
        images = images.to(self.device)
        images = images[:n_predictions]
        bboxes = bboxes[:n_predictions]
        env = NeedleEnv(images, bboxes, self.patch_size, self.max_ep_len)
        plots = self.predict(env)
        return plots

    @torch.no_grad()
    def predict(self, env: NeedleEnv) -> torch.Tensor:
        """Evaluates the model on a batch of images.
        Return a plot of its trajectories on all images.

        ---
        Args:
            env: The environment to evaluate the model on.

        ---
        Returns:
            The images of all predicted trajectories of the model.
                Shape of [batch_size, 3, height, width].
        """
        self.model.eval()
        memory = None
        actions = torch.zeros((env.batch_size,), dtype=torch.long, device=self.device)
        patches, infos = env.reset()

        positions = torch.zeros(
            (env.batch_size, env.max_ep_len + 1, 2), dtype=torch.long, device=env.device
        )
        positions[:, 0] = infos["positions"]

        masks = torch.zeros(
            (env.batch_size, env.max_ep_len + 1),
            dtype=torch.bool,
            device=self.device,
        )
        masks[:, 0] = True

        for step_id in range(env.max_ep_len):
            logits, memory = self.model(patches, actions, memory)
            actions = logits.argmax(dim=1)  # Greedy policy.

            patches, _, terminated, _, infos = env.step(actions)
            positions[:, step_id + 1] = infos["positions"]
            masks[:, step_id + 1] = ~terminated

        # The last terminated state is not counted in the masks,
        # so we need to shift the masks by 1 to make sure we include id.
        masks = torch.roll(masks, shifts=1, dims=(1,))
        masks[:, 0] = True

        images = [
            plot_trajectory(image, pos[mask], env.patch_size, bboxes)
            for image, pos, mask, bboxes in zip(
                env.images, positions, masks, env.bboxes
            )
        ]
        images = torch.stack(images, dim=0)
        return images
