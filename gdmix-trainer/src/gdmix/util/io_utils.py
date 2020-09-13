import argparse
import collections
import itertools
from typing import Iterator

import fastavro
import json
import logging
import numpy as np
import os
import tensorflow as tf
import time

from gdmix.models.schemas import BAYESIAN_LINEAR_MODEL_SCHEMA

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

INTERCEPT = "(INTERCEPT)"


def try_write_avro_blocks(f, schema, records, suc_msg=None, err_msg=None, wipe_records=False):
    """
    write a block into avro file. This is used continuously when the whole file does not fit in memory.

    :param wipe_records: Wipe records after a successful write if True
    :param f: file handle.
    :param schema: avro schema used by the writer.
    :param records: a set of records to be written to the avro file.
    :param suc_msg: message to print when write succeeds.
    :param err_msg: message to print when write fails.
    :return: none
    """
    try:
        fastavro.writer(f, schema, records)
        if wipe_records:
            records.clear()
        if suc_msg:
            logger.info(suc_msg)
    except Exception as exp:
        if err_msg:
            logger.error(exp)
            logger.error(err_msg)
        raise


def load_linear_models_from_avro(model_file, feature_file):
    """
    Load linear models from avro files.
    The models are in photon-ml format.
    Intercept is moved to the end of the coefficient array.
    :param model_file: Model avro file, photon-ml format
    :param feature_file: A file containing all features of the model (intercept excluded)
    :return:
    """

    def get_one_model_weights(model_record, feature_map):
        """
        Load a single model from avro record
        :param model_record: photon-ml LR model in avro record format
        :param feature_map: feature name to index map
        :return: a numpy array of the model coefficients, intercept is at the end. Elements are in np.float64.
        """
        num_features = len(feature_map)
        model_coefficients = np.zeros(num_features+1, dtype=np.float64)
        for ntv in model_record["means"]:
            name, term, value = ntv['name'], ntv['term'], np.float64(ntv['value'])
            if name == INTERCEPT and term == '':
                model_coefficients[num_features] = value  # Intercept at the end.
            else:
                full_feature_name = name_term_to_string(name, term)
                if full_feature_name in feature_map:  # Take only the features that in the current training dataset.
                    feature_index = feature_map[full_feature_name]
                    model_coefficients[feature_index] = value
        return model_coefficients

    models = []
    feature_map = get_feature_map(feature_file)
    with tf.io.gfile.GFile(model_file, 'rb') as fo:
        avro_reader = fastavro.reader(fo)
        for record in avro_reader:
            model_coefficients = get_one_model_weights(record, feature_map)
            models.append(model_coefficients)
    return models


def gen_one_avro_model(model_id, model_class, weight_indices, weight_values, bias, feature_list):
    """
    generate the record for one LR model in photon-ml avro format
    :param model_id: model id
    :param model_class: model class
    :param weight_indices: LR weight vector indices
    :param weight_values: LR weight vector values
    :param bias: the bias/offset/intercept
    :param feature_list: corresponding feature names
    :return: a model in avro format
    """
    records = {u'modelId': model_id, u'modelClass': model_class, u'means': [],
               u'lossFunction': ""}
    record = {u'name': INTERCEPT, u'term': '', u'value': bias}
    records[u'means'].insert(0, record)
    for w_i, w_v in zip(weight_indices.flatten(), weight_values.flatten()):
        feat = feature_list[w_i]
        name, term = name_term_from_string(feat)
        record = {u'name': name, u'term': term, u'value': w_v}
        records[u'means'].append(record)
    return records


