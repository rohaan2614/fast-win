import torch
import numpy as np
import random
from torch.utils.data import Dataset
from config import get_parms
from shared.compression import quantize, dequantize_tensor, top_k, random_k

args = get_parms("utils").parse_args()


def generate_gaussian_matrix(d: int, 
                             f: int = args.f, 
                             chunk_size: int = 1, 
                             device="cpu") -> torch.Tensor:
    G = torch.empty(d, f, device=device)
    for i in range(0, d, chunk_size):
        end_idx = min(i + chunk_size, d)
        G[i:end_idx, :] = torch.randn(end_idx - i, f, device=device)
    return G


def get_approx_optimal_weights(G: torch.Tensor, 
                               delta: torch.Tensor, 
                               f: int = args.f, 
                               chunk_size: int = 1000) -> torch.Tensor:
    delta = delta.to(G.device)
    w = torch.zeros(f, device=G.device)
    d = G.size(0)
    for i in range(0, d, chunk_size):
        end_idx = min(i + chunk_size, d)
        G_chunk = G[i:end_idx, :]
        w += G_chunk.T @ delta[i:end_idx] / f
    return w


def set_all_param_zero(model):
    with torch.no_grad():
        for p in model.parameters():
            p.zero_()


def set_flatten_model_back(model, x_flattern):
    with torch.no_grad():
        start = 0
        for p in model.parameters():
            if not p.requires_grad:
                continue
            p_extract = x_flattern[start : (start + p.numel())]
            p.set_(p_extract.view(p.shape).clone())
            if p.grad is not None:
                p.grad.zero_()
            start += p.numel()


def get_flatten_model_param(model):
    with torch.no_grad():
        return torch.cat(
            [p.detach().view(-1) for p in model.parameters() if p.requires_grad]
        )


def get_flatten_model_grad(model) -> torch.Tensor:
    with torch.no_grad():
        return torch.cat(
            [p.grad.detach().view(-1) for p in model.parameters() if p.requires_grad]
        )


def accuracy(output, target):
    # get the index of the max log-probability
    pred = output.data.max(1, keepdim=True)[1]
    return pred.eq(target.data.view_as(pred)).cpu().float().mean()


class Metric(object):
    def __init__(self, name):
        self.name = name
        self.sum = 0
        self.n = 0

    def update(self, val):
        if isinstance(val, torch.Tensor):
            self.sum += val.detach().cpu()
        else:
            self.sum += val
        self.n += 1

    @property
    def avg(self):
        return self.sum / self.n


class Agent:
    def __init__(self, *, model, optimizer, scheduler, criterion, train_loader, device):
        self.model = model.to(device)
        self.criterion = criterion
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.train_loader = train_loader
        self.train_loss = Metric("train_loss")
        self.train_accuracy = Metric("train_accuracy")
        self.device = device
        self.batch_idx = 0
        self.epoch = 0
        self.data_generator = self.get_one_train_batch()
        self.model_grad = torch.zeros_like(get_flatten_model_param(self.model))
        self.G = torch.zeros_like(get_flatten_model_param(self.model)).to(self.device)

    def pull_G(self, server):
        self.G = server.G

    def get_one_train_batch(self):
        for batch_idx, (inputs, targets) in enumerate(self.train_loader):
            yield batch_idx, (inputs, targets)

    def reset_epoch(self):
        self.data_generator = self.get_one_train_batch()
        self.batch_idx = 0
        self.epoch += 1
        self.train_loss = Metric("train_loss")
        self.train_accuracy = Metric("train_accuracy")

    def pull_model_from_server(self, server):
        # print("pull_model_from_server")
        if self.device != "cpu":
            # Notice the device between server and client may be different.
            with torch.device(self.device):
                # This context manager is necessary for the clone operation.
                set_flatten_model_back(
                    self.model, server.flatten_params.to(self.device)
                )
        else:
            set_flatten_model_back(self.model, server.flatten_params)

    def decay_lr_in_optimizer(self, gamma: float):
        for g in self.optimizer.param_groups:
            g["lr"] *= gamma

    def train_k_step_fedavg(self, k: int):
        self.model.train()
        # Initialize an empty gradient tensor
        self.model_grad = torch.zeros_like(get_flatten_model_param(self.model))
        for i in range(k):
            try:
                batch_idx, (inputs, targets) = next(self.data_generator)
            except StopIteration:
                loss, acc = self.train_loss.avg, self.train_accuracy.avg
                self.reset_epoch()
                return loss, acc
            inputs, targets = inputs.to(self.device), targets.to(self.device)
            self.model.zero_grad()
            outputs = self.model(inputs)
            loss = self.criterion(outputs, targets)
            loss.backward()
            # Get the gradient and add it to model_grad
            self.model_grad += get_flatten_model_grad(self.model)

            self.optimizer.step()
            self.train_loss.update(loss.item())
            self.train_accuracy.update(accuracy(outputs, targets).item())
        return self.train_loss.avg, self.train_accuracy.avg

    def eval(self, test_dataloader) -> tuple[float, float]:
        self.model.eval()
        val_accuracy = Metric("val_accuracy")
        val_loss = Metric("val_loss")
        for batch_idx, (inputs, targets) in enumerate(test_dataloader):
            inputs, targets = inputs.to(self.device), targets.to(self.device)
            outputs = self.model(inputs)
            val_accuracy.update(accuracy(outputs, targets).item())
            val_loss.update(self.criterion(outputs, targets).item())
        return val_loss.avg, val_accuracy.avg


