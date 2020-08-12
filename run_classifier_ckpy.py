# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT finetuning runner."""

from __future__ import absolute_import, division, print_function

import argparse
import logging
import os
import sys
import random
from tqdm import tqdm, trange

import numpy as np

import torch
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset, Dataset)
from torch.utils.data.distributed import DistributedSampler
from torch.nn import CrossEntropyLoss, MSELoss

# from tensorboardX import SummaryWriter

# from file_utils import WEIGHTS_NAME, CONFIG_NAME
from modeling_classification import BertForSequenceClassification, WEIGHTS_NAME, CONFIG_NAME
from tokenization import BertTokenizer
from optimization import BertAdam, warmup_linear

from run_classifier_dataset_utils import processors, output_modes, convert_examples_to_features, compute_metrics

if sys.version_info[0] == 2:
    import cPickle as pickle
else:
    import pickle


logger = logging.getLogger(__name__)

class InputDataset(Dataset):
    def __init__(self, input_ids, attn_masks, segment_ids, labels):
        self.input_ids = input_ids
        self.attn_masks = attn_masks
        self.segment_ids = segment_ids
        self.labels = labels

        self.pads = {
            "input_ids": 0,
            "attention_mask": 0,
            "token_type_ids": 0
        }

    def __len__(self):
        return len(self.input_ids)

    def __getitem__(self, item):
        return {
            "input_ids": self.input_ids[item],
            "attention_mask": self.attn_masks[item],
            "token_type_ids": self.segment_ids[item],
        }, {
            "labels": self.labels[item]
        }

    def collate(self, example):
        seq_insts = [e[0] for e in example]
        int_insts = [e[1] for e in example]
        max_length = max([len(x["input_ids"]) for x in seq_insts])
        
        inputs = {}
        labels = {}

        for key in seq_insts[0].keys():
            seq = [inst[key] + [self.pads[key]] * (max_length - len(inst[key])) for inst in seq_insts]
            inputs[key] = torch.tensor(seq, dtype=torch.long)
        for key in int_insts[0].keys():
            labels[key] = torch.tensor([inst[key] for inst in int_insts])

        return inputs, labels

