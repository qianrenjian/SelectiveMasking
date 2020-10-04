import logging 
import torch
import torch.nn as nn
import numpy as np
import spacy
import collections
import multiprocessing
from spacy.lang.en import English
from tqdm import tqdm
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler, TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from torch.nn.functional import softmax

from modeling_classification import BertForSequenceClassification, BertForTokenClassification
from tokenization import BertTokenizer

logger = logging.getLogger(__name__)
MaskedTokenInstance = collections.namedtuple("MaskedTokenInstance", ["tokens", "info"])
MaskedItemInfo = collections.namedtuple("MaskedItemInfo", ["current_pos", "sen_doc_pos", "sen_right_id", "doc_ground_truth"])
nlp = English()
sentencizer = nlp.create_pipe("sentencizer")
nlp.add_pipe(sentencizer)

class InputFeatures(object):
    def __init__(self, input_ids, input_mask, segment_ids=None):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids

class SC(nn.Module):
    def __init__(self, mask_rate, top_sen_rate, threshold, bert_model, do_lower_case, max_seq_length, label_list, sen_batch_size, use_gpu=True):
        super(SC, self).__init__()
        self.mask_rate = mask_rate # bert 里面的mask_rate，现在没有用
        self.top_sen_rate = top_sen_rate # 每段话按句子评分排序后取前百分之多少
        self.threshold = threshold # 选词的那个threshold
        self.label_list = label_list # 所有的label : ["1", "2", "3", "4", "5"]
        self.num_labels = len(self.label_list)
        self.max_seq_length = max_seq_length # bert里面的max_seq_length
        self.tokenizer = BertTokenizer.from_pretrained(bert_model, do_lower_case=do_lower_case)
        print(self.num_labels)
        self.model = BertForSequenceClassification.from_pretrained(bert_model, num_labels=self.num_labels)
        self.device = torch.device("cuda" if torch.cuda.is_available() and use_gpu else "cpu")
        print(self.device)
        self.model.to(self.device)
        self.n_gpu = torch.cuda.device_count()
        self.sen_batch_size = sen_batch_size # 给sentence 分类的时候的batch_size
        self.vocab = list(self.tokenizer.vocab.keys())
        if self.n_gpu > 1:
            self.model = torch.nn.DataParallel(self.model)

    def convert_examples_to_features(self, data):
        # 原来create pretraining data 里面的convert_examples_to_features函数
        features = []
        for (ex_index, tokens_a) in enumerate(data):
            if ex_index % 10000 == 0:
                logger.info("Writing example %d of %d" % (ex_index, len(data)))
            if len(tokens_a) > self.max_seq_length - 2:
                tokens_a = tokens_a[:(self.max_seq_length - 2)]
            tokens = ["[CLS]"] + tokens_a + ["[SEP]"]
            
            segment_ids = [0] * len(tokens)
            input_ids = self.tokenizer.convert_tokens_to_ids(tokens)
            input_mask = [1] * len(input_ids)
            
            padding = [0] * (self.max_seq_length - len(input_ids))
            input_ids += padding
            input_mask += padding
            segment_ids += padding

            assert len(input_ids) == self.max_seq_length
            assert len(input_mask) == self.max_seq_length
            assert len(segment_ids) == self.max_seq_length

            features.append(InputFeatures(input_ids=input_ids, input_mask=input_mask, segment_ids=segment_ids))

        return features

    def evaluate(self, data, batch_size):
        # 给一堆句子评分，返回所有句子的分类结果和所有句子五个类的评分
        eval_features = self.convert_examples_to_features(data)
        all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)

        eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids)
        eval_sampler = SequentialSampler(eval_data)
        eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=batch_size)

        self.model.eval()
        preds = []
        # for input_ids, input_mask, segment_ids, in eval_dataloader:
        for input_ids, input_mask, segment_ids, in tqdm(eval_dataloader, desc="Evaluating"):
            input_ids = input_ids.to(self.device)
            input_mask = input_mask.to(self.device)
            segment_ids = segment_ids.to(self.device)
            with torch.no_grad():
                logits = self.model(input_ids, token_type_ids=segment_ids, attention_mask=input_mask)
                logits = softmax(logits, dim=1)
            if len(preds) == 0:
                preds.append(logits.detach().cpu().numpy())
            else:
                preds[0] = np.append(preds[0], logits.detach().cpu().numpy(), axis=0)

        preds_arg = np.argmax(preds[0], axis=1)
        return preds_arg, preds[0]

    def create_mask(self, mask_poses, sen, rng):
        # 根据需要mask的位置生成mask
        masked_info = [{} for token in sen]
        for pos in mask_poses:
            lexeme = nlp.vocab[sen[pos]]
            if lexeme.is_stop:
                # print(sen[pos])
                # 去除停用词
                continue
            if rng.random() < 0.8:
                mask_token = "[MASK]"
            else:
                if rng.random() < 0.5:
                    mask_token = sen[pos]
                else:
                    mask_token = self.vocab[rng.randint(0, len(self.vocab) - 1)]
            masked_info[pos]["mask"] = mask_token
            masked_info[pos]["label"] = sen[pos]
        return masked_info

    def create_reverse_mask(self, mask_poses, sen, rng):
        reverse_mask_poses = [i for i in range(len(sen)) if i not in mask_poses]
        rng.shuffle(reverse_mask_poses)
        cand_indexes = reverse_mask_poses[0:max(1, int(self.mask_rate * len(sen)))]
        masked_info = [{} for token in sen]
        for cand_index in cand_indexes:
            if rng.random() < 0.8:
                mask_token = "[MASK]"
            else:
                if rng.random() < 0.5:
                    mask_token = sen[cand_index]
                else:
                    mask_token = self.vocab[rng.randint(0, len(self.vocab) - 1)]
            masked_info[cand_index]["mask"] = mask_token
            masked_info[cand_index]["label"] = sen[cand_index]
        return masked_info

    def forward(self, data, all_labels, dupe_factor, rng):
        # 输入没有tokenized 的段，和每段对应的分类结果
        # data: not tokenized
        # convert label to ids
        doc_num = len(data)
        label_map = {label : i for i, label in enumerate(self.label_list)}
        all_label_ids = [label_map[label] for label in all_labels]
        
        # convert data, segment data to sentences
        # tokenized_data = []
        sentences = []
        sen_doc_ids = [] # [0, 0, ..., 0, 1, 1, ..., 1, ...] 每个句子对应原来段的id
        for (doc_id, doc) in enumerate(data):
            # tokenized_data.append(self.tokenizer.tokenize(doc))
            doc = nlp(doc)
            tL = [self.tokenizer.tokenize(sen.text) for sen in doc.sents]
            sentences.extend(tL)
            sen_doc_ids.extend([doc_id] * len(tL))

        logger.info("Begin eval for all sentence")
        sens_preds, sens_pred_scores = self.evaluate(sentences, self.sen_batch_size)

        # 所有分类正确的句子的信息
        right_sens = [] # 分类正确的句子
        right_preds = [] # 分类正确句子对应的分类结果
        right_scores = [] # 分类正确的句子的分数
        right_sen_doc_ids = [] # 分类正确的句子属于的那个段的id
        right_sen_doc_poses = [] # 分类正确的句子在sentences中的位置
        i = 0
        for doc_id in range(doc_num):
            ds = []
            while i < len(sen_doc_ids) and sen_doc_ids[i] == doc_id:
                sen_pred = sens_preds[i]
                doc_ground_truth = all_label_ids[doc_id]
                # compare with ground truth
                if doc_ground_truth == sen_pred:
                    # 每个tuple: (sentence, doc_id, sen_doc_pos, pred, score of ground truth)
                    ds.append((sentences[i], doc_id, i, sen_pred, sens_pred_scores[i][doc_ground_truth]))
                i += 1
            if len(ds) == 0:
                continue
            ds = sorted(ds, key=lambda x : x[-1], reverse=True)
            t_sen, t_sen_doc_id, t_sen_doc_pos, t_pred, t_score = zip(*ds[0:max(int(self.top_sen_rate * len(ds)), 1)])  # select top sentences
            right_sens.extend(t_sen)
            right_preds.extend(t_pred)
            right_scores.extend(t_score)
            right_sen_doc_ids.extend(t_sen_doc_id)
            right_sen_doc_poses.extend(t_sen_doc_pos)
        
        right_sens_num = len(right_sens)
        # convert right sentence to reverse

        masked_sens = [] # 所有mask的句子
        masked_item_infos = [] # 每个句子的一些其他信息
        
        # init
        for sen_right_id, (sen_doc_pos, sen) in enumerate(zip(right_sen_doc_poses, right_sens)):
            masked_sens.append(sen[0:1]) # 一开始每个句子长度是1
            # 每个info结构：sen_doc_pos: 原来的句子在sentences里面的位置，sen_right_id: 原来的句子在right_sens里面的位置，doc_ground_truth: 分类结果的ground truth
            masked_item_infos.append({"sen_doc_pos": sen_doc_pos, "sen_right_id": sen_right_id, "doc_ground_truth": all_label_ids[sen_doc_ids[sen_doc_pos]]})

        mask_poses_d = {} # 所有mask位置，key: 句子在sentences中的位置，value: 句子选出来的mask位置
        mask_pos = 0 # 正在测试mask的词的位置，每一轮之后加1
        # 每一轮循环过后，masked_sens里面的句子长度会加一（上一个词没有被选中，并加入了下一个词），或者不变（上一个词被选中了，并加入了下一个词），句子个数逐渐变少
        while len(masked_sens) != 0:
            _, mask_sens_scores = self.evaluate(masked_sens, self.sen_batch_size)
            masked_sens_num = len(masked_sens)
            temp_masked_sens = []
            temp_masked_item_infos = []
            for masked_sen, masked_item_info, mask_sens_score in zip(masked_sens, masked_item_infos, mask_sens_scores):
                sen_doc_pos = masked_item_info["sen_doc_pos"]
                doc_ground_truth = masked_item_info["doc_ground_truth"]
                sen_right_id = masked_item_info["sen_right_id"]
                origin_score = right_scores[sen_right_id] # 原句的分
                if origin_score - mask_sens_score[doc_ground_truth] < self.threshold:
                    # choose as mask
                    if sen_doc_pos in mask_poses_d:
                        mask_poses_d[sen_doc_pos].append(mask_pos)
                    else:
                        mask_poses_d[sen_doc_pos] = [mask_pos]
                    masked_sen.pop()
                
                # 加入下一个词
                if mask_pos + 1 < len(right_sens[sen_right_id]):
                    masked_sen.append(right_sens[sen_right_id][mask_pos + 1])
                    temp_masked_sens.append(masked_sen)
                    temp_masked_item_infos.append(masked_item_info)
                
                masked_sens = temp_masked_sens
                masked_item_infos = temp_masked_item_infos
            mask_pos += 1       

        # print(mask_poses_d)
        # for key, value in mask_poses_d.items():
            # print([sentences[key][pos] for pos in mask_poses_d[key]])
        
        all_documents = []

        # 生成带有mask信息的document
        # all_document = [[m_info0（每个句子的mask信息）, m_info1,... ]（第一段）, [...]（第二段）, ...]
        for _ in range(dupe_factor):
            i = 0
            for doc_id in tqdm(range(doc_num), desc="Generating All Documents"):
                all_documents.append([])
                while i < len(sen_doc_ids) and doc_id == sen_doc_ids[i]:
                    mask_poses = []
                    if i in mask_poses_d:
                        mask_poses = mask_poses_d[i]
                    m_info = self.create_mask(mask_poses, sentences[i], rng)
                    all_documents[-1].append(MaskedTokenInstance(tokens=sentences[i], info=m_info))
                    i += 1
                # print(all_documents[-1])
        return all_documents