def local_update_selected_clients_fedavg(clients: list[Agent], server, local_update):
    train_loss_sum, train_acc_sum = 0, 0
    for client in clients:
        train_loss, train_acc = client.train_k_step_fedavg(k=local_update)
        train_loss_sum += train_loss
        train_acc_sum += train_acc
    return train_loss_sum / len(clients), train_acc_sum / len(clients)


class Server:
    def __init__(self, *, model, criterion, device):
        self.model = model.to(device)
        self.flatten_params = get_flatten_model_param(self.model).to(device)
        self.criterion = criterion
        self.device = device
        self.num_arb_participation = 0
        self.num_uni_participation = 0
        self.momentum = self.flatten_params.clone().zero_()
        self.G = generate_gaussian_matrix(
            get_flatten_model_param(self.model).size(0)
        ).to(device)

    def avg_clients(self, clients: list[Agent], weights=0):
        if args.algo == "fedavg":
            for client in clients:
                delta = client.model_grad
                Gw = self.G @ get_approx_optimal_weights(G=self.G, 
                                                         delta=client.model_grad).to(self.device)
                mse = torch.mean((delta - Gw) ** 2)
                print('MSE:', mse)
                self.flatten_params -= Gw.mul_(args.lr / len(clients))
            set_flatten_model_back(self.model, self.flatten_params)
        self.G = generate_gaussian_matrix(
            get_flatten_model_param(self.model).size(0)
        ).to(self.device)

    def eval(self, test_dataloader) -> tuple[float, float]:
        self.model.eval()
        val_accuracy = Metric("val_accuracy")
        val_loss = Metric("val_loss")
        for batch_idx, (inputs, targets) in enumerate(test_dataloader):
            inputs, targets = inputs.to(self.device), targets.to(self.device)
            outputs = self.model(inputs)
            val_accuracy.update(accuracy(outputs, targets).item())
            val_loss.update(self.criterion(outputs, targets).item())
        return val_loss.avg, val_accuracy.avg

    # Determine the sampling method by q
    def determine_sampling(self, q: float, sampling_type: float) -> float:
        if "_" in sampling_type:
            sampling_methods = sampling_type.split("_")
            if random.random() < q:
                return "uniform"
            else:
                return sampling_methods[1]
        else:
            return sampling_type


class DatasetSplit(Dataset):
    def __init__(self, dataset, idxs):
        self.dataset = dataset
        self.idxs = list(idxs)

    def __len__(self):
        return len(self.idxs)

    def __getitem__(self, item):
        image, label = self.dataset[self.idxs[item]]
        return image, label


def partition(dataset, n_nodes, data_dirichlet_alpha):
    dict_users = {i: np.array([], dtype="int64") for i in range(n_nodes)}

    if isinstance(dataset.targets, torch.Tensor):
        labels = dataset.targets.numpy()
    else:
        labels = np.array(dataset.targets)

    min_label = min(labels)
    max_label = max(labels)
    num_labels = max_label - min_label + 1

    label_distributions_each_node = np.random.dirichlet(
        data_dirichlet_alpha * np.ones(num_labels), n_nodes
    )
    sum_prob_per_label = np.sum(label_distributions_each_node, axis=0)

    indices_per_label = []
    for i in range(min_label, max_label + 1):
        indices_per_label.append([j for j in range(len(labels)) if labels[j] == i])

    start_index_per_label = np.zeros(num_labels, dtype="int64")
    for n in range(n_nodes):
        for i in range(num_labels):
            end_index = int(
                np.round(
                    len(indices_per_label[i])
                    * np.sum(label_distributions_each_node[: n + 1, i])
                    / sum_prob_per_label[i]
                )
            )
            dict_users[n] = np.concatenate(
                (
                    dict_users[n],
                    np.array(
                        indices_per_label[i][start_index_per_label[i] : end_index],
                        dtype="int64",
                    ),
                ),
                axis=0,
            )
            start_index_per_label[i] = end_index

    actual_label_distributions_each_node = [
        np.array(
            [
                len([j for j in labels[dict_users[n]] if j == i])
                for i in range(min_label, max_label + 1)
            ],
            dtype="int64",
        )
        / len(dict_users[n])
        for n in range(n_nodes)
    ]

    return dict_users, actual_label_distributions_each_node, num_labels


def data_each_node(data_train, n_nodes, data_dirichlet_alpha):
    dict_users, actual_label_distributions_each_node, num_labels = partition(
        data_train, n_nodes, data_dirichlet_alpha
    )
    return dict_users
