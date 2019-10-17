import tensorflow as tf
from tensorflow.keras import backend as K
from tensorflow.keras.layers import Layer, Lambda, Dropout, Input
from tensorflow.keras import activations, initializers, constraints, regularizers
from .misc import SqueezedSparseConversion
from ..mapper.node_mappers import RelationalFullBatchNodeGenerator


class RelationalGraphConvolution(Layer):
    """
        Relational Graph Convolution (RGCN) Keras layer.

        Original paper: Modeling Relational Data with Graph Convolutional Networks.
        Thomas N. Kipf, Michael Schlichtkrull (2017).
            https://arxiv.org/pdf/1703.06103.pdf

        Notes:
          - The inputs are tensors with a batch dimension of 1:
            Keras requires this batch dimension, and for full-batch methods
            we only have a single "batch".

          - There are 2 + R inputs required (where R is the number of relationships): the node features, the output
            indices (the nodes that are to be selected in the final layer)
            and a normalized adjacency matrix for each relationship

          - The output indices are used when ``final_layer=True`` and the returned outputs
            are the final-layer features for the nodes indexed by output indices.

          - If ``final_layer=False`` all the node features are output in the same ordering as
            given by the adjacency matrix.

        Args:
            units (int): dimensionality of output feature vectors
            num_relationships (int): the number of relationships in the graph
            num_bases (int): the number of basis matrices to use for parameterizing the weight matrices as described in
                the paper; defaults to 0.
            activation (str or func): nonlinear activation applied to layer's output to obtain output features
            use_bias (bool): toggles an optional bias
            final_layer (bool): If False the layer returns output for all nodes,
                                if True it returns the subset specified by the indices passed to it.
            kernel_initializer (str or func): The initialiser to use for the weights;
                defaults to 'glorot_uniform'.
            kernel_regularizer (str or func): The regulariser to use for the weights;
                defaults to None.
            kernel_constraint (str or func): The constraint to use for the weights;
                defaults to None.
            bias_initializer (str or func): The initialiser to use for the bias;
                defaults to 'zeros'.
            bias_regularizer (str or func): The regulariser to use for the bias;
                defaults to None.
            bias_constraint (str or func): The constraint to use for the bias;
                defaults to None.
        """
    def __init__(
            self,
            units,
            num_relationships,
            num_bases=0,
            activation=None,
            use_bias=True,
            final_layer=False,
            kernel_initializer="glorot_uniform",
            bias_initializer="zeros",
            kernel_regularizer=None,
            bias_regularizer=None,
            activity_regularizer=None,
            kernel_constraint=None,
            bias_constraint=None,
            **kwargs
    ):
        if "input_shape" not in kwargs and "input_dim" in kwargs:
            kwargs["input_shape"] = (kwargs.get("input_dim"),)

        super().__init__(**kwargs)

        self.units = units
        self.num_relationships = num_relationships
        self.num_bases = num_bases
        self.activation = activations.get(activation)
        self.use_bias = use_bias
        self.kernel_initializer = initializers.get(kernel_initializer)
        self.bias_initializer = initializers.get(bias_initializer)
        self.kernel_regularizer = regularizers.get(kernel_regularizer)
        self.bias_regularizer = regularizers.get(bias_regularizer)
        self.activity_regularizer = regularizers.get(activity_regularizer)
        self.kernel_constraint = constraints.get(kernel_constraint)
        self.bias_constraint = constraints.get(bias_constraint)

        self.final_layer = final_layer

    def get_config(self):
        """
        Gets class configuration for Keras serialization.
        Used by keras model serialization.

        Returns:
            A dictionary that contains the config of the layer
        """

        config = {
            "units": self.units,
            "use_bias": self.use_bias,
            "final_layer": self.final_layer,
            "activation": activations.serialize(self.activation),
            "kernel_initializer": initializers.serialize(self.kernel_initializer),
            "bias_initializer": initializers.serialize(self.bias_initializer),
            "kernel_regularizer": regularizers.serialize(self.kernel_regularizer),
            "bias_regularizer": regularizers.serialize(self.bias_regularizer),
            "activity_regularizer": regularizers.serialize(self.activity_regularizer),
            "kernel_constraint": constraints.serialize(self.kernel_constraint),
            "bias_constraint": constraints.serialize(self.bias_constraint),
            "num_relationships": self.num_relationships,
            "num_bases": self.num_bases
        }

        base_config = super().get_config()
        return {**base_config, **config}

    def compute_output_shape(self, input_shapes):
        """
        Computes the output shape of the layer.

        Args:
            input_shapes (tuple of ints)
                Shape tuples can include None for free dimensions, instead of an integer.

        Returns:
            An input shape tuple.
        """
        feature_shape, out_shape, A_shape = input_shapes

        batch_dim = feature_shape[0]
        if self.final_layer:
            out_dim = out_shape[1]
        else:
            out_dim = feature_shape[1]

        return batch_dim, out_dim, self.units

    def build(self, input_shapes):
        """
        Builds the layer

        Args:
            input_shapes (list of int): shapes of the layer's inputs
            (node features, node_indices, and adjacency matrices)

        """
        feat_shape = input_shapes[0]
        input_dim = int(feat_shape[-1])

        if self.num_bases != 0:

            self.bases = self.add_weight(
                shape=(input_dim, self.units, self.num_bases),  # hyperparametr B
                initializer=self.kernel_initializer,
                name="bases",
                regularizer=self.kernel_regularizer,
                constraint=self.kernel_constraint,
            )

            self.coefficients = [
                self.add_weight(
                    shape=(self.num_bases, ),  # hyperparametr B
                    initializer=self.kernel_initializer,
                    name="coeff",
                    regularizer=self.kernel_regularizer,
                    constraint=self.kernel_constraint,
                ) for _ in range(self.num_relationships)]

            self.relational_kernels = [tf.einsum("ijk,k->ij", self.bases, coeff) for
                                       coeff in self.coefficients]

        else:
            self.bases = None
            self.coefficients = None
            self.relational_kernels = [
                self.add_weight(
                    shape=(input_dim, self.units),
                    initializer=self.kernel_initializer,
                    regularizer=self.kernel_regularizer,
                    constraint=self.kernel_constraint,
                ) for _ in range(self.num_relationships)
            ]

        self.self_kernel = self.add_weight(
            shape=(input_dim, self.units),  # hyperparametr B
            initializer=self.kernel_initializer,
            regularizer=self.kernel_regularizer,
            constraint=self.kernel_constraint,
        )

        if self.use_bias:
            self.bias = self.add_weight(
                shape=(self.units,),
                initializer=self.bias_initializer,
                name="bias",
                regularizer=self.bias_regularizer,
                constraint=self.bias_constraint,
            )
        else:
            self.bias = None

        self.built = True

    def call(self, inputs):
        """
        Applies the layer.

        Args:
            inputs (list): a list of 2 + R input tensors that includes
                node features (size 1 x N x F),
                output indices (size 1 x M),
                and a graph adjacency matrix (size N x N) for each relationship.
                R is the number of relationships in the graph (edge type),
                N is the number of nodes in the graph, and
                F is the dimensionality of node features.

        Returns:
            Keras Tensor that represents the output of the layer.
        """
        features, out_indices, *As = inputs
        batch_dim, n_nodes, _ = K.int_shape(features)
        if batch_dim != 1:
            raise ValueError(
                "Currently full-batch methods only support a batch dimension of one"
            )

        # Remove singleton batch dimension
        features = K.squeeze(features, 0)
        out_indices = K.squeeze(out_indices, 0)

        # Calculate the layer operation of RGCN
        output = K.dot(features, self.self_kernel)

        for i in range(self.num_relationships):
            h_graph = K.dot(As[i], features)
            output += K.dot(h_graph, self.relational_kernels[i])

        # Add optional bias & apply activation
        if self.bias is not None:
            output += self.bias
        output = self.activation(output)

        # On the final layer we gather the nodes referenced by the indices
        if self.final_layer:
            output = K.gather(output, out_indices)

        # Add batch dimension back if we removed it
        if batch_dim == 1:
            output = K.expand_dims(output, 0)

        return output


