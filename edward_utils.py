# coding=utf-8
# Copyright 2023 The Edward2 Authors.
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

"""Normalization layers.

## References:

[1] Yuichi Yoshida, Takeru Miyato. Spectral Norm Regularization for Improving
    the Generalizability of Deep Learning.
    _arXiv preprint arXiv:1705.10941_, 2017. https://arxiv.org/abs/1705.10941

[2] Takeru Miyato, Toshiki Kataoka, Masanori Koyama, Yuichi Yoshida.
    Spectral normalization for generative adversarial networks.
    In _International Conference on Learning Representations_, 2018.

[3] Henry Gouk, Eibe Frank, Bernhard Pfahringer, Michael Cree.
    Regularisation of neural networks by enforcing lipschitz continuity.
    _arXiv preprint arXiv:1804.04368_, 2018. https://arxiv.org/abs/1804.04368
"""

import numpy as np
import tensorflow as tf
import tensorflow.compat.v1 as tf1
import math

class SpectralNormalizationConv2D(tf.keras.layers.Wrapper):
  """Implements spectral normalization for Conv2D layer based on [3]."""

  def __init__(self,
               layer,
               iteration=1,
               norm_multiplier=0.95,
               training=True,
               aggregation=tf.VariableAggregation.MEAN,
               legacy_mode=False,
               **kwargs):
    """Initializer.

    Args:
      layer: (tf.keras.layers.Layer) A TF Keras layer to apply normalization to.
      iteration: (int) The number of power iteration to perform to estimate
        weight matrix's singular value.
      norm_multiplier: (float) Multiplicative constant to threshold the
        normalization. Usually under normalization, the singular value will
        converge to this value.
      training: (bool) Whether to perform power iteration to update the singular
        value estimate.
      aggregation: (tf.VariableAggregation) Indicates how a distributed variable
        will be aggregated. Accepted values are constants defined in the class
        tf.VariableAggregation.
      legacy_mode: (bool) Whether to use the legacy implementation where the
        dimension of the u and v vectors are set to the batch size. It should
        not be enabled unless for backward compatibility reasons.
      **kwargs: (dict) Other keyword arguments for the layers.Wrapper class.
    """
    self.iteration = iteration
    self.do_power_iteration = training
    self.aggregation = aggregation
    self.norm_multiplier = norm_multiplier
    self.legacy_mode = legacy_mode

    # Set layer attributes.
    layer._name += '_spec_norm'

    if not isinstance(layer, tf.keras.layers.Conv2D):
      raise ValueError(
          'layer must be a `tf.keras.layer.Conv2D` instance. You passed: {input}'
          .format(input=layer))
    super(SpectralNormalizationConv2D, self).__init__(layer, **kwargs)

  def build(self, input_shape):  # pytype: disable=signature-mismatch  # overriding-parameter-count-checks
    self.layer.build(input_shape)
    self.layer.kernel._aggregation = self.aggregation  # pylint: disable=protected-access
    self._dtype = self.layer.kernel.dtype

    # Shape (kernel_size_1, kernel_size_2, in_channel, out_channel).
    self.w = self.layer.kernel
    self.w_shape = self.w.shape.as_list()
    self.strides = self.layer.strides
    # self.padding = "SAME" # self.layer.padding.upper()
    self.padding = self.layer.padding.upper()

    # Set the dimensions of u and v vectors.
    batch_size = input_shape[0]
    uv_dim = batch_size if self.legacy_mode else 1

    # Resolve shapes.
    in_height = input_shape[1]
    in_width = input_shape[2]
    in_channel = self.w_shape[2]

    if self.padding == "SAME":
        out_height = in_height // self.strides[0]
        out_width = in_width // self.strides[1]
    else:
        out_height = (in_height - self.w_shape[0]) // self.strides[0] + 1
        out_width = (in_width - self.w_shape[1]) // self.strides[1] + 1
    out_channel = self.w_shape[3]

    self.in_shape = (uv_dim, in_height, in_width, in_channel)
    self.out_shape = (uv_dim, out_height, out_width, out_channel)

    self.v = self.add_weight(
        shape=self.in_shape,
        initializer=tf.initializers.random_normal(),
        trainable=False,
        name='v',
        dtype=self.dtype,
        aggregation=self.aggregation)

    self.u = self.add_weight(
        shape=self.out_shape,
        initializer=tf.initializers.random_normal(),
        trainable=False,
        name='u',
        dtype=self.dtype,
        aggregation=self.aggregation)

    super(SpectralNormalizationConv2D, self).build()

  def call(self, inputs):
    u_update_op, v_update_op, w_update_op = self.update_weights()
    output = self.layer(inputs)
    w_restore_op = self.restore_weights()

    # Register update ops.
    self.add_update(u_update_op)
    self.add_update(v_update_op)
    self.add_update(w_update_op)
    self.add_update(w_restore_op)

    return output

  def update_weights(self):
    """Computes power iteration for convolutional filters based on [3]."""
    # Initialize u, v vectors.
    u_hat = self.u
    v_hat = self.v

    if self.do_power_iteration:
      for _ in range(self.iteration):
        # Updates v.
        v_ = tf.nn.conv2d_transpose(
            u_hat,
            self.w,
            output_shape=self.in_shape,
            strides=self.strides,
            padding=self.padding)
        v_hat = tf.nn.l2_normalize(tf.reshape(v_, [1, -1]))
        v_hat = tf.reshape(v_hat, v_.shape)

        # Updates u.
        u_ = tf.nn.conv2d(v_hat, self.w, strides=self.strides, padding=self.padding)
        u_hat = tf.nn.l2_normalize(tf.reshape(u_, [1, -1]))
        u_hat = tf.reshape(u_hat, u_.shape)

    v_w_hat = tf.nn.conv2d(v_hat, self.w, strides=self.strides, padding=self.padding)

    sigma = tf.matmul(tf.reshape(v_w_hat, [1, -1]), tf.reshape(u_hat, [-1, 1]))
    # Convert sigma from a 1x1 matrix to a scalar.
    sigma = tf.reshape(sigma, [])

    u_update_op = self.u.assign(u_hat)
    v_update_op = self.v.assign(v_hat)

    w_norm = tf.cond((self.norm_multiplier / sigma) < 1, lambda:      # pylint:disable=g-long-lambda
                     (self.norm_multiplier / sigma) * self.w, lambda: self.w)

    w_update_op = self.layer.kernel.assign(w_norm)

    return u_update_op, v_update_op, w_update_op

  def restore_weights(self):
    """Restores layer weights to maintain gradient update (See Alg 1 of [1])."""
    return self.layer.kernel.assign(self.w)


