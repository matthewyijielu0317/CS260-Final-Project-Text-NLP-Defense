"""
Parts based on https://colab.research.google.com/drive/1pTuQhug6Dhl9XalKB0zUGf4FIdYFlpcX
"""
import os
import math
import torch
import numpy as np
import torch.nn as nn
from torch.nn.utils import clip_grad_norm_
import torch.optim as optim
from copy import deepcopy
from config import Config
from logger import Logger
from data_module import DataModule
from models.cnn import CNN
from models.lstm import LSTM
from models.bert_wrapper import BertWrapper
from utils import (
    prep_seq,
    pad,
    load_model,
    compute_accuracy,
    inference,
    shuffle_lists,
    list_join,
    copy_file,
)
from tensorboardX import SummaryWriter
from transformers import AdamW, get_linear_schedule_with_warmup


def save_model(epoch):
    save_path = "{}/checkpoints/epoch_{}".format(config.model_base_path, epoch)

    if not os.path.exists(save_path):
        os.makedirs(save_path)

    torch.save(
        {
            "epoch": epoch,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
        },
        "{}/model.pth".format(save_path),
    )


def eval_model(epoch, bert_wrapper=None):
    global best_epoch

    num_batches = int(math.ceil(len(data_module.val_texts) / config.batch_size_val))
    predictions = []
    total_loss = []

    for batch in range(num_batches):
        sentences = data_module.val_texts[
            batch * config.batch_size_val : (batch + 1) * config.batch_size_val
        ]
        labels = data_module.val_pols[
            batch * config.batch_size_val : (batch + 1) * config.batch_size_val
        ]

        labels = torch.tensor(labels, dtype=torch.int64)
        labels = labels.cuda() if config.gpu else labels

        preds, outputs = inference(
            sentences,
            model,
            data_module.word_to_idx,
            config,
            bert_wrapper=bert_wrapper,
            val=True,
        )

        predictions += preds
        loss = criterion(outputs, labels)
        total_loss.append(loss.item())

    acc = compute_accuracy(predictions, data_module.val_pols)
    total_loss = np.mean(total_loss)

    if total_loss < best_epoch[1]:
        best_epoch = (epoch, total_loss)

    logger.log.info("Best epoch up to now: {}".format(best_epoch))
    logger.log.info(
        "Val: epoch {}, loss {}, accuracy {}".format(epoch, total_loss, acc)
    )
    val_writer.add_scalar("Val/accuracy", acc, epoch)
    val_writer.add_scalar("Val/loss", total_loss, epoch)
    val_writer.close()


def test_model(bert_wrapper=None):
    if config.test_on_val:
        data_module.test_texts = data_module.val_texts
        data_module.test_pols = data_module.val_pols

    num_batches = int(math.ceil(len(data_module.test_texts) / config.batch_size_test))
    predictions = []

    for batch in range(num_batches):
        sentences = data_module.test_texts[
            batch * config.batch_size_test : (batch + 1) * config.batch_size_test
        ]
        labels = data_module.test_pols[
            batch * config.batch_size_test : (batch + 1) * config.batch_size_test
        ]

        preds, probs = inference(
            deepcopy(sentences),
            model,
            data_module.word_to_idx,
            config,
            bert_wrapper=bert_wrapper,
        )
        predictions += preds

        for idx in range(len(sentences)):
            sent = sentences[idx]

            logger.log.info(
                "=============== {} ===============".format(
                    batch * config.batch_size_test + (idx + 1)
                )
            )
            logger.log.info("Sentence: {}".format(list_join(sent)))
            logger.log.info("Label: {}".format(labels[idx]))
            logger.log.info("Prediction: {}".format(preds[idx]))
            logger.log.info("Confidence: {}".format(max(probs[idx])))

    logger.log.info(
        "Test accuracy: {}".format(compute_accuracy(predictions, data_module.test_pols))
    )


