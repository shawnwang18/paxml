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

"""Module with the stochastic gradient function classes."""

from __future__ import annotations

import abc
from collections.abc import Callable
import math
from typing import Any

from flax import struct
import jax
from jax import numpy as jnp
import optax
from paxml.ghostnorm import base as ghostnorm_base
from praxis import base_hyperparams
from praxis import base_layer
from praxis import py_utils
from praxis import pytypes

JTensor = pytypes.JTensor
NestedJTensor = pytypes.NestedJTensor
NestedMap = py_utils.NestedMap
PRNGKey = pytypes.PRNGKey
PARAMS = base_layer.PARAMS
ScalarFloat = pytypes.ScalarFloat
PMAP_PARALLEL_AXIS_NAME = base_layer.PMAP_PARALLEL_AXIS_NAME


@struct.dataclass
class GradAuxInfo:
  aux_info: Any
  loss_weight: float | ScalarFloat = 1.0


@struct.dataclass
class DPGradAuxInfo(GradAuxInfo):
  dp_aux_info: Any = None


class BaseStochasticGradient(
    base_hyperparams.FiddleBaseParameterizable, metaclass=abc.ABCMeta
):
  """Stochastic gradient function."""

  def process_aux_info(self, aux_info: GradAuxInfo) -> GradAuxInfo:
    """Processes auxiliary info returned by `grad_fn`.

    Args:
      aux_info: Auxiliary info to be processed.

    Returns:
      Processed version of auxiliary info.
    """
    return aux_info

  @abc.abstractmethod
  def grad_fn(
      self,
      loss_fn: Callable[..., Any],
      mdl_vars_grad: NestedJTensor,
      mdl_vars_nograd_and_inputs: tuple[NestedJTensor, NestedMap],
      prng_key: PRNGKey,
  ) -> tuple[tuple[JTensor, GradAuxInfo], NestedJTensor]:
    """Main gradients function.

    Intended to accept a loss function, model parameters, input data, and
    a pseudorandom key, and return the loss (possibly with auxiliary info)
    and the gradient of the loss. Based on `jax.value_and_grad`.

    Args:
      loss_fn: Loss function.
      mdl_vars_grad: Model variables for which to compute gradient.
      mdl_vars_nograd_and_inputs: Tuple containing model variables for which
        gradients should not be computed, and input examples on which to call
        `loss_fn`.
      prng_key: A pseudorandom key.

    Returns:
      A tuple ((loss, auxiliary info), gradients).
    """


class StandardGradient(BaseStochasticGradient):
  """Standard gradient function."""

  def grad_fn(
      self,
      loss_fn: Callable[..., tuple[JTensor, GradAuxInfo]],
      mdl_vars_grad: NestedJTensor,
      mdl_vars_nograd_and_inputs: tuple[NestedJTensor, NestedMap],
      prng_key: PRNGKey,
  ) -> tuple[tuple[JTensor, GradAuxInfo], NestedJTensor]:
    grad_fn = jax.value_and_grad(loss_fn, has_aux=True, allow_int=True)
    (values, aux), grads = grad_fn(
        mdl_vars_grad, mdl_vars_nograd_and_inputs, prng_key
    )
    aux = self.process_aux_info(aux)
    return (values, aux), grads


def _clipping_bound_scaling(
    use_loss_weight_scaling: bool = False,
    loss_weight: float | None = None,
) -> float:
  """Parse use_loss_weight_scaling to get the right scaling of clipping bound.

  Args:
    use_loss_weight_scaling: Whether to use loss_weight to scale the clipping
      bound. If set to False, use 1. / jax.device_count() under pmap, or 1.0
      otherwise for scaling. If set to True, use loss_weight.
    loss_weight: The loss_weight used to scale the gradients.

  Returns:
    The scaling to apply to the clipping bound in float.
  """
  if use_loss_weight_scaling:
    if loss_weight is None:
      raise ValueError(
          'loss_weight must be set when use_loss_weight_scaling is set to True.'
      )
    else:
      return loss_weight
  else:
    if base_layer.is_running_under_pmap():
      # TODO(b/315502275): The following line assumes the loss aggregator being
      # used takes a weighted average of the loss. For example, the default
      # loss aggregator /third_party/py/paxml/base_metrics.py;l=398-407. Should
      # add support for customized scaling factor under pmap.
      return 1.0 / jax.device_count()
    else:
      return 1.0