class OrthogonalRandomFeatures(tf.keras.initializers.Orthogonal):
  """Generates a orthogonal Gaussian matrix for a random feature Dense layer.

  Generates a 2D matrix of form W = stddev * Q @ S [1], where Q is a random
  orthogonal matrix of shape (num_rows, num_cols), and S is a diagonal matrix
  of i.i.d. random variables following chi(df = num_rows) distribution that
  imitates the column norm of a random Gaussian matrix.
  """

  def __init__(self, stddev=1.0, random_norm=True, seed=None):
    """Initializer.

    Args:
      stddev: (float) The standard deviation of the random matrix.
      random_norm: (bool) Whether to sample the norms of the random matrix
        columns from a chi(df=num_cols) distribution, or fix it to
        sqrt(num_cols). These two options corresponds to the construction in
        Theorem 1 and Theorem 2 of [1].
      seed: (int) Random seed.
    """
    super(OrthogonalRandomFeatures, self).__init__(gain=stddev, seed=seed)
    self.stddev = stddev
    self.random_norm = random_norm

  def _sample_orthogonal_matrix(self, shape, dtype):
    return super(OrthogonalRandomFeatures, self).__call__(shape, dtype=dtype)  # pytype: disable=attribute-error  # typed-keras

  def __call__(self, shape, dtype=tf.float32, **kwargs):
    # Sample orthogonal matrices.
    num_rows, num_cols = shape
    if num_rows < num_cols:
      # When num_row < num_col, sample multiple (num_row, num_row) matrices and
      # then concatenate following [1].
      ortho_mat_list = []
      num_cols_sampled = 0

      while num_cols_sampled < num_cols:
        ortho_mat_square = self._sample_orthogonal_matrix(
            (num_rows, num_rows), dtype=dtype)
        ortho_mat_list.append(ortho_mat_square)
        num_cols_sampled += num_rows

      # Reshape the matrix to the target shape (num_rows, num_cols)
      ortho_mat = tf.concat(ortho_mat_list, axis=-1)
      ortho_mat = ortho_mat[:, :num_cols]
    else:
      ortho_mat = self._sample_orthogonal_matrix(shape, dtype=dtype)

    # Sample random feature norms.
    if self.random_norm:
      # Construct Monte-Carlo estimate of squared column norm of a random
      # Gaussian matrix.
      feature_norms_square = tf.random.normal(shape=ortho_mat.shape)**2
    else:
      # Use mean of the squared column norm (i.e., E(z**2)=1) instead.
      feature_norms_square = tf.ones(shape=ortho_mat.shape)

    feature_norms = tf.reduce_sum(feature_norms_square, axis=0)
    feature_norms = tf.sqrt(feature_norms)

    # Returns the random feature matrix with orthogonal column and Gaussian-like
    # column norms.
    return ortho_mat * feature_norms

  def get_config(self):
    config = {
        'stddev': self.stddev,
        'random_norm': self.random_norm,
    }
    new_config = super(OrthogonalRandomFeatures, self).get_config()
    config.update(new_config)
    return config




