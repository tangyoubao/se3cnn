# pylint: disable=C,R,E1101
'''
Based on a1

+ one less layer
'''
import torch
import torch.nn as nn
from se3_cnn.convolution import SE3Convolution
from se3_cnn import SO3
from util_cnn.model import Model
import logging
import numpy as np

logger = logging.getLogger("trainer")


class CNN(nn.Module):

    def __init__(self, number_of_classes):
        super(CNN, self).__init__()

        logger.info("Create CNN for classify %d classes", number_of_classes)

        representations = [
            [(1, SO3.repr1)],  # 64
            [(16, SO3.repr1), (6, SO3.repr3), (2, SO3.repr5), (1, SO3.repr7)],  # (64+2*3-(5-1)) / 2 = 33
            [(16, SO3.repr1), (6, SO3.repr3), (2, SO3.repr5), (1, SO3.repr7)],  # (33 + 2) / 2 = 17
            [(number_of_classes, SO3.repr1)]]  # (17 + 2) / 2 = 9

        self.convolutions = []

        for i in range(len(representations) - 1):
            non_lin = i < len(representations) - 2
            conv = SE3Convolution(5, representations[i + 1], representations[i],
                                  bias_relu=non_lin, norm_relu=False, scalar_batch_norm=True, radial_type="triangles", stride=2, padding=3)
            setattr(self, 'conv{}'.format(i), conv)
            self.convolutions.append(conv)

        self.bn_in = nn.BatchNorm3d(1, affine=False)
        self.bn_out = nn.BatchNorm3d(number_of_classes, affine=True)

    def forward(self, x):
        '''
        :param x: [batch, features, x, y, z]
        '''
        x = self.bn_in(x.contiguous())
        for conv in self.convolutions:
            x = conv(x)

        # [batch, features]
        x = x.mean(-1).mean(-1).mean(-1)
        x = self.bn_out(x.contiguous())
        return x


class MyModel(Model):

    def __init__(self):
        super(MyModel, self).__init__()
        self.cnn = None

    def initialize(self, number_of_classes):
        self.cnn = CNN(number_of_classes)

    def get_cnn(self):
        if self.cnn is None:
            raise ValueError("Need to call initialize first")
        return self.cnn

    def get_batch_size(self, epoch=None):
        return 16

    def get_learning_rate(self, epoch):
        if epoch < 20:
            return 1e-1
        return 1e-2

    def load_files(self, files):
        images = np.array([np.load(file)['arr_0'] for file in files], dtype=np.float32)
        images = images.reshape((-1, 1, 64, 64, 64))
        images = torch.FloatTensor(images)
        return images
