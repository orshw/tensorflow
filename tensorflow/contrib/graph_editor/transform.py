# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Class to transform an subgraph into another.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from copy import deepcopy

from six import iteritems

from tensorflow.contrib.graph_editor import edit
from tensorflow.contrib.graph_editor import reroute
from tensorflow.contrib.graph_editor import subgraph
from tensorflow.contrib.graph_editor import util
from tensorflow.python.framework import ops as tf_ops
from tensorflow.python.platform import tf_logging as logging

__all__ = [
    "replace_t_with_placeholder_handler",
    "keep_t_if_possible_handler",
    "assign_renamed_collections_handler",
    "transform_op_if_inside_handler",
    "copy_op_handler",
    "transform_op_in_place",
    "Transformer",
    "copy",
]


def replace_t_with_placeholder_handler(info, t):
  """Transform a tensor into a placeholder tensor.

  This handler is typically used to transform a subgraph input tensor into a
  placeholder.

  Args:
    info: Transform._Info instance.
    t: tensor whose input must be transformed into a place holder.
  Returns:
    The tensor generated by the newly created place holder.
  """
  with info.graph_.as_default():
    t_ = util.make_placeholder_from_tensor(t, scope=info.scope_)
  return t_


def keep_t_if_possible_handler(info, t):
  """Transform a tensor into itself (identity) if possible.

  This handler transform a tensor into itself if the source and destination
  graph are the same. Otherwise it will create a placeholder.
  This handler is typically used to transform a hidden input tensors.

  Args:
    info: Transform._Info instance.
    t: tensor whose input must be transformed into a place holder.
  Returns:
    The tensor generated by the newly created place holder.
  """
  if info.graph is info.graph_:
    return t
  else:
    return replace_t_with_placeholder_handler(info, t)


def assign_renamed_collections_handler(info, elem, elem_):
  """Add the transformed elem to the (renamed) collections of elem.

  Args:
    info: Transform._Info instance.
    elem: the original element (tf.Tensor or tf.Operation)
    elem_: the transformed element
  """
  # TODO(fkp): handle known special cases
  for name, collection in iteritems(elem.graph._collections):  # pylint: disable=protected-access
    if elem not in collection:
      continue
    collection_name_ = info.transformer.new_name(name)
    info.graph_.add_to_collection(collection_name_, elem_)


def transform_op_if_inside_handler(info, op, keep_if_possible=True):
  """Transform an optional op only if it is inside the subgraph.

  This handler is typically use to handle original op: it is fine to keep them
  if they are inside the subgraph, otherwise they are just ignored.

  Args:
    info: Transform._Info instance.
    op: the optional op to transform (or ignore).
    keep_if_possible: re-attach to the original op if possible, that is,
      if the source graph and the destination graph are the same.
  Returns:
    The transformed op or None.
  """
  if op is None:
    return None
  if op in info.sgv.ops:
    return info.transformer._transform_op(op)  # pylint: disable=protected-access
  else:
    if keep_if_possible and info.graph is info.graph_:
      return op
    else:
      return None


def copy_op_handler(info, op):
  """Copy a tf.Operation.

  Args:
    info: Transform._Info instance.
    op: the tf.Operation to be copied.
  Returns:
    A copy of op.
  """
  # pylint: disable=protected-access

  # Transform control inputs:
  control_inputs_ = [info.transformer.transform_control_input_handler(info, ci)
                     for ci in op.control_inputs]
  control_inputs_ = [ci for ci in control_inputs_ if ci is not None]

  # Transform it if any:
  original_op_ = info.transformer.transform_original_op_hanlder(info,
                                                                op._original_op)

  # Transform inputs:
  inputs_ = [info.transformer._transform_t(t) for t in op.inputs]

  # Clone the node def:
  node_def_ = deepcopy(op._node_def)

  # Transform name:
  name_ = info.transformer.new_name(op.name)
  name_ = info.graph_.unique_name(name_)
  node_def_.name = name_

  # Copy the other inputs needed for initialization
  output_types_ = op._output_types[:]
  input_types_ = op._input_types[:]

  # Make a copy of the op_def too.
  # Its unique to every _type_ of Operation.
  op_def_ = deepcopy(op._op_def)

  # Initialize a new Operation instance
  op_ = tf_ops.Operation(node_def_, info.graph_, inputs_, output_types_,
                         control_inputs_, input_types_, original_op_, op_def_)
  info.graph_._add_op(op_)

  # pylint: enable=protected-access
  return op_


