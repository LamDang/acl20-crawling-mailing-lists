#!/usr/bin/env python3
#
# Deep message segmenter to classify lines of an email or newsgroup message.

from util.util import *

from datetime import datetime
import fastText
from itertools import chain
from keras import callbacks, layers, models
from keras.utils import Sequence
import numpy as np
import json
import plac
import sys


label_map_int = {
    'paragraph': 0,
    'closing': 1,
    'inline_headers': 2,
    'log_data': 3,
    'mua_signature': 4,
    'patch': 5,
    'personal_signature': 6,
    'quotation': 7,
    'quotation_marker': 8,
    'raw_code': 9,
    'salutation': 10,
    'section_heading': 11,
    'tabular': 12,
    'technical': 13,
    'visual_separator': 14,
    '<empty>': 15,
    '<pad>': 16
}


def labels_to_onehot(labels_dict):
    onehots = np.eye(len(labels_dict))
    onehot_dict = {l: onehots[i] for i, l in enumerate(labels_dict)}
    return onehot_dict


label_map_inverse = {label_map_int[k]: k for k in label_map_int}
label_map = labels_to_onehot(label_map_int)

INPUT_DIM = 100
OUTPUT_DIM = len(label_map)
BATCH_SIZE = 128
MAX_LEN = 12
CONTEXT = 4


@plac.annotations(
    cmd=('Command', 'positional', None, str, None, 'CMD'),
    fasttext_model=('FastText Model', 'positional', None, str, None, 'FASTTEXT_BIN'),
    keras_model=('Keras HDF5 model', 'positional', None, str, None, 'HDF5'),
    input_file=('Input JSONL file', 'positional', None, str, None, 'JSONL'),
    output_json=('Output JSONL file', 'option', 'o', str, None, 'OUTPUT'),
    validation_input=('Validation Data JSON', 'option', 'v', str, None, 'JSONL')
)
def main(cmd, fasttext_model, keras_model, input_file, output_json=None, validation_input=None):
    print('Loading FastText model...')
    load_fasttext_model(fasttext_model)

    if cmd == 'train':
        train_model(input_file, keras_model, validation_input)
    elif cmd == 'predict':
        line_model = models.load_model(keras_model)
        line_model._make_predict_function()
        predict(line_model, input_file, output_json)
    else:
        print('Invalid command.', file=sys.stderr)
        exit(1)


