# coding=utf-8
# Copyright 2022 The Pax Authors.
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

import functools
import math
import os

import jax
from paxml import base_experiment
from paxml import seqio_input
from paxml.contrib.gpu.scripts_gpu import tfds_lambada
from paxml.contrib.gpu.scripts_gpu import tfds_pile
from paxml.tasks.lm.params.c4 import TaskRegistry
from praxis import base_input
from praxis import pax_fiddle
import seqio
import t5.data
from t5.data import preprocessors as t5_preprocessors
import tensorflow as tf

### for now, make sure to set 'VOCAB_PATH' as an environment variable in your bash script
vocab_path = os.getenv('VOCAB_PATH', None)
assert (
    vocab_path is not None and vocab_path != ''
), 'Make sure to set VOCAB_PATH as an environment variable'
vocab = t5.data.SentencePieceVocabulary(vocab_path)

GPT_OUTPUT_FEATURES_LM = {
    'targets': t5.data.Feature(vocabulary=vocab, add_eos=True)
}

TaskRegistry.add_versioned_tfds_task(
    'the_pile_lm',
    versions=['1.0.0'],
    pinned_version='1.0.0',
    tfds_name='ThePile',
    tfds_data_dir=None,
    preprocessors=[
        functools.partial(
            t5_preprocessors.rekey, key_map={'inputs': None, 'targets': 'text'}
        ),
        seqio.preprocessors.tokenize,
        t5_preprocessors.reduce_concat_tokens,
        t5_preprocessors.split_tokens_to_targets_length,
        seqio.preprocessors.append_eos_after_trim,
    ],
    output_features=GPT_OUTPUT_FEATURES_LM,
    metric_fns=[],
    shuffle_buffer_size=100000,
)

LAMBADA_OUTPUT_FEATURES = {
    'inputs': seqio.Feature(vocabulary=vocab, add_eos=True, required=False),
    'targets': seqio.Feature(vocabulary=vocab, add_eos=True),
}

TaskRegistry.add_versioned_tfds_task(
    'lambada_eval',
    versions=['1.0.0'],
    pinned_version='1.0.0',
    tfds_name='MyLambada',
    tfds_data_dir=None,
    preprocessors=[
        seqio.preprocessors.tokenize,
    ],
    output_features=LAMBADA_OUTPUT_FEATURES,
    metric_fns=[],
    shuffle_buffer_size=None,
)

BOOLQ_OUTPUT_FEATURES = {
    'inputs': seqio.Feature(vocabulary=vocab, add_eos=False),
    'targets': seqio.Feature(vocabulary=vocab, add_eos=False),
}

def concatenate_passage_and_question(dataset):
  @seqio.map_over_dataset
  def _my_fn(x):
    inputs = x['passage'] + '\nquestion: ' + x['question'] + '?\nanswer:'
    @tf.function
    def yesno(label):
      return tf.cond(
        label > 0, true_fn=lambda: 'yes', false_fn=lambda: 'no'
      )
    return {'inputs': inputs, 'targets': yesno(x['label'])}

  return _my_fn(dataset)

TaskRegistry.add_versioned_tfds_task(
    'boolq_eval',
    versions=['1.0.2'],
    pinned_version='1.0.2',
    tfds_name='super_glue/boolq',
    tfds_data_dir=None,
    preprocessors=[
        concatenate_passage_and_question,
        seqio.preprocessors.tokenize,
    ],
    output_features=BOOLQ_OUTPUT_FEATURES,
    metric_fns=[],
    shuffle_buffer_size=None,
)

class PileUnsupervisedDataset(base_experiment.BaseExperiment):
  """Used for training Baseline ULM."""

  PERCORE_BATCH_SIZE = 1
  MAX_SEQ_LEN = 2048
  TRAIN_INPUT_RANDOM_SEED = None

  def _dataset_common(
      self, is_training
  ) -> pax_fiddle.Config[base_input.BaseInput]:
    num_local_devices = jax.local_device_count()
    if self.PERCORE_BATCH_SIZE >= 1:
      batch_size_per_process = int(self.PERCORE_BATCH_SIZE * num_local_devices)
      num_infeed_hosts = jax.process_count()
    else:
      global_batch_size = int(
          self.PERCORE_BATCH_SIZE * num_local_devices * jax.process_count()
      )
      batch_size_per_process = math.ceil(
          self.PERCORE_BATCH_SIZE * num_local_devices
      )
      num_infeed_hosts = global_batch_size // batch_size_per_process

    p = pax_fiddle.Config(
        seqio_input.SeqIOInput,
        name='PileTrain' if is_training else 'PileValidation',
        mixture_name='the_pile_lm',
        split_name='train' if is_training else 'validation',
        task_feature_lengths={'targets': self.MAX_SEQ_LEN},
        use_cached=False,
        repeat=True if is_training else False,
        feature_converter=seqio_input.LanguageModelFeatures(
            pack=(self.PACKED_INPUT if is_training else False),
            use_custom_packing_ops=False,
        ),
        is_training=is_training,
        input_random_seed=(
            self.TRAIN_INPUT_RANDOM_SEED if is_training else 4321
        ),
        batch_size=batch_size_per_process,
        num_infeed_hosts=num_infeed_hosts,
        reset_for_eval=False if is_training else True,
        shuffle=True,
    )
    return p

  def datasets(self) -> list[pax_fiddle.Config[base_input.BaseInput]]:
    """Returns a list of dataset parameters."""
    return [
        self._dataset_common(is_training=True),
        self._dataset_common(is_training=False),
    ]


