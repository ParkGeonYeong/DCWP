import os
import torch
from torch.utils.data.dataset import Dataset, Subset
from torch.utils import data
from torchvision import transforms as T
from munch import Munch
from data.transforms import transforms, use_preprocess
from data.dataset import CMNISTDataset, CIFAR10Dataset, bFFHQDataset, IdxDataset
from glob import glob
from PIL import Image

dataset_name_dict = {'cifar10c': CIFAR10Dataset,
                     'cmnist': CMNISTDataset,
                     'bffhq': bFFHQDataset}

def get_original_loader(args, mode='unsup', return_dataset=False):
    dataset_name = args.data
    transform = transforms['preprocess' if use_preprocess[dataset_name] else 'original'][dataset_name]['train']
    dataset_class = dataset_name_dict[dataset_name]

    if mode == 'unsup':
        if args.use_unsup_data:
            dataset = dataset_class(root=args.train_root_dir, name=dataset_name, split='train', mode='unsup', use_unsup_data=True,
                                    transform=transform, labeled_ratio=args.labeled_ratio, conflict_pct=args.conflict_pct)
        else:
            return None # If you want to use unsupervised dataset, you should assign args.labeled_ratio < 1.

    elif mode == 'sup':
        if args.use_unsup_data:
            dataset = dataset_class(root=args.train_root_dir, name=dataset_name, split='train', mode='sup', use_unsup_data=True,
                                    transform=transform, labeled_ratio=args.labeled_ratio, conflict_pct=args.conflict_pct)
        else:
            dataset = dataset_class(root=args.train_root_dir, name=dataset_name, split='train', mode='sup', use_unsup_data=False,
                                    transform=transform, labeled_ratio=1., conflict_pct=args.conflict_pct)

    dataset = IdxDataset(dataset)

    if return_dataset:
        return dataset
    else:
        return data.DataLoader(dataset=dataset,
                            batch_size=args.batch_size,
                            shuffle=True,
                            num_workers=args.num_workers,
                            pin_memory=True)

def get_concat_loader(args):
    """NOT used"""
    # For VAE only. VAE can use both labeled and unlabeled data.
    unsup_dataset = get_original_loader(args, mode='unsup', return_dataset=True)
    sup_dataset = get_original_loader(args, mode='sup', return_dataset=True)

    if unsup_dataset is not None:
        concat_dataset = data.ConcatDataset([unsup_dataset, sup_dataset])
    else:
        concat_dataset = sup_dataset

    return data.DataLoader(dataset=concat_dataset,
                        batch_size=args.batch_size,
                        shuffle=True,
                        num_workers=args.num_workers,
                        pin_memory=True)

def get_aug_loader(args, mode='unsup'):
    # align (original, augmented) dataset
    pass

def get_val_loader(args):
    dataset_name = args.data
    transform = transforms['preprocess' if use_preprocess[dataset_name] else 'original'][dataset_name]['test']
    dataset_class = dataset_name_dict[dataset_name]

    dataset = dataset_class(root=args.val_root_dir, split='test', transform=transform)
    dataset = IdxDataset(dataset)
    return data.DataLoader(dataset=dataset,
                           batch_size=args.batch_size,
                           shuffle=False,
                           num_workers=args.num_workers,
                           pin_memory=True)

class InputFetcher:
    def __init__(self, loader_sup, loader_unsup=None, use_unsup_data=True, mode='augment',
                 latent_dim=16, num_classes=10):
        self.loader_sup = loader_sup
        self.loader_unsup = loader_unsup
        self.use_unsup_data = use_unsup_data
        self.mode = mode
        self.num_classes = num_classes
        self.latent_dim = latent_dim
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    def _fetch_sup(self):
        try:
            idx, x, attr, fname = next(self.iter) # attr: (class_label, bias_label)
        except (AttributeError, StopIteration):
            self.iter = iter(self.loader_sup)
            idx, x, attr, fname = next(self.iter)
        return idx, x, attr, fname

    def _fetch_unsup(self):
        try:
            idx, x, attr, fname = next(self.iter_unsup) # attr is not allowed to be used in training
        except (AttributeError, StopIteration):
            self.iter_unsup = iter(self.loader_unsup)
            idx, x, attr, fname  = next(self.iter_unsup)
        return idx, x, attr, fname

    def _shuffle_domain_label(self, y):
        return y[torch.randperm(len(y))]

    def _label2onehot(self, labels, num_domain, latent_dim):
        """Convert label indices to one-hot vectors.
        @param labels: (N,) domain (class) labels
        @param latent_dim: int, width of output vector

        output: (N, num_domain, latent_dim, latent_dim) one-hot encoded domain labels
        """
        batch_size = labels.size(0)
        out = torch.zeros(batch_size, num_domain)
        out[range(batch_size), labels.long()] = 1

        out = out.view(out.size(0), out.size(1), 1, 1)
        out = out.repeat(1, 1, latent_dim, latent_dim)
        return out

    def __next__(self):
        if self.mode == 'FeatureSwap' or self.mode == 'ours':
            idx, x, attr, fname = self._fetch_sup()
            y = attr[:, 0]
            bias_label = attr[:, 1]

            y_trg = self._shuffle_domain_label(y)

            c_trg = self._label2onehot(y_trg, self.num_classes, self.latent_dim)
            c_src = self._label2onehot(y, self.num_classes, self.latent_dim)

            if self.use_unsup_data:
                _, x_u, attr_u, fname_u = self._fetch_unsup()
                inputs = Munch(index=idx, x_sup=x, y=y, bias_label=bias_label,
                               fname_sup=fname,
                               y_trg=y_trg, c_trg=c_trg, c_src=c_src,
                               x_unsup=x_u, attr_unsup=attr_u, fname_unsup=fname_u)
            else:
                inputs = Munch(index=idx, x_sup=x, y=y, bias_label=bias_label,
                               fname_sup=fname,
                               y_trg=y_trg, c_trg=c_trg, c_src=c_src)

        elif self.mode == 'test':
            idx, x, attr, fname = self._fetch_sup()
            inputs = Munch(index=idx, x_sup=x, attr_sup=attr, fname_sup=fname)
        else:
            raise NotImplementedError

        return Munch({k: v if 'fname' in k else v.to(self.device)
                      for k, v in inputs.items()})

