import os
import argparse
from copy import deepcopy

from munch import Munch
from torch.backends import cudnn
import torch

from training.pruning_solver import PruneSolver
from training.feature_swap_solver import FeatureSwapSolver
from util import setup, save_config


def main(args):
    print(args)
    args = setup(args) # Making folders following exp_name
    save_config(args)
    cudnn.benchmark = True
    torch.manual_seed(args.seed)

    if args.mode == 'prune':
        solver = PruneSolver(args)
    elif args.mode == 'featureswap':
        solver = FeatureSwapSolver(args)
    elif args.mode == 'LfF':
        solver = LfFSolver(args)
    elif args.mode == 'JTT':
        solver = JTTSolver(args)
    else:
        raise NotImplementedError

    #TODO: if pseudo_label file does not exists, train biased model first

    if args.phase == 'train':
        solver.train()
    else:
        solver.evaluate()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # Data arguments
    parser.add_argument('--data', type=str, default='cmnist',
                        choices=['cmnist', 'cifar10c', 'bffhq'])
    parser.add_argument('--cmnist_use_mlp', default=False, action='store_true')
    parser.add_argument('--conflict_pct', type=float, default=5., choices=[0.5, 1., 2., 5.],
                        help='Percent of bias-conflicting data')
    parser.add_argument('--phase', type=str, default='train',
                        choices=['train', 'test'])

    # weight for objective functions
    parser.add_argument('--lambda_con', type=float, default=0)
    parser.add_argument('--lambda_sparse', type=float, default=1e-8)
    parser.add_argument('--lambda_upweight', type=float, default=20)

    # training arguments
    parser.add_argument('--batch_size', type=int, default=256,
                        help='Batch size for training')
    parser.add_argument('--do_lr_scheduling', default=True)
    parser.add_argument('--lr_decay_step', type=int, default=600)
    parser.add_argument('--lr_gamma', type=float, default=0.1)
    parser.add_argument('--lr_main', type=float, default=1e-1)
    parser.add_argument('--lr_prune', type=float, default=1e-2)
    parser.add_argument('--beta1', type=float, default=0.9)
    parser.add_argument('--beta2', type=float, default=0.99)
    parser.add_argument('--pretrain_iter', type=int, default=2000)
    parser.add_argument('--pruning_iter', type=int, default=2000)
    parser.add_argument('--retrain_iter', type=int, default=2000)
    parser.add_argument('--weight_decay', type=float, default=1e-4) #TODO: weight decay is important in JTT!

    # misc
    parser.add_argument('--mode', type=str, required=True,
                        choices=['prune', 'featureswap', 'LfF', 'JTT'])
    parser.add_argument('--num_workers', type=int, default=4,
                        help='Number of workers used in DataLoader')
    parser.add_argument('--seed', type=int, default=7777,
                        help='Seed for random number generator')

    # directory for training
    parser.add_argument('--train_root_dir', type=str, default='/home/user/research/dataset')
    parser.add_argument('--val_root_dir', type=str, default='/home/user/research/dataset')
    parser.add_argument('--log_dir', type=str, default='expr/log')
    parser.add_argument('--result_dir', type=str, default='expr/results',
                        help='Directory for saving generated images')
    parser.add_argument('--checkpoint_dir', type=str, default='expr/checkpoints',
                        help='Directory for saving network checkpoints')
    parser.add_argument('--exp_name', type=str, default=None,
                        help='Nametag for the experiment')

    # step size
    parser.add_argument('--print_every', type=int, default=500)
    parser.add_argument('--save_every', type=int, default=500)
    parser.add_argument('--eval_every', type=int, default=500)

    args = parser.parse_args()
    main(args)
