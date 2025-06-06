########################################################################################################
# The RWKV v2-RNN Language Model - https://github.com/BlinkDL/RWKV-LM
########################################################################################################

from torch.utils.data.dataloader import DataLoader
from torch.optim.lr_scheduler import LambdaLR
from torch.nn import functional as F
import torch.nn as nn
import torch.optim as optim
import torch
from tqdm.auto import tqdm
import numpy as np
import logging
from src.spikingjelly.clock_driven import functional
import os
import datetime
import sys
import math
import pdb
from accelerate import Accelerator
from src.model import L2Wrap
from transformers import is_datasets_available
import datasets

# import wandb  # comment this if you don't have wandb
# print('logging to wandb... (comment it if you don\'t have wandb)')
accelerator = Accelerator()

logger = logging.getLogger(__name__)
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.allow_tf32 = True
torch.backends.cuda.matmul.allow_tf32 = True

log_file = open("wik8-0.01.txt", "a")


class TrainerConfig:
    max_epochs = 10
    batch_size = 64
    learning_rate = 4e-4
    betas = (0.9, 0.99)
    eps = 1e-8
    grad_norm_clip = 1.0
    lr_decay = True  # linear warmup followed by cosine decay
    warmup_tokens = 0
    final_tokens = 0
    epoch_save_frequency = 0
    epoch_save_path = 'trained-'
    num_workers = 0  # for DataLoader

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class Trainer:

    def __init__(self, model, train_dataset, valid_dataset, test_dataset, data_collator, config):
        self.model = model
        self.train_dataset = train_dataset
        self.test_dataset = test_dataset
        self.valid_dataset = valid_dataset
        self.data_collator = data_collator
        self.config = config
        self.avg_loss = -1
        self.min_dev_loss = 100
        self.dev_loss = -1
        self.steps = 0

        #         if 'wandb' in sys.modules:
        #             cfg = model.config
        #             for k in config.__dict__:
        #                 setattr(cfg, k, config.__dict__[k])  # combine cfg
        #             wandb.init(project="RWKV-LM", name=self.get_run_name() + '-' +
        #                        datetime.datetime.today().strftime('%Y-%m-%d-%H-%M-%S'), config=cfg, save_code=False)

        self.device = 'cpu'
        if torch.cuda.is_available():  # take over whatever gpus are on the system
            self.device = torch.cuda.current_device()

    def get_run_name(self):
        raw_model = self.model.module if hasattr(
            self.model, "module") else self.model
        cfg = raw_model.config
        run_name = str(cfg.vocab_size) + '-' + str(cfg.ctx_len) + '-' + \
                   cfg.model_type + '-' + str(cfg.n_layer) + '-' + str(cfg.n_embd)
        return run_name
    
    def _remove_unused_columns(self, dataset: "datasets.Dataset"):
        
        ignored_columns = ['text', 'attention_masks']
        
        return dataset.remove_columns(ignored_columns)
    
    def get_train_dataloader(self) -> DataLoader:
        """
        Returns the training [`~torch.utils.data.DataLoader`].

        Will use no sampler if `train_dataset` does not implement `__len__`, a random sampler (adapted to distributed
        training if necessary) otherwise.

        Subclass and override this method if you want to inject some custom behavior.
        """
        if self.train_dataset is None:
            raise ValueError("Trainer: training requires a train_dataset.")

        train_dataset = self.train_dataset
        data_collator = self.data_collator        
        train_dataset = self._remove_unused_columns(train_dataset)

        # train_sampler = self._get_train_sampler()

        return DataLoader(
            train_dataset,
            batch_size=self.config.batch_size,
            # sampler=train_sampler,
            collate_fn=data_collator,
            # drop_last=self.args.dataloader_drop_last,
            num_workers=self.config.num_workers
            # pin_memory=self.args.dataloader_pin_memory,            
            # worker_init_fn=seed_worker,
        )

    def train(self):
        model, config = self.model, self.config
        raw_model = model.module if hasattr(self.model, "module") else model
        optimizer = raw_model.configure_optimizers(config)
        optimizer = accelerator.prepare(optimizer)
        model = accelerator.prepare(model)

        def run_epoch(split):
            is_train = split == 'train'
            data = self.train_dataset if is_train else self.test_dataset
            if split == 'valid':
                data = self.valid_dataset
            # pdb.set_trace()
            model.train(is_train)
            train_dataloader = self.get_train_dataloader()

            # for step, inputs in enumerate(train_dataloader):
            #     inputs = {k: v.to(self.device) for k, v in inputs.items()}
            #     x, y = inputs['input_ids'], inputs['labels']
                
            loader = accelerator.prepare(train_dataloader)
            pbar = tqdm(enumerate(loader), total=len(
                loader), bar_format='{l_bar}{bar:10}{r_bar}{bar:-10b}',
                        disable=not accelerator.is_local_main_process) if is_train else enumerate(loader)

            model.train(is_train)
            dev_loss_all = 0

            for it, batch in pbar:
                x, y = batch['input_ids'], batch['labels']  # Adjust keys based on your dataset structure                
                # x = x.to(self.device)  # place data on the correct device
                # y = y.to(self.device)

                with torch.set_grad_enabled(is_train):
                    loss = model(x, y)  # forward the model
                    functional.reset_net(model)

                if is_train:  # backprop and update the parameters
                    model.zero_grad()
                    # loss.backward()
                    accelerator.backward(loss)

                    if config.grad_norm_clip > 0:
                        torch.nn.utils.clip_grad_norm_(
                            model.parameters(), config.grad_norm_clip)

                    optimizer.step()

                    if config.lr_decay:  # decay the learning rate based on our progress
                        # number of tokens processed this step (i.e. label is not -100)
                        self.tokens += (y >= 0).sum()
                        lr_final_factor = config.lr_final / config.learning_rate
                        if self.tokens < config.warmup_tokens:
                            # linear warmup
                            lr_mult = lr_final_factor + \
                                      (1 - lr_final_factor) * float(self.tokens) / \
                                      float(config.warmup_tokens)
                            progress = 0
                        else:
                            # cosine learning rate decay
                            progress = float(self.tokens - config.warmup_tokens) / float(
                                max(1, config.final_tokens - config.warmup_tokens))
                            lr_mult = (0.5 + lr_final_factor / 2) + (0.5 - lr_final_factor /
                                                                     2) * math.cos(
                                math.pi * progress)  # better 1.0 ~ 0.1
                        lr = config.learning_rate * lr_mult
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = lr
                    else:
                        lr = config.learning_rate

                    now_loss = loss.item()  # report progress
                    self.lr = lr

                    #                     if 'wandb' in sys.modules:
                    #                         wandb.log({"loss": now_loss},
                    #                                   step=self.steps * self.config.batch_size)
                    self.steps += 1

                    if self.avg_loss < 0:
                        self.avg_loss = now_loss
                    else:
                        factor = 1 / (it + 1)
                        self.avg_loss = self.avg_loss * \
                                        (1.0 - factor) + now_loss * factor
                    pbar.set_description(
                        f"mini-epoch {epoch + 1} prog {progress * 100.0:.2f}% iter {it}: ppl {math.exp(self.avg_loss):.2f} loss {self.avg_loss:.4f} lr {lr:e}")
                else:
                    dev_loss_all += loss.item()
            if not is_train:
                self.dev_loss = dev_loss_all / len(loader)

        self.tokens = 0  # counter used for learning rate decay
        for epoch in range(config.max_epochs):
            save_flag = False

            run_epoch('train')
            log_file.write(
                f'{epoch + 1} {self.avg_loss:.6f} {math.exp(self.avg_loss):.4f} {self.lr:.8f} {datetime.datetime.now()} \n')
            log_file.flush()