class LambadaDataset(base_experiment.BaseExperiment):
  """Used for zero-shot eval."""

  PERCORE_BATCH_SIZE: int = 1
  MAX_SEQ_LEN: int = 2048

  def _dataset_common(
      self, is_training
  ) -> pax_fiddle.Config[base_input.BaseInput]:
    num_local_devices = jax.local_device_count()
    if self.PERCORE_BATCH_SIZE >= 1:
      batch_size_per_process = int(self.PERCORE_BATCH_SIZE * num_local_devices)
      num_infeed_hosts = jax.process_count()
    else:
      global_batch_size = int(
          self.PERCORE_BATCH_SIZE * num_local_devices * jax.process_count()
      )
      # batch_size_per_process = num_local_devices
      batch_size_per_process = int(self.PERCORE_BATCH_SIZE * num_local_devices)
      num_infeed_hosts = global_batch_size // batch_size_per_process
    p = pax_fiddle.Config(
        seqio_input.SeqIOInput,
        name='LambadaValidation',
        mixture_name='lambada_eval',
        split_name='test',
        ## 'targets' is only one word
        task_feature_lengths={'targets': 64, 'inputs': self.MAX_SEQ_LEN - 64},
        use_cached=False,
        repeat=True if is_training else False,
        feature_converter=seqio_input.LanguageModelFeatures(
            pack=False,
            use_custom_packing_ops=False,
            weights_on_targets_only=True,
        ),
        is_training=is_training,
        input_random_seed=4321,
        batch_size=batch_size_per_process,
        num_infeed_hosts=num_infeed_hosts,
        reset_for_eval=False if is_training else True,
        shuffle=False,
        eval_loop_num_batches=-1,
    )
    return p

  def datasets(self) -> list[pax_fiddle.Config[base_input.BaseInput]]:
    """Returns a list of dataset parameters."""
    return [self._dataset_common(is_training=False)]


class BoolQDataset(base_experiment.BaseExperiment):
  """Used for zero-shot eval."""

  PERCORE_BATCH_SIZE: int = 8
  MAX_SEQ_LEN: int = 4096
  BOS_ID: int = 1
  EOS_ID: int = 2

  s = seqio.SentencePieceVocabulary(vocab_path)
  TRUE_TOKEN: int = s.encode('yes')
  FALSE_TOKEN: int = s.encode('no')

  def _dataset_common(
      self, is_training
  ) -> pax_fiddle.Config[base_input.BaseInput]:
    num_local_devices = jax.local_device_count()
    if self.PERCORE_BATCH_SIZE >= 1:
      batch_size_per_process = int(self.PERCORE_BATCH_SIZE * num_local_devices)
      num_infeed_hosts = jax.process_count()
    else:
      global_batch_size = int(
          self.PERCORE_BATCH_SIZE * num_local_devices * jax.process_count()
      )
      # batch_size_per_process = num_local_devices
      batch_size_per_process = int(self.PERCORE_BATCH_SIZE * num_local_devices)
      num_infeed_hosts = global_batch_size // batch_size_per_process
    p = pax_fiddle.Config(
        seqio_input.SeqIOInput,
        name='BoolQValidation',
        mixture_name='boolq_eval',
        split_name='validation',
        ## 'targets' is only one word
        task_feature_lengths={'targets': 64, 'inputs': self.MAX_SEQ_LEN - 64},
        use_cached=False,
        repeat=True if is_training else False,
        feature_converter=seqio_input.LanguageModelFeatures(
            pack=False,
            use_custom_packing_ops=False,
            weights_on_targets_only=True,
            bos_id=self.BOS_ID,
            eos_id=self.EOS_ID
        ),
        is_training=is_training,
        input_random_seed=4321,
        batch_size=batch_size_per_process,
        num_infeed_hosts=num_infeed_hosts,
        reset_for_eval=False if is_training else True,
        shuffle=False,
        eval_loop_num_batches=-1
    )
    return p

  def datasets(self) -> list[pax_fiddle.Config[base_input.BaseInput]]:
    """Returns a list of dataset parameters."""
    return [self._dataset_common(is_training=False)]

