# Copyright (c) 2019, Facebook, Inc. and its affiliates. All Rights Reserved
"""
Run BERT on several relation extraction benchmarks.
Adding some special tokens instead of doing span pair prediction in this version.
"""

import argparse
import logging
import os
import random
import time
import json

import numpy as np
import torch
import yaml
from torch.utils.data import DataLoader, TensorDataset
from collections import Counter
from tqdm import tqdm
from torch.nn import CrossEntropyLoss
import torch.nn.functional as F
from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE, WEIGHTS_NAME, CONFIG_NAME
from pytorch_pretrained_bert.modeling import BertForSequenceClassification
from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.optimization import BertAdam, warmup_linear
from link_prediction_models import ConvE
from scorer import score

CLS = "[CLS]"
SEP = "[SEP]"

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

class AttrDict(dict):
    def __init__(self, *args, **kwargs):
        super(AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self

def create_model_name(cfg_dict):
    top_level_name = 'SemEval'
    approach_type = 'SpanBERT-JRRELP' if cfg_dict['with_jrrelp'] is not None else 'SpanBERT'
    main_name = '{}-{}-{}-{}-{}-{}'.format(
        cfg_dict['feature_mode'], cfg_dict['learning_rate'], cfg_dict['warmup_proportion'],
        cfg_dict['seed'], cfg_dict['eval_metric'], cfg_dict['max_seq_length']
    )
    if cfg_dict.get('kglp', None) is not None and cfg_dict['with_jrrelp']:
        kglp_task = '{}-{}-{}-{}'.format(
            cfg_dict['jrrelp_lambda'],
            cfg_dict['without_observed'],
            cfg_dict['without_verification'],
            cfg_dict['exclude_no_relation']
        )
        lp_cfg = cfg_dict['kglp']
        kglp_name = '{}-{}-{}-{}-{}-{}-{}-{}-{}-{}'.format(
            lp_cfg['input_drop'], lp_cfg['hidden_drop'],
            lp_cfg['feat_drop'], lp_cfg['rel_emb_dim'],
            lp_cfg['use_bias'], lp_cfg['filter_channels'],
            lp_cfg['stride'],
            lp_cfg['ent_emb_shape1'],
            lp_cfg['rel_emb_shape1'],
            lp_cfg['kernel_size']
        )

        aggregate_name = os.path.join(top_level_name, approach_type, main_name, kglp_task, kglp_name)
    else:
        aggregate_name = os.path.join(top_level_name, approach_type, main_name)
    return aggregate_name

class InputExample(object):
    """A single training/test example for span pair classification."""

    def __init__(self, guid, sentence, span1, span2, ner1, ner2, label):
        self.guid = guid
        self.sentence = sentence
        self.span1 = span1
        self.span2 = span2
        self.ner1 = ner1
        self.ner2 = ner2
        self.label = label


# class InputFeatures(object):
#     """A single set of features of data."""
#
#     def __init__(self, input_ids, input_mask, segment_ids, label_id):
#         self.input_ids = input_ids
#         self.input_mask = input_mask
#         self.segment_ids = segment_ids
#         self.label_id = label_id

class InputFeatures(object):
    """A single set of features of data."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

class DataProcessor(object):
    """Processor for the TACRED data set."""

    @classmethod
    def _read_json(cls, input_file):
        with open(input_file, "r", encoding='utf-8') as reader:
            data = json.load(reader)
        return data

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_json(os.path.join(data_dir, "train_sampled.json")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_json(os.path.join(data_dir, "dev.json")), "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_json(os.path.join(data_dir, "test.json")), "test")

    def get_labels(self, data_dir, negative_label="no_relation"):
        """See base class."""
        dataset = self._read_json(os.path.join(data_dir, "train_sampled.json"))
        count = Counter()
        for example in dataset:
            count[example['relation']] += 1
        logger.info("%d labels" % len(count))
        # Make sure the negative label is alwyas 0
        labels = [negative_label]
        for label, count in count.most_common():
            logger.info("%s: %.2f%%" % (label, count * 100.0 / len(dataset)))
            if label not in labels:
                labels.append(label)
        return labels

    def _create_examples(self, dataset, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for example in dataset:
            sentence = [convert_token(token) for token in example['token']]
            assert example['subj_start'] >= 0 and example['subj_start'] <= example['subj_end'] \
                and example['subj_end'] < len(sentence)
            assert example['obj_start'] >= 0 and example['obj_start'] <= example['obj_end'] \
                and example['obj_end'] < len(sentence)
            examples.append(InputExample(guid=example['id'],
                             sentence=sentence,
                             span1=(example['subj_start'], example['subj_end']),
                             span2=(example['obj_start'], example['obj_end']),
                             ner1=example['subj_type'],
                             ner2=example['obj_type'],
                             label=example['relation']))
        return examples



def convert_examples_to_features(examples, label2id, max_seq_length, tokenizer, special_tokens, mode='text'):
    special_tokens = {
        'SUBJ=0': '[unused1]',
        'SUBJ=TITLE': '[unused2]',
        'SUBJ=CAUSE_OF_DEATH': '[unused3]',
        'SUBJ=CRIMINAL_CHARGE': '[unused4]',
        'SUBJ=ORGANIZATION': '[unused5]',
        'SUBJ=DATE': '[unused6]',
        'SUBJ=DURATION': '[unused7]',
        'SUBJ=PERSON': '[unused8]',
        'SUBJ=NUMBER': '[unused9]',
        'SUBJ=STATE_OR_PROVINCE': '[unused10]',
        'SUBJ=MONEY': '[unused11]',
        'SUBJ=IDEOLOGY': '[unused12]',
        'SUBJ=TIME': '[unused13]',
        'SUBJ=COUNTRY': '[unused14]',
        'SUBJ=RELIGION': '[unused15]',
        'SUBJ=LOCATION': '[unused16]',
        'SUBJ=ORDINAL': '[unused17]',
        'SUBJ=NATIONALITY': '[unused18]',

        'OBJ=O': '[unused19]',
        'OBJ=TITLE': '[unused20]',
        'OBJ=CAUSE_OF_DEATH': '[unused21]',
        'OBJ=CRIMINAL_CHARGE': '[unused22]',
        'OBJ=DATE': '[unused23]',
        'OBJ=SET': '[unused24]',
        'OBJ=DURATION': '[unused25]',
        'OBJ=MONEY': '[unused26]',
        'OBJ=RELIGION': '[unused27]',
        'OBJ=COUNTRY': '[unused28]',
        'OBJ=IDEOLOGY': '[unused29]',
        'OBJ=NATIONALITY': '[unused30]',
        'OBJ=LOCATION': '[unused31]',
        'OBJ=MISC': '[unused32]',
        'OBJ=TIME': '[unused33]',
        'ORG=ORDINAL': '[unused34]',

    }
    """Loads a data file into a list of `InputBatch`s."""

    object_indices = np.arange(19, 35).tolist()
    kg = {}
    object_offset = 19
    def get_special_token(w):
        if w not in special_tokens:
            special_tokens[w] = "[unused%d]" % (len(special_tokens) + 1)
        return special_tokens[w]

    num_tokens = 0
    num_fit_examples = 0
    num_shown_examples = 0
    features = []
    for (ex_index, example) in enumerate(examples):
        if ex_index % 10000 == 0:
            logger.info("Writing example %d of %d" % (ex_index, len(examples)))

        tokens = [CLS]
        SUBJECT_START = get_special_token("SUBJ_START")
        SUBJECT_END = get_special_token("SUBJ_END")
        OBJECT_START = get_special_token("OBJ_START")
        OBJECT_END = get_special_token("OBJ_END")
        SUBJECT_NER = get_special_token("SUBJ=%s" % example.ner1)
        OBJECT_NER = get_special_token("OBJ=%s" % example.ner2)
        subject_id, object_id = tokenizer.convert_tokens_to_ids([SUBJECT_NER, OBJECT_NER])
        relation_id = label2id[example.label]
        e1rel = (subject_id, relation_id)
        if e1rel not in kg:
            kg[e1rel] = set()
        # Subtract offset so that the JRRELP loss labels are indexed correctly
        zero_indexed_object_id = object_id - object_offset
        if zero_indexed_object_id > 16:
            print('Object Id: {} | Offset: {} | Zero Index: {}'.format(object_id, object_offset, zero_indexed_object_id))
            print('Object NER: {}'.format(OBJECT_NER))
            print(special_tokens)
            exit()
        kg[e1rel].add(zero_indexed_object_id)

        if mode.startswith("text"):
            for i, token in enumerate(example.sentence):
                if i == example.span1[0]:
                    tokens.append(SUBJECT_START)
                if i == example.span2[0]:
                    tokens.append(OBJECT_START)
                for sub_token in tokenizer.tokenize(token):
                    tokens.append(sub_token)
                if i == example.span1[1]:
                    tokens.append(SUBJECT_END)
                if i == example.span2[1]:
                    tokens.append(OBJECT_END)
            if mode == "text_ner":
                tokens = tokens + [SEP, SUBJECT_NER, SEP, OBJECT_NER, SEP]
            else:
                tokens.append(SEP)
        else:
            subj_tokens = []
            obj_tokens = []
            for i, token in enumerate(example.sentence):
                if i == example.span1[0]:
                    tokens.append(SUBJECT_NER)
                if i == example.span2[0]:
                    tokens.append(OBJECT_NER)
                if (i >= example.span1[0]) and (i <= example.span1[1]):
                    for sub_token in tokenizer.tokenize(token):
                        subj_tokens.append(sub_token)
                elif (i >= example.span2[0]) and (i <= example.span2[1]):
                    for sub_token in tokenizer.tokenize(token):
                        obj_tokens.append(sub_token)
                else:
                    for sub_token in tokenizer.tokenize(token):
                        tokens.append(sub_token)
            if mode == "ner_text":
                tokens.append(SEP)
                for sub_token in subj_tokens:
                    tokens.append(sub_token)
                tokens.append(SEP)
                for sub_token in obj_tokens:
                    tokens.append(sub_token)
            tokens.append(SEP)
        num_tokens += len(tokens)

        if len(tokens) > max_seq_length:
            tokens = tokens[:max_seq_length]
        else:
            num_fit_examples += 1

        segment_ids = [0] * len(tokens)
        input_ids = tokenizer.convert_tokens_to_ids(tokens)
        input_mask = [1] * len(input_ids)
        padding = [0] * (max_seq_length - len(input_ids))
        input_ids += padding
        input_mask += padding
        segment_ids += padding
        label_id = label2id[example.label]
        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        if num_shown_examples < 20:
            if (ex_index < 5) or (label_id > 0):
                num_shown_examples += 1
                logger.info("*** Example ***")
                logger.info("guid: %s" % (example.guid))
                logger.info("tokens: %s" % " ".join(
                        [str(x) for x in tokens]))
                logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
                logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
                logger.info("segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
                logger.info("label: %s (id = %d)" % (example.label, label_id))

        features.append(
                InputFeatures(input_ids=input_ids,
                              input_mask=input_mask,
                              segment_ids=segment_ids,
                              label_id=label_id,
                              subject_id=subject_id
                              ))
    # Add KG outputs to features
    for feature in tqdm(features):
        feature_subject = feature.subject_id
        feature_label = feature.label_id
        known_object_ids = list(kg[(feature_subject, feature_label)])
        if feature_label == label2id['Other']:
            known_objects = np.ones(len(object_indices), dtype=np.float32)
        else:
            known_objects = np.zeros(len(object_indices), dtype=np.float32)
            known_objects[known_object_ids] = 1.
        feature.known_objects = known_objects

    logger.info("Average #tokens: %.2f" % (num_tokens * 1.0 / len(examples)))
    logger.info("%d (%.2f %%) examples can fit max_seq_length = %d" % (num_fit_examples,
                num_fit_examples * 100.0 / len(examples), max_seq_length))
    return features


def convert_token(token):
    """ Convert PTB tokens to normal tokens """
    if (token.lower() == '-lrb-'):
            return '('
    elif (token.lower() == '-rrb-'):
        return ')'
    elif (token.lower() == '-lsb-'):
        return '['
    elif (token.lower() == '-rsb-'):
        return ']'
    elif (token.lower() == '-lcb-'):
        return '{'
    elif (token.lower() == '-rcb-'):
        return '}'
    return token


def simple_accuracy(preds, labels):
    return (preds == labels).mean()


def compute_f1(preds, labels):
    n_gold = n_pred = n_correct = 0
    for pred, label in zip(preds, labels):
        if pred != 0:
            n_pred += 1
        if label != 0:
            n_gold += 1
        if (pred != 0) and (label != 0) and (pred == label):
            n_correct += 1
    if n_correct == 0:
        return {'precision': 0.0, 'recall': 0.0, 'f1': 0.0}
    else:
        prec = n_correct * 1.0 / n_pred
        recall = n_correct * 1.0 / n_gold
        if prec + recall > 0:
            f1 = 2.0 * prec * recall / (prec + recall)
        else:
            f1 = 0.0
        return {'precision': prec, 'recall': recall, 'f1': f1}


def evaluate(model, device, eval_dataloader, eval_label_ids, num_labels, id2label, verbose=True):
    model.eval()
    eval_loss = 0
    nb_eval_steps = 0
    preds = []
    for input_ids, input_mask, segment_ids, label_ids in eval_dataloader:
        input_ids = input_ids.to(device)
        input_mask = input_mask.to(device)
        segment_ids = segment_ids.to(device)
        label_ids = label_ids.to(device)
        with torch.no_grad():
            logits, _ = model(input_ids, segment_ids, input_mask, labels=None)
        loss_fct = CrossEntropyLoss()
        tmp_eval_loss = loss_fct(logits.view(-1, num_labels), label_ids.view(-1))
        eval_loss += tmp_eval_loss.mean().item()
        nb_eval_steps += 1
        if len(preds) == 0:
            preds.append(logits.detach().cpu().numpy())
        else:
            preds[0] = np.append(
                preds[0], logits.detach().cpu().numpy(), axis=0)

    eval_loss = eval_loss / nb_eval_steps
    preds = np.argmax(preds[0], axis=1).reshape(-1)
    pred_labels = [id2label[pred_id] for pred_id in preds]
    eval_labels = [id2label[label_id] for label_id in eval_label_ids.numpy().reshape(-1)]
    result = score(eval_labels, pred_labels, verbose=verbose)
    # result = compute_f1(preds, eval_label_ids.numpy())
    result['accuracy'] = simple_accuracy(preds, eval_label_ids.numpy())
    result['eval_loss'] = eval_loss
    if verbose:
        logger.info("***** Eval results *****")
        for key in sorted(result.keys()):
            logger.info("  %s = %s", key, str(result[key]))
    return preds, result


def main(args):
    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    n_gpu = torch.cuda.device_count()

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

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
    if args.do_train:
        logger.addHandler(logging.FileHandler(os.path.join(args.output_dir, "train.log"), 'w'))
    else:
        logger.addHandler(logging.FileHandler(os.path.join(args.output_dir, "eval.log"), 'w'))
    logger.info(args)
    logger.info("device: {}, n_gpu: {}, 16-bits training: {}".format(
        device, n_gpu, args.fp16))
    object_indices = torch.tensor([3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19], dtype=torch.long).to(
        device)
    processor = DataProcessor()
    label_list = processor.get_labels(args.data_dir, args.negative_label)
    label2id = {label: i for i, label in enumerate(label_list)}
    id2label = {i: label for i, label in enumerate(label_list)}
    num_labels = len(label_list)
    tokenizer = BertTokenizer.from_pretrained(args.model, do_lower_case=args.do_lower_case)

    special_tokens = {}
    if args.do_eval:
        eval_examples = processor.get_dev_examples(args.data_dir)
        eval_features = convert_examples_to_features(
            eval_examples, label2id, args.max_seq_length, tokenizer, special_tokens, args.feature_mode)
        logger.info("***** Dev *****")
        logger.info("  Num examples = %d", len(eval_examples))
        logger.info("  Batch size = %d", args.eval_batch_size)
        all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_id for f in eval_features], dtype=torch.long)
        eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
        eval_dataloader = DataLoader(eval_data, batch_size=args.eval_batch_size)
        eval_label_ids = all_label_ids

    if args.do_train:
        train_examples = processor.get_train_examples(args.data_dir)
        train_features = convert_examples_to_features(
                train_examples, label2id, args.max_seq_length, tokenizer, special_tokens, args.feature_mode)

        if args.train_mode == 'sorted' or args.train_mode == 'random_sorted':
            train_features = sorted(train_features, key=lambda f: np.sum(f.input_mask))
        else:
            random.shuffle(train_features)
        # SpanBERT Items
        all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_id for f in train_features], dtype=torch.long)
        # JRRELP Items
        all_known_objects = torch.tensor([f.known_objects for f in train_features], dtype=torch.float32)
        all_known_subjects = torch.tensor([f.subject_id for f in train_features], dtype=torch.long)
        train_data = TensorDataset(
            # SpanBERT Items
            all_input_ids, all_input_mask, all_segment_ids, all_label_ids,
            # JRRELP Items
            all_known_objects, all_known_subjects
        )
        train_dataloader = DataLoader(train_data, batch_size=args.train_batch_size)
        train_batches = [batch for batch in train_dataloader]

        num_train_optimization_steps = \
            len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

        logger.info("***** Training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_optimization_steps)

        best_result = None
        eval_step = max(1, len(train_batches) // args.eval_per_epoch)
        lrs = [args.learning_rate] if args.learning_rate else \
            [1e-6, 2e-6, 3e-6, 5e-6, 1e-5, 2e-5, 3e-5, 5e-5]
        for lr in lrs:
            model = BertForSequenceClassification.from_pretrained(
                args.model, cache_dir=str(PYTORCH_PRETRAINED_BERT_CACHE), num_labels=num_labels)
            if args.fp16:
                model.half()
            model.to(device)
            if n_gpu > 1:
                model = torch.nn.DataParallel(model)

            param_optimizer = list(model.named_parameters())
            no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
            optimizer_grouped_parameters = [
                {'params': [p for n, p in param_optimizer
                            if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
                {'params': [p for n, p in param_optimizer
                            if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
            ]
            if args.with_jrrelp:
                # TODO: This is hardcoded. move it to config file
                # kglp_config = {
                #     'input_drop': 0.5,
                #     'hidden_drop': 0.75,
                #     'feat_drop': 0.75,
                #     'ent_emb_dim': 1024,
                #     'ent_emb_shape1': 8,
                #     'rel_emb_dim': 1024,
                #     'rel_emb_shape1': 8,
                #     'use_bias': True,
                #     'kernel_size': '(3, 3)',
                #     'filter_channels': 32,
                #     'stride': 1,
                #     'padding': 0,
                #     'num_objects': len(object_indices.cpu().numpy().tolist()),
                # }
                kglp_config = args['kglp']
                kglp_config['num_objects'] = len(object_indices.cpu().numpy().tolist())
                kglp_model = ConvE(kglp_config)
                if args.fp16:
                    kglp_model.half()
                kglp_model.to(device)
            if args.fp16:
                try:
                    from apex.optimizers import FP16_Optimizer
                    from apex.optimizers import FusedAdam
                except ImportError:
                    raise ImportError("Please install apex from https://www.github.com/nvidia/apex"
                                      "to use distributed and fp16 training.")

                optimizer = FusedAdam(optimizer_grouped_parameters,
                                      lr=lr,
                                      bias_correction=False,
                                      max_grad_norm=1.0)
                if args.loss_scale == 0:
                    optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
                else:
                    optimizer = FP16_Optimizer(optimizer, static_loss_scale=args.loss_scale)

            else:
                optimizer = BertAdam(optimizer_grouped_parameters,
                                     lr=lr,
                                     warmup=args.warmup_proportion,
                                     t_total=num_train_optimization_steps)

            start_time = time.time()
            global_step = 0
            tr_loss = 0
            nb_tr_examples = 0
            nb_tr_steps = 0
            for epoch in range(int(args.num_train_epochs)):
                model.train()
                logger.info("Start epoch #{} (lr = {})...".format(epoch, lr))
                if args.train_mode == 'random' or args.train_mode == 'random_sorted':
                    random.shuffle(train_batches)
                for step, batch in enumerate(train_batches):
                    batch = tuple(t.to(device) for t in batch)
                    input_ids, input_mask, segment_ids, label_ids, known_objects, subject_ids = batch
                    loss, pred_rels = model(input_ids, segment_ids, input_mask, label_ids)
                    if n_gpu > 1:
                        loss = loss.mean()

                    if args.with_jrrelp:
                        if args.fp16:
                            known_objects = known_objects.half()
                        # Get Input Embeddings
                        # Relation Embeddings come from SpanBERT output later
                        label_embs = F.embedding(label_ids, model.classifier.weight)
                        # Subject/Object Embeddings come from SpanBERT input layer
                        subject_embs = model.bert.embeddings.word_embeddings(subject_ids)
                        object_embs = model.bert.embeddings.word_embeddings(object_indices)
                        # Get KGLP model object logits
                        standard_logits = kglp_model(subject_embs, label_embs, object_embs)
                        cyclic_logits = kglp_model(subject_embs, pred_rels, object_embs)
                        # Compute JRRELP auxiliary loss terms
                        standard_loss = kglp_model.loss(standard_logits, known_objects)
                        standard_loss *= (1. - args['without_observed'])
                        cyclic_loss = kglp_model.loss(cyclic_logits, known_objects)
                        cyclic_loss *= (1 - args['without_verification'])
                        if args.exclude_no_relation:
                            no_relation_blacklist = torch.eq(label_ids, label2id['Other']).eq(0).type(torch.half).unsqueeze(-1)
                            standard_loss *= no_relation_blacklist
                            cyclic_loss *= no_relation_blacklist
                        standard_loss = standard_loss.mean()
                        cyclic_loss = cyclic_loss.mean()
                        # Aggregate loss with JRRELP weight
                        loss += args.jrrelp_lambda * (standard_loss + cyclic_loss)

                    if args.gradient_accumulation_steps > 1:
                        loss = loss / args.gradient_accumulation_steps

                    if args.fp16:
                        optimizer.backward(loss)
                    else:
                        loss.backward()

                    tr_loss += loss.item()
                    nb_tr_examples += input_ids.size(0)
                    nb_tr_steps += 1

                    if (step + 1) % args.gradient_accumulation_steps == 0:
                        if args.fp16:
                            lr_this_step = lr * \
                                warmup_linear(global_step/num_train_optimization_steps, args.warmup_proportion)
                            for param_group in optimizer.param_groups:
                                param_group['lr'] = lr_this_step
                        optimizer.step()
                        optimizer.zero_grad()
                        global_step += 1

                    if (step + 1) % eval_step == 0:
                        logger.info('Epoch: {}, Step: {} / {}, used_time = {:.2f}s, loss = {:.6f}'.format(
                                     epoch, step + 1, len(train_batches),
                                     time.time() - start_time, tr_loss / nb_tr_steps))
                        save_model = False
                        if args.do_eval:
                            preds, result = evaluate(model, device, eval_dataloader, eval_label_ids, num_labels, id2label)
                            model.train()
                            result['global_step'] = global_step
                            result['epoch'] = epoch
                            result['learning_rate'] = lr
                            result['batch_size'] = args.train_batch_size
                            logger.info("First 20 predictions:")
                            for pred, label in zip(preds[:20], eval_label_ids.numpy()[:20]):
                                sign = u'\u2713' if pred == label else u'\u2718'
                                logger.info("pred = %s, label = %s %s" % (id2label[pred], id2label[label], sign))
                            if (best_result is None) or (result[args.eval_metric] > best_result[args.eval_metric]):
                                best_result = result
                                save_model = True
                                logger.info("!!! Best dev %s (lr=%s, epoch=%d): %.2f" %
                                            (args.eval_metric, str(lr), epoch, result[args.eval_metric] * 100.0))
                        else:
                            save_model = True

                        if save_model:
                            model_to_save = model.module if hasattr(model, 'module') else model
                            output_model_file = os.path.join(args.output_dir, WEIGHTS_NAME)
                            output_config_file = os.path.join(args.output_dir, CONFIG_NAME)
                            torch.save(model_to_save.state_dict(), output_model_file)
                            model_to_save.config.to_json_file(output_config_file)
                            tokenizer.save_vocabulary(args.output_dir)
                            if best_result:
                                output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
                                print('Saving results to: {}'.format(output_eval_file))
                                with open(output_eval_file, "w") as writer:
                                    for key in sorted(result.keys()):
                                        writer.write("%s = %s\n" % (key, str(result[key])))

    if args.do_eval:
        if args.eval_test:
            eval_examples = processor.get_test_examples(args.data_dir)
            eval_features = convert_examples_to_features(
                eval_examples, label2id, args.max_seq_length, tokenizer, special_tokens, args.feature_mode)
            logger.info("***** Test *****")
            logger.info("  Num examples = %d", len(eval_examples))
            logger.info("  Batch size = %d", args.eval_batch_size)
            all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
            all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
            all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
            all_label_ids = torch.tensor([f.label_id for f in eval_features], dtype=torch.long)
            eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
            eval_dataloader = DataLoader(eval_data, batch_size=args.eval_batch_size)
            eval_label_ids = all_label_ids
        model = BertForSequenceClassification.from_pretrained(args.output_dir, num_labels=num_labels)
        if args.fp16:
            model.half()
        model.to(device)
        preds, result = evaluate(model, device, eval_dataloader, eval_label_ids, num_labels, id2label)
        with open(os.path.join(args.output_dir, "predictions.txt"), "w") as f:
            for ex, pred in zip(eval_examples, preds):
                f.write("%s\t%s\n" % (ex.guid, id2label[pred]))
        with open(os.path.join(args.output_dir, "test_results.txt"), "w") as f:
            for key in sorted(result.keys()):
                f.write("%s = %s\n" % (key, str(result[key])))


if __name__ == "__main__":
    def str2bool(v):
        return v.lower() in ('true')

    # parser = argparse.ArgumentParser()
    # parser.add_argument("--model", default=None, type=str, required=True)
    # parser.add_argument("--data_dir", default=None, type=str, required=True,
    #                     help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    # parser.add_argument("--output_dir", default=None, type=str, required=True,
    #                     help="The output directory where the model predictions and checkpoints will be written.")
    # parser.add_argument("--eval_per_epoch", default=10, type=int,
    #                     help="How many times it evaluates on dev set per epoch")
    # parser.add_argument("--max_seq_length", default=128, type=int,
    #                     help="The maximum total input sequence length after WordPiece tokenization. \n"
    #                          "Sequences longer than this will be truncated, and sequences shorter \n"
    #                          "than this will be padded.")
    # parser.add_argument("--negative_label", default="no_relation", type=str)
    # parser.add_argument("--do_train", action='store_true', help="Whether to run training.")
    # parser.add_argument("--train_mode", type=str, default='random_sorted', choices=['random', 'sorted', 'random_sorted'])
    # parser.add_argument("--do_eval", action='store_true', help="Whether to run eval on the dev set.")
    # parser.add_argument("--do_lower_case", action='store_true', help="Set this flag if you are using an uncased model.")
    # parser.add_argument("--eval_test", action="store_true", help="Whether to evaluate on final test set.")
    # parser.add_argument("--feature_mode", type=str, default="ner", choices=["text", "ner", "text_ner", "ner_text"])
    # parser.add_argument("--train_batch_size", default=32, type=int,
    #                     help="Total batch size for training.")
    # parser.add_argument("--eval_batch_size", default=8, type=int,
    #                     help="Total batch size for eval.")
    # parser.add_argument("--eval_metric", default="f1", type=str)
    # parser.add_argument("--learning_rate", default=None, type=float,
    #                     help="The initial learning rate for Adam.")
    # parser.add_argument("--num_train_epochs", default=3.0, type=float,
    #                     help="Total number of training epochs to perform.")
    # parser.add_argument("--warmup_proportion", default=0.1, type=float,
    #                     help="Proportion of training to perform linear learning rate warmup for. "
    #                          "E.g., 0.1 = 10%% of training.")
    # parser.add_argument("--no_cuda", action='store_true',
    #                     help="Whether not to use CUDA when available")
    # parser.add_argument('--seed', type=int, default=42,
    #                     help="random seed for initialization")
    # parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
    #                     help="Number of updates steps to accumulate before performing a backward/update pass.")
    # parser.add_argument('--fp16', action='store_true',
    #                     help="Whether to use 16-bit float precision instead of 32-bit")
    # parser.add_argument('--loss_scale', type=float, default=0,
    #                     help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
    #                          "0 (default value): dynamic loss scaling.\n"
    #                          "Positive power of 2: static loss scaling value.\n")
    # parser.add_argument('--with_jrrelp', type=str2bool, default=False,
    #                     help='Whether to include JRRELP')
    # parser.add_argument('--jrrelp_lambda', type=float, default=.1,
    #                     help='JRRELP lambda term for loss wieght')
    # args = parser.parse_args()

    # cfg_dict = vars(args)
    cwd = os.getcwd()
    # on_server = 'Desktop' not in cwd
    config_path = os.path.join(cwd, 'config', 'model.yaml')
    # config_path = os.path.join(cwd, 'configs', 'nell_config.yaml')
    with open(config_path, 'r') as file:
        cfg_dict = yaml.load(file)
    args = AttrDict(cfg_dict)
    if args.with_jrrelp:
        kglp_param_path = os.path.join(cwd, 'config', 'jrrelp.yaml')
        with open(kglp_param_path, 'r') as handle:
            kglp_config = yaml.load(handle)
            args['kglp'] = kglp_config
    args['output_dir'] = os.path.join('tacred_dir', create_model_name(args))

    main(args)