def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--bert_model", default=None, type=str, required=True,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                        "bert-large-uncased, bert-base-cased, bert-large-cased, bert-base-multilingual-uncased, "
                        "bert-base-multilingual-cased, bert-base-chinese.")
    parser.add_argument("--task_name",
                        default=None,
                        type=str,
                        required=True,
                        help="The name of the task to train.")
    parser.add_argument("--output_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--vocab_file", 
                        default="", 
                        type=str)
    parser.add_argument("--cache_dir",
                        default="",
                        type=str,
                        help="Where do you want to store the pre-trained models downloaded from s3")
    parser.add_argument("--max_seq_length",
                        default=128,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--do_train",
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval",
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_lower_case",
                        action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=8,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=3.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--no_cuda",
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument('--overwrite_output_dir',
                        action='store_true',
                        help="Overwrite the content of the output directory")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument('--fp16',
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument(
        "--fp16_opt_level",
        type=str,
        default="O1",
        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
        "See details at https://nvidia.github.io/apex/amp.html",
    )
    parser.add_argument('--loss_scale',
                        type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")
    parser.add_argument("--ckpt", type=str, help="ckpt position")
    parser.add_argument('--server_ip', type=str, default='', help="Can be used for distant debugging.")
    parser.add_argument('--server_port', type=str, default='', help="Can be used for distant debugging.")
    args = parser.parse_args()

    if args.server_ip and args.server_port:
        # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd
        print("Waiting for debugger attach")
        ptvsd.enable_attach(address=(args.server_ip, args.server_port), redirect_output=True)
        ptvsd.wait_for_attach()

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
    args.device = device

    logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt = '%m/%d/%Y %H:%M:%S',
                        level = logging.INFO if args.local_rank in [-1, 0] else logging.WARN)

    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                            args.gradient_accumulation_steps))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_eval:
        raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    # if os.path.exists(args.output_dir) and os.listdir(args.output_dir) and args.do_train and not args.overwrite_output_dir:
        # raise ValueError("Output directory ({}) already exists and is not empty.".format(args.output_dir))
    if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
        os.makedirs(args.output_dir)

    task_name = args.task_name.lower()

    if task_name not in processors:
        raise ValueError("Task not found: %s" % (task_name))

    processor = processors[task_name]()
    output_mode = output_modes[task_name]

    label_list = processor.get_labels()
    num_labels = len(label_list)

    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab
    
    if args.vocab_file:
        tokenizer = BertTokenizer(args.vocab_file, args.do_lower_case)
    else:
        tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)
    
    model = BertForSequenceClassification.from_pretrained(args.bert_model, num_labels=num_labels)

    if args.ckpt:
        print("load from", args.ckpt)
        model_dict = model.state_dict()
        ckpt = torch.load(args.ckpt)
        pretrained_dict = ckpt['model']
        new_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict.keys()}
        model_dict.update(new_dict)
        print('Total : {}, update: {}'.format(len(pretrained_dict), len(new_dict)))
        model.load_state_dict(model_dict)
    
    if args.local_rank == 0:
        torch.distributed.barrier()

    # if args.fp16:
    #     model.half()
    model.to(device)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          device_ids=[args.local_rank],
                                                          output_device=args.local_rank,
                                                          find_unused_parameters=True)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    global_step = 0
    nb_tr_steps = 0
    tr_loss = 0

    if args.do_train:
        # if args.local_rank in [-1, 0]:
        #     tb_writer = SummaryWriter()

        # Prepare data loader
        train_examples = processor.get_train_examples(args.data_dir)
        print(len(train_examples))
        cached_train_features_file = os.path.join(args.data_dir, 'train_{0}_{1}_{2}'.format(
            list(filter(None, args.bert_model.split('/'))).pop(),
                        str(args.max_seq_length),
                        str(task_name)))
        try:
            with open(cached_train_features_file, "rb") as reader:
                train_features = pickle.load(reader)
        except:
            train_features = convert_examples_to_features(
                train_examples, label_list, args.max_seq_length, tokenizer, output_mode)
            if args.local_rank == -1 or torch.distributed.get_rank() == 0:
                logger.info("  Saving train features into cached file %s", cached_train_features_file)
                with open(cached_train_features_file, "wb") as writer:
                    pickle.dump(train_features, writer)

        # all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        # all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
        # all_segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)

        all_input_ids = [f.input_ids for f in train_features]
        all_input_mask = [f.input_mask for f in train_features]
        all_segment_ids = [f.segment_ids for f in train_features]

        if output_mode == "classification":
            all_label_ids = [f.label_id for f in train_features]
        elif output_mode == "regression":
            all_label_ids = [f.label_id for f in train_features]

        # train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
        train_data = InputDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)

        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size, collate_fn=train_data.collate)

        num_train_optimization_steps = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

        # Prepare optimizer

        param_optimizer = list(model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
            ]
        optimizer = BertAdam(optimizer_grouped_parameters,
                     lr=args.learning_rate,
                     warmup=args.warmup_proportion,
                     t_total=num_train_optimization_steps)
        if args.fp16:
            try:
                from apex import amp
            except ImportError:
                raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
            model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

        # if args.fp16:
        #     try:
        #         from apex.optimizers import FP16_Optimizer
        #         from apex.optimizers import FusedAdam
        #     except ImportError:
        #         raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        #     optimizer = FusedAdam(optimizer_grouped_parameters,
        #                           lr=args.learning_rate,
        #                           bias_correction=False,
        #                           max_grad_norm=1.0)
        #     if args.loss_scale == 0:
        #         optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
        #     else:
        #         optimizer = FP16_Optimizer(optimizer, static_loss_scale=args.loss_scale)
        #     # warmup_linear = WarmupLinearSchedule(warmup=args.warmup_proportion, t_total=num_train_optimization_steps)

        # else:
        #     optimizer = BertAdam(optimizer_grouped_parameters,
        #                          lr=args.learning_rate,
        #                          warmup=args.warmup_proportion,
        #                          t_total=num_train_optimization_steps)

        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_optimization_steps)

        model.train()
        for e in trange(int(args.num_train_epochs), desc="Epoch", disable=args.local_rank not in [-1, 0]):
            tr_loss = 0
            nb_tr_examples, nb_tr_steps = 0, 0
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0])):
                inputs, labels = batch
                for key in inputs.keys():
                    inputs[key] = inputs[key].to(args.device)
                for key in labels.keys():
                    labels[key] = labels[key].to(args.device)
                # define a new function to compute loss values for both output_modes
                label_ids = labels["labels"]
                logits = model(**inputs)

                if output_mode == "classification":
                    loss_fct = CrossEntropyLoss()
                    loss = loss_fct(logits.view(-1, num_labels), label_ids.view(-1))
                elif output_mode == "regression":
                    loss_fct = MSELoss()
                    loss = loss_fct(logits.view(-1), label_ids.view(-1))

                if n_gpu > 1:
                    loss = loss.mean() # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                # if args.fp16:
                #     optimizer.backward(loss)
                # else:
                #     loss.backward()

                if args.fp16:
                    with amp.scale_loss(loss, optimizer) as scaled_loss:
                        scaled_loss.backward()
                else:
                    loss.backward()

                tr_loss += loss.item()
                # nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    # if args.fp16:
                    #     # modify learning rate with special warm up BERT uses
                    #     # if args.fp16 is False, BertAdam is used that handles this automatically
                    #     lr_this_step = args.learning_rate * warmup_linear(global_step / num_train_optimization_steps, args.warmup_proportion)
                    #     # lr_this_step = args.learning_rate * warmup_linear.get_lr(global_step, args.warmup_proportion)
                    #     for param_group in optimizer.param_groups:
                    #         param_group['lr'] = lr_this_step
                    optimizer.step()
                    optimizer.zero_grad()
                    global_step += 1
                    # if args.local_rank in [-1, 0]:
                    #     tb_writer.add_scalar('lr', optimizer.get_lr()[0], global_step)
                    #     tb_writer.add_scalar('loss', loss.item(), global_step)
            # save each epoch
            model_to_save = model.module if hasattr(model, 'module') else model
            output_model_file = os.path.join(args.output_dir, WEIGHTS_NAME+str(e))
            torch.save(model_to_save.state_dict(), output_model_file)
    ### Saving best-practices: if you use defaults names for the model, you can reload it using from_pretrained()
    ### Example:
    if args.do_train and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        # Save a trained model, configuration and tokenizer
        model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self

        # If we save using the predefined names, we can load using `from_pretrained`
        output_model_file = os.path.join(args.output_dir, WEIGHTS_NAME)
        output_config_file = os.path.join(args.output_dir, CONFIG_NAME)

        torch.save(model_to_save.state_dict(), output_model_file)
        with open(output_config_file, 'w') as f:
            f.write(model_to_save.config.to_json_string())

        # model_to_save.config.to_json_string(output_config_file)
        # tokenizer.save_vocabulary(args.output_dir)

        # Load a trained model and vocabulary that you have fine-tuned
        # model = BertForSequenceClassification.from_pretrained(args.output_dir, num_labels=num_labels)
        # tokenizer = BertTokenizer.from_pretrained(args.output_dir, do_lower_case=args.do_lower_case)

        # Good practice: save your training arguments together with the trained model
        output_args_file = os.path.join(args.output_dir, 'training_args.bin')
        torch.save(args, output_args_file)
    else:
        model = BertForSequenceClassification.from_pretrained(args.bert_model, num_labels=num_labels)

    ### Evaluation
    if args.do_eval and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        best_acc = 0
        best_epoch = 0
        for e in range(int(args.num_train_epochs)):
            weight_path = os.path.join(args.output_dir, WEIGHTS_NAME+str(e))
            result = evaluate(args, model, weight_path, processor, device, task_name, "dev", label_list, tokenizer, output_mode, num_labels)
            if result["acc"] > best_acc:
                best_acc = result["acc"]
                best_epoch = e

            output_eval_file = os.path.join(args.output_dir, "eval_results_{}.txt".format(e))

            with open(output_eval_file, "w") as writer:
                logger.info("***** Dev Eval results *****")
                for key in sorted(result.keys()):
                    logger.info("  %s = %s", key, str(result[key]))
                    writer.write("%s = %s\n" % (key, str(result[key])))

        with open(os.path.join(args.output_dir, "all_results.txt"), "w") as writer:
            writer.write("best acc: {}\nepoch: {}".format(best_acc, best_epoch))

        test_weight_path = os.path.join(args.output_dir, WEIGHTS_NAME+str(best_epoch))
        test_result = evaluate(args, model, test_weight_path, processor, device, task_name, "test", label_list, tokenizer, output_mode, num_labels)
        test_output_eval_file = os.path.join(args.output_dir, "test_eval_results.txt")

        with open(test_output_eval_file, "w") as writer:
            logger.info("***** Test Eval results *****")
            for key in sorted(test_result.keys()):
                logger.info("  %s = %s", key, str(test_result[key]))
                writer.write("%s = %s\n" % (key, str(test_result[key])))