class ASC(nn.Module):
    def __init__(self, mask_rate, top_sen_rate, threshold, bert_model, do_lower_case, max_seq_length, label_list, sen_batch_size, use_gpu=True):
        super(ASC, self).__init__()
        self.mask_rate = mask_rate 
        self.top_sen_rate = top_sen_rate 
        self.threshold = threshold
        self.label_list = label_list
        self.num_labels = len(self.label_list)
        self.max_seq_length = max_seq_length
        self.tokenizer = BertTokenizer.from_pretrained(bert_model, do_lower_case=do_lower_case)
        self.model = BertForSequenceClassification.from_pretrained(bert_model, num_labels=self.num_labels)
        self.device = torch.device("cuda" if torch.cuda.is_available() and use_gpu else "cpu")
        print(self.device)
        self.model.to(self.device)
        self.n_gpu = torch.cuda.device_count()
        self.sen_batch_size = sen_batch_size
        self.vocab = list(self.tokenizer.vocab.keys())
        if self.n_gpu > 1:
            self.model = torch.nn.DataParallel(self.model)
    
    def evaluate(self, data, batch_size):
        eval_features = self.convert_examples_to_features(data)
        all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)

        eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids)
        eval_sampler = SequentialSampler(eval_data)
        eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=batch_size)

        self.model.eval()
        preds = []
        for input_ids, input_mask, segment_ids, in tqdm(eval_dataloader, desc="Evaluating"):
            input_ids = input_ids.to(self.device)
            input_mask = input_mask.to(self.device)
            segment_ids = segment_ids.to(self.device)
            with torch.no_grad():
                logits = self.model(input_ids, token_type_ids=segment_ids, attention_mask=input_mask)
                logits = softmax(logits, dim=1)
            if len(preds) == 0:
                preds.append(logits.detach().cpu().numpy())
            else:
                preds[0] = np.append(preds[0], logits.detach().cpu().numpy(), axis=0)

        preds_arg = np.argmax(preds[0], axis=1)
        return preds_arg, preds[0]

    def create_mask(self, mask_poses, sen, rng):
        masked_info = [{} for token in sen]
        for pos in mask_poses:
            lexeme = nlp.vocab[sen[pos]]
            if lexeme.is_stop:
                # print("stop words: ", sen[pos])
                continue
            if rng.random() < 0.8:
                mask_token = "[MASK]"
            else:
                if rng.random() < 0.5:
                    mask_token = sen[pos]
                else:
                    mask_token = self.vocab[rng.randint(
                        0, len(self.vocab) - 1)]
            masked_info[pos]["mask"] = mask_token
            masked_info[pos]["label"] = sen[pos]
        return masked_info

    def convert_examples_to_features(self, data):
        features = []
        for (ex_index, item) in enumerate(data):
            tokens_b = item["text"]
            if len(tokens_b) > self.max_seq_length - 2:
                tokens_b = tokens_b[:(self.max_seq_length - 2)]
            tokens_a = item["aspect"]
            tokens = ["[CLS]"] + tokens_a + ["[SEP]"] + tokens_b + ["[SEP]"]
        
            segment_ids = [0] * (len(tokens_a) + 2) + [1] * (len(tokens_b) + 1)
            input_ids = self.tokenizer.convert_tokens_to_ids(tokens)
            input_mask = [1] * len(input_ids)
        
            padding = [0] * (self.max_seq_length - len(input_ids))
            input_ids += padding
            input_mask += padding
            segment_ids += padding
            assert len(input_ids) == self.max_seq_length
            assert len(input_mask) == self.max_seq_length
            assert len(segment_ids) == self.max_seq_length
            features.append(InputFeatures(input_ids=input_ids, input_mask=input_mask, segment_ids=segment_ids))

        return features

    def forward(self, data, all_labels, dupe_factor, rng):
        # data[i]: {"text": ... , "facts": ["aspect1": label1, "aspect2": label2, ...]}
        doc_num = len(data)
        label_map = {label : i for i, label in enumerate(self.label_list)}
        
        sen_doc_ids = []
        sentences = []
        texts = []
        for (doc_id, doc) in enumerate(data):
            text = self.tokenizer.tokenize(doc["text"])
            texts.append(text)
            for fact in doc["facts"]:
                prefix = fact["category"] if "category" in fact else fact["term"]
                if fact["polarity"] != "conflict":
                    sentences.append({"text": text, "aspect": self.tokenizer.tokenize(prefix), "label": label_map[fact["polarity"]]})
                    sen_doc_ids.append(doc_id)

        logger.info("Begin eval for all sentence")
        sens_preds, sens_pred_scores = self.evaluate(sentences, self.sen_batch_size)

        right_sens = [] # 分类正确的句子
        right_scores = [] # 分类正确的句子的分数
        right_sen_doc_ids = [] # 分类正确的句子属于的那个段的id
        i = 0
        for sen_id in range(len(sentences)):
            if sens_preds[sen_id] == sentences[sen_id]["label"]:
                right_sens.append(sentences[sen_id])
                right_sen_doc_ids.append(sen_doc_ids[sen_id])
                right_scores.append(sens_pred_scores[sen_id][sentences[sen_id]["label"]])

        masked_sens = [] # 所有mask的句子
        masked_item_infos = [] # 每个句子的一些其他信息
        
        # init
        for sen_right_id, sen in enumerate(right_sens):
            masked_sens.append({"text": sen["text"][0:1], "aspect": sen["aspect"], "label": sen["label"], "sen_right_id": sen_right_id}) # 一开始每个句子长度是1

        mask_poses_L = [set() for i in range(doc_num)] # 所有mask位置
        mask_pos = 0 # 正在测试mask的词的位置，每一轮之后加1
        while len(masked_sens) != 0:
            _, mask_sens_scores = self.evaluate(masked_sens, self.sen_batch_size)
            masked_sens_num = len(masked_sens)
            temp_masked_sens = []
            temp_masked_item_infos = []
            for masked_sen, mask_sens_score in zip(masked_sens, mask_sens_scores):
                doc_ground_truth = masked_sen["label"]
                sen_right_id = masked_sen["sen_right_id"]
                origin_score = right_scores[sen_right_id] # 原句的分
                right_sen_doc_id = right_sen_doc_ids[sen_right_id]
                # print(right_sens[sen_right_id]["text"], masked_sen["text"], " origin: ", origin_score, "mask: ", mask_sens_score[doc_ground_truth])
                if origin_score - mask_sens_score[doc_ground_truth] < self.threshold:
                    mask_poses_L[right_sen_doc_id].add(mask_pos)
                    masked_sen["text"].pop()
                
                if mask_pos + 1 < len(right_sens[sen_right_id]["text"]):
                    masked_sen["text"].append(right_sens[sen_right_id]["text"][mask_pos + 1])
                    temp_masked_sens.append(masked_sen)
                
                masked_sens = temp_masked_sens
            mask_pos += 1       

        
        all_documents = []
        for doc_id in range(doc_num):
            mask_poses = mask_poses_L[doc_id]
            print([texts[doc_id][pos] for pos in mask_poses])

        for _ in range(dupe_factor):
            for doc_id in tqdm(range(doc_num), desc="Generating All Documents"):
                mask_poses = mask_poses_L[doc_id]
                m_info = self.create_mask(mask_poses, texts[doc_id], rng)
                all_documents.append([MaskedTokenInstance(tokens=texts[doc_id], info=m_info)])

        return all_documents