class PercoreClippedDpSgdGradient(BaseStochasticGradient):
  """DP-SGD stochastic gradient function using per-core clipping.

  Differentially private stochastic gradient using per-core clipping, whose
  running time matches non-private baseline.

  NOTE: this class assumes you are running under Pmap Partitioning
  (ICI_MESH_SHAPE = None).  The behavior is undefined under Pjit Partitioning
  (ICI_MESH_SHAPE != None).

  Experimental results with zero noise multiplier:
    Non-private baseline: http://tb/4569190445426541226
    PercoreClippedGradient: http://tb/1885364525575923265
    MicrobatchDpSgdStochasticGradient: http://tb/2683981470965501622

  Attributes:
    l2_norm_clip: The L2 clipping bound used to clip per-core gradients. If set
      to None, no clipping is applied.
    noise_multiplier: The noise multiplier used to decide the noise scale. See
      Section 5.3.2 of https://arxiv.org/pdf/2303.00654.pdf for more details.
    use_loss_weight_scaling: Whether to use aux.loss_weight to scale the
      clipping bound. If set to False, use 1. / jax.device_count() under pmap,
      or 1.0 otherwise for scaling. If set to True, use aux.loss_weight. Note
      for models like CtcModel, loss_weight is different across TPU cores, which
      can cause unexpected behavior for differential privacy so we only allow
      this option to be turned on for empirical privacy with noise_multiplier =
      0.0.
    normalize_gradients: Whether to apply Gradient Normalization as implemented
      in eqn 3 of https://arxiv.org/abs/2204.13650 to reduce the dependence
      between clipping value and learning rate. Note that normalization is only
      applied post-clipping.
    adaptive_clipping_method: Choose an adaptive method to set the clipping
      bound. Default to None when clipping bound is given by l2_norm_clip.
      Currently supported method includes: 'min': use the minimum per-core
      gradient as the clipping bound.
  """

  l2_norm_clip: float | None = 0.0
  noise_multiplier: float = 0.0
  use_loss_weight_scaling: bool = False
  normalize_gradients: bool = False
  adaptive_clipping_method: str | None = None

  def _clip_gradients(
      self, grads: NestedMap, l2_norm_clip: float = 1.0
  ) -> tuple[NestedMap, jax.Array, Any]:
    assert (
        self.adaptive_clipping_method is not None or self.l2_norm_clip > 0.0
    ), (
        f'Clipping bound must be either adaptive or positive. {l2_norm_clip} is'
        ' provided.'
    )

    # Clip the per-core mean gradient.
    grads_flat, grads_treedef = jax.tree_util.tree_flatten(grads)
    global_grad_norm = optax.global_norm(grads_flat)
    divisor = jnp.maximum(global_grad_norm / l2_norm_clip, 1.0)
    num_clipped = jnp.greater(divisor, 1.0)
    clipped_flat = [g / divisor for g in grads_flat]
    clipped = jax.tree_util.tree_unflatten(grads_treedef, clipped_flat)

    return clipped, num_clipped, global_grad_norm

  def _add_noise(  # pytype: disable=annotation-type-mismatch  # jax-ndarray
      self,
      grads: NestedMap,
      noise_stddev: float,
      clipping_bound_scaling: float,
      prng_key: PRNGKey = None,
  ) -> NestedMap:
    prng_keys = jax.random.split(
        prng_key, len(jax.tree_util.tree_leaves(grads))
    )
    prng_tree = jax.tree_util.tree_unflatten(
        jax.tree_util.tree_structure(grads), prng_keys
    )

    if base_layer.is_running_under_pmap():
      # Note: Because `l2_norm_clip` is scaled with `clipping_bound_scaling`, we
      # need to scale the noise_std accordingly to compensate for the change.
      # Moreover, each device adds independent Gaussian noises, and then the
      # noisy gradients are added with `psum``. Because the sum of num_devices
      # copies of independent Gaussian noises is equivalent to a single Gaussian
      # with std scaled by `sqrt(num_devices)``, we need to further scale the
      # noise_std on each device to correct this.
      noise_stddev *= clipping_bound_scaling * jnp.sqrt(clipping_bound_scaling)

    def _add_noise_to_array(x, prng):
      return x + noise_stddev * jax.random.normal(prng, shape=x.shape)

    final_grads = jax.tree.map(_add_noise_to_array, grads, prng_tree)
    return final_grads

  def grad_fn(
      self,
      loss_fn: Callable[..., tuple[JTensor, GradAuxInfo]],
      mdl_vars_grad: NestedJTensor,
      mdl_vars_nograd_and_inputs: tuple[NestedJTensor, NestedMap],
      prng_key: PRNGKey,
  ) -> tuple[tuple[JTensor, GradAuxInfo], NestedJTensor]:
    if not base_layer.is_running_under_pmap():
      # TODO(b/310986925): Fix and test this implementation under jit.
      raise ValueError(
          'PercoreClippedDpSgdGradient is only supported when running under'
          ' Pmap Partitioning.  Please set ICI_MESH_SHAPE = None.'
      )
    assert not (
        self.use_loss_weight_scaling and self.noise_multiplier != 0.0
    ), 'Noise multiplier must be 0.0 when use_loss_weight_scaling is True.'

    # Obtain the per-core mean gradient.
    grad_fn = jax.value_and_grad(loss_fn, has_aux=True, allow_int=True)
    (values, aux), grads = grad_fn(
        mdl_vars_grad, mdl_vars_nograd_and_inputs, prng_key
    )
    aux = self.process_aux_info(aux)

    clipping_bound_scaling = _clipping_bound_scaling(
        self.use_loss_weight_scaling, aux.loss_weight
    )

    if self.adaptive_clipping_method == 'min':
      grads_norm = optax.global_norm(grads)
      self.l2_norm_clip = (
          jax.lax.pmin(grads_norm, axis_name=PMAP_PARALLEL_AXIS_NAME)
          / clipping_bound_scaling
      )
    elif self.adaptive_clipping_method is not None:
      raise ValueError(
          'Unsupported adaptive clipping method:'
          f' {self.adaptive_clipping_method}'
      )

    if self.l2_norm_clip is not None:
      grads, num_clipped, grad_norm = self._clip_gradients(
          grads, clipping_bound_scaling * self.l2_norm_clip
      )

    if self.normalize_gradients:
      grads = jax.tree.map(lambda x: x / self.l2_norm_clip, grads)

    # Optimization if using this class only for clipping (e.g., with DP-MF)
    if self.noise_multiplier > 0.0:
      noise_stddev = self.noise_multiplier * (
          1.0 if self.normalize_gradients else self.l2_norm_clip
      )
      grads = self._add_noise(
          grads, noise_stddev, clipping_bound_scaling, prng_key
      )

    return (
        values,
        DPGradAuxInfo(
            dp_aux_info={
                'frac_clipped': num_clipped,
                'per_core_grad_norm': grad_norm,
            },
            aux_info=aux.aux_info,
            loss_weight=aux.loss_weight,
        ),
    ), grads