_SUPPORTED_LIKELIHOOD = ('binary_logistic', 'poisson', 'gaussian')


class RandomFeatureGaussianProcess(tf.keras.layers.Layer):
  """Gaussian process layer with random feature approximation.

  During training, the model updates the maximum a posteriori (MAP) logits
  estimates and posterior precision matrix using minibatch statistics. During
  inference, the model divides the MAP logit estimates by the predictive
  standard deviation, which is equivalent to approximating the posterior mean
  of the predictive probability via the mean-field approximation.

  User can specify different types of random features by setting
  `use_custom_random_features=True`, and change the initializer and activations
  of the custom random features. For example:

    MLP Kernel: initializer='random_normal', activation=tf.nn.relu
    RBF Kernel: initializer='random_normal', activation=tf.math.cos

  A linear kernel can also be specified by setting gp_kernel_type='linear' and
  `use_custom_random_features=True`.

  Attributes:
    units: (int) The dimensionality of layer.
    num_inducing: (int) The number of random features for the approximation.
    is_training: (tf.bool) Whether the layer is set in training mode. If so the
      layer updates the Gaussian process' variance estimate using statistics
      computed from the incoming minibatches.
  """

  def __init__(self,
               units,
               num_inducing=1024,
               gp_kernel_type='gaussian',
               gp_kernel_scale=1.,
               gp_output_bias=0.,
               normalize_input=True,
               gp_kernel_scale_trainable=False,
               gp_output_bias_trainable=False,
               gp_cov_momentum=0.999,
               gp_cov_ridge_penalty=1e-6,
               scale_random_features=True,
               use_custom_random_features=True,
               custom_random_features_initializer=None,
               custom_random_features_activation=None,
               l2_regularization=0.,
               gp_cov_likelihood='gaussian',
               return_gp_cov=True,
               return_random_features=False,
               dtype=None,
               name='random_feature_gaussian_process',
               **gp_output_kwargs):
    """Initializes a random-feature Gaussian process layer instance.

    Args:
      units: (int) Number of output units.
      num_inducing: (int) Number of random Fourier features used for
        approximating the Gaussian process.
      gp_kernel_type: (string) The type of kernel function to use for Gaussian
        process. Currently default to 'gaussian' which is the Gaussian RBF
        kernel.
      gp_kernel_scale: (float) The length-scale parameter of the a
        shift-invariant kernel function, i.e., for RBF kernel:
        exp(-|x1 - x2|**2 / gp_kernel_scale).
      gp_output_bias: (float) Scalar initial value for the bias vector.
      normalize_input: (bool) Whether to normalize the input to Gaussian
        process.
      gp_kernel_scale_trainable: (bool) Whether the length scale variable is
        trainable.
      gp_output_bias_trainable: (bool) Whether the bias is trainable.
      gp_cov_momentum: (float) A discount factor used to compute the moving
        average for posterior covariance matrix.
      gp_cov_ridge_penalty: (float) Initial Ridge penalty to posterior
        covariance matrix.
      scale_random_features: (bool) Whether to scale the random feature
        by sqrt(2. / num_inducing).
      use_custom_random_features: (bool) Whether to use custom random
        features implemented using tf.keras.layers.Dense.
      custom_random_features_initializer: (str or callable) Initializer for
        the random features. Default to random normal which approximates a RBF
        kernel function if activation function is cos.
      custom_random_features_activation: (callable) Activation function for the
        random feature layer. Default to cosine which approximates a RBF
        kernel function.
      l2_regularization: (float) The strength of l2 regularization on the output
        weights.
      gp_cov_likelihood: (string) Likelihood to use for computing Laplace
        approximation for covariance matrix. Default to `gaussian`.
      return_gp_cov: (bool) Whether to also return GP covariance matrix.
        If False then no covariance learning is performed.
      return_random_features: (bool) Whether to also return random features.
      dtype: (tf.DType) Input data type.
      name: (string) Layer name.
      **gp_output_kwargs: Additional keyword arguments to dense output layer.
    """
    super(RandomFeatureGaussianProcess, self).__init__(name=name, dtype=dtype)
    self.units = units
    self.num_inducing = num_inducing

    self.normalize_input = normalize_input
    self.gp_input_scale = (
        1. / tf.sqrt(gp_kernel_scale) if gp_kernel_scale is not None else None)
    self.gp_feature_scale = tf.sqrt(2. / float(num_inducing))

    self.scale_random_features = scale_random_features
    self.return_random_features = return_random_features
    self.return_gp_cov = return_gp_cov

    self.gp_kernel_type = gp_kernel_type
    self.gp_kernel_scale = gp_kernel_scale
    self.gp_output_bias = gp_output_bias
    self.gp_kernel_scale_trainable = gp_kernel_scale_trainable
    self.gp_output_bias_trainable = gp_output_bias_trainable

    self.use_custom_random_features = use_custom_random_features
    self.custom_random_features_initializer = custom_random_features_initializer
    self.custom_random_features_activation = custom_random_features_activation

    self.l2_regularization = l2_regularization
    self.gp_output_kwargs = gp_output_kwargs

    self.gp_cov_momentum = gp_cov_momentum
    self.gp_cov_ridge_penalty = gp_cov_ridge_penalty
    self.gp_cov_likelihood = gp_cov_likelihood

    if self.use_custom_random_features:
      # Default to Gaussian RBF kernel with orthogonal random features.
      self.random_features_bias_initializer = tf.random_uniform_initializer(
          minval=0., maxval=2. * math.pi, seed=0)
      if self.custom_random_features_initializer is None:
        self.custom_random_features_initializer = (
            OrthogonalRandomFeatures(stddev=1.0, seed=0))
      if self.custom_random_features_activation is None:
        self.custom_random_features_activation = tf.math.cos

  def build(self, input_shape):
    self._build_sublayer_classes()
    if self.normalize_input:
      self._input_norm_layer = self.input_normalization_layer(
          name='gp_input_normalization')
      self._input_norm_layer.build(input_shape)
      input_shape = self._input_norm_layer.compute_output_shape(input_shape)

    self._random_feature = self._make_random_feature_layer(
        name='gp_random_feature')
    self._random_feature.build(input_shape)
    input_shape = self._random_feature.compute_output_shape(input_shape)

    if self.return_gp_cov:
      self._gp_cov_layer = self.covariance_layer(
          momentum=self.gp_cov_momentum,
          ridge_penalty=self.gp_cov_ridge_penalty,
          likelihood=self.gp_cov_likelihood,
          dtype=self.dtype,
          name='gp_covariance')
      self._gp_cov_layer.build(input_shape)

    self._gp_output_layer = self.dense_layer(
        units=self.units,
        use_bias=False,
        kernel_regularizer=tf.keras.regularizers.l2(self.l2_regularization),
        dtype=self.dtype,
        name='gp_output_weights',
        **self.gp_output_kwargs)
    self._gp_output_layer.build(input_shape)

    self._gp_output_bias = self.bias_layer(
        initial_value=[self.gp_output_bias] * self.units,
        dtype=self.dtype,
        trainable=self.gp_output_bias_trainable,
        name='gp_output_bias')

    self.built = True

  def _build_sublayer_classes(self):
    """Defines sublayer classes."""
    self.bias_layer = tf.Variable
    self.dense_layer = tf.keras.layers.Dense
    self.covariance_layer = LaplaceRandomFeatureCovariance
    self.input_normalization_layer = tf.keras.layers.LayerNormalization

  def _make_random_feature_layer(self, name):
    """Defines random feature layer depending on kernel type."""
    if not self.use_custom_random_features:
      # Use default RandomFourierFeatures layer from tf.keras.
      return tf.keras.layers.experimental.RandomFourierFeatures(
          output_dim=self.num_inducing,
          kernel_initializer=self.gp_kernel_type,
          scale=self.gp_kernel_scale,
          trainable=self.gp_kernel_scale_trainable,
          dtype=self.dtype,
          name=name)

    if self.gp_kernel_type.lower() == 'linear':
      custom_random_feature_layer = tf.keras.layers.Lambda(
          lambda x: x, name=name)
    else:
      # Use user-supplied configurations.
      custom_random_feature_layer = self.dense_layer(
          units=self.num_inducing,
          use_bias=True,
          activation=self.custom_random_features_activation,
          kernel_initializer=self.custom_random_features_initializer,
          bias_initializer=self.random_features_bias_initializer,
          trainable=False,
          name=name)

    return custom_random_feature_layer

  def reset_covariance_matrix(self):
    """Resets covariance matrix of the GP layer.

    This function is useful for reseting the model's covariance matrix at the
    begining of a new epoch.
    """
    self._gp_cov_layer.reset_precision_matrix()

  def call(self, inputs, global_step=None, training=None):
    # Computes random features.
    gp_inputs = inputs
    if self.normalize_input:
      gp_inputs = self._input_norm_layer(gp_inputs)
    elif self.use_custom_random_features and self.gp_input_scale is not None:
      # Supports lengthscale for custom random feature layer by directly
      # rescaling the input.
      gp_input_scale = tf.cast(self.gp_input_scale, inputs.dtype)
      gp_inputs = gp_inputs * gp_input_scale

    gp_feature = self._random_feature(gp_inputs)

    if self.scale_random_features:
      # Scale random feature by 2. / sqrt(num_inducing) following [1].
      # When using GP layer as the output layer of a nerual network,
      # it is recommended to turn this scaling off to prevent it from changing
      # the learning rate to the hidden layers.
      gp_feature_scale = tf.cast(self.gp_feature_scale, inputs.dtype)
      gp_feature = gp_feature * gp_feature_scale

    # Computes posterior center (i.e., MAP estimate) and variance.
    gp_output = self._gp_output_layer(gp_feature) + self._gp_output_bias

    if self.return_gp_cov:
      gp_covmat = self._gp_cov_layer(gp_feature, gp_output, training)

    # Assembles model output.
    model_output = [gp_output,]
    if self.return_gp_cov:
      model_output.append(gp_covmat)
    if self.return_random_features:
      model_output.append(gp_feature)

    return model_output