class MailLinesSequence(Sequence):
    def __init__(self, input_descriptor, labeled=True, batch_size=None, line_shape=(MAX_LEN, INPUT_DIM),
                 input_is_raw_text=False, max_lines=None):
        self.labeled = labeled
        self.mail_lines = []
        self.mail_start_index_map = {}
        self.mail_end_index_map = {}

        self.batch_size = batch_size
        self.line_shape = line_shape

        if self.labeled:
            self.padding_line = [(None, label_map['<pad>'])]
        else:
            self.padding_line = [None]

        if not input_is_raw_text:
            self._load_jsonl(input_descriptor, max_lines)
        else:
            self._load_raw_text(input_descriptor, max_lines)

    def _load_jsonl(self, jsonl_filedesc, max_lines):
        context_padding = self.padding_line * CONTEXT

        for i, json_text in enumerate(jsonl_filedesc):
            mail_json = json.loads(json_text)

            lines = None
            if not self.labeled:
                lines = [l + '\n' for l in mail_json['text'].split('\n')]

            elif self.labeled and mail_json['labels']:
                lines = [l for l in label_lines(mail_json)]

            # Skip overly long mails (probably just excessive log data)
            if len(lines) > 5000:
                continue

            if lines:
                self.mail_start_index_map[len(self.mail_lines) + CONTEXT] = mail_json
                self.mail_end_index_map[len(self.mail_lines) + CONTEXT + len(lines)] = mail_json
                self.mail_lines.extend(context_padding + lines + context_padding)

            if max_lines is not None and i >= max_lines:
                break

        if self.batch_size is None:
            self.batch_size = len(self.mail_lines)

    def _load_raw_text(self, raw_text, max_lines):
        if max_lines is not None:
            lines = [l + '\n' for l in raw_text.split('\n')[:max_lines]]
        else:
            lines = [l + '\n' for l in raw_text.split('\n')]

        if lines:
            context_padding = self.padding_line * CONTEXT
            self.mail_lines.extend(context_padding + lines + context_padding)

        if self.batch_size is None:
            self.batch_size = len(self.mail_lines)

    def __len__(self):
        return int(np.ceil(len(self.mail_lines) / self.batch_size))

    def __getitem__(self, index):
        index = index * self.batch_size

        batch = np.empty((self.batch_size,) + self.line_shape)
        batch_prev = np.empty((self.batch_size,) + self.line_shape)
        batch_context = np.empty((self.batch_size, CONTEXT * 2 + 1) + self.line_shape)
        batch_labels = np.empty((self.batch_size, OUTPUT_DIM))

        end_index = index + self.batch_size if self.batch_size is not None else len(self.mail_lines)

        padding_lines = self.padding_line * CONTEXT
        mail_slice = padding_lines + self.mail_lines[index:end_index] + padding_lines

        for i, line in enumerate(mail_slice):
            if i < CONTEXT or i >= len(mail_slice) - CONTEXT:
                continue

            if self.labeled:
                batch_labels[i - CONTEXT] = line[1]
                # line_text = line[0] if line[0] is not None else '<PAD>\n'
                # print('{:>20}    --->    {}'.format(label_map_inverse[np.argmax(line[1])], line_text), end='')

            line_vecs = []
            for c in chain(mail_slice[i - CONTEXT:i], [line], mail_slice[i + 1:i + 1 + CONTEXT]):
                if self.labeled:
                    c, _ = c    # type: tuple

                # Check if this is a padding line
                if c is None:
                    line_vecs.append(np.ones(self.line_shape) * -1)
                else:
                    line_vecs.append(pad_2d_sequence(get_word_vectors(c), self.line_shape[0]))

            batch[i - CONTEXT] = line_vecs[CONTEXT]
            batch_prev[i - CONTEXT] = line_vecs[CONTEXT - 1]
            batch_context[i - CONTEXT] = np.stack(line_vecs)

        if self.labeled:
            return [batch, batch_prev, batch_context], batch_labels

        return [batch, batch_prev, batch_context]


def pad_2d_sequence(seq, max_len):
    if seq.shape[0] > max_len:
        pivot_idx = int(np.ceil(max_len * .75))
        seq = np.concatenate((seq[:pivot_idx], seq[seq.shape[0] - max_len + pivot_idx:]))

    return np.pad(seq, ((0, max(0, max_len - seq.shape[0])), (0, 0)), 'constant')


def train_model(input_file, output_model, validation_input=None):
    tb_callback = callbacks.TensorBoard(log_dir='./data/graph/' + str(datetime.now()), update_freq=1000,
                                        histogram_freq=0, write_grads=True, write_graph=False, write_images=False)
    es_callback = callbacks.EarlyStopping(monitor='val_loss', verbose=1, patience=5)
    cp_callback = callbacks.ModelCheckpoint(output_model + '.epoch-{epoch:02d}.loss-{val_loss:.2f}.hdf5')

    def get_base_line_model():
        line_input = layers.Input(shape=(MAX_LEN, INPUT_DIM))
        masking = layers.Masking(0)(line_input)
        bi_gru = layers.Bidirectional(layers.GRU(128), merge_mode='sum')(masking)
        bi_gru = layers.BatchNormalization()(bi_gru)
        bi_gru = layers.Activation('relu')(bi_gru)
        return line_input, bi_gru

    def get_context_model():
        context_input = layers.Input(shape=(CONTEXT * 2 + 1, MAX_LEN, INPUT_DIM))
        conv2d = layers.Conv2D(128, (4, 4))(context_input)
        conv2d = layers.BatchNormalization()(conv2d)
        conv2d = layers.Activation('relu')(conv2d)
        conv2d = layers.Conv2D(64, (3, 3))(conv2d)
        conv2d = layers.Activation('relu')(conv2d)
        conv2d = layers.MaxPooling2D(2)(conv2d)
        flatten = layers.Flatten()(conv2d)
        dense = layers.Dense(128)(flatten)
        dense = layers.Activation('relu')(dense)
        return context_input, dense

    line_input_cur, line_model_cur = get_base_line_model()
    line_input_prev, line_model_prev = get_base_line_model()
    context_input, context_model = get_context_model()

    concat = layers.concatenate([line_model_cur, line_model_prev, context_model])
    dropout = layers.Dropout(0.25)(concat)
    dense_2 = layers.Dense(OUTPUT_DIM)(dropout)
    output = layers.Activation('softmax')(dense_2)

    line_model = models.Model(inputs=[line_input_cur, line_input_prev, context_input], outputs=output)
    line_model.compile(optimizer='adam', loss='categorical_hinge',
                       metrics=['categorical_accuracy'])
    line_model.summary()

    train_seq = MailLinesSequence(input_file, labeled=True, batch_size=BATCH_SIZE)
    val_seq = MailLinesSequence(validation_input, labeled=True) if validation_input else None

    line_model.fit_generator(train_seq, epochs=15, validation_data=val_seq, shuffle=True,
                             max_queue_size=100, callbacks=[tb_callback, es_callback, cp_callback])