class ModelGen(nn.Module):
    def __init__(self, mask_rate, bert_model, do_lower_case, max_seq_length, sen_batch_size, with_rand=False, use_gpu=True):
        super(ModelGen, self).__init__()
        self.mask_rate = mask_rate
        self.max_seq_length = max_seq_length # bert里面的max_seq_length
        self.tokenizer = BertTokenizer.from_pretrained(bert_model, do_lower_case=do_lower_case)
        self.model = BertForTokenClassification.from_pretrained(bert_model, num_labels=2)
        self.device = torch.device("cuda" if torch.cuda.is_available() and use_gpu else "cpu")
        print(self.device)
        self.model.to(self.device)
        self.n_gpu = torch.cuda.device_count()
        self.sen_batch_size = sen_batch_size # 给sentence 分类的时候的batch_size
        print(sen_batch_size)
        self.vocab = list(self.tokenizer.vocab.keys())
        self.with_rand = with_rand
        if self.n_gpu > 1:
            self.model = torch.nn.DataParallel(self.model)
    
    def create_mask(self, mask_poses, sen, rng):
        # 根据需要mask的位置生成mask
        # print(sen)
        # print([sen[pos] for pos in mask_poses])
        # max_mask_num = int(max(1, self.mask_rate * len(sen)))
        # mask_poses = mask_poses[0:max_mask_num]
        masked_info = [{} for token in sen]
        for pos in mask_poses:
            if rng.random() < 0.8:
                mask_token = "[MASK]"
            else:
                if rng.random() < 0.5:
                    mask_token = sen[pos]
                else:
                    mask_token = self.vocab[rng.randint(0, len(self.vocab) - 1)]
            masked_info[pos]["mask"] = mask_token
            masked_info[pos]["label"] = sen[pos]
        return masked_info

    def convert_examples_to_features(self, data):
        features = []
        for tokens in tqdm(data, desc="converting to features"):
            if len(tokens) >= self.max_seq_length - 1:
                tokens = tokens[0:(self.max_seq_length - 2)]
            ntokens = []
            ntokens.append("[CLS]")
            for token in tokens:
                ntokens.append(token)
            ntokens.append("[SEP]")
            input_ids = self.tokenizer.convert_tokens_to_ids(ntokens)
            input_mask = [1] * len(input_ids)
            while len(input_ids) < self.max_seq_length:
                input_ids.append(0)
                input_mask.append(0)
        
            features.append(InputFeatures(input_ids=input_ids, input_mask=input_mask))

        return features

    def evaluate(self, data, batch_size):
        eval_features = self.convert_examples_to_features(data)
        all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
        del eval_features
        eval_data = TensorDataset(all_input_ids, all_input_mask)
        eval_sampler = SequentialSampler(eval_data)
        eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=batch_size)

        self.model.eval()
        preds = []
        # for input_ids, input_mask in eval_dataloader:
        all_res = []
        all_logits = []
        for input_ids, input_mask in tqdm(eval_dataloader, desc="Evaluating"):
            input_ids = input_ids.to(self.device)
            input_mask = input_mask.to(self.device)
            with torch.no_grad():
                logits = self.model(input_ids, attention_mask=input_mask)
            # res = np.argmax(logits, axis=2)
            res = torch.argmax(logits, dim=2).detach().cpu().numpy()
            logits = logits.detach().cpu().numpy()
            all_res.extend(res)
            all_logits.extend(logits)
        


        N = len(all_res)
        # with multiprocessing.Pool(10) as pool:
        #     preds = pool.map(per_proc, list(zip(all_res, all_input_mask, all_logits)))
        for i in tqdm(range(0, N), desc="Begin CPU"):
            r, m, l = all_res[i], all_input_mask[i], all_logits[i]
            K = len(m)
            t = []
            for j in range(1, K):
                mm, rr, ll = m[j], r[j], l[j]
                if mm == 1:
                    t.append((rr, ll[rr]))
            t.pop() # pop out [SEP]
            # t = [(rr, ll[rr]) for mm, rr, ll in zip(m[1:-1], r[1:-1], l[1:-1]) if mm == 1]
            preds.append(t)
        # logits = [[ll for mm, ll in zip(m[1:-1], l[1:-1]) if mm == 1] for m, l in zip(input_mask, logits)]
            # preds.extend(logits)
        return preds


    def forward(self, data, all_labels, dupe_factor, rng):
        # data: not tokenized
        doc_num = len(data)
        # convert data, segment data to sentences
        # tokenized_data = []
        sentences = []
        sen_doc_ids = []  # [0, 0, ..., 0, 1, 1, ..., 1, ...] 每个句子对应原来段的id
        for (doc_id, doc) in enumerate(tqdm(data)):
            # tokenized_data.append(self.tokenizer.tokenize(doc))
            doc = nlp(doc)
            tL = [self.tokenizer.tokenize(sen.text) for sen in doc.sents]
            sentences.extend(tL)
            sen_doc_ids.extend([doc_id] * len(tL))
            del tL

        preds = self.evaluate(sentences, self.sen_batch_size)

        all_documents = []
        rand_all_documents = []
        for _ in range(dupe_factor):
            i = 0
            for doc_id in tqdm(range(doc_num), desc="Generating All Documents"):
                all_documents.append([])
                if self.with_rand:
                    rand_all_documents.append([])
                while i < len(sen_doc_ids) and doc_id == sen_doc_ids[i]:
                    mask_poses = [(pos, pred[1]) for (pos, pred) in enumerate(preds[i]) if pred[0] == 1]
                    mask_poses = sorted(mask_poses, key=lambda x: x[1], reverse=True) # 按评分排序
                    max_mask_num = int(max(1, self.mask_rate * len(sentences[i])))
                    mask_poses = [pos for pos, _ in mask_poses[0:max_mask_num]]
                    m_info = self.create_mask(mask_poses, sentences[i], rng)
                    all_documents[-1].append(MaskedTokenInstance(tokens=sentences[i], info=m_info))
                    if self.with_rand:
                        cand_indexes = [i for i in range(len(sentences[i]))]
                        rng.shuffle(cand_indexes)
                        rand_mask_poses = cand_indexes[0:len(mask_poses)] #与mask_poses的长度保持一致
                        rand_m_info = self.create_mask(rand_mask_poses, sentences[i], rng)
                        rand_all_documents[-1].append(MaskedTokenInstance(tokens=sentences[i], info=rand_m_info))
                    i += 1
                # print(all_documents[-1])
                # print(rand_all_documents[-1])
        if self.with_rand:
            print("with rand")
            return all_documents, rand_all_documents
        else:
            return all_documents