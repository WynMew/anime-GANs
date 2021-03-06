import os
import sys
import numpy as np

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.nn.parameter import Parameter
from torch.nn import functional as F
from torch.nn.init import kaiming_normal_, calculate_gain
if sys.version_info.major == 3:
    from functools import reduce

class PixelNormLayer(nn.Module):
    """
    Pixelwise feature vector normalization.
    """
    def __init__(self, eps=1e-8):
        super(PixelNormLayer, self).__init__()
        self.eps = eps
    
    def forward(self, x):
        return x / torch.sqrt(torch.mean(x ** 2, dim=1, keepdim=True) + 1e-8)

    def __repr__(self):
        return self.__class__.__name__ + '(eps = %s)' % (self.eps)

def mean(tensor, axis, **kwargs):
    if isinstance(axis, int):
        axis = [axis]
    for ax in axis:
        tensor = torch.mean(tensor, axis=ax, **kwargs)
    return tensor


class MinibatchStatConcatLayer(nn.Module):
    """Minibatch stat concatenation layer.
    - averaging tells how much averaging to use ('all', 'spatial', 'none')
    """
    def __init__(self, averaging='all'):
        super(MinibatchStatConcatLayer, self).__init__()
        self.averaging = averaging.lower()
        if 'group' in self.averaging:
            self.n = int(self.averaging[5:])
        else:
            assert self.averaging in ['all', 'flat', 'spatial', 'none', 'gpool'], 'Invalid averaging mode'%self.averaging
        self.adjusted_std = lambda x, **kwargs: torch.sqrt(torch.mean((x - torch.mean(x, **kwargs)) ** 2, **kwargs) + 1e-8) #Tstdeps in the original implementation

    def forward(self, x):
        shape = list(x.size())
        target_shape = shape.copy()
        vals = self.adjusted_std(x, dim=0, keepdim=True)# per activation, over minibatch dim
        if self.averaging == 'all':  # average everything --> 1 value per minibatch
            target_shape[1] = 1
            vals = torch.mean(vals, dim=1, keepdim=True)#vals = torch.mean(vals, keepdim=True)

        elif self.averaging == 'spatial':  # average spatial locations
            if len(shape) == 4:
                vals = mean(vals, axis=[2,3], keepdim=True)  # torch.mean(torch.mean(vals, 2, keepdim=True), 3, keepdim=True)
        elif self.averaging == 'none':  # no averaging, pass on all information
            target_shape = [target_shape[0]] + [s for s in target_shape[1:]]
        elif self.averaging == 'gpool':  # EXPERIMENTAL: compute variance (func) over minibatch AND spatial locations.
            if len(shape) == 4:
                vals = mean(x, [0,2,3], keepdim=True)  # torch.mean(torch.mean(torch.mean(x, 2, keepdim=True), 3, keepdim=True), 0, keepdim=True)
        elif self.averaging == 'flat':  # variance of ALL activations --> 1 value per minibatch
            target_shape[1] = 1
            vals = torch.FloatTensor([self.adjusted_std(x)])
        else:  # self.averaging == 'group'  # average everything over n groups of feature maps --> n values per minibatch
            target_shape[1] = self.n
            vals = vals.view(self.n, self.shape[1]/self.n, self.shape[2], self.shape[3])
            vals = mean(vals, axis=0, keepdim=True).view(1, self.n, 1, 1)
        vals = vals.expand(*target_shape)
        return torch.cat([x, vals], 1) # feature-map concatanation

    def __repr__(self):
        return self.__class__.__name__ + '(averaging = %s)' % (self.averaging)


class MinibatchDiscriminationLayer(nn.Module):
    def __init__(self, num_kernels):
        super(MinibatchDiscriminationLayer, self).__init__()
        self.num_kernels = num_kernels

    def forward(self, x):
        pass