def export_linear_model_to_avro(model_ids,
                                list_of_weight_indices,
                                list_of_weight_values,
                                biases,
                                feature_file,
                                output_file,
                                model_log_interval=1000,
                                model_class="com.linkedin.photon.ml.supervised.classification.LogisticRegressionModel"
                                ):
    """
    Export random effect logistic regression model in avro format for photon-ml to consume
    :param model_ids:               a list of model ids used in generated avro file
    :param list_of_weight_indices:  list of indices for entity-specific model weights
    :param list_of_weight_values:   list of values for entity-specific model weights
    :param biases:                  list of entity bias terms
    :param feature_file:            a file containing all the features, typically generated by avro2tf.
    :param output_file:             full file path for the generated avro file.
    :param model_log_interval:      write model every model_log_interval models.
    :param model_class:             the model class defined by photon-ml.
    :return: None
    """
    # STEP [1] - Read feature list
    feature_list = read_feature_list(feature_file)

    # STEP [2] - Read number of features and moels
    num_features = len(feature_list)
    num_models = len(biases)
    logger.info("found {} models".format(num_models))
    logger.info("num features: {}".format(num_features))

    # STEP [3]
    schema = fastavro.parse_schema(json.loads(BAYESIAN_LINEAR_MODEL_SCHEMA))

    def batched_records():
        for i in range(0, num_models):
            records = gen_one_avro_model(str(model_ids[i]), model_class, list_of_weight_indices[i],
                                         list_of_weight_values[i],
                                         biases[i], feature_list)
            yield records

    batched_write_avro(batched_records(), output_file, schema, model_log_interval)
    logger.info(f"dumped avro model file at {output_file}")


def read_feature_list(feature_file):
    """
    Get feature names from the feature file.
    Note: intercept is not included here since it is not part of the raw data.
    :param feature_file: user provided feature file, each row is a "name,term" feature name
    :return: list of feature names
    """
    feature_list = []
    with tf.io.gfile.GFile(feature_file) as f:
        f.seekable = lambda: False
        for line in f:
            fields = line.strip()
            feature_list.append(fields)
    return feature_list


def name_term_from_string(name_term_string):
    """
    Convert "name,term" string to (name, term)
    :param name_term_string: A string where name and term joined by ","
    :return: (name, term) tuple
    """
    name, *term = name_term_string.split(',')
    assert len(term) <= 1, f"One ',' expected, but found more in {name_term_string!r}."
    return name, term[0] if term else ''


def name_term_to_string(name, term):
    """
    Convert (name, term) to "name,term" string.
    :param name: Name of the feature.
    :param term: Term of the feature.
    :return: "name,term" string
    """
    return ','.join([name, term])


def get_feature_map(feature_file):
    """
    Get feature -> index map.
    The index of a feature is the position of the feature in the file.
    The index starts from zero.
    :param feature_file: The file containing a list of features.
    :return: a dict of feature_name and its index.
    """
    feature_list = read_feature_list(feature_file)
    n = len(feature_list)
    return dict(zip(feature_list, range(n)))


def read_json_file(file_path: str):
    """ Load a json file from a path.

    :param file_path: Path string to json file.
    :return: dict. The decoded json object.

    Raises IOError if path does not exist.
    Raises ValueError if load fails.
    """

    if not tf.io.gfile.exists(file_path):
        raise IOError(f"Path '{file_path}' does not exist.")
    try:
        with tf.io.gfile.GFile(file_path) as json_file:
            return json.load(json_file)
    except Exception as e:
        raise ValueError(f"Error '{e}' while loading file '{file_path}'.")