class DpSgdStochasticGradient(BaseStochasticGradient):
  """DP-SGD stochastic gradient function.

  If using this function with Pjit partitioning, since the implementation here
  performs special processing across the batch dimension, inefficient code may
  be generated if the model attempts to shard intermediate tensors across the
  same resources as the batch. It is recommended to ensure that your model does
  not explicitly lay out tensors in this manner if using this SGF.

  Attributes:
    l2_norm_clip: The L2 clipping bound used to clip per-core gradients.
    noise_multiplier: The noise multiplier used to decide the noise scale. See
      Section 5.3.2 of https://arxiv.org/pdf/2303.00654.pdf for more details.
    use_loss_weight_scaling: Whether to use aux.loss_weight to scale the
      clipping bound. If set to False, use 1. / jax.device_count() under pmap,
      or 1.0 otherwise for scaling. If set to True, use aux.loss_weight. Note
      for models like CtcModel, loss_weight is different across TPU cores, which
      can cause unexpected behavior for differential privacy so we only allow
      this option to be turned on for empirical privacy with noise_multiplier =
      0.0.
    normalize_gradients: Whether to apply Gradient Normalization as implemented
      in eqn 3 of https://arxiv.org/abs/2204.13650 to reduce the dependence
      between clipping value and learning rate. Note that normalization is only
      applied post-clipping.
    inner_batch_size: Number of examples to process at one time. If set to
      `None`, this will be set to batch size as determined by the 0th element of
      the shape of the input. This may be useful if a large batch size is
      desired (for example, for better privacy-utility tradeoffs), but we cannot
      fit all of the per-example gradients in memory. When set, the code
      computes `inner_batch_size` per-example gradients at a time, accumulating
      the total clipped gradient as it goes. Note that the setting of
      `inner_batch_size` has no effect on the value of the final gradients--it
      affects only the feasibility and speed of the computation. NOTE: the
      meaning of a non-None value for `inner_batch_size` _will change_ when
      running under jit vs pmap. When running under pmap, inner_batch_size is
      applied in a per-device manner, so inner_batch_size identical to the
      per-device batch size is identical to the default behavior of None. When
      running under jit, however, `inner_batch_size` is applied to the _global_
      batch, so inner_batch_size identical to the global batch size matches the
      default behavior, and a lower value introduces virtual batching.
  """

  l2_norm_clip: float = 0.0
  noise_multiplier: float = 0.0
  use_loss_weight_scaling: bool = False
  normalize_gradients: bool = False
  inner_batch_size: int | None = None

  def _clip_and_mean_gradients(
      self,
      grads: NestedMap,
      l2_norm_clip: float = 1.0,
      microbatch_size: int = 1,
  ) -> tuple[NestedMap, GradAuxInfo, int]:
    def _reshape_and_mean(g):
      return jnp.mean(
          jnp.reshape(g, [-1, microbatch_size, *g.shape[1:]]), axis=1
      )

    grads = jax.tree.map(_reshape_and_mean, grads)
    grads_flat, grads_treedef = jax.tree_util.tree_flatten(grads)
    sum_clipped, num_clipped = optax.per_example_global_norm_clip(
        grads=grads_flat, l2_norm_clip=l2_norm_clip
    )
    sum_grads = jax.tree_util.tree_unflatten(grads_treedef, sum_clipped)

    # Normalize gradients across all examples.
    batch_size = grads_flat[0].shape[0]
    clipped_grads_mean = jax.tree.map(lambda x: x / batch_size, sum_grads)
    frac_clipped = num_clipped / batch_size
    dp_aux_info = {'frac_clipped': frac_clipped}

    return clipped_grads_mean, dp_aux_info, batch_size  # pytype: disable=bad-return-type  # jax-types

  def _add_noise(  # pytype: disable=annotation-type-mismatch  # jax-ndarray
      self,
      grads: NestedMap,
      noise_stddev: float,
      clipping_bound_scaling: float,
      prng_key: PRNGKey = None,
  ) -> NestedMap:
    prng_keys = jax.random.split(
        prng_key, len(jax.tree_util.tree_leaves(grads))
    )
    prng_tree = jax.tree_util.tree_unflatten(
        jax.tree_util.tree_structure(grads), prng_keys
    )

    if base_layer.is_running_under_pmap():
      # Note: Because `l2_norm_clip` is scaled with `clipping_bound_scaling`, we
      # need to scale the noise_std accordingly to compensate for the change.
      # Moreover, each device adds independent Gaussian noises, and then the
      # noisy gradients are added with `psum``. Because the sum of num_devices
      # copies of independent Gaussian noises is equivalent to a single Gaussian
      # with std scaled by `sqrt(num_devices)``, we need to further scale the
      # noise_std on each device to correct this.
      noise_stddev *= clipping_bound_scaling * jnp.sqrt(clipping_bound_scaling)

    def _add_noise_to_array(x, prng):
      return x + noise_stddev * jax.random.normal(prng, shape=x.shape)

    final_grads = jax.tree.map(_add_noise_to_array, grads, prng_tree)
    return final_grads

  def _prepare_inputs(self, inputs):
    """Reshape inputs to prepare for vmap to find per-example gradients."""
    return jax.tree.map(jax.tree_util.Partial(jnp.expand_dims, axis=1), inputs)

  def process_aux_info(self, aux_info: GradAuxInfo) -> GradAuxInfo:
    aux_info = jax.tree.map(jax.tree_util.Partial(jnp.mean, axis=0), aux_info)
    return aux_info

  def grad_fn(
      self,
      loss_fn: Callable[..., Any],
      mdl_vars_grad: NestedJTensor,
      mdl_vars_nograd_and_inputs: tuple[NestedJTensor, NestedMap],
      prng_key: PRNGKey,
  ) -> tuple[tuple[JTensor, DPGradAuxInfo], NestedJTensor]:
    assert (
        self.l2_norm_clip > 0.0
    ), f'Clipping bound must be positive. {self.l2_norm_clip} is provided.'
    assert not (
        self.use_loss_weight_scaling and self.noise_multiplier != 0.0
    ), 'Noise multiplier must be 0.0 when use_loss_weight_scaling is True.'

    mdl_vars_nograd, inputs = mdl_vars_nograd_and_inputs
    inputs = self._prepare_inputs(inputs)

    # Get batch size.
    input_leaf = jax.tree_util.tree_leaves(inputs)[0]
    batch_size = input_leaf.shape[0]
    microbatch_size = input_leaf.shape[1]

    if self.inner_batch_size is None:
      inner_batch_size = batch_size * microbatch_size
    else:
      inner_batch_size = self.inner_batch_size

    if batch_size * microbatch_size % inner_batch_size != 0:
      raise ValueError(
          '`batch_size * microbatch_size` must be divisible by'
          ' `inner_batch_size`.'
      )

    batch_splits = math.gcd(batch_size, inner_batch_size)
    microbatch_splits = inner_batch_size // batch_splits

    num_iters = batch_size // batch_splits
    inner_prng_keys = jax.random.split(prng_key, num_iters)

    grad_fn = jax.vmap(
        jax.value_and_grad(loss_fn, has_aux=True, allow_int=True),
        in_axes=(None, (None, 0), None),
        out_axes=0,
    )

    def reshape_batch(x):
      return jnp.reshape(
          x,
          [
              # We leave the batch-dimension as the zeroth axis to give the
              # compiler the best chance at finding an effective sharding for
              # processing the inner batches in the global jit programming
              # paradigm.
              inner_batch_size,
              -1,
              microbatch_size // microbatch_splits,
              *x.shape[2:],
          ],
      )

    inputs = jax.tree.map(reshape_batch, inputs)

    def _process_inner_batch(index: int) -> Any:
      """Computes mean clipped gradient for inner batch specified by index."""
      new_inputs = jax.tree.map(lambda x: x[:, index, ...], inputs)

      # Compute loss and gradients.
      (values, aux), grads = grad_fn(
          mdl_vars_grad, (mdl_vars_nograd, new_inputs), inner_prng_keys[index]
      )
      clipping_bound_scaling = _clipping_bound_scaling(
          self.use_loss_weight_scaling, loss_weight=aux.loss_weight
      )

      # Clip and aggregate gradients.
      grads, dp_aux_info, _ = self._clip_and_mean_gradients(
          grads,
          clipping_bound_scaling * self.l2_norm_clip,
          microbatch_splits,
      )
      # Aggregate values and aux.
      values = jax.tree.map(jax.tree_util.Partial(jnp.mean, axis=0), values)
      aux = self.process_aux_info(aux)
      return (
          values,
          DPGradAuxInfo(
              dp_aux_info=dp_aux_info,
              aux_info=aux.aux_info,
              loss_weight=aux.loss_weight,
          ),
          grads,
      )

    def _loop_process_inner_batch(index: int, val: Any) -> Any:
      """Wrapper for _process_inner_batch suitable for fori_loop."""
      cur_values, cur_aux, cur_grads = val
      values, aux, grads = _process_inner_batch(index)

      new_values = jax.tree.map(jnp.add, cur_values, values)
      new_aux = jax.tree.map(jnp.add, cur_aux, aux)
      new_grads = jax.tree.map(jnp.add, cur_grads, grads)
      return (new_values, new_aux, new_grads)

    # Loop over inner batches, summing the results together.
    # We have to do one iteration first to get the correct shape of the return
    # values.
    values, aux, grads = jax.lax.fori_loop(
        1, num_iters, _loop_process_inner_batch, _process_inner_batch(0)
    )

    # Normalize results by number of inner batches.
    values, aux, grads = jax.tree.map(
        jax.tree_util.Partial(jnp.multiply, 1.0 / num_iters),
        (values, aux, grads),
    )

    # Add noise to normalized gradients.
    if self.normalize_gradients:
      grads = jax.tree.map(lambda x: x / self.l2_norm_clip, grads)

    # Optimization if using this class only for clipping (e.g., with DP-MF)
    if self.noise_multiplier > 0.0:
      noise_stddev = (
          self.noise_multiplier
          / batch_size
          * (1.0 if self.normalize_gradients else self.l2_norm_clip)
      )
      grads = self._add_noise(
          grads,
          noise_stddev,
          _clipping_bound_scaling(self.use_loss_weight_scaling),
          prng_key,
      )
    return (values, aux), grads