def evaluate(args, model, weight_path, processor, device, task_name, mode, label_list, tokenizer, output_mode, num_labels, ):
    model.load_state_dict(torch.load(weight_path))
    model.to(device)
    print("Loading From: ", weight_path)
    if mode == "test":
        eval_examples = processor.get_test_examples(args.data_dir)
        cached_eval_features_file = os.path.join(args.data_dir, 'test_{0}_{1}_{2}'.format(
            list(filter(None, args.bert_model.split('/'))).pop(),
            str(args.max_seq_length),
            str(task_name)))
    else:
        eval_examples = processor.get_dev_examples(args.data_dir)
        cached_eval_features_file = os.path.join(args.data_dir, 'dev_{0}_{1}_{2}'.format(
            list(filter(None, args.bert_model.split('/'))).pop(),
            str(args.max_seq_length),
            str(task_name)))

    try:
        with open(cached_eval_features_file, "rb") as reader:
            eval_features = pickle.load(reader)
    except:
        eval_features = convert_examples_to_features(
            eval_examples, label_list, args.max_seq_length, tokenizer, output_mode)
        if args.local_rank == -1 or torch.distributed.get_rank() == 0:
            logger.info(
                "  Saving eval features into cached file %s", cached_eval_features_file)
            with open(cached_eval_features_file, "wb") as writer:
                pickle.dump(eval_features, writer)

    logger.info("***** Running evaluation *****")
    logger.info("  Num examples = %d", len(eval_examples))
    logger.info("  Batch size = %d", args.eval_batch_size)
    all_input_ids = [f.input_ids for f in eval_features]
    all_input_mask = [f.input_mask for f in eval_features]
    all_segment_ids = [f.segment_ids for f in eval_features]

    if output_mode == "classification":
        all_label_ids = [f.label_id for f in eval_features]
    elif output_mode == "regression":
        all_label_ids = [f.label_id for f in eval_features]

    eval_data = InputDataset(
        all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
    # Run prediction for full data
    if args.local_rank == -1:
        eval_sampler = SequentialSampler(eval_data)
    else:
        # Note that this sampler samples randomly
        eval_sampler = DistributedSampler(eval_data)
    eval_dataloader = DataLoader(
        eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size, collate_fn=eval_data.collate)

    model.eval()
    eval_loss = 0
    nb_eval_steps = 0
    preds = []
    out_label_ids = None

    for batch in tqdm(eval_dataloader, desc="Evaluating"):
        inputs, labels = batch
        for key in inputs.keys():
            inputs[key] = inputs[key].to(args.device)
        for key in labels.keys():
            labels[key] = labels[key].to(args.device)
        # define a new function to compute loss values for both output_modes
        label_ids = labels["labels"]

        with torch.no_grad():
            logits = model(**inputs)

        # create eval loss and other metric required by the task
        if output_mode == "classification":
            loss_fct = CrossEntropyLoss()
            tmp_eval_loss = loss_fct(
                logits.view(-1, num_labels), label_ids.view(-1))
        elif output_mode == "regression":
            loss_fct = MSELoss()
            tmp_eval_loss = loss_fct(
                logits.view(-1), label_ids.view(-1))

        eval_loss += tmp_eval_loss.mean().item()
        nb_eval_steps += 1
        if len(preds) == 0:
            preds.append(logits.detach().cpu().numpy())
            out_label_ids = label_ids.detach().cpu().numpy()
        else:
            preds[0] = np.append(
                preds[0], logits.detach().cpu().numpy(), axis=0)
            out_label_ids = np.append(
                out_label_ids, label_ids.detach().cpu().numpy(), axis=0)

    eval_loss = eval_loss / nb_eval_steps
    preds = preds[0]
    # print(preds[0])
    if output_mode == "classification":
        preds = np.argmax(preds, axis=1)
    elif output_mode == "regression":
        preds = np.squeeze(preds)
    result = compute_metrics(task_name, preds, out_label_ids)

    return result


if __name__ == "__main__":
    main()
