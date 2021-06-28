
import abc
import copy

import torch
from torch import nn
from torch.ao.utils import _module_to_fqn, _fqn_to_module
from torch.nn.utils import parametrize

from .utils import FakeSparsity

SUPPORTED_MODULES = {
    nn.Linear
}


class BaseSparsifier(abc.ABC):
    r"""Base class for all sparsifiers.

    Abstract methods that need to be implemented:

    - update_mask: Function to compute a new mask for all keys in the
        `module_groups`.

    Args:
        - model [nn.Module]: model to configure. The model itself is not saved
            but used for the state_dict saving / loading.
        - config [list]: configuration elements could either be instances of
            nn.Module or dict maps. The dicts must have a key 'module' with the
            value being an instance of a nn.Module.
        - defaults [dict]: default configurations will be attached to the
            configuration. Only the keys that don't exist in the `config` will
            be updated.

    Example::

        >>> config = [model.layer1, {'module': model.linear2, 'sparsity_level': 0.5}]
        >>> defaults = {'sparsity_level': 0.7}
        >>> # model.layer1 will have `sparsity_level` = 0.7 (getting default)
        >>> sparsifier = BaseSparsifier(config, defaults)
    """
    def __init__(self, defaults):
        super().__init__()
        self.defaults = defaults
        if self.defaults is None:
            self.defaults = dict()

        self.state = {}
        self.module_groups = []
        self.enable_mask_update = False

    def __getstate__(self):
        return {
            'defaults': self.defaults,
            'state': self.state,
            'module_groups': self.module_groups,
        }

    def __setstate__(self, state):
        self.__dict__.update(state)

    def __repr__(self):
        format_string = self.__class__.__name__ + ' ('
        for i, sparse_args in enumerate(self.module_groups):
            module = sparse_args['module']
            format_string += '\n'
            format_string += f'\tModule Group {i}\n'
            format_string += f'\t    module: {module}\n'
            for key in sorted(sparse_args.keys()):
                if key == 'module':
                    continue
                format_string += f'\t    {key}: {sparse_args[key]}\n'
        format_string += ')'
        return format_string

    def state_dict(self):
        r"""Returns the state of the optimizer as a :class:`dict`.

        It contains:
        * module_groups - a list containing all sparsity configuration groups
            with the key 'path' specifying the layer path within a model
        """
        module_groups = [
            dict(filter(lambda key_value: key_value[0] != 'module', mg.items()))
            for mg in self.module_groups
        ]
        return {
            'module_groups': module_groups,
        }

    def load_state_dict(self, state_dict, strict=True):
        module_groups = copy.deepcopy(state_dict['module_groups'])
        for group in module_groups:
            layer = _fqn_to_module(self.model, group['path'])
            if strict and layer is None:
                raise RuntimeError(f'Error loading group["path"] into the model')
            group['module'] = layer
        self.__setstate__({'module_groups': module_groups})

    def prepare(self, model, config):
        r"""Prepares a model, by adding the parametrizations.

        Note::

            The model is modified inplace. If you need to preserve the original
            model, use copy.deepcopy.
        """
        self.config = config
        self.model = model
        # If no config -- try getting all the supported layers
        if self.config is None:
            # Add all models to the config
            self.config = []
            stack = [model]
            while stack:
                module = stack.pop()
                for name, child in module.named_children():
                    if type(child) in SUPPORTED_MODULES:
                        self.config.append(child)
                    else:
                        stack.append(child)

        for module_config in self.config:
            if isinstance(module_config, nn.Module):
                module_config = {'module': module_config}
            local_args = copy.deepcopy(self.defaults)
            local_args.update(module_config)
            module = local_args['module']
            module_path = _module_to_fqn(self.model, module)
            if module_path and module_path[0] == '.':
                module_path = module_path[1:]
            local_args['path'] = module_path
            self.module_groups.append(local_args)

        self._prepare()

    def _prepare(self, *args, **kwargs):
        r"""Adds mask parametrization to the layer weight
        """
        for config in self.module_groups:
            module = config['module']
            if getattr(module, 'mask', None) is None:
                module.register_buffer('mask', torch.ones(module.weight.shape))
            param = config.get('parametrization', FakeSparsity)
            parametrize.register_parametrization(module, 'weight',
                                                 param(module.mask))

    def squash_mask(self, *args, **kwargs):
        for config in self.module_groups:
            module = config['module']
            parametrize.remove_parametrizations(module, 'weight',
                                                leave_parametrized=True)
            if getattr(module._parameters, 'mask', None):
                del module._parameters['mask']
            elif getattr(module._buffers, 'mask', None):
                del module._buffers['mask']
            delattr(module, 'mask')

    def convert(self):
        # TODO: Call the torch.ao.utils.convert in here
        raise NotImplementedError('`convert` is not implemented. '
            'Please, use `torch.ao.utils.convert` instead.')

    def step(self, use_path=True):
        if not self.enable_mask_update:
            return
        with torch.no_grad():
            for config in self.module_groups:
                module = config['module']
                self.update_mask(module, **config)

    @abc.abstractmethod
    def update_mask(self, layer, **kwargs):
        pass