class MicrobatchDpSgdStochasticGradient(DpSgdStochasticGradient):
  """DP-SGD stochastic gradient function with microbatch.

  Attributes:
    microbatch_size: The number of samples in one micro-batch. See Section 5.6
      of https://arxiv.org/pdf/2303.00654.pdf for more details.
  """

  microbatch_size: int = 1

  def _prepare_inputs(self, inputs):
    return jax.tree.map(self._prepare_for_microbatching, inputs)

  def _prepare_for_microbatching(self, tensor: JTensor) -> JTensor:
    """Reshapes tensor for vmap with microbatch size support.

    Args:
      tensor: the input tensor, of shape `(batch_size, ...)`, where the
        batch_size should be dividable by the microbatch_size.

    Returns:
      The input tensor reshaped into shape `(batch_size//microbatch_size,
      microbatch_size, ...)`.
    """
    batch_size = tensor.shape[0]
    microbatch_size = self.microbatch_size
    return tensor.reshape(
        (batch_size // microbatch_size, microbatch_size, *tensor.shape[1:])
    )


class AugMulDpSgdStochasticGradient(MicrobatchDpSgdStochasticGradient):
  """DP-SGD with Augmentation Multiplicity.

  Augmentation multiplicity generates multiple different augmentations for each
  training example, and do the l2-norm clipping on the average gradient for
  all the augmentations of each training example.

  If the augmentation happens at the data pipeline, the
  MicrobatchDpSgdStochasticGradient can be used directly. This subclass is for
  the special case where the augmentation happens inside the model call (e.g.
  the current Bert implementation). This class simply makes multiple identical
  copies of each input example, and let the model call handle the augmentation.
  """

  def _prepare_for_microbatching(self, tensor: JTensor) -> JTensor:
    shape = tensor.shape
    num_repeat = self.microbatch_size
    return jnp.repeat(tensor, num_repeat, axis=0).reshape(
        (shape[0], num_repeat, *shape[1:])
    )


class PerLayerDpSgdStochasticGradient(DpSgdStochasticGradient):
  """DP-SGD stochastic gradient function with per-layer clipping.

  Attributes:
    use_uniform: If `True` uses the uniform variant of per-layer clipping.
      Otherwise, uses the scaled variant.
  """

  use_uniform: bool = True

  def _clip_and_mean_gradients(
      self,
      grads: NestedMap,
      l2_norm_clip: float = 1.0,
      microbatch_size: int = 1,
  ) -> tuple[NestedMap, GradAuxInfo, int]:
    def _reshape_and_mean(g):
      return jnp.mean(
          jnp.reshape(g, [-1, microbatch_size, *g.shape[1:]]), axis=1
      )

    grads = jax.tree.map(_reshape_and_mean, grads)
    grads_flat, grads_treedef = jax.tree_flatten(grads)
    sum_grads_flat, num_clipped_flat = optax.per_example_layer_norm_clip(
        grads=grads_flat,
        global_l2_norm_clip=l2_norm_clip,
        uniform=self.use_uniform,
    )

    sum_grads = jax.tree_unflatten(grads_treedef, sum_grads_flat)
    num_clipped = jax.tree_unflatten(grads_treedef, num_clipped_flat)

    # Compute per-layer grad norms.
    def map_layer_norm(grads_list):
      return [jnp.linalg.norm(g, ord=None, axis=None) for g in grads_list]

    per_example_layer_grad_norms = jax.vmap(map_layer_norm)(grads_flat)
    sum_layer_grad_norms = [
        per_example_layer_grad_norms[i].sum(0)
        for i in range(len(per_example_layer_grad_norms))
    ]
    sum_layer_grad_norms = jax.tree_unflatten(
        grads_treedef, sum_layer_grad_norms
    )

    # Normalize gradients across all examples.
    batch_size = grads_flat[0].shape[0]
    mean_clipped_grads = jax.tree.map(lambda x: x / batch_size, sum_grads)
    mean_layer_grad_norms = jax.tree.map(
        lambda x: x / batch_size, sum_layer_grad_norms
    )

    # Compute frac clipped statistics across all layers
    frac_clipped = jax.tree.map(lambda x: x / batch_size, num_clipped)
    frac_clipped_flat, _ = jax.tree_util.tree_flatten(frac_clipped)
    frac_clipped_flat = jnp.stack(frac_clipped_flat)
    mean_frac_clipped = jnp.mean(frac_clipped_flat)
    stdev_frac_clipped = jnp.std(frac_clipped_flat)

    dp_aux_info = {
        'frac_clipped': frac_clipped,
        'mean_frac_clipped': mean_frac_clipped,
        'stdev_frac_clipped': stdev_frac_clipped,
        'mean_layer_grad_norms': mean_layer_grad_norms,
    }
    return mean_clipped_grads, dp_aux_info, batch_size  # pytype: disable=bad-return-type  # jax-types


class GhostClippingDpSgdStochasticGradient(DpSgdStochasticGradient):
  """DP-SGD stochastic gradient function with Ghost Norm Clipping.

  This class implements DP-SGD without materializing the per-example gradients.
  This reduces memory cost for DP-SGD training and allows large batch training
  without needing to do (sequential) gradient accumulation.

  To use this method, all the parametric layers (layers with trainable
  parameters) in the model need to implement the ghost norm protocol. Please
  see `paxml.ghostnorm` for more details.

  This class computes the clipped gradients in two passes. In the first pass,
  the ghost norm protocol is used to estimate the per-example gradient norms
  from each layers. The norms are aggregated, and then used to calculate
  per-example scaling coefficients. The ghost norm protocol is used again to
  compute the weighted average gradients according to the coefficients. The cost
  of each ghost norm protocol pass should be approximately equal to the cost
  of a standard back-propagation.
  """

  def grad_fn(
      self,
      loss_fn: Callable[..., Any],
      mdl_vars_grad: NestedJTensor,
      mdl_vars_nograd_and_inputs: tuple[NestedJTensor, NestedMap],
      prng_key: PRNGKey,
  ) -> tuple[tuple[JTensor, DPGradAuxInfo], NestedJTensor]:
    assert (
        self.inner_batch_size is None
    ), 'inner_batch_size is not supported yet by GhostClipping.'

    grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
    batch_size = jax.tree_util.tree_flatten(mdl_vars_nograd_and_inputs[1])[0][
        0
    ].shape[0]

    # Pass 1: get per-example gradient norms
    scales = jnp.ones(batch_size)
    params_with_sq_norms = jax.tree.map(
        lambda x: ghostnorm_base.ParamWithAux(x, scales), mdl_vars_grad[PARAMS]
    )
    (_, aux), grad_with_sq_norms = grad_fn(
        {**mdl_vars_grad, PARAMS: params_with_sq_norms},
        mdl_vars_nograd_and_inputs,
        prng_key,
    )

    is_leaf = lambda node: isinstance(node, ghostnorm_base.ParamWithAux)
    grad_norms = jnp.sqrt(
        sum(
            x.aux
            for x in jax.tree_util.tree_flatten(
                grad_with_sq_norms[PARAMS], is_leaf=is_leaf
            )[0]
        )
    )

    # PAX scales the loss by global batch size under pmap, specifically:
    # - under pmap:
    #   - loss = local_loss_sum / (local_batch_size * num_devices)
    #   - loss_weight = 1 / num_devices
    # - not under pmap:
    #   - loss = local_loss_sum / local_batch_size
    #   - loss_weight depends on specific models, sometimes it's
    #     local_batch_size, sometimes it's just 1
    if base_layer.is_running_under_pmap():
      # correct grad norm calculation
      num_devices = 1 / aux.loss_weight
      grad_norms *= num_devices

    frac_clipped = 0.0
    if self.l2_norm_clip is not None:
      scales = jnp.minimum(1.0, self.l2_norm_clip / grad_norms)
      frac_clipped = jnp.mean(scales < 1.0)
      if self.normalize_gradients:
        # Scale gradients to have norm at most 1 instead of l2_norm_clip.
        scales = scales / self.l2_norm_clip

    # Pass 2: get average of clipped gradients
    params_with_sq_norms = jax.tree.map(
        lambda x: ghostnorm_base.ParamWithAux(x, scales), mdl_vars_grad[PARAMS]
    )
    (loss, aux), clipped_grads = grad_fn(
        {**mdl_vars_grad, PARAMS: params_with_sq_norms},
        mdl_vars_nograd_and_inputs,
        prng_key,
    )
    clipped_grads[PARAMS] = jax.tree.map(
        lambda x: x.param, clipped_grads[PARAMS], is_leaf=is_leaf
    )

    # Note here noise stddev is divided by num_devices because in PAX the loss
    # is scaled by global batch size when pmap is used (see above)
    if self.noise_multiplier > 0.0:
      noise_stddev = (
          self.noise_multiplier
          / batch_size
          * (1.0 if self.normalize_gradients else self.l2_norm_clip)
      )
      noised_grads = self._add_noise(
          clipped_grads, noise_stddev, aux.loss_weight, prng_key
      )
    else:
      # Optimization if using this class only for clipping (e.g., with DP-MF)
      noised_grads = clipped_grads

    aux = DPGradAuxInfo(
        dp_aux_info={'frac_clipped': frac_clipped},
        aux_info=aux.aux_info,
        loss_weight=aux.loss_weight,
    )
    return (loss, aux), noised_grads