def transform_op_in_place(info, op, detach_outputs=False):
  """Transform a op in-place - experimental!

  Transform an operation in place. It reconnects the inputs if they have been
  modified. if detach_outputs is True, the outputs of op are also detached.

  Args:
    info: Transform._Info instance.
    op: the op to transform in place.
    detach_outputs: if True, the outputs of op are detached, ready for the user
      to add more operation.
  Returns:
    The transformed op.
  """
  # recursive call to the inputs:
  inputs = [info.transformer._transform_t(t) for t in op.inputs]  # pylint: disable=protected-access
  # re-connect to the inputs if they have changed:
  if inputs != list(op.inputs):
    reroute.reroute_a2b_ts(inputs, op.inputs)
  # detach op from its consumer first ?
  if detach_outputs:
    edit.detach_outputs(op)
  return op


class Transformer(object):
  """Transform a subgraph into another one.

  By default, the constructor create a transform which copy a subgraph and
  replaces inputs with placeholders. This behavior can be modified by changing
  the handlers.
  """

  class _Info(object):
    """Transformer temporary data.

    An instance of this class holds all the information relevant to a call
    to a transformer instance (that is, a call to __call__). An instance
    is created for the life-time of the __call__ function and is passed as
    argument to the handlers.
    """

    def __init__(self, transformer, sgv, dst_graph, dst_scope, src_scope):
      self.transformer = transformer
      self.sgv = sgv
      self.ops = frozenset(sgv.ops)
      self.control_outputs = util.ControlOutputs(sgv.graph)
      self.graph = sgv.graph
      self.scope = src_scope
      self.graph_ = dst_graph
      self.scope_ = dst_scope
      self.transformed_ops = {}
      self.transformed_ts = {}

    def create_ops_mapping(self):
      """Return the mapping from original ops to transformed ops."""
      return {op.name: op_.name for op, op_ in iteritems(self.transformed_ops)}

  def __init__(self):
    """Transformer constructor.

    The following members can be modified:
    transform_op_handler: handle the transformation of a tf.Operation.
      This handler defaults to a simple copy.
    assign_collections_handler: handle the assignment of collections.
      This handler defaults to assigning new collections created under the
      given name-scope.
    transform_external_input_handler: handle the transform of the inputs to
      the given subgraph. This handler defaults to creating placeholders
      instead of the ops just before the input tensors of the subgraph.
    transform_external_hidden_input_handler: handle the transform of the
      hidden inputs of the subgraph, that is, the inputs which are not listed
      in sgv.inputs. This handler defaults to a transform which keep the same
      input if the source and destination graphs are the same, otherwise
      use placeholders.
    transform_original_op_hanlder: handle the transform of original_op. This
      handler defaults to transforming original_op only if they are in the
      subgraph, otherwise they are ignored.
    """

    # handlers
    self.transform_op_handler = copy_op_handler
    self.transform_control_input_handler = transform_op_if_inside_handler
    self.assign_collections_handler = assign_renamed_collections_handler
    self.transform_external_input_handler = replace_t_with_placeholder_handler
    self.transform_external_hidden_input_handler = keep_t_if_possible_handler
    self.transform_original_op_hanlder = transform_op_if_inside_handler

    # temporary per-call variable
    self._info = None

  def __call__(self,
               sgv,
               dst_graph,
               dst_scope,
               src_scope="",
               reuse_dst_scope=False):
    """Execute the transformation.

    Args:
      sgv: the source subgraph-view.
      dst_graph: the destination graph.
      dst_scope: the destination scope.
      src_scope: the source scope, which specify the path from which the
        relative path of the transformed nodes are computed. For instance, if
        src_scope is a/ and dst_scoped is b/, then the node a/x/y will have a
        relative path of x/y and will be transformed into b/x/y.
      reuse_dst_scope: if True the dst_scope is re-used if it already exists.
        Otherwise, the scope is given a unique name based on the one given
        by postfixing an underscore followed by a digit (default).
    Returns:
      A tuple `(sgv, ops_mapping)` where:
        `sgv` is the transformed subgraph view;
        `ops_mapping` is a dictionary mapping the name of the original ops
        to the name of the transformed ops.
    Raises:
      ValueError: if the argumens are invalid.
    """
    sgv = subgraph.make_view(sgv)
    if not isinstance(dst_graph, tf_ops.Graph):
      raise TypeError("Expected a tf.Graph, got: {}".format(type(dst_graph)))

    src_scope = util.scope_finalize(src_scope)
    dst_scope = util.scope_finalize(dst_scope)

    # Potentially create new scope if reuse_dst_scope is False
    if dst_scope and not reuse_dst_scope:
      dst_scope = util.scope_finalize(dst_graph.unique_name(dst_scope[:-1]))

    if sgv.graph is dst_graph and not dst_scope:
      logging.warning("The source and the destination are the same! "
                      "Beware: in-place transormation are currently "
                      "experimental.")
    self._info = Transformer._Info(self, sgv, dst_graph, dst_scope, src_scope)

    # This is a recursive call. In practice, the graph is transformed bottom-up.
    for output_t in self._info.sgv.outputs:
      self._transform_t(output_t)

    sgv_ = self._transform_sgv(sgv)

    ops_mapping = self._info.create_ops_mapping()
    self._info = None
    return sgv_, ops_mapping

  def _transform_sgv(self, sgv):
    """Transform a subgraph view.

    For convenience, a transform operation returns a subgraph view of the
    transformed graph.

    Args:
      sgv: the subgraph to be transformed.
    Returns:
      The transformed subgraph.
    """
    ops_ = [op_ for _, op_ in iteritems(self._info.transformed_ops)]
    sgv_ = subgraph.SubGraphView(ops_)
    sgv_inputs_ = sgv_.inputs
    sgv_outputs_ = sgv_.outputs

    # re-order inputs
    input_map_ = []
    for input_t in sgv.inputs:
      if input_t not in self._info.transformed_ts:
        continue
      input_t_ = self._info.transformed_ts[input_t]
      if input_t_ not in sgv_inputs_:
        continue
      input_t_index_ = sgv_.input_index(input_t_)
      input_map_.append(input_t_index_)

    # re-order outputs
    output_map_ = []
    for output_t in sgv.outputs:
      if output_t not in self._info.transformed_ts:
        continue
      output_t_ = self._info.transformed_ts[output_t]
      if output_t_ not in sgv_outputs_:
        continue
      output_t_index_ = sgv_.output_index(output_t_)
      output_map_.append(output_t_index_)

    return sgv_.remap(input_map_, output_map_)

  def _transform_t(self, t):
    """Transform a tf.Tensor.

    Args:
      t: the tensor to be transformed.
    Returns:
      The transformed tensor.
    """
    if t in self._info.transformed_ts:
      return self._info.transformed_ts[t]

    op, op_index = t.op, t.value_index

    # If op is not in the subgraph:
    if op not in self._info.ops:
      # t_ is an input of the subgraph
      if t in self._info.sgv.inputs:
        t_ = self.transform_external_input_handler(self._info, t)
      # t_ is a hidden input of the subgraph
      else:
        t_ = self.transform_external_hidden_input_handler(self._info, t)
    # If op is in the subgraph, just transform it:
    else:
      op_ = self._transform_op(op)
      t_ = op_.outputs[op_index]

    # assign to collection
    if t is not t_:
      self.assign_collections_handler(self._info, t, t_)

    self._info.transformed_ts[t] = t_
    return t_

  def _transform_op(self, op):
    """Transform a tf.Operation.

    Args:
      op: the operation to be transformed.
    Returns:
      The transformed operation.
    """
    if op in self._info.transformed_ops:
      return self._info.transformed_ops[op]

    op_ = self.transform_op_handler(self._info, op)

    # Add to all the active control dependencies
    self._info.graph_._record_op_seen_by_control_dependencies(op_)  # pylint: disable=protected-access

    # All to all the active devices
    for device_function in reversed(self._info.graph_._device_function_stack):  # pylint: disable=protected-access
      op_._set_device(device_function(op_))  # pylint: disable=protected-access

    # TODO(fkp): Establish clear policy about what context managers are allowed.

    # assign to collection
    if op is not op_:
      self.assign_collections_handler(self._info, op, op_)

    self._info.transformed_ops[op] = op_
    return op_

  def new_name(self, name):
    """Compute a destination name from a source name.

    Args:
      name: the name to be "transformed".
    Returns:
      The transformed name.
    Raises:
      ValueError: if the source scope is used (that is, not an empty string)
        and the source name does not belong to the source scope.
    """
    scope = self._info.scope
    if not name.startswith(scope):
      raise ValueError("{} does not belong to source scope: {}.".format(name,
                                                                        scope))
    rel_name = name[len(scope):]
    name_ = self._info.scope_ + rel_name
    return name_


def copy(sgv, dst_graph=None, dst_scope="", src_scope="",
         reuse_dst_scope=False):
  """Copy a subgraph.

  Args:
    sgv: the source subgraph-view. This argument is converted to a subgraph
      using the same rules than the function subgraph.make_view.
    dst_graph: the destination graph.
    dst_scope: the destination scope.
    src_scope: the source scope.
    reuse_dst_scope: if True the dst_scope is re-used if it already exists.
      Otherwise, the scope is given a unique name based on the one given
      by postfixing an underscore followed by a digit (default).
  Returns:
    The subgraph view of the copied subgraph.
  Raises:
    TypeError: if dst_graph is not a tf.Graph.
    StandardError: if sgv cannot be converted to a SubGraphView using
      the same rules than the function subgraph.make_view.
  """
  sgv = subgraph.make_view(sgv)
  if dst_graph is None:
    dst_graph = sgv.graph
  if not isinstance(dst_graph, tf_ops.Graph):
    raise TypeError("Expected a tf.Graph, got: {}".format(type(dst_graph)))

  copier = Transformer()
  return copier(
      sgv, dst_graph, dst_scope, src_scope, reuse_dst_scope=reuse_dst_scope)
