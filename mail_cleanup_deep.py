#!/usr/bin/env python3

from datetime import datetime
import fastText
from itertools import chain
from keras import callbacks, layers, models
from keras.utils import Sequence
import numpy as np
import json
import plac
import re
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
MAX_LEN = 15
CONTEXT = 4


@plac.annotations(
    cmd=('Command', 'positional', None, str, None, 'CMD'),
    model=('Keras model', 'positional', None, str, None, 'K5'),
    input_file=('Input JSONL file', 'positional', None, str, None, 'JSONL'),
    fasttext_model=('FastText Model', 'positional', None, str, None, 'FASTTEXT_BIN'),
    output_json=('Output JSONL file', 'option', 'o', str, None, 'OUTPUT'),
    validation_input=('Validation Data JSON', 'option', 'v', str, None, 'JSONL')
)
def main(cmd, model, input_file, fasttext_model, output_json=None, validation_input=None):
    print('Loading FastText model...')
    load_fasttext_model(fasttext_model)

    if cmd == 'train':
        train_model(input_file, model, validation_input)
    elif cmd == 'predict':
        predict(model, input_file, output_json)
    else:
        print('Invalid command.', file=sys.stderr)
        exit(1)


class MailLinesSequence(Sequence):
    def __init__(self, input_file, labeled=True, batch_size=None, line_shape=(MAX_LEN, INPUT_DIM)):
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

        self._load_json(input_file)

    def _load_json(self, input_file):
        context_padding = self.padding_line * CONTEXT

        for input_line in open(input_file, 'r'):
            mail_json = json.loads(input_line)

            lines = None
            if not self.labeled:
                lines = [l + '\n' for l in mail_json['text'].split('\n')]

            elif self.labeled and mail_json['annotations']:
                lines = [l for l in label_lines(mail_json)]

            # Skip overly long mails (probably just excessive log data)
            if len(lines) > 5000:
                continue

            if lines:
                self.mail_start_index_map[len(self.mail_lines)] = mail_json
                self.mail_end_index_map[len(self.mail_lines) + len(lines)] = mail_json
                self.mail_lines.extend(context_padding + lines + context_padding)

        if self.batch_size is None:
            self.batch_size = len(self.mail_lines)

    def __len__(self):
        return int(np.ceil(len(self.mail_lines) / self.batch_size))

    def __getitem__(self, index):
        index = index * self.batch_size

        batch = np.empty((self.batch_size,) + self.line_shape)
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
            batch_context[i - CONTEXT] = np.stack(line_vecs)

        if self.labeled:
            return [batch, batch_context], batch_labels

        return [batch, batch_context]


def pad_2d_sequence(seq, max_len):
    return np.pad(seq[:max_len], ((0, max(0, max_len - seq.shape[0])), (0, 0)), 'constant')


def train_model(input_file, output_model, validation_input=None):
    tb_callback = callbacks.TensorBoard(log_dir='./data/graph/' + str(datetime.now()), update_freq=1000,
                                        histogram_freq=0, write_grads=True, write_graph=False, write_images=False)
    es_callback = callbacks.EarlyStopping(monitor='val_loss', verbose=1, patience=5)
    cp_callback = callbacks.ModelCheckpoint(output_model + '.epoch-{epoch:02d}.loss-{val_loss:.2f}.hdf5')

    # Line model
    line_input = layers.Input(shape=(MAX_LEN, INPUT_DIM))
    masking = layers.Masking(0)(line_input)
    bi_lstm = layers.Bidirectional(layers.GRU(128), merge_mode='sum')(masking)
    bi_lstm = layers.BatchNormalization()(bi_lstm)
    bi_lstm = layers.Activation('relu')(bi_lstm)
    
    context_input = layers.Input(shape=(CONTEXT * 2 + 1, MAX_LEN, INPUT_DIM))
    conv2d = layers.Conv2D(5, (3, 3))(context_input)
    conv2d = layers.BatchNormalization()(conv2d)
    conv2d = layers.Activation('relu')(conv2d)
    conv2d = layers.Conv2D(5, (3, 3))(conv2d)
    conv2d = layers.Activation('relu')(conv2d)
    conv2d = layers.MaxPooling2D(2)(conv2d)
    flatten = layers.Flatten()(conv2d)
    #dropout_1 = layers.Dropout(0.25)(dropout_1)
    dense_1 = layers.Dense(128)(flatten)
    dense_1 = layers.Activation('relu')(dense_1)

    concat = layers.concatenate([bi_lstm, dense_1])

    dense_2 = layers.Dropout(0.5)(concat)
    dense_2 = layers.Dense(OUTPUT_DIM)(dense_2)
    softmax = layers.Activation('softmax')(dense_2)

    line_model = models.Model(inputs=[line_input, context_input], outputs=softmax)
    line_model.compile(optimizer='adam', loss='categorical_crossentropy',
                       metrics=['categorical_accuracy', 'mean_squared_error'])
    line_model.summary()

    train_seq = MailLinesSequence(input_file, labeled=True, batch_size=BATCH_SIZE)
    val_seq = MailLinesSequence(validation_input, labeled=True) if validation_input else None

    line_model.fit_generator(train_seq, epochs=15, validation_data=val_seq, shuffle=True,
                             max_queue_size=100, callbacks=[tb_callback, es_callback, cp_callback])