class RGCN:
    """
    A stack of Relational Graph Convolutional layers that implement a relational graph
    convolution neural network model as in https://arxiv.org/pdf/1703.06103.pdf

    The model minimally requires specification of the layer sizes as a list of ints
    corresponding to the feature dimensions for each hidden layer,
    activation functions for each hidden layers, and a generator object.

    To use this class as a Keras model, the features and pre-processed adjacency matrix
    should be supplied using the :class:`RelationalFullBatchNodeGenerator` class.
    The generator object should be instantiated as follows::

        generator = RelationalFullBatchNodeGenerator(G)

    Note that currently the RGCN class is compatible with both sparse and dense adjacency
    matrices and the :class:`RelationalFullBatchNodeGenerator` will default to sparse.

    For more details, please see the RGCN demo notebook:

    emos/node-classification/rgcn/rgcn-aifb-node-classification-example.ipynb

    Notes:
      - The inputs are tensors with a batch dimension of 1. These are provided by the \
        :class:`RelationalFullBatchNodeGenerator` object.

      - The nodes provided to the :class:`RelationalFullBatchNodeGenerator.flow` method are
        used by the final layer to select the predictions for those nodes in order.
        However, the intermediate layers before the final layer order the nodes
        in the same way as the adjacency matrix.

    Examples:
        Creating a RGCN node classification model from an existing :class:`StellarDiGraph`
        object ``G``::

            generator = RelationalFullBatchNodeGenerator(G)
            rgcn = RGCN(
                    layer_sizes=[32, 4],
                    activations=["elu","softmax"],
                    bases=10,
                    generator=generator,
                    dropout=0.5
                )
            x_inp, predictions = rgcn.node_model()

    Args:
        layer_sizes (list of int): Output sizes of RGCN layers in the stack.
        generator (RelationalFullBatchNodeGenerator): The generator instance.
        num_bases (int): Specifies number of basis matrices to use for the weight matrics of the RGCN layer
            as in the paper. Defaults to 0 which specifies that no basis decomposition is used.
        bias (bool): If True, a bias vector is learnt for each layer in the RGCN model.
        dropout (float): Dropout rate applied to input features of each RGCN layer.
        activations (list of str or func): Activations applied to each layer's output;
            defaults to ['relu', ..., 'relu'].
        kernel_regularizer (str or func): The regulariser to use for the weights of each layer;
            defaults to None.
    """


    def __init__(
        self, layer_sizes, generator, bias=True,
        num_bases=0, dropout=0.0, activations=None, **kwargs
    ):
        if not isinstance(generator, RelationalFullBatchNodeGenerator):
            raise TypeError("Generator should be a instance of RelationalFullBatchNodeGenerator")

        n_layers = len(layer_sizes)
        self.layer_sizes = layer_sizes
        self.activations = activations
        self.bias = bias
        self.num_bases = num_bases
        self.dropout = dropout
        self.generator = generator
        self.support = 1

        # Check if the generator is producing a sparse matrix
        self.use_sparse = generator.use_sparse

        # Activation function for each layer
        if activations is None:
            activations = ["relu"] * n_layers

        elif len(activations) != n_layers:
            raise ValueError(
                "Invalid number of activations; require one function per layer"
            )

        self.activations = activations
        self.num_bases = num_bases

        # Optional regulariser, etc. for weights and biases
        self._get_regularisers_from_keywords(kwargs)

        # Initialize a stack of RGCN layers
        self._layers = []
        for ii in range(n_layers):
            self._layers.append(Dropout(self.dropout))
            self._layers.append(
                RelationalGraphConvolution(
                    self.layer_sizes[ii],
                    num_relationships=len(generator.As),
                    num_bases=self.num_bases,
                    activation=self.activations[ii],
                    use_bias=self.bias,
                    final_layer=ii == (n_layers - 1),
                    **self._regularisers
                )
            )

    def _get_regularisers_from_keywords(self, kwargs):
        regularisers = {}
        for param_name in [
            "kernel_initializer",
            "kernel_regularizer",
            "kernel_constraint",
            "bias_initializer",
            "bias_regularizer",
            "bias_constraint",
        ]:
            param_value = kwargs.pop(param_name, None)
            if param_value is not None:
                regularisers[param_name] = param_value
        self._regularisers = regularisers

    def __call__(self, x):
        """
        Apply a stack of RGCN layers to the inputs.
        The input tensors are expected to be a list of the following:
        [Node features shape (1, N, F), Output indices (1, O)] +
        [Adjacency indices for each relationship (1, E, 2) for _ in range(R)]
        [Adjacency values for each relationshiop (1, E) for _ in range(R)]


        where N is the number of nodes, F the number of input features,
              E is the number of edges, O the number of output nodes,
              R is the number of relationships in the graph (edge types).

        Args:
            x (Tensor): input tensors

        Returns:
            Output tensor
        """
        x_in, out_indices, *As = x

        # Currently we require the batch dimension to be one for full-batch methods
        batch_dim, n_nodes, _ = K.int_shape(x_in)
        if batch_dim != 1:
            raise ValueError(
                "Currently full-batch methods only support a batch dimension of one"
            )

        N_edge_types = len(self.generator.As)

        # Convert input indices & values to sparse matrices
        if self.use_sparse:

            As_indices = As[:N_edge_types]
            As_values = As[N_edge_types:]

            Ainput = [
                SqueezedSparseConversion(
                    shape=(n_nodes, n_nodes), dtype=As_values[i].dtype
                )([As_indices[i], As_values[i]])
                for i in range(N_edge_types)
            ]

        # Otherwise, create dense matrices from input tensor
        else:
            Ainput = [Lambda(lambda A: K.squeeze(A, 0))(A_) for A_ in As]

        h_layer = x_in

        for layer in self._layers:
            if isinstance(layer, RelationalGraphConvolution):
                # For an RGCN layer add the adjacency matrices and output indices
                # Note that the output indices are only used if `final_layer=True`
                h_layer = layer([h_layer, out_indices] + Ainput)
            else:
                # For other (non-graph) layers only supply the input tensor
                h_layer = layer(h_layer)

        return h_layer

    def node_model(self):
        """
        Builds a RGCN model for node prediction

        Returns:
            tuple: `(x_inp, x_out)`, where
            `x_inp` is a list of Keras input tensors for the RGCN model (containing node features,
             node indices, and the indices and values for the sparse adjacency matrices for each relationship),
            and `x_out` is a Keras tensor for the RGCN model output.
        """
        # Placeholder for node features
        N_nodes = self.generator.features.shape[0]
        N_feat = self.generator.features.shape[1]
        N_edge_types = len(self.generator.As)

        # Inputs for features & target indices
        x_t = Input(batch_shape=(1, N_nodes, N_feat))
        out_indices_t = Input(batch_shape=(1, None), dtype="int32")

        # Create inputs for sparse or dense matrices
        if self.use_sparse:
            # Placeholders for the sparse adjacency matrix
            A_indices_t = [Input(batch_shape=(1, None, 2), dtype="int64") for i in range(N_edge_types)]
            A_values_t = [Input(batch_shape=(1, None)) for i in range(N_edge_types)]
            A_placeholders = A_indices_t + A_values_t

        else:
            # Placeholders for the dense adjacency matrix
            A_placeholders = [Input(batch_shape=(1, N_nodes, N_nodes)) for i in range(N_edge_types)]

        x_inp = [x_t, out_indices_t] + A_placeholders
        x_out = self(x_inp)

        # Flatten output by removing singleton batch dimension
        if x_out.shape[0] == 1:
            self.x_out_flat = Lambda(lambda x: K.squeeze(x, 0))(x_out)
        else:
            self.x_out_flat = x_out

        return x_inp, x_out