def run_epoch(epoch, scheduler=None, bert_wrapper=None):
    num_batches = int(math.ceil(len(data_module.train_texts) / config.batch_size_train))
    total_loss = []

    model.train()

    data_module.train_texts, data_module.train_pols = shuffle_lists(
        data_module.train_texts, data_module.train_pols
    )

    for batch in range(num_batches):
        sentences = data_module.train_texts[
            batch * config.batch_size_train : (batch + 1) * config.batch_size_train
        ]
        labels = data_module.train_pols[
            batch * config.batch_size_train : (batch + 1) * config.batch_size_train
        ]

        if not config.use_BERT:
            sentences = [
                pad(config.max_len, sentence, config.pad_token)
                for sentence in sentences
            ]
            inputs = [
                prep_seq(sentence, data_module.word_to_idx, config.unk_token)
                for sentence in sentences
            ]
        else:
            inputs, masks = [
                list(x)
                for x in zip(
                    *[bert_wrapper.pre_pro(sentence) for sentence in sentences]
                )
            ]

        inputs = torch.tensor(inputs, dtype=torch.int64)
        labels = torch.tensor(labels, dtype=torch.int64)

        if config.gpu:
            inputs, labels = inputs.cuda(), labels.cuda()

        model.zero_grad()

        if config.use_BERT:
            masks = torch.tensor(masks)
            masks = masks.cuda() if config.gpu else masks

            outputs = model(
                inputs, token_type_ids=None, attention_mask=masks, labels=labels
            )

            loss = outputs.loss
            loss.backward()

            if config.clip_norm > 0:
                clip_grad_norm_(model.parameters(), config.clip_norm)

            optimizer.step()
            scheduler.step()
        else:
            outputs = model(inputs)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()

        total_loss.append(loss.item())

        logger.log.info(
            "Epoch {}, batch {}/{}: loss {}".format(
                epoch, batch + 1, num_batches, loss.item()
            )
        )
        train_writer.add_scalar(
            "Train/batch_loss", loss.item(), epoch * num_batches + batch
        )

    logger.log.info("Train: epoch {}, loss {}".format(epoch, np.mean(total_loss)))

    # https://github.com/lanpa/tensorboardX
    for name, var in model.named_parameters():
        if var.requires_grad:
            train_writer.add_histogram(name, var.clone().cpu().data.numpy(), epoch)

    train_writer.add_scalar("Train/epoch_loss", np.mean(total_loss), epoch)
    train_writer.close()

    eval_model(epoch, bert_wrapper=bert_wrapper)

    logger.log.info("Save model at epoch {}".format(epoch))
    save_model(epoch)

    if epoch == config.num_epoch:
        os.rename(
            "{}/checkpoints/epoch_{}".format(config.model_base_path, best_epoch[0]),
            "{}/checkpoints/e_best".format(config.model_base_path),
        )


if __name__ == "__main__":
    config = Config()
    logger = Logger(config)
    data_module = DataModule(config, logger)
    config.vocab_size = len(data_module.vocab)
    bert_wrapper = None

    if config.mode == "train":
        train_writer = SummaryWriter(config.tb_log_train_path)
        val_writer = SummaryWriter(config.tb_log_val_path)
        criterion = nn.CrossEntropyLoss()
        best_epoch = (0, np.inf)
        optimizer, scheduler = None, None

        copy_file(config)

        if config.use_BERT:
            bert_wrapper = BertWrapper(config, logger)
            model = bert_wrapper.model

            optimizer = AdamW(
                model.parameters(),
                lr=config.learning_rate,
                eps=config.adam_eps,
                weight_decay=config.weight_decay,
            )

            total_steps = config.num_epoch * int(
                math.ceil(len(data_module.train_texts) / config.batch_size_train)
            )

            scheduler = get_linear_schedule_with_warmup(
                optimizer,
                num_warmup_steps=int(total_steps * config.warmup_percent),
                num_training_steps=total_steps,
            )
        else:
            if config.model_type == "cnn":
                model = CNN(
                    config, logger, pre_trained_embs=data_module.init_pretrained
                )
            else:
                model = LSTM(
                    config, logger, pre_trained_embs=data_module.init_pretrained
                )

            optimizer = optim.Adam(model.parameters(), lr=config.learning_rate)

        if config.gpu:
            model.cuda()

        logger.log.info("Start training")

        for epoch in range(1, config.num_epoch + 1):
            run_epoch(epoch, scheduler=scheduler, bert_wrapper=bert_wrapper)

        logger.log.info("Finished training")

        model = load_model(
            "{}/checkpoints/e_best/model.pth".format(config.model_base_path),
            model,
            logger,
        )
        test_model(bert_wrapper=bert_wrapper)
    elif config.mode == "test":
        if config.use_BERT:
            bert_wrapper = BertWrapper(config, logger)
            model = bert_wrapper.model
        elif config.model_type == "cnn":
            model = CNN(config, logger)
        else:
            model = LSTM(config, logger)

        model = load_model(config.load_model_path, model, logger)

        if config.gpu:
            model.cuda()

        test_model(bert_wrapper=bert_wrapper)
    else:
        logger.log.info("Incorrect mode {}. Exit.".format(config.mode))
        exit()