def predict(line_model, input_file, output_json=None):
    output_json_file = None
    if output_json:
        output_json_file = open(output_json, 'w')

    to_stdout = output_json is None

    print('Predicting {}...'.format(input_file))
    with open(input_file, 'r') as f:
        while True:
            pred_seq = MailLinesSequence(f, labeled=False, batch_size=256, max_lines=1000)
            if len(pred_seq) == 0:
                break

            predictions = line_model.predict_generator(
                pred_seq, verbose=(not to_stdout), steps=(None if not to_stdout else 10))
            export_mail_annotation_spans(predictions, pred_seq, output_json_file, verbose=to_stdout)

            if output_json_file:
                output_json_file.flush()

    if output_json_file:
        output_json_file.close()


def predict_raw_email(line_model, email):
    pred_seq = MailLinesSequence(email, labeled=False, input_is_raw_text=True)
    return (pred for i, pred in
            enumerate(post_process_labels(pred_seq.mail_lines, line_model.predict_generator(pred_seq)))
            if CONTEXT <= i < len(pred_seq.mail_lines) - CONTEXT)


def post_process_labels(lines, labels_softmax):
    lines = ([None] * CONTEXT) + lines + ([None] * CONTEXT)
    sm_pad = np.ones((CONTEXT, OUTPUT_DIM)) * -1
    labels_softmax = np.concatenate((sm_pad, labels_softmax, sm_pad))

    for i, (line, label) in enumerate(zip(lines, labels_softmax)):
        # Skip padding
        if i < CONTEXT:
            continue
        if i >= len(lines) - CONTEXT:
            break

        label_argmax = np.argmax(label)
        label_argsort = np.argsort(label)[::-1]
        label_text = label_map_inverse[label_argmax]

        context = min(3, CONTEXT)

        prev_l = [label_map_inverse[np.argmax(l)] for l in labels_softmax[i - context:i]]
        next_l = [label_map_inverse[np.argmax(l)] for l in labels_softmax[i + 1:i + 1 + context]]

        prev_set = set([l for l in prev_l if l not in ['<empty>', '<pad>']])
        next_set = set([l for l in next_l if l not in ['<empty>', '<pad>']])

        if line is None:
            yield '<PAD>\n', '<pad>'
            labels_softmax[i] = label_map['<pad>']
            continue

        # Correct <empty>
        if line.strip() == '':
            label_text = '<empty>'

        # Empty lines have to be empty
        elif (label_text == '<empty>' and line.strip() != '') or label_text == '<pad>':
            label_text = prev_l[-1] if prev_l[-1] not in ['<empty>', '<pad>'] else 'paragraph'

        # Bleeding quotations
        elif label_text == 'quotation' and prev_l[-1] == 'quotation' \
                and lines[i - 1].strip() and lines[i - 1].strip() \
                and next_l[0] != 'quotation' and lines[i - 1].strip()[0] != line.strip()[0] \
                and prev_l[-1] not in ['<empty>', '<pad>']:
            label_text = prev_l[-1]

        # Quotations
        elif label_text not in ['quotation', 'quotation_marker', 'inline_header'] \
                and (line.strip().startswith('>') or line.strip().startswith('|')) \
                and (label_map_int['quotation'] in label_argsort[:3] or prev_l[-1] == 'quotation'):
            label_text = 'quotation'

        # Quotation markers
        elif label_text == 'quotation' and prev_l[-1] in ['<empty>', '<pad>'] \
                and label_map_int['quotation_marker'] in label_argsort[:3]:
            label_text = 'quotation_marker'

        # Interrupted short blocks
        elif label_text != prev_l[-1] and next_l[0] == prev_l[-1] \
                and prev_l[-1] in ['closing', 'personal_signature', 'mua_signature', 'inline-header', 'technical']:
            label_text = prev_l[-1]

        # Interrupted long blocks
        elif len(prev_set) == 1 and label_text != [*prev_set][0] and [*prev_set][0] in next_set \
                and [*prev_set][0] in ['mua_signature', 'personal_signature',
                                       'patch', 'code', 'tabular', 'technical'] \
                and label_map_int[[*prev_set][0]] == label_argsort[1]:
            label_text = [*prev_set][0]

        # Interrupting stray classes
        elif label_text in ['technical', 'mua_signature', 'personal_signature', 'patch', 'tabular'] \
                and prev_l[-1] != label_text and prev_l[-1] not in ['<pad>', '<empty>'] \
                and (next_l[0] == prev_l[-1] or (next_l[1] == prev_l[-1] and next_l[0] == '<empty>')):
            label_text = prev_l[-1]

        labels_softmax[i] = label_map[label_text]
        yield line, label_text