class GDropLayer(nn.Module):
    """
    # Generalized dropout layer. Supports arbitrary subsets of axes and different
    # modes. Mainly used to inject multiplicative Gaussian noise in the network.
    """
    def __init__(self, mode='mul', strength=0.2, axes=(0,1), normalize=False):
        super(GDropLayer, self).__init__()
        self.mode = mode.lower()
        assert self.mode in ['mul', 'drop', 'prop'], 'Invalid GDropLayer mode'%mode
        self.strength = strength
        self.axes = [axes] if isinstance(axes, int) else list(axes)
        self.normalize = normalize
        self.gain = None

    def forward(self, x, deterministic=False):
        if deterministic or not self.strength:
            return x

        rnd_shape = [s if axis in self.axes else 1 for axis, s in enumerate(x.size())]  # [x.size(axis) for axis in self.axes]
        if self.mode == 'drop':
            p = 1 - self.strength
            rnd = np.random.binomial(1, p=p, size=rnd_shape) / p
        elif self.mode == 'mul':
            rnd = (1 + self.strength) ** np.random.normal(size=rnd_shape)
        else:
            coef = self.strength * x.size(1) ** 0.5
            rnd = np.random.normal(size=rnd_shape) * coef + 1

        if self.normalize:
            rnd = rnd / np.linalg.norm(rnd, keepdims=True)
        rnd = Variable(torch.from_numpy(rnd).type(x.data.type()))
        if x.is_cuda:
            rnd = rnd.cuda()
        return x * rnd

    def __repr__(self):
        param_str = '(mode = %s, strength = %s, axes = %s, normalize = %s)' % (self.mode, self.strength, self.axes, self.normalize)
        return self.__class__.__name__ + param_str


class LayerNormLayer(nn.Module):
    """
    Layer normalization. Custom reimplementation based on the paper: https://arxiv.org/abs/1607.06450
    """
    def __init__(self, incoming, eps=1e-4):
        super(LayerNormLayer, self).__init__()
        self.incoming = incoming
        self.eps = eps
        self.gain = Parameter(torch.FloatTensor([1.0]), requires_grad=True)
        self.bias = None

        if self.incoming.bias is not None:
            self.bias = self.incoming.bias
            self.incoming.bias = None

    def forward(self, x):
        x = x - mean(x, axis=range(1, len(x.size())))
        x = x * 1.0/(torch.sqrt(mean(x**2, axis=range(1, len(x.size())), keepdim=True) + self.eps))
        x = x * self.gain
        if self.bias is not None:
            x += self.bias
        return x

    def __repr__(self):
        param_str = '(incoming = %s, eps = %s)' % (self.incoming.__class__.__name__, self.eps)
        return self.__class__.__name__ + param_str


def resize_activations(v, so):
    """
    Resize activation tensor 'v' of shape 'si' to match shape 'so'.
    :param v:
    :param so:
    """
    si = list(v.size())
    so = list(so)
    assert len(si) == len(so) and si[0] == so[0]

    # Decrease feature maps.
    if si[1] > so[1]:
        v = v[:, :so[1]]

    # Shrink spatial axes.
    if len(si) == 4 and (si[2] > so[2] or si[3] > so[3]):
        assert si[2] % so[2] == 0 and si[3] % so[3] == 0
        ks = (si[2] // so[2], si[3] // so[3])
        v = F.avg_pool2d(v, kernel_size=ks, stride=ks, ceil_mode=False, padding=0, count_include_pad=False)

    if si[2] < so[2]: 
        assert so[2] % si[2] == 0 and so[2] / si[2] == so[3] / si[3] 
        v = F.upsample(v, scale_factor=so[2]//si[2], mode='nearest')

    # Increase feature maps.
    if si[1] < so[1]:
        z = torch.zeros((v.shape[0], so[1] - si[1]) + so[2:])
        v = torch.cat([v, z], 1)
    return v

    def __init__(self):
        super(ConcatLayer, self).__init__()

    def forward(self, x, y):
        return torch.cat([x, y], 1)


class ReshapeLayer(nn.Module):
    def __init__(self, new_shape):
        super(ReshapeLayer, self).__init__()
        self.new_shape = new_shape  # not include minibatch dimension

    def forward(self, x):
        assert reduce(lambda u,v: u*v, self.new_shape) == reduce(lambda u,v: u*v, x.size()[1:])
        return x.view(-1, *self.new_shape)


def he_init(layer, nonlinearity='conv2d', param=None):
    nonlinearity = nonlinearity.lower()
    kaiming_normal_(layer.weight, nonlinearity=nonlinearity, a=param)