def predict(input_model, input_file, output_json=None):
    line_model = models.load_model(input_model + '.hdf5')

    output_json_file = None
    if output_json:
        output_json_file = open(output_json, 'w')

    train_seq = MailLinesSequence(input_file, labeled=False, batch_size=100)

    predictions = line_model.predict_generator(train_seq, steps=200,  verbose=False)
    export_mail_annotation_spans(predictions, train_seq, output_json_file)

    if output_json_file:
        output_json_file.close()


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

        prev_l = [label_map_inverse[np.argmax(l)] for l in labels_softmax[i - CONTEXT:i]]
        next_l = [label_map_inverse[np.argmax(l)] for l in labels_softmax[i + 1:i + 1 + CONTEXT]]

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

        # Quotation markers
        elif label_text == 'quotation' and prev_l[-1] in ['<empty>', '<pad>'] \
                and label_map_int['quotation_marker'] in label_argsort[:3]:
            label_text = 'quotation_marker'

        # Interrupted closings / signatures
        elif label_text != prev_l[-1] and next_l[0] == prev_l[-1] \
                and prev_l[-1] in ['closing', 'personal_signature', 'mua_signature']:
            label_text = prev_l[-1]

        # Interrupted larger blocks
        elif len(prev_set) == 1 and label_text != [*prev_set][0] and [*prev_set][0] in next_set \
                and [*prev_set][0] in ['mua_signature', 'personal_signature', 'patch', 'code', 'tabular', 'technical'] \
                and label_map_int[[*prev_set][0]] == label_argsort[1]:
            label_text = [*prev_set][0]

        # Personal signatures in MUA signatures
        elif label_text == 'personal_signature' and prev_l[-1] == 'mua_signature' \
                and (next_l[0] == 'mua_signature' or next_l[0] == '<pad>'):
            label_text = 'mua_signature'

        # MUA signatures in personal signatures
        elif label_text == 'mua_signature' and prev_l[-1] == 'personal_signature' \
                and (next_l[0] == 'personal_signature' or next_l[0] == '<pad>'):
            label_text = 'personal_signature'

        # Stray technical
        elif label_text == 'technical' and prev_l[-1] not in ['technical', '<empty>', '<pad>'] \
                and next_l[0] != 'technical':
            label_text = prev_l[-1]

        labels_softmax[i] = label_map[label_text]
        yield line, label_text


def export_mail_annotation_spans(predictions_softmax, pred_sequence, output_file=None):
    text = ''
    annotations = []
    prev_label = None
    cur_label = '<pad>'
    start_offset = 0

    for i, (line, label_text) in enumerate(post_process_labels(pred_sequence.mail_lines, predictions_softmax)):
        cur_label = label_text
        if prev_label is None:
            prev_label = cur_label

        cur_offset = len(text) - 1
        text += line
        if cur_label != prev_label:
            if output_file and prev_label not in ['<pad>', '<empty>']:
                annotations.append((start_offset, cur_offset, prev_label))

            start_offset = cur_offset + 1
            prev_label = cur_label

        print('{:>20}    --->    {}'.format(label_text, line), end='')

    print()

    # if not output_file:
    #     return
    #
    # if cur_label not in ['<empty>', '<pad>']:
    #     annotations.append((start_offset, len(text) - 1, cur_label))
    #
    # d = mail_dict.copy()
    #
    # if 'id' in d:
    #     del d['id']
    #
    # d.update({'labels': annotations, 'annotations': annotations})
    # json.dump(d, output_file)
    # output_file.write('\n')


def contextualize(lines, context=CONTEXT):
    lines_copy = []
    pad_rows(lines_copy, [], context)
    lines_copy.extend(lines)
    pad_rows(lines_copy, [], context)

    c_lines = []
    for i, line in enumerate(lines_copy):
        if i < context or i >= len(lines_copy) - context:
            continue

        prev_vec = lines_copy[i - context:i]
        next_vec = lines_copy[i + 1:i + 1 + context]

        c_lines.append(np.concatenate(prev_vec + [line] + next_vec))

    return np.array(c_lines)


def pad_rows(rows, labels, pad=1, shape=(MAX_LEN, INPUT_DIM)):
    for _ in range(pad):
        rows.append(np.ones(shape) * -1)
        labels.append(label_map['<pad>'])


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
    _model = fastText.load_model(model_path)


def get_word_vectors(text):
    text = re.sub(r'([a-zA-Z0-9_\-\./+]+)@((\[[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.)|' +
                  r'(([a-zA-Z0-9\-]+\.)+))([a-zA-Z]{2,4}|[0-9]{1,3})(\]?)', 'mail@address', text)
    matrix = [_model.get_word_vector(w) for w in fastText.tokenize(text)]
    return np.array(matrix)


def get_word_vector(word):
    return _model.get_word_vector(word)


if __name__ == '__main__':
    plac.call(main)