def str2bool(v):
    """
    handle argparse can't parse boolean well.
    ref: https://stackoverflow.com/questions/15008758/parsing-boolean-values-with-argparse/36031646
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() == 'true'
    else:
        raise argparse.ArgumentTypeError('Boolean or string value expected.')


def copy_files(input_files, output_dir):
    """
    Copy a list of files to the output directory.
    The destination files will be overwritten.
    :param input_files: a list of files
    :param output_dir: output directory
    :return: the list of copied files
    """

    logger.info("Copy files to local")
    if not tf.io.gfile.exists(output_dir):
        tf.io.gfile.mkdir(output_dir)
    start_time = time.time()
    copied_files = []
    for f in input_files:
        fname = os.path.join(output_dir, os.path.basename(f))
        tf.io.gfile.copy(f, fname, overwrite=True)
        copied_files.append(fname)
    logger.info("Files copied to Local: {}".format(copied_files))
    logger.info("--- %s seconds ---" % (time.time() - start_time))
    return copied_files


def namedtuple_with_defaults(typename, field_names, defaults=()):
    """
    Namedtuple with default values is supported since 3.7, wrap it to be compatible with version <= 3.6
    :param typename: the type name of the namedtuple
    :param field_names: the field names of the namedtuple
    :param defaults: the default values of the namedtuple
    :return: namedtuple with defaults
    """
    T = collections.namedtuple(typename, field_names)
    T.__new__.__defaults__ = (None,) * len(T._fields)
    if isinstance(defaults, collections.Mapping):
        prototype = T(**defaults)
    else:
        prototype = T(*defaults)
    T.__new__.__defaults__ = tuple(prototype)
    return T


def batched_write_avro(records: Iterator, output_file, schema, write_frequency=1000, batch_size=1024):
    """ For the first block, the file needs to be open in âwâ mode, while the
        rest of the blocks needs the âaâ mode. This restriction makes it
        necessary to open the files at least twice, one for the first block,
        one for the remaining. So itâs not possible to put them into the
        while loop within a file context.  """
    f = None
    t0 = time.time()
    n_batch = 0
    logger.info(f"Writing to {output_file} with batch size of {batch_size}.")
    try:
        for batch in _chunked_iterator(records, batch_size):
            if n_batch == 0:
                with tf.io.gfile.GFile(output_file, 'wb') as f0:  # Create the file in 'w' mode
                    f0.seekable = lambda: False
                    try_write_avro_blocks(f0, schema, batch, None, create_error_message(n_batch, output_file))
                f = tf.io.gfile.GFile(output_file, 'ab+')  # reopen the file in 'a' mode for later writes
                f.seekable = f.readable = lambda: True
                f.seek(0, 2)  # seek to the end of the file, 0 is offset, 2 means the end of file
            else:
                try_write_avro_blocks(f, schema, batch, None, create_error_message(n_batch, output_file))
            n_batch += 1
            if n_batch % write_frequency == 0:
                delta_time = time.time() - t0
                logger.info(f"nbatch = {n_batch}, deltaT = {delta_time:0.2f} seconds, speed = {n_batch / delta_time :0.2f} batches/sec")
        logger.info(f"Finished writing to {output_file}.")
    finally:
        f and f.close()


def _chunked_iterator(iterator: Iterator, chuck_size):
    while True:
        chunk_it = itertools.islice(iterator, chuck_size)
        try:
            first_el = next(chunk_it)
            yield itertools.chain((first_el,), chunk_it)
        except StopIteration:
            return


def create_error_message(n_batch, output_file) -> str:
    return f'An error occurred while writing batch #{n_batch} to path {output_file}'


def dataset_reader(dataset=None, iterator=None):
    """Create an python iterator/generator with the TF dataset or interator"""
    assert dataset and not iterator or iterator, "Either dataset or iterator_and_fetches are needed."
    logger.info("Dataset initialized")
    # Create TF iterator
    iterator = iterator or tf.compat.v1.data.make_initializable_iterator(dataset)
    # Iterate through TF dataset in a throttled manner
    # (Forking after the TensorFlow runtime creates internal threads is unsafe, use config provided in this
    # link -
    # https://github.com/tensorflow/tensorflow/issues/14442)
    with tf.compat.v1.Session(config=tf.compat.v1.ConfigProto(use_per_session_threads=True)) as sess:
        sess.run(iterator.initializer)
        while True:
            try:
                # Extract and process raw entity data
                yield sess.run(iterator.get_next())
            except tf.errors.OutOfRangeError:
                break


def get_inference_output_avro_schema(metadata, has_label, has_logits_per_coordinate, schema_params, has_weight=False):
    fields = [{'name': schema_params.sample_id, 'type': 'long'}, {'name': schema_params.prediction_score, 'type': 'float'}]
    if has_label:
        fields.append({'name': schema_params.label, 'type': 'int'})
    if has_weight or metadata.get(schema_params.sample_weight) is not None:
        fields.append({'name': schema_params.sample_weight, 'type': 'float'})
    if has_logits_per_coordinate:
        fields.append({'name': schema_params.prediction_score_per_coordinate, 'type': 'float'})
    return {'name': 'validation_result', 'type': 'record', 'fields': fields}
