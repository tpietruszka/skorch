"""Neural net classes."""

import fnmatch
from itertools import chain
from functools import partial
import re
import tempfile
import warnings
import inspect

import numpy as np
from sklearn.base import BaseEstimator
import torch
from torch.utils.data import DataLoader

from skorch.callbacks import EpochTimer
from skorch.callbacks import PrintLog
from skorch.callbacks import EpochScoring
from skorch.callbacks import BatchScoring
from skorch.dataset import Dataset
from skorch.dataset import CVSplit
from skorch.dataset import get_len
from skorch.exceptions import DeviceWarning
from skorch.exceptions import NotInitializedError
from skorch.history import History
from skorch.utils import duplicate_items
from skorch.utils import get_dim
from skorch.utils import is_dataset
from skorch.utils import to_numpy
from skorch.utils import to_tensor
from skorch.utils import params_for


# pylint: disable=unused-argument
def train_loss_score(net, X=None, y=None):
    return net.history[-1, 'batches', -1, 'train_loss']


# pylint: disable=unused-argument
def valid_loss_score(net, X=None, y=None):
    return net.history[-1, 'batches', -1, 'valid_loss']


# pylint: disable=too-many-instance-attributes
class NeuralNet(object):
    # pylint: disable=anomalous-backslash-in-string
    """NeuralNet base class.

    The base class covers more generic cases. Depending on your use
    case, you might want to use ``NeuralNetClassifier`` or
    ``NeuralNetRegressor``.

    In addition to the parameters listed below, there are parameters
    with specific prefixes that are handled separately. To illustrate
    this, here is an example:

    >>> net = NeuralNet(
    ...    ...,
    ...    optimizer=torch.optimizer.SGD,
    ...    optimizer__momentum=0.95,
    ...)

    This way, when ``optimizer`` is initialized, ``NeuralNet`` will
    take care of setting the ``momentum`` parameter to 0.95.

    (Note that the double underscore notation in
    ``optimizer__momentum`` means that the parameter ``momentum``
    should be set on the object ``optimizer``. This is the same
    semantic as used by sklearn.)

    Furthermore, this allows to change those parameters later:

    ``net.set_params(optimizer__momentum=0.99)``

    This can be useful when you want to change certain parameters
    using a callback, when using the net in an sklearn grid search,
    etc.

    By default an ``EpochTimer``, ``AverageLoss``, ``BestLoss``, and
    ``PrintLog`` callback is installed for the user's convenience.

    Parameters
    ----------
    module : torch module (class or instance)
      A torch module. In general, the uninstantiated class should be
      passed, although instantiated modules will also work.

    criterion : torch criterion (class)
      The uninitialized criterion (loss) used to optimize the
      module.

    optimizer : torch optim (class, default=torch.optim.SGD)
      The uninitialized optimizer (update rule) used to optimize the
      module

    lr : float (default=0.01)
      Learning rate passed to the optimizer. You may use ``lr`` instead
      of using ``optimizer__lr``, which would result in the same outcome.

    max_epochs : int (default=10)
      The number of epochs to train for each ``fit`` call. Note that you
      may keyboard-interrupt training at any time.

    batch_size : int (default=128)
      Mini-batch size. Use this instead of setting
      ``iterator_train__batch_size`` and ``iterator_test__batch_size``,
      which would result in the same outcome.

    iterator_train : torch DataLoader
      The default ``torch.utils.data.DataLoader`` used for training
      data.

    iterator_valid : torch DataLoader
      The default ``torch.utils.data.DataLoader`` used for validation
      and test data, i.e. during inference.

    dataset : torch Dataset (default=skorch.dataset.Dataset)
      The dataset is necessary for the incoming data to work with
      pytorch's ``DataLoader``. It has to implement the ``__len__`` and
      ``__getitem__`` methods. The provided dataset should be capable of
      dealing with a lot of data types out of the box, so only change
      this if your data is not supported. Additionally, dataset should
      accept a ``device`` parameter to indicate the location of the
      data (e.g., CUDA).
      You should generally pass the uninitialized ``Dataset`` class
      and define additional arguments to X and y by prefixing them
      with ``dataset__``. It is also possible to pass an initialzed
      ``Dataset``, in which case no additional arguments may be
      passed.

    train_split : None or callable (default=skorch.dataset.CVSplit(5))
      If None, there is no train/validation split. Else, train_split
      should be a function or callable that is called with X and y
      data and should return the tuple ``X_train, X_valid, y_train,
      y_valid``. The validation data may be None.

    callbacks : None or list of Callback instances (default=None)
      More callbacks, in addition to those returned by
      ``get_default_callbacks``. Each callback should inherit from
      skorch.Callback. If not None, a list of tuples (name, callback)
      should be passed, where names should be unique. Callbacks may or
      may not be instantiated.
      Alternatively, it is possible to just pass a list of callbacks,
      which results in names being inferred from the class name.
      The callback name can be used to set parameters on specific
      callbacks (e.g., for the callback with name ``'print_log'``, use
      ``net.set_params(callbacks__print_log__keys=['epoch',
      'train_loss'])``).

    warm_start : bool (default=False)
      Whether each fit call should lead to a re-initialization of the
      module (cold start) or whether the module should be trained
      further (warm start).

    verbose : int (default=1)
      Control the verbosity level.

    device : str, torch.device (default='cpu')
      The compute device to be used. If set to 'cuda', data in torch
      tensors will be pushed to cuda tensors before being sent to the
      module.

    Attributes
    ----------
    prefixes\_ : list of str
      Contains the prefixes to special parameters. E.g., since there
      is the ``'module'`` prefix, it is possible to set parameters like
      so: ``NeuralNet(..., optimizer__momentum=0.95)``.

    cuda_dependent_attributes\_ : list of str
      Contains a list of all attributes whose values depend on a CUDA
      device. If a ``NeuralNet`` trained with a CUDA-enabled device is
      unpickled on a machine without CUDA or with CUDA disabled, the
      listed attributes are mapped to CPU.  Expand this list if you
      want to add other cuda-dependent attributes.

    initialized\_ : bool
      Whether the NeuralNet was initialized.

    module\_ : torch module (instance)
      The instantiated module.

    criterion\_ : torch criterion (instance)
      The instantiated criterion.

    callbacks\_ : list of tuples
      The complete (i.e. default and other), initialized callbacks, in
      a tuple with unique names.

    """
    prefixes_ = ['module', 'iterator_train', 'iterator_valid', 'optimizer',
                 'criterion', 'callbacks', 'dataset']

    cuda_dependent_attributes_ = ['module_', 'optimizer_']

    # pylint: disable=too-many-arguments
    def __init__(
            self,
            module,
            criterion,
            optimizer=torch.optim.SGD,
            lr=0.01,
            max_epochs=10,
            batch_size=128,
            iterator_train=DataLoader,
            iterator_valid=DataLoader,
            dataset=Dataset,
            train_split=CVSplit(5),
            callbacks=None,
            warm_start=False,
            verbose=1,
            device='cpu',
            **kwargs
    ):
        self.module = module
        self.criterion = criterion
        self.optimizer = optimizer
        self.lr = lr
        self.max_epochs = max_epochs
        self.batch_size = batch_size
        self.iterator_train = iterator_train
        self.iterator_valid = iterator_valid
        self.dataset = dataset
        self.train_split = train_split
        self.callbacks = callbacks
        self.warm_start = warm_start
        self.verbose = verbose
        self.device = device

        self._check_deprecated_params(**kwargs)
        history = kwargs.pop('history', None)
        initialized = kwargs.pop('initialized_', False)

        # catch arguments that seem to not belong anywhere
        unexpected_kwargs = []
        for key in kwargs:
            if key.endswith('_'):
                continue
            if any(key.startswith(p) for p in self.prefixes_):
                continue
            unexpected_kwargs.append(key)
        if unexpected_kwargs:
            msg = ("__init__() got unexpected argument(s) {}."
                   "Either you made a typo, or you added new arguments "
                   "in a subclass; if that is the case, the subclass "
                   "should deal with the new arguments explicitely.")
            raise TypeError(msg.format(', '.join(unexpected_kwargs)))
        vars(self).update(kwargs)

        self.history = history
        self.initialized_ = initialized

    @property
    def _default_callbacks(self):
        return [
            ('epoch_timer', EpochTimer()),
            ('train_loss', BatchScoring(
                train_loss_score,
                name='train_loss',
                on_train=True,
            )),
            ('valid_loss', BatchScoring(
                valid_loss_score,
                name='valid_loss',
            )),
            ('print_log', PrintLog()),
        ]

    def get_default_callbacks(self):
        return self._default_callbacks

    def notify(self, method_name, **cb_kwargs):
        """Call the callback method specified in ``method_name`` with
        parameters specified in ``cb_kwargs``.

        Method names can be one of:
        * on_train_begin
        * on_train_end
        * on_epoch_begin
        * on_epoch_end
        * on_batch_begin
        * on_batch_end

        """
        getattr(self, method_name)(self, **cb_kwargs)
        for _, cb in self.callbacks_:
            getattr(cb, method_name)(self, **cb_kwargs)

    # pylint: disable=unused-argument
    def on_train_begin(self, net, **kwargs):
        pass

    # pylint: disable=unused-argument
    def on_train_end(self, net, **kwargs):
        pass

    # pylint: disable=unused-argument
    def on_epoch_begin(self, net, **kwargs):
        self.history.new_epoch()
        self.history.record('epoch', len(self.history))

    # pylint: disable=unused-argument
    def on_epoch_end(self, net, **kwargs):
        pass

    # pylint: disable=unused-argument
    def on_batch_begin(self, net, training=False, **kwargs):
        self.history.new_batch()

    def on_batch_end(self, net, **kwargs):
        pass

    def on_grad_computed(self, net, named_parameters, **kwargs):
        pass

    def _yield_callbacks(self):
        """Yield all callbacks set on this instance.

        Handles these cases:
          * default and user callbacks
          * callbacks with and without name
          * initialized and uninitialized callbacks
          * puts PrintLog(s) last

        """
        print_logs = []
        for item in self.get_default_callbacks() + (self.callbacks or []):
            if isinstance(item, (tuple, list)):
                name, cb = item
            else:
                cb = item
                if isinstance(cb, type):  # uninitialized:
                    name = cb.__name__
                else:
                    name = cb.__class__.__name__
            if isinstance(cb, PrintLog) or (cb == PrintLog):
                print_logs.append((name, cb))
            else:
                yield name, cb
        yield from print_logs

    def initialize_callbacks(self):
        """Initializes all callbacks and save the result in the
        ``callbacks_`` attribute.

        Both ``default_callbacks`` and ``callbacks`` are used (in that
        order). Callbacks may either be initialized or not, and if
        they don't have a name, the name is inferred from the class
        name. The ``initialize`` method is called on all callbacks.

        The final result will be a list of tuples, where each tuple
        consists of a name and an initialized callback. If names are
        not unique, a ValueError is raised.

        """
        names_seen = set()
        callbacks_ = []

        class Dummy:
            # We cannot use None as dummy value since None is a
            # legitimate value to be set.
            pass

        for name, cb in self._yield_callbacks():
            if name in names_seen:
                raise ValueError("The callback name '{}' appears more than "
                                 "once.".format(name))
            names_seen.add(name)

            # check if callback itself is changed
            param_callback = getattr(self, 'callbacks__' + name, Dummy)
            if param_callback is not Dummy:  # callback itself was set
                cb = param_callback

            # below: check for callback params
            # don't set a parameter for non-existing callback
            params = self._get_params_for('callbacks__{}'.format(name))
            if (cb is None) and params:
                raise ValueError("Trying to set a parameter for callback {} "
                                 "which does not exist.".format(name))
            if cb is None:
                continue

            if isinstance(cb, type):  # uninitialized:
                cb = cb(**params)
            else:
                cb.set_params(**params)
            cb.initialize()
            callbacks_.append((name, cb))

        self.callbacks_ = callbacks_
        return self

    def initialize_criterion(self):
        """Initializes the criterion."""
        criterion_params = self._get_params_for('criterion')
        self.criterion_ = self.criterion(**criterion_params)
        return self

    def initialize_module(self):
        """Initializes the module.

        Note that if the module has learned parameters, those will be
        reset.

        """
        kwargs = self._get_params_for('module')
        module = self.module
        is_initialized = isinstance(module, torch.nn.Module)

        if kwargs or not is_initialized:
            if is_initialized:
                module = type(module)

            if is_initialized or self.initialized_:
                if self.verbose:
                    print("Re-initializing module!")

            module = module(**kwargs)

        self.module_ = module.to(self.device)
        return self

    def initialize_optimizer(self):
        """Initialize the model optimizer. If ``self.optimizer__lr``
        is not set, use ``self.lr`` instead.

        """
        args, kwargs = self._get_params_for_optimizer(
            'optimizer', self.module_.named_parameters())

        if 'lr' not in kwargs:
            kwargs['lr'] = self.lr

        self.optimizer_ = self.optimizer(*args, **kwargs)

        # some optimizers require scoring closures to call them multiple times during each step. Those need special
        # treatment in train_step() - the actually used
        optimizer_step_signature = inspect.signature(self.optimizer_.step)
        self.optimizer_is_iterative_= optimizer_step_signature.parameters['closure'].default is inspect.Parameter.empty

    def initialize_history(self):
        """Initializes the history."""
        self.history = History()

    def initialize(self):
        """Initializes all components of the NeuralNet and returns
        self.

        """
        self.initialize_callbacks()
        self.initialize_criterion()
        self.initialize_module()
        self.initialize_optimizer()
        self.initialize_history()

        self.initialized_ = True
        return self

    def check_data(self, X, y=None):
        pass

    def validation_step(self, Xi, yi, **fit_params):
        """Perform a forward step using batched data and return the
        resulting loss.

        The module is set to be in evaluation mode (e.g. dropout is
        not applied).

        Parameters
        ----------
        Xi : input data
          A batch of the input data.

        yi : target data
          A batch of the target data.

        **fit_params : dict
          Additional parameters passed to the ``forward`` method of
          the module and to the train_split call.

        """
        self.module_.eval()
        y_pred = self.infer(Xi, **fit_params)
        loss = self.get_loss(y_pred, yi, X=Xi, training=False)
        return {
            'loss': loss,
            'y_pred': y_pred,
            }

    def train_step(self, Xi, yi, **fit_params):
        """Perform a forward step using batched data, update module
        parameters, and return the loss.

        The module is set to be in train mode (e.g. dropout is
        applied).

        Parameters
        ----------
        Xi : input data
          A batch of the input data.

        yi : target data
          A batch of the target data.

        **fit_params : dict
          Additional parameters passed to the ``forward`` method of
          the module and to the train_split call.

        """
        y_pred = None
        loss = None

        def loss_closure():
            nonlocal y_pred
            nonlocal loss
            self.module_.train()
            self.optimizer_.zero_grad()
            y_pred = self.infer(Xi, **fit_params)
            loss = self.get_loss(y_pred, yi, X=Xi, training=True)
            loss.backward()
            self.notify(
                'on_grad_computed',
                named_parameters=list(self.module_.named_parameters())
            )
            return loss

        self.optimizer_.step(loss_closure)

        if self.optimizer_is_iterative_:
            # re-computing as we cannot know if the results of the last loss_closure() were actually used
            y_pred = self.infer(Xi, **fit_params)
            loss = self.get_loss(y_pred, yi, X=Xi, training=True)

        return {
            'loss': loss,
            'y_pred': y_pred,
        }

    def evaluation_step(self, Xi, training=False):
        """Perform a forward step to produce the output used for
        prediction and scoring.

        Therefore the module is set to evaluation mode by default
        beforehand which can be overridden to re-enable features
        like dropout by setting ``training=True``.

        """
        self.module_.train(training)
        return self.infer(Xi)

    def fit_loop(self, X, y=None, epochs=None, **fit_params):
        """The proper fit loop.

        Contains the logic of what actually happens during the fit
        loop.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        y : target data, compatible with skorch.dataset.Dataset
          The same data types as for ``X`` are supported. If your X is
          a Dataset that contains the target, ``y`` may be set to
          None.

        epochs : int or None (default=None)
          If int, train for this number of epochs; if None, use
          ``self.max_epochs``.

        **fit_params : dict
          Additional parameters passed to the ``forward`` method of
          the module and to the train_split call.

        """
        self.check_data(X, y)
        epochs = epochs if epochs is not None else self.max_epochs

        dataset_train, dataset_valid = self.get_split_datasets(
            X, y, **fit_params)
        on_epoch_kwargs = {
            'dataset_train': dataset_train,
            'dataset_valid': dataset_valid,
        }

        for _ in range(epochs):
            self.notify('on_epoch_begin', **on_epoch_kwargs)

            for Xi, yi in self.get_iterator(dataset_train, training=True):
                self.notify('on_batch_begin', X=Xi, y=yi, training=True)
                step = self.train_step(Xi, yi, **fit_params)
                self.history.record_batch(
                    'train_loss', step['loss'].data.item())
                self.history.record_batch('train_batch_size', get_len(Xi))
                self.notify('on_batch_end', X=Xi, y=yi, training=True, **step)

            if dataset_valid is None:
                self.notify('on_epoch_end', **on_epoch_kwargs)
                continue

            for Xi, yi in self.get_iterator(dataset_valid, training=False):
                self.notify('on_batch_begin', X=Xi, y=yi, training=False)
                step = self.validation_step(Xi, yi, **fit_params)
                self.history.record_batch(
                    'valid_loss', step['loss'].data.item())
                self.history.record_batch('valid_batch_size', get_len(Xi))
                self.notify('on_batch_end', X=Xi, y=yi, training=False, **step)

            self.notify('on_epoch_end', **on_epoch_kwargs)
        return self

    # pylint: disable=unused-argument
    def partial_fit(self, X, y=None, classes=None, **fit_params):
        """Fit the module.

        If the module is initialized, it is not re-initialized, which
        means that this method should be used if you want to continue
        training a model (warm start).

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        y : target data, compatible with skorch.dataset.Dataset
          The same data types as for ``X`` are supported. If your X is
          a Dataset that contains the target, ``y`` may be set to
          None.

        classes : array, sahpe (n_classes,)
          Solely for sklearn compatibility, currently unused.

        **fit_params : dict
          Additional parameters passed to the ``forward`` method of
          the module and to the train_split call.

        """
        if not self.initialized_:
            self.initialize()

        self.notify('on_train_begin')
        try:
            self.fit_loop(X, y, **fit_params)
        except KeyboardInterrupt:
            pass
        self.notify('on_train_end')
        return self

    def fit(self, X, y=None, **fit_params):
        """Initialize and fit the module.

        If the module was already initialized, by calling fit, the
        module will be re-initialized (unless ``warm_start`` is True).

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        y : target data, compatible with skorch.dataset.Dataset
          The same data types as for ``X`` are supported. If your X is
          a Dataset that contains the target, ``y`` may be set to
          None.

        **fit_params : dict
          Additional parameters passed to the ``forward`` method of
          the module and to the train_split call.

        """
        if not self.warm_start or not self.initialized_:
            self.initialize()

        self.partial_fit(X, y, **fit_params)
        return self

    def forward_iter(self, X, training=False, device='cpu'):
        """Yield outputs of module forward calls on each batch of data.
        The storage device of the yielded tensors is determined
        by the ``device`` parameter.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        training : bool (default=False)
          Whether to set the module to train mode or not.

        device : string (default='cpu')
          The device to store each inference result on.
          This defaults to CPU memory since there is genereally
          more memory available there. For performance reasons
          this might be changed to a specific CUDA device,
          e.g. 'cuda:0'.

        Yields
        ------
        yp : torch tensor
          Result from a forward call on an individual batch.

        """
        dataset = X if is_dataset(X) else self.get_dataset(X)
        iterator = self.get_iterator(dataset, training=training)
        for Xi, _ in iterator:
            yp = self.evaluation_step(Xi, training=training)
            if isinstance(yp, tuple):
                yield tuple(n.to(device) for n in yp)
            else:
                yield yp.to(device)

    def forward(self, X, training=False, device='cpu'):
        """Gather and concatenate the output from forward call with
        input data.

        The outputs from ``self.module_.forward`` are gathered on the
        compute device specified by ``device`` and then concatenated
        using ``torch.cat``. If multiple outputs are returned by
        ``self.module_.forward``, each one of them must be able to be
        concatenated this way.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        training : bool (default=False)
          Whether to set the module to train mode or not.

        device : string (default='cpu')
          The device to store each inference result on.
          This defaults to CPU memory since there is genereally
          more memory available there. For performance reasons
          this might be changed to a specific CUDA device,
          e.g. 'cuda:0'.

        Returns
        -------
        y_infer : torch tensor
          The result from the forward step.

        """
        y_infer = list(self.forward_iter(X, training=training, device=device))

        is_multioutput = len(y_infer) > 0 and isinstance(y_infer[0], tuple)
        if is_multioutput:
            return tuple(map(torch.cat, zip(*y_infer)))
        return torch.cat(y_infer)

    def _merge_x_and_fit_params(self, x, fit_params):
        duplicates = duplicate_items(x, fit_params)
        if duplicates:
            msg = "X and fit_params contain duplicate keys: "
            msg += ', '.join(duplicates)
            raise ValueError(msg)

        x_dict = dict(x)  # shallow copy
        x_dict.update(fit_params)
        return x_dict

    def infer(self, x, **fit_params):
        """Perform a single inference step on a batch of data.

        Parameters
        ----------
        x : input data
          A batch of the input data.

        **fit_params : dict
          Additional parameters passed to the ``forward`` method of
          the module and to the train_split call.

        """
        x = to_tensor(x, device=self.device)
        if isinstance(x, dict):
            x_dict = self._merge_x_and_fit_params(x, fit_params)
            return self.module_(**x_dict)
        return self.module_(x, **fit_params)

    def predict_proba(self, X):
        """Return the output of the module's forward method as a numpy
        array.

        If forward returns multiple outputs as a tuple, it is assumed
        that the first output contains the relevant information. The
        other values are ignored.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        Returns
        -------
        y_proba : numpy ndarray

        """
        y_probas = []
        for yp in self.forward_iter(X, training=False):
            yp = yp[0] if isinstance(yp, tuple) else yp
            y_probas.append(to_numpy(yp))
        y_proba = np.concatenate(y_probas, 0)
        return y_proba

    def predict(self, X):
        """Where applicable, return class labels for samples in X.

        If the module's forward method returns multiple outputs as a
        tuple, it is assumed that the first output contains the
        relevant information. The other values are ignored.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        Returns
        -------
        y_pred : numpy ndarray

        """
        return self.predict_proba(X)

    # pylint: disable=unused-argument
    def get_loss(self, y_pred, y_true, X=None, training=False):
        """Return the loss for this batch.

        Parameters
        ----------
        y_pred : torch tensor
          Predicted target values

        y_true : torch tensor
          True target values.

        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        train : bool (default=False)
          Whether train mode should be used or not.

        """
        y_true = to_tensor(y_true, device=self.device)
        return self.criterion_(y_pred, y_true)

    def get_dataset(self, X, y=None):
        """Get a dataset that contains the input data and is passed to
        the iterator.

        Override this if you want to initialize your dataset
        differently.

        If ``dataset__device`` is not set, use ``self.device`` instead.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        y : target data, compatible with skorch.dataset.Dataset
          The same data types as for ``X`` are supported. If your X is
          a Dataset that contains the target, ``y`` may be set to
          None.

        Returns
        -------
        dataset
          The initialized dataset.

        """
        if is_dataset(X):
            return X

        dataset = self.dataset
        is_initialized = not callable(dataset)

        kwargs = self._get_params_for('dataset')
        if kwargs and is_initialized:
            raise TypeError("Trying to pass an initialized Dataset while "
                            "passing Dataset arguments ({}) is not "
                            "allowed.".format(kwargs))

        if is_initialized:
            return dataset

        if 'device' not in kwargs:
            kwargs['device'] = self.device

        return dataset(X, y, **kwargs)

    def get_split_datasets(self, X, y=None, **fit_params):
        """Get internal train and validation datasets.

        The validation dataset can be None if ``self.train_split`` is
        set to None; then internal validation will be skipped.

        Override this if you want to change how the net splits
        incoming data into train and validation part.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        y : target data, compatible with skorch.dataset.Dataset
          The same data types as for ``X`` are supported. If your X is
          a Dataset that contains the target, ``y`` may be set to
          None.

        **fit_params : dict
          Additional parameters passed to the train_split call.

        Returns
        -------
        dataset_train
          The initialized training dataset.

        dataset_valid
          The initialized validation dataset or None

        """
        dataset = self.get_dataset(X, y)
        if self.train_split:
            dataset_train, dataset_valid = self.train_split(
                dataset, y, **fit_params)
        else:
            dataset_train, dataset_valid = dataset, None
        return dataset_train, dataset_valid

    def get_iterator(self, dataset, training=False):
        """Get an iterator that allows to loop over the batches of the
        given data.

        If ``self.iterator_train__batch_size`` and/or
        ``self.iterator_test__batch_size`` are not set, use
        ``self.batch_size`` instead.

        Parameters
        ----------
        dataset : torch Dataset (default=skorch.dataset.Dataset)
          Usually, ``self.dataset``, initialized with the corresponding
          data, is passed to ``get_iterator``.

        training : bool (default=False)
          Whether to use ``iterator_train`` or ``iterator_test``.

        Returns
        -------
        iterator
          An instantiated iterator that allows to loop over the
          mini-batches.

        """
        if training:
            kwargs = self._get_params_for('iterator_train')
            iterator = self.iterator_train
        else:
            kwargs = self._get_params_for('iterator_valid')
            iterator = self.iterator_valid

        if 'batch_size' not in kwargs:
            kwargs['batch_size'] = self.batch_size

        return iterator(dataset, **kwargs)

    def _get_params_for(self, prefix):
        return params_for(prefix, self.__dict__)

    def _get_params_for_optimizer(self, prefix, named_parameters):
        """Parse kwargs configuration for the optimizer identified by
        the given prefix. Supports param group assignment using wildcards:

            optimizer__lr=0.05,
            optimizer__param_groups=[
                ('rnn*.period', {'lr': 0.3, 'momentum': 0}),
                ('rnn0', {'lr': 0.1}),
            ]

        The first positional argument are the param groups.
        """
        kwargs = self._get_params_for(prefix)
        params = list(named_parameters)
        pgroups = []

        for pattern, group in kwargs.pop('param_groups', []):
            matches = [i for i, (name, _) in enumerate(params) if
                       fnmatch.fnmatch(name, pattern)]
            if matches:
                p = [params.pop(i)[1] for i in reversed(matches)]
                pgroups.append({'params': p, **group})

        if params:
            pgroups.append({'params': [p for _, p in params]})

        return [pgroups], kwargs

    def _get_param_names(self):
        return self.__dict__.keys()

    def _get_params_callbacks(self, deep=True):
        """sklearn's .get_params checks for `hasattr(value,
        'get_params')`. This returns False for a list. But our
        callbacks reside within a list. Hence their parameters have to
        be retrieved separately.
        """
        params = {}
        if not deep:
            return params

        callbacks_ = getattr(self, 'callbacks_', [])
        for key, val in chain(callbacks_, self._default_callbacks):
            name = 'callbacks__' + key
            params[name] = val
            if val is None:  # callback deactivated
                continue
            for subkey, subval in val.get_params().items():
                subname = name + '__' + subkey
                params[subname] = subval
        return params

    def get_params(self, deep=True, **kwargs):
        params = BaseEstimator.get_params(self, deep=deep, **kwargs)
        # Callback parameters are not returned by .get_params, needs
        # special treatment.
        params_cb = self._get_params_callbacks(deep=deep)
        params.update(params_cb)
        return params

    # XXX remove once deprecation for use_cuda is phased out
    # Also remember to update NeuralNet docstring
    def _check_deprecated_params(self, **kwargs):
        if kwargs.get('use_cuda') is not None:
            msg = ("The parameter use_cuda is no longer supported. Use "
                   "device='cuda' instead.")
            raise ValueError(msg)

    def set_params(self, **kwargs):
        """Set the parameters of this class.

        Valid parameter keys can be listed with ``get_params()``.

        Returns
        -------
        self

        """
        self._check_deprecated_params(**kwargs)
        normal_params, cb_params, special_params = {}, {}, {}
        for key, val in kwargs.items():
            if key.startswith('callbacks'):
                cb_params[key] = val
            elif any(key.startswith(prefix) for prefix in self.prefixes_):
                special_params[key] = val
            else:
                normal_params[key] = val

        BaseEstimator.set_params(self, **normal_params)

        for key, val in special_params.items():
            if key.endswith('_'):
                raise ValueError("Not sure: Should this ever happen?")
            else:
                setattr(self, key, val)

        if cb_params:
            # callbacks need special treatmeant since they are list of tuples
            self.initialize_callbacks()
            self._set_params_callback(**cb_params)
        if any(key.startswith('criterion') for key in special_params):
            self.initialize_criterion()
        if any(key.startswith('module') for key in special_params):
            self.initialize_module()
            self.initialize_optimizer()
        if any(key.startswith('optimizer') for key in special_params):
            # Model selectors such as GridSearchCV will set the
            # parameters before .initialize() is called, therefore we
            # need to make sure that we have an initialized model here
            # as the optimizer depends on it.
            if not hasattr(self, 'module_'):
                self.initialize_module()
            self.initialize_optimizer()

        vars(self).update(kwargs)

        return self

    def _set_params_callback(self, **params):
        # model after sklearn.utils._BaseCompostion._set_params
        # 1. All steps
        if 'callbacks' in params:
            setattr(self, 'callbacks', params.pop('callbacks'))

        # 2. Step replacement
        names, _ = zip(*getattr(self, 'callbacks_'))
        for key in params.copy():
            name = key[11:]  # drop 'callbacks__'
            if '__' not in name and name in names:
                self._replace_callback(name, params.pop(key))

        # 3. Step parameters and other initilisation arguments
        for key in params.copy():
            name = key[11:]
            part0, part1 = name.split('__')
            kwarg = {part1: params.pop(key)}
            callback = dict(self.callbacks_).get(part0)
            if callback is not None:
                callback.set_params(**kwarg)
            else:
                raise ValueError(
                    "Trying to set a parameter for callback {} "
                    "which does not exist.".format(part0))

        return self

    def _replace_callback(self, name, new_val):
        # assumes `name` is a valid callback name
        callbacks_new = self.callbacks_[:]
        for i, (cb_name, _) in enumerate(callbacks_new):
            if cb_name == name:
                callbacks_new[i] = (name, new_val)
                break
        setattr(self, 'callbacks_', callbacks_new)

    def __getstate__(self):
        state = self.__dict__.copy()
        for key in self.cuda_dependent_attributes_:
            if key in state:
                val = state.pop(key)
                with tempfile.SpooledTemporaryFile() as f:
                    torch.save(val, f)
                    f.seek(0)
                    state[key] = f.read()

        return state

    def __setstate__(self, state):
        def uses_cuda(device):
            if isinstance(device, torch.device):
                device = device.type
            return device.startswith('cuda')

        disable_cuda = False
        for key in self.cuda_dependent_attributes_:
            if key not in state:
                continue
            dump = state.pop(key)
            with tempfile.SpooledTemporaryFile() as f:
                f.write(dump)
                f.seek(0)
                if (
                        uses_cuda(state['device']) and
                        not torch.cuda.is_available()
                ):
                    disable_cuda = True
                    val = torch.load(
                        f, map_location=lambda storage, loc: storage)
                else:
                    val = torch.load(f)
            state[key] = val
        if disable_cuda:
            warnings.warn(
                "Model configured to use CUDA but no CUDA devices "
                "available. Loading on CPU instead.",
                DeviceWarning)
            state['device'] = 'cpu'

        self.__dict__.update(state)

    def save_params(self, f):
        """Save only the module's parameters, not the whole object.

        To save the whole object, use pickle.

        Parameters
        ----------
        f : file-like object or str
          See ``torch.save`` documentation.

        Examples
        --------
        >>> before = NeuralNetClassifier(mymodule)
        >>> before.save_params('path/to/file')
        >>> after = NeuralNetClassifier(mymodule).initialize()
        >>> after.load_params('path/to/file')

        """
        if not hasattr(self, 'module_'):
            raise NotInitializedError(
                "Cannot save parameters of an un-initialized model. "
                "Please initialize first by calling .initialize() "
                "or by fitting the model with .fit(...).")
        torch.save(self.module_.state_dict(), f)

    def load_params(self, f):
        """Load only the module's parameters, not the whole object.

        To save and load the whole object, use pickle.

        Parameters
        ----------
        f : file-like object or str
          See ``torch.load`` documentation.

        Examples
        --------
        >>> before = NeuralNetClassifier(mymodule)
        >>> before.save_params('path/to/file')
        >>> after = NeuralNetClassifier(mymodule).initialize()
        >>> after.load_params('path/to/file')

        """
        if not hasattr(self, 'module_'):
            raise NotInitializedError(
                "Cannot load parameters of an un-initialized model. "
                "Please initialize first by calling .initialize() "
                "or by fitting the model with .fit(...).")

        use_cuda = self.device.startswith('cuda')
        cuda_req_not_met = (use_cuda and not torch.cuda.is_available())
        if use_cuda or cuda_req_not_met:
            # Eiher we want to load the model to the CPU in which case
            # we are loading in a way where it doesn't matter if the data
            # was on the GPU or not or the model was on the GPU but there
            # is no CUDA device available.
            if cuda_req_not_met:
                warnings.warn(
                    "Model configured to use CUDA but no CUDA devices "
                    "available. Loading on CPU instead.",
                    ResourceWarning)
                self.device = 'cpu'
            model = torch.load(f, lambda storage, loc: storage)
        else:
            model = torch.load(f)

        self.module_.load_state_dict(model)

    def __repr__(self):
        params = self.get_params(deep=False)

        to_include = ['module']
        to_exclude = []
        parts = [str(self.__class__) + '[uninitialized](']
        if self.initialized_:
            parts = [str(self.__class__) + '[initialized](']
            to_include = ['module_']
            to_exclude = ['module__']

        for key, val in sorted(params.items()):
            if not any(key.startswith(prefix) for prefix in to_include):
                continue
            if any(key.startswith(prefix) for prefix in to_exclude):
                continue

            val = str(val)
            if '\n' in val:
                val = '\n  '.join(val.split('\n'))
            parts.append('  {}={},'.format(key, val))

        parts.append(')')
        return '\n'.join(parts)