def export_mail_annotation_spans(predictions_softmax, pred_sequence, output_file=None, verbose=True):
    text = ''
    annotations = []
    prev_label = None
    cur_label = '<pad>'
    start_offset = 0
    mail_dict = None
    skip_lines = CONTEXT

    def write_annotations(d, a):
        if not a or 'text' not in d or not d['text']:
            return

        d = {k: d[k] for k in d if k != 'id'}
        d.update({'labels': a, 'text': d['text'].lstrip()})

        json.dump(d, output_file)
        output_file.write('\n')

    for i, (line, label_text) in enumerate(post_process_labels(pred_sequence.mail_lines, predictions_softmax)):
        # Skip padding
        if i < skip_lines:
            continue
        skip_lines = i

        cur_label = label_text
        if prev_label is None:
            prev_label = cur_label

        if i in pred_sequence.mail_start_index_map:
            if verbose:
                print(' {0:>>20}    --->    <<< MAIL START >>>'.format(''))
            mail_dict = pred_sequence.mail_start_index_map[i]

        cur_offset = len(text) - 1
        text += line
        text = text.lstrip()

        if i in pred_sequence.mail_end_index_map:
            if output_file:
                if prev_label not in ['<pad>', '<empty>']:
                    annotations.append((start_offset, cur_offset, prev_label))
                write_annotations(mail_dict, annotations)

            mail_dict = None
            annotations.clear()
            start_offset = 0
            prev_label = None
            text = ''
            skip_lines += CONTEXT * 2
            continue

        if verbose:
            print(' {:>20}    --->    {}'.format(label_text, line), end='')

        if cur_label != prev_label:
            if output_file and prev_label not in ['<pad>', '<empty>']:
                annotations.append((start_offset, cur_offset, prev_label))

            start_offset = cur_offset + 1
            prev_label = cur_label

    if output_file and mail_dict:
        if cur_label not in ['<empty>', '<pad>']:
            annotations.append((start_offset, len(text) - 1, cur_label))
        write_annotations(mail_dict, annotations)


def label_lines(doc):
    lines = [l + '\n' for l in doc['text'].split('\n')]
    annotations = sorted(doc['annotations'], key=lambda a: a['start_offset'], reverse=True)
    offset = 0
    for l in lines:
        end_offset = offset + len(l)

        if annotations and offset > annotations[-1]['end_offset']:
            annotations.pop()

        if not annotations or not l.strip():
            yield l, label_map['<empty>']
            offset = end_offset
            continue

        if offset < annotations[-1]['end_offset'] and end_offset > annotations[-1]['start_offset']:
            yield l,  label_map[annotations[-1]['label']]
        else:
            yield l, label_map['<empty>']

        offset = end_offset


_model = None


def load_fasttext_model(model_path):
    global _model
    if not _model:
        _model = fastText.load_model(model_path)


def get_word_vectors(text):
    matrix = [_model.get_word_vector(w) for w in fastText.tokenize(normalize_message_text(text))]
    return np.array(matrix)


def get_word_vector(word):
    if _model is None:
        raise RuntimeError("FastText vectors not loaded. Call load_fasttext_model() first.")
    return _model.get_word_vector(word)


if __name__ == '__main__':
    plac.call(main)