class LaplaceRandomFeatureCovariance(tf.keras.layers.Layer):
  """Computes the Gaussian Process covariance using Laplace method.

  At training time, this layer updates the Gaussian process posterior using
  model features in minibatches.

  Attributes:
    momentum: (float) A discount factor used to compute the moving average for
      posterior precision matrix. Analogous to the momentum factor in batch
      normalization. If -1 then update covariance matrix using a naive sum
      without momentum, which is desirable if the goal is to compute the exact
      covariance matrix by passing through data once (say in the final epoch).
    ridge_penalty: (float) Initial Ridge penalty to weight covariance matrix.
      This value is used to stablize the eigenvalues of weight covariance
      estimate so that the matrix inverse can be computed for Cov = inv(t(X) * X
      + s * I). The ridge factor s cannot be too large since otherwise it will
      dominate the t(X) * X term and make covariance estimate not meaningful.
    likelihood: (str) The likelihood to use for computing Laplace approximation
      for the covariance matrix. Can be one of ('binary_logistic', 'poisson',
      'gaussian').
  """

  def __init__(
      self,
      momentum=0.999,
      ridge_penalty=1e-6,
      likelihood='gaussian',
      dtype=None,
      name='laplace_covariance',
      use_on_read_synchronization_for_single_replica_vars=False,
  ):
    if likelihood not in _SUPPORTED_LIKELIHOOD:
      raise ValueError(
          f'"likelihood" must be one of {_SUPPORTED_LIKELIHOOD}, got {likelihood}.'
      )
    self.ridge_penalty = ridge_penalty
    self.momentum = momentum
    self.likelihood = likelihood
    self.use_on_read_synchronization_for_single_replica_vars = (
        use_on_read_synchronization_for_single_replica_vars
    )
    super(LaplaceRandomFeatureCovariance, self).__init__(dtype=dtype, name=name)

  def compute_output_shape(self, input_shape):
    gp_feature_dim = input_shape[-1]
    return tf.TensorShape([gp_feature_dim, gp_feature_dim])

  def build(self, input_shape):
    gp_feature_dim = input_shape[-1]

    # Convert gp_feature_dim to int value for TF1 compatibility.
    if isinstance(gp_feature_dim, tf.compat.v1.Dimension):
      gp_feature_dim = gp_feature_dim.value

    # Posterior precision matrix for the GP's random feature coefficients.
    self.initial_precision_matrix = tf.zeros(
        (gp_feature_dim, gp_feature_dim), dtype=self.dtype
    )

    var_synchronization = tf.VariableSynchronization.AUTO
    if self.use_on_read_synchronization_for_single_replica_vars:
      var_synchronization = tf.VariableSynchronization.ON_READ
    self.precision_matrix = self.add_weight(
        name='gp_precision_matrix',
        shape=(gp_feature_dim, gp_feature_dim),
        dtype=self.dtype,
        initializer=tf.keras.initializers.Zeros(),
        trainable=False,
        synchronization=var_synchronization,
        aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
    )

    # This variable is used during eval in all replicas so needs to sync this
    # variable and we can't use ON_READ synchronization that doesn't propagate
    # updates immediately. Therefore, use AUTO synchronization.
    self.covariance_matrix = self.add_weight(
        name='gp_covariance_matrix',
        shape=(gp_feature_dim, gp_feature_dim),
        dtype=self.dtype,
        trainable=False,
        synchronization=tf.VariableSynchronization.AUTO,
        aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
    )

    # Boolean flag to indicate whether to update the covariance matrix (i.e.,
    # by inverting the newly updated precision matrix) during inference.
    # Use AUTO synchronization like the above variable.
    self.if_update_covariance = self.add_weight(
        name='update_gp_covariance',
        dtype=tf.bool,
        shape=(),
        trainable=False,
        synchronization=tf.VariableSynchronization.AUTO,
        aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
    )

    self.built = True

  def update_feature_precision_matrix(self, gp_feature, logits):
    """Computes the updated precision matrix of feature weights."""
    if self.likelihood != 'gaussian':
      if logits is None:
        raise ValueError(
            f'"logits" cannot be None when likelihood={self.likelihood}')

      if logits.shape[-1] != 1:
        raise ValueError(
            f'likelihood={self.likelihood} only support univariate logits.'
            f'Got logits dimension: {logits.shape[-1]}')

    batch_size = tf.shape(gp_feature)[0]
    batch_size = tf.cast(batch_size, dtype=gp_feature.dtype)

    # Computes batch-specific normalized precision matrix.
    if self.likelihood == 'binary_logistic':
      prob = tf.sigmoid(logits)
      prob_multiplier = prob * (1. - prob)
    elif self.likelihood == 'poisson':
      prob_multiplier = tf.exp(logits)
    else:
      prob_multiplier = 1.

    gp_feature_adjusted = tf.sqrt(prob_multiplier) * gp_feature
    precision_matrix_minibatch = tf.matmul(
        gp_feature_adjusted, gp_feature_adjusted, transpose_a=True)

    # Updates the population-wise precision matrix.
    if self.momentum > 0:
      # Use moving-average updates to accumulate batch-specific precision
      # matrices.
      precision_matrix_minibatch = precision_matrix_minibatch / batch_size
      precision_matrix_new = (
          self.momentum * self.precision_matrix +
          (1. - self.momentum) * precision_matrix_minibatch)
    else:
      # Compute exact population-wise covariance without momentum.
      # If use this option, make sure to pass through data only once.
      precision_matrix_new = self.precision_matrix + precision_matrix_minibatch

    return precision_matrix_new

  def reset_precision_matrix(self):
    """Resets precision matrix to its initial value.

    This function is useful for reseting the model's covariance matrix at the
    begining of a new epoch.
    """
    precision_matrix_reset_op = self.precision_matrix.assign(
        self.initial_precision_matrix)
    self.add_update(precision_matrix_reset_op)

  def update_feature_covariance_matrix(self):
    """Computes the feature covariance if self.if_update_covariance=True.

    GP layer computes the covariancce matrix of the random feature coefficient
    by inverting the precision matrix. Since this inversion op is expensive,
    we will invoke it only when there is new update to the precision matrix
    (where self.if_update_covariance will be flipped to `True`.).

    Returns:
      The updated covariance_matrix.
    """
    # Cast precision_matrix to a linalg.inv compatible format.
    precision_matrix = tf.cast(self.precision_matrix, tf.float32)
    covariance_matrix = tf.cast(self.covariance_matrix, tf.float32)
    gp_feature_dim = tf.shape(precision_matrix)[0]
    # Compute covariance matrix update only when `if_update_covariance=True`.
    covariance_matrix_updated = tf.cond(
        self.if_update_covariance,
        lambda: tf.linalg.inv(  # pylint: disable=g-long-lambda
            self.ridge_penalty * tf.eye(gp_feature_dim, dtype=self.dtype)
            + precision_matrix
        ),
        lambda: covariance_matrix,
    )
    return tf.cast(covariance_matrix_updated, self.dtype)

  def compute_predictive_covariance(self, gp_feature):
    """Computes posterior predictive variance.

    Approximates the Gaussian process posterior using random features.
    Given training random feature Phi_tr (num_train, num_hidden) and testing
    random feature Phi_ts (batch_size, num_hidden). The predictive covariance
    matrix is computed as (assuming Gaussian likelihood):

    s * Phi_ts @ inv(t(Phi_tr) * Phi_tr + s * I) @ t(Phi_ts),

    where s is the ridge factor to be used for stablizing the inverse, and I is
    the identity matrix with shape (num_hidden, num_hidden).

    Args:
      gp_feature: (tf.Tensor) The random feature of testing data to be used for
        computing the covariance matrix. Shape (batch_size, gp_hidden_size).

    Returns:
      (tf.Tensor) Predictive covariance matrix, shape (batch_size, batch_size).
    """
    # Computes the covariance matrix of the gp prediction.
    cov_feature_product = tf.matmul(self.covariance_matrix, gp_feature,
                                    transpose_b=True) * self.ridge_penalty
    gp_cov_matrix = tf.matmul(gp_feature, cov_feature_product)
    return gp_cov_matrix

  def _get_training_value(self, training=None):
    if training is None:
      training = tf.keras.backend.learning_phase()

    if isinstance(training, int):
      training = bool(training)

    return training

  def call(self, inputs, logits=None, training=None):
    """Minibatch updates the GP's posterior precision matrix estimate.

    Args:
      inputs: (tf.Tensor) GP random features, shape (batch_size,
        gp_hidden_size).
      logits: (tf.Tensor) Pre-activation output from the model. Needed
        for Laplace approximation under a non-Gaussian likelihood.
      training: (tf.bool) whether or not the layer is in training mode. If in
        training mode, the gp_weight covariance is updated using gp_feature.

    Returns:
      gp_stddev (tf.Tensor): GP posterior predictive variance,
        shape (batch_size, batch_size).
    """
    batch_size = tf.shape(inputs)[0]
    training = self._get_training_value(training)

    if training:
      # Computes the updated feature precision matrix.
      precision_matrix_updated = self.update_feature_precision_matrix(
          gp_feature=inputs, logits=logits)

      # Updates precision matrix.
      precision_matrix_update_op = self.precision_matrix.assign(
          precision_matrix_updated)

      # Enables covariance update in the next inference call.
      enable_covariance_update_op = self.if_update_covariance.assign(
          tf.constant(True, dtype=tf.bool))

      self.add_update(precision_matrix_update_op)
      self.add_update(enable_covariance_update_op)

      # Return null estimate during training.
      return tf.eye(batch_size, dtype=self.dtype)
    else:
      # Lazily computes feature covariance matrix during inference.
      covariance_matrix_updated = self.update_feature_covariance_matrix()

      # Store updated covariance matrix.
      covariance_matrix_update_op = self.covariance_matrix.assign(
          covariance_matrix_updated)

      # Disable covariance update in future inference calls (to avoid the
      # expensive tf.linalg.inv op) unless there are new updates to precision
      # matrix.
      disable_covariance_update_op = self.if_update_covariance.assign(
          tf.constant(False, dtype=tf.bool))

      self.add_update(covariance_matrix_update_op)
      self.add_update(disable_covariance_update_op)

      return self.compute_predictive_covariance(gp_feature=inputs)

if __name__ == "__main__":
    conv2D = SpectralNormalizationConv2D(tf.keras.layers.Conv2D(
        32,
        kernel_size=(3, 3),
        strides=2,
        padding="valid",
        use_bias=False,
    ), iteration=1, norm_multiplier=6.)
    no_spectral_cov = tf.keras.layers.Conv2D(
        32,
        kernel_size=(3, 3),
        strides=2,
        padding="valid",
        use_bias=False,
    )
    x = tf.random.normal((1, 229, 229, 3))

    print("out", conv2D(x).shape)
    print("no spesral", no_spectral_cov(x).shape)