#######################
# NeuralNetClassifier #
#######################

def accuracy_pred_extractor(y):
    return np.argmax(to_numpy(y), axis=1)


neural_net_clf_doc_start = """NeuralNet for classification tasks

    Use this specifically if you have a standard classification task,
    with input data X and target y.

"""

neural_net_clf_criterion_text = """

    criterion : torch criterion (class, default=torch.nn.NLLLoss)
      Negative log likelihood loss. Note that the module should return
      probabilities, the log is applied during ``get_loss``."""


def get_neural_net_clf_doc(doc):
    doc = neural_net_clf_doc_start + doc.split("\n ", 4)[-1]
    pattern = re.compile(r'(\n\s+)(criterion .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + neural_net_clf_criterion_text + doc[end:]
    return doc


# pylint: disable=missing-docstring
class NeuralNetClassifier(NeuralNet):
    __doc__ = get_neural_net_clf_doc(NeuralNet.__doc__)

    def __init__(
            self,
            module,
            criterion=torch.nn.NLLLoss,
            train_split=CVSplit(5, stratified=True),
            *args,
            **kwargs
    ):
        super(NeuralNetClassifier, self).__init__(
            module,
            criterion=criterion,
            train_split=train_split,
            *args,
            **kwargs
        )

    @property
    def _default_callbacks(self):
        return [
            ('epoch_timer', EpochTimer()),
            ('train_loss', BatchScoring(
                train_loss_score,
                name='train_loss',
                on_train=True,
            )),
            ('valid_loss', BatchScoring(
                valid_loss_score,
                name='valid_loss',
            )),
            ('valid_acc', EpochScoring(
                'accuracy',
                name='valid_acc',
                lower_is_better=False,
            )),
            ('print_log', PrintLog()),
        ]

    # pylint: disable=signature-differs
    def check_data(self, X, y):
        if (
                (y is None) and
                (not is_dataset(X)) and
                (self.iterator_train is DataLoader)
        ):
            msg = ("No y-values are given (y=None). You must either supply a "
                   "Dataset as X or implement your own DataLoader for "
                   "training (and your validation) and supply it using the "
                   "``iterator_train`` and ``iterator_valid`` parameters "
                   "respectively.")
            raise ValueError(msg)

    # pylint: disable=arguments-differ
    def get_loss(self, y_pred, y_true, *args, **kwargs):
        if isinstance(self.criterion_, torch.nn.NLLLoss):
            y_pred = torch.log(y_pred)
        return super().get_loss(y_pred, y_true, *args, **kwargs)

    # pylint: disable=signature-differs
    def fit(self, X, y, **fit_params):
        """See ``NeuralNet.fit``.

        In contrast to ``NeuralNet.fit``, ``y`` is non-optional to
        avoid mistakenly forgetting about ``y``. However, ``y`` can be
        set to ``None`` in case it is derived dynamically from
        ``X``.

        """
        # pylint: disable=useless-super-delegation
        # this is actually a pylint bug:
        # https://github.com/PyCQA/pylint/issues/1085
        return super(NeuralNetClassifier, self).fit(X, y, **fit_params)

    def predict_proba(self, X):
        """Where applicable, return probability estimates for
        samples.

        If the module's forward method returns multiple outputs as a
        tuple, it is assumed that the first output contains the
        relevant information. The other values are ignored.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        Returns
        -------
        y_proba : numpy ndarray

        """
        # Only the docstring changed from parent.
        # pylint: disable=useless-super-delegation
        return super().predict_proba(X)

    def predict(self, X):
        """Where applicable, return class labels for samples in X.

        If the module's forward method returns multiple outputs as a
        tuple, it is assumed that the first output contains the
        relevant information. The other values are ignored.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        Returns
        -------
        y_pred : numpy ndarray

        """
        y_preds = []
        for yp in self.forward_iter(X, training=False):
            yp = yp[0] if isinstance(yp, tuple) else yp
            y_preds.append(to_numpy(yp.max(-1)[-1]))
        y_pred = np.concatenate(y_preds, 0)
        return y_pred


######################
# NeuralNetRegressor #
######################

neural_net_reg_doc_start = """NeuralNet for regression tasks

    Use this specifically if you have a standard regression task,
    with input data X and target y. y must be 2d.

"""

neural_net_reg_criterion_text = """

    criterion : torch criterion (class, default=torch.nn.MSELoss)
      Mean squared error loss."""


def get_neural_net_reg_doc(doc):
    doc = neural_net_reg_doc_start + doc.split("\n ", 4)[-1]
    pattern = re.compile(r'(\n\s+)(criterion .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + neural_net_reg_criterion_text + doc[end:]
    return doc


# pylint: disable=missing-docstring
class NeuralNetRegressor(NeuralNet):
    __doc__ = get_neural_net_reg_doc(NeuralNet.__doc__)

    def __init__(
            self,
            module,
            criterion=torch.nn.MSELoss,
            *args,
            **kwargs
    ):
        super(NeuralNetRegressor, self).__init__(
            module,
            criterion=criterion,
            *args,
            **kwargs
        )

    # pylint: disable=signature-differs
    def check_data(self, X, y):
        if (
                (y is None) and
                (not is_dataset(X)) and
                (self.iterator_train is DataLoader)
        ):
            raise ValueError("No y-values are given (y=None). You must "
                             "implement your own DataLoader for training "
                             "(and your validation) and supply it using the "
                             "``iterator_train`` and ``iterator_valid`` "
                             "parameters respectively.")
        elif y is None:
            # The user implements its own mechanism for generating y.
            return

        if get_dim(y) == 1:
            msg = (
                "The target data shouldn't be 1-dimensional but instead have "
                "2 dimensions, with the second dimension having the same size "
                "as the number of regression targets (usually 1). Please "
                "reshape your target data to be 2-dimensional "
                "(e.g. y = y.reshape(-1, 1).")
            raise ValueError(msg)

    # pylint: disable=signature-differs
    def fit(self, X, y, **fit_params):
        """See ``NeuralNet.fit``.

        In contrast to ``NeuralNet.fit``, ``y`` is non-optional to
        avoid mistakenly forgetting about ``y``. However, ``y`` can be
        set to ``None`` in case it is derived dynamically from
        ``X``.

        """
        # pylint: disable=useless-super-delegation
        # this is actually a pylint bug:
        # https://github.com/PyCQA/pylint/issues/1085
        return super(NeuralNetRegressor, self).fit(X, y, **fit_params)