#             run_epoch('valid')
#             log_file.write(
#                 f'{epoch + 1} {self.dev_loss:.6f} {math.exp(self.dev_loss):.4f} {self.lr:.8f} {datetime.datetime.now()} \n')
#             log_file.flush()
            #             run_epoch('test')
            #             log_file.write(
            #                 f'{epoch+1} {self.dev_loss:.6f} {math.exp(self.dev_loss):.4f} {self.lr:.8f} {datetime.datetime.now()} \n')
            #             log_file.flush()

            #             if self.dev_loss < self.min_dev_loss:
            #                 self.min_dev_loss = self.dev_loss
            #                 save_flag = True

            if (self.config.epoch_save_frequency > 0 and epoch % self.config.epoch_save_frequency == 0) or (
                    epoch == config.max_epochs - 1):
                # DataParallel wrappers keep raw model object in .module
                accelerator.wait_for_everyone()
                unwrapped_model = accelerator.unwrap_model(model)
                raw_model = unwrapped_model.module if hasattr(
                    unwrapped_model, "module") else unwrapped_model
                torch.save(raw_model.state_dict(),
                           self.config.epoch_save_path + str(epoch + 1) + '.pth')

#             if epoch >=100 and save_flag:
#                 accelerator.wait_for_everyone()
#                 unwrapped_model = accelerator.unwrap_model(model)
#                 raw_model = unwrapped_model.module if hasattr(
#                     unwrapped_model, "module") else unwrapped_model
#                 torch.save(raw_model.state_dict(),
#                            self.config.epoch_save_path + + str(epoch+1) + 'best_dev' + '.pth')
