import os
from os.path import join as ospj
import time
import datetime
from munch import Munch
import logging
import numpy as np

import torch

import util.utils as utils
from util.utils import MultiDimAverageMeter
from data.data_loader import InputFetcher
from data.data_loader import get_original_loader, get_val_loader
from training.solver import Solver


class FeatureSwapSolver(Solver):
    # Hyperparameter settings in util/__init__.py
    def __init__(self, args):
        super(FeatureSwapSolver, self).__init__(args)
        for net in self.nets.keys():
            self.optims[net] = torch.optim.Adam(
                params=self.nets[net].parameters(),
                lr=args.lr_pre,
                betas=(args.beta1, args.beta2),
                weight_decay=0
            )

        self.scheduler = Munch()
        if not args.no_lr_scheduling:
            for net in self.nets.keys():
                self.scheduler[net] = torch.optim.lr_scheduler.StepLR(
                    self.optims[net], step_size=args.lr_decay_step_pre, gamma=args.lr_gamma_pre)

    def validation(self, fetcher, swap=False):
        self.nets.biased_F.eval()
        self.nets.debiased_F.eval()
        self.nets.biased_C.eval()
        self.nets.debiased_C.eval()

        attrwise_acc_meter_bias = MultiDimAverageMeter(self.attr_dims)
        attrwise_acc_meter_debias = MultiDimAverageMeter(self.attr_dims)
        iterator = enumerate(fetcher)

        total_correct, total_num = 0, 0
        total_correct_b, total_num_b = 0, 0
        total_correct_b_bias, total_num_b_bias = 0, 0

        for index, (_, data, attr, fname) in iterator:
            label = attr[:, 0].to(self.device)
            data = data.to(self.device)

            with torch.no_grad():
                z_l = self.nets.debiased_F.extract(data)
                z_b = self.nets.biased_F.extract(data)

                z = torch.cat((z_l, z_b), dim=1)
                logit = self.nets.debiased_C(z)
                logit_b = self.nets.biased_C(z)

                pred = logit.data.max(1, keepdim=True)[1].squeeze(1)
                pred_b = logit_b.data.max(1, keepdim=True)[1].squeeze(1)

                correct = (pred == label).long()
                correct_b = (pred_b == label).long()

                total_correct += correct.sum()
                total_num += correct.shape[0]
                total_correct_b += correct_b.sum()
                total_num_b += correct_b.shape[0]

            attr = attr[:, [0, 1]]
            attrwise_acc_meter_bias.add(correct_b.cpu(), attr.cpu())
            attrwise_acc_meter_debias.add(correct.cpu(), attr.cpu())

        total_acc_d = total_correct/float(total_num)
        total_acc_b = total_correct_b/float(total_num_b)

        accs_b = attrwise_acc_meter_bias.get_mean()
        accs_d = attrwise_acc_meter_debias.get_mean()

        self.nets.biased_F.train()
        self.nets.debiased_F.train()
        self.nets.biased_C.train()
        self.nets.debiased_C.train()

        return total_acc_b, total_acc_d, accs_b, accs_d

    def set_loss_ema(self, dataset):
        try:
            train_target_attr = dataset.y_array
        except:
            raise ValueError('Please define y_array for your dataset')

        self.sample_loss_ema_b = utils.EMA(train_target_attr, num_classes=self.num_classes, alpha=0.7)
        self.sample_loss_ema_d = utils.EMA(train_target_attr, num_classes=self.num_classes, alpha=0.7)

    def compute_dis_loss(self, x, idx, label):
        z_l = self.nets.debiased_F.extract(x)
        z_b = self.nets.biased_F.extract(x)

        # Gradients of z_b are not backpropagated to z_l (and vice versa) in order to guarantee disentanglement of representation.
        z_conflict = torch.cat((z_l, z_b.detach()), dim=1)
        z_align = torch.cat((z_l.detach(), z_b), dim=1)

        # Prediction using z=[z_l, z_b]
        pred_conflict = self.nets.debiased_C(z_conflict)
        pred_align = self.nets.biased_C(z_align)

        loss_dis_conflict = self.criterion(pred_conflict, label).detach()
        loss_dis_align = self.criterion(pred_align, label).detach()

        # EMA sample loss
        self.sample_loss_ema_d.update(loss_dis_conflict, idx)
        self.sample_loss_ema_b.update(loss_dis_align, idx)

        # class-wise normalize
        loss_dis_conflict = self.sample_loss_ema_d.parameter[idx].clone().detach()
        loss_dis_align = self.sample_loss_ema_b.parameter[idx].clone().detach()

        loss_dis_conflict = loss_dis_conflict.to(self.device)
        loss_dis_align = loss_dis_align.to(self.device)

        for c in range(self.num_classes):
            class_index = torch.where(label == c)[0].to(self.device)
            max_loss_conflict = self.sample_loss_ema_d.max_loss(c)
            max_loss_align = self.sample_loss_ema_b.max_loss(c)
            loss_dis_conflict[class_index] /= max_loss_conflict
            loss_dis_align[class_index] /= max_loss_align

        loss_weight = loss_dis_align / (loss_dis_align + loss_dis_conflict + 1e-8)                          # Eq.1 (reweighting module) in the main paper
        loss_dis_conflict = self.criterion(pred_conflict, label) * loss_weight.to(self.device)              # Eq.2 W(z)CE(C_i(z),y)
        loss_dis_align = self.bias_criterion(pred_align, label)                                             # Eq.2 GCE(C_b(z),y)

        return z_l, z_b, loss_dis_conflict, loss_dis_align, loss_weight

    def compute_swap_loss(self, z_b, z_l, label, loss_weight):
        indices = np.random.permutation(z_b.size(0))
        z_b_swap = z_b[indices]         # z tilde
        label_swap = label[indices]     # y tilde

        # Prediction using z_swap=[z_l, z_b tilde]
        # Again, gradients of z_b tilde are not backpropagated to z_l (and vice versa) in order to guarantee disentanglement of representation.
        z_mix_conflict = torch.cat((z_l, z_b_swap.detach()), dim=1)
        z_mix_align = torch.cat((z_l.detach(), z_b_swap), dim=1)

        # Prediction using z_swap
        pred_mix_conflict = self.nets.debiased_C(z_mix_conflict)
        pred_mix_align = self.nets.biased_C(z_mix_align)

        loss_swap_conflict = self.criterion(pred_mix_conflict, label) * loss_weight.to(self.device)     # Eq.3 W(z)CE(C_i(z_swap),y)
        loss_swap_align = self.bias_criterion(pred_mix_align, label_swap)                               # Eq.3 GCE(C_b(z_swap),y tilde)

        return z_b_swap, label_swap, loss_swap_conflict, loss_swap_align

    def train(self):
        logging.info('=== Start training ===')
        args = self.args
        nets = self.nets
        optims = self.optims

        fetcher = InputFetcher(self.loaders.train)
        fetcher_val = self.loaders.val
        start_time = time.time()
        self.set_loss_ema(get_original_loader(args, return_dataset=True))

        for i in range(args.total_iter):
            # fetch images and labels
            inputs = next(fetcher)
            idx, x, label, fname = inputs.index, inputs.x, inputs.y, inputs.fname
            z_l, z_b, loss_dis_conflict, loss_dis_align, loss_weight = self.compute_dis_loss(x, idx, label)

            # feature-level augmentation : augmentation after certain iteration (after representation is disentangled at a certain level)
            if i+1 > args.swap_iter:
                z_b_swap, label_swap, loss_swap_conflict, loss_swap_align = self.compute_swap_loss(z_b, z_l, label, loss_weight)
            else:
                # before feature-level augmentation
                loss_swap_conflict = torch.tensor([0]).float().to(self.device)
                loss_swap_align = torch.tensor([0]).float().to(self.device)

            loss_dis  = loss_dis_conflict.mean() + args.lambda_dis_align * loss_dis_align.mean()                # Eq.2 L_dis
            loss_swap = loss_swap_conflict.mean() + args.lambda_swap_align * loss_swap_align.mean()             # Eq.3 L_swap
            loss = loss_dis + self.args.lambda_swap * loss_swap                                                 # Eq.4 Total objective

            self._reset_grad()
            loss.backward()
            optims.biased_F.step()
            optims.debiased_F.step()
            optims.biased_C.step()
            optims.debiased_C.step()

            # print out log info
            if (i+1) % args.print_every == 0:
                elapsed = time.time() - start_time
                elapsed = str(datetime.timedelta(seconds=elapsed))[:-7]
                log = "Elapsed time [%s], Iteration [%i/%i], LR [%.4f]" % (elapsed, i+1, args.total_iter,
                                                                           optims.biased_F.param_groups[-1]['lr'])

                all_losses = dict()
                for loss, key in zip([loss_dis_conflict.mean().item(), loss_dis_align.mean().item(),
                                      loss_swap_conflict.mean().item(), loss_swap_align.mean().item()],
                                     ['dis_conflict/', 'dis_align/',
                                      'swap_conflict/', 'swap_align/']):
                    all_losses[key] = loss
                log += ' '.join(['%s: [%f]' % (key, value) for key, value in all_losses.items()])
                print(log)
                print('Average Loss weight:', loss_weight.mean().item())
                logging.info(log)

            # save model checkpoints
            if (i+1) % args.save_every == 0:
                self._save_checkpoint(step=i+1, token='debias')

            if (i+1) % args.eval_every == 0:
                total_acc_b, total_acc_d, valid_attrwise_acc_b, valid_attrwise_acc_d = self.validation(fetcher_val)
                self.report_validation(valid_attrwise_acc_b, total_acc_b, i, which='bias')
                self.report_validation(valid_attrwise_acc_d, total_acc_d, i, which='debias')

            if not self.args.no_lr_scheduling:
                self.scheduler.biased_F.step()
                self.scheduler.debiased_F.step()
                self.scheduler.biased_C.step()
                self.scheduler.debiased_C.step()

    def evaluate(self):
        fetcher_val = self.loaders.val
        #self._load_checkpoint(self.args.total_iter, 'debias')
        self._load_checkpoint(15000, 'debias')

        total_acc_b, total_acc_d, valid_attrwise_acc_b, valid_attrwise_acc_d = self.validation(fetcher_val)
        self.report_validation(valid_attrwise_acc_b, total_acc_b, 0, which='bias_test', save_in_result=True)
        self.report_validation(valid_attrwise_acc_d, total_acc_d, 0, which='debias_test', save_in_result=True)
