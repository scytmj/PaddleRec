# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

import paddle.fluid as fluid
import paddle.fluid.layers.tensor as tensor
import paddle.fluid.layers.control_flow as cf

from paddlerec.core.utils import envs
from paddlerec.core.model import Model as ModelBase


class Model(ModelBase):
    def __init__(self, config):
        ModelBase.__init__(self, config)

    def _init_hyper_parameters(self):
        self.vocab_size = envs.get_global_env("hyper_parameters.vocab_size")
        self.emb_dim = envs.get_global_env("hyper_parameters.emb_dim")
        self.hidden_size = envs.get_global_env("hyper_parameters.hidden_size")

    def input_data(self, is_infer=False, **kwargs):
        if is_infer:
            user_data = fluid.data(
                name="user", shape=[None, 1], dtype="int64", lod_level=1)
            all_item_data = fluid.data(
                name="all_item", shape=[None, self.vocab_size], dtype="int64")
            pos_label = fluid.data(
                name="pos_label", shape=[None, 1], dtype="int64")
            return [user_data, all_item_data, pos_label]
        else:
            user_data = fluid.data(
                name="user", shape=[None, 1], dtype="int64", lod_level=1)
            pos_item_data = fluid.data(
                name="p_item", shape=[None, 1], dtype="int64", lod_level=1)
            neg_item_data = fluid.data(
                name="n_item", shape=[None, 1], dtype="int64", lod_level=1)
            return [user_data, pos_item_data, neg_item_data]

    def net(self, inputs, is_infer=False):
        if is_infer:
            self._infer_net(inputs)
            return
        user_data = inputs[0]
        pos_item_data = inputs[1]
        neg_item_data = inputs[2]
        emb_shape = [self.vocab_size, self.emb_dim]
        self.user_encoder = GrnnEncoder()
        self.item_encoder = BowEncoder()
        self.pairwise_hinge_loss = PairwiseHingeLoss()

        user_emb = fluid.embedding(
            input=user_data, size=emb_shape, param_attr="emb.item")
        pos_item_emb = fluid.embedding(
            input=pos_item_data, size=emb_shape, param_attr="emb.item")
        neg_item_emb = fluid.embedding(
            input=neg_item_data, size=emb_shape, param_attr="emb.item")
        user_enc = self.user_encoder.forward(user_emb)
        pos_item_enc = self.item_encoder.forward(pos_item_emb)
        neg_item_enc = self.item_encoder.forward(neg_item_emb)
        user_hid = fluid.layers.fc(input=user_enc,
                                   size=self.hidden_size,
                                   param_attr='user.w',
                                   bias_attr="user.b")
        pos_item_hid = fluid.layers.fc(input=pos_item_enc,
                                       size=self.hidden_size,
                                       param_attr='item.w',
                                       bias_attr="item.b")
        neg_item_hid = fluid.layers.fc(input=neg_item_enc,
                                       size=self.hidden_size,
                                       param_attr='item.w',
                                       bias_attr="item.b")
        cos_pos = fluid.layers.cos_sim(user_hid, pos_item_hid)
        cos_neg = fluid.layers.cos_sim(user_hid, neg_item_hid)
        hinge_loss = self.pairwise_hinge_loss.forward(cos_pos, cos_neg)
        avg_cost = fluid.layers.mean(hinge_loss)
        correct = self._get_correct(cos_neg, cos_pos)

        self._cost = avg_cost
        self._metrics["correct"] = correct
        self._metrics["hinge_loss"] = hinge_loss

    def _infer_net(self, inputs):
        user_data = inputs[0]
        all_item_data = inputs[1]
        pos_label = inputs[2]

        user_emb = fluid.embedding(
            input=user_data,
            size=[self.vocab_size, self.emb_dim],
            param_attr="emb.item")
        all_item_emb = fluid.embedding(
            input=all_item_data,
            size=[self.vocab_size, self.emb_dim],
            param_attr="emb.item")
        all_item_emb_re = fluid.layers.reshape(
            x=all_item_emb, shape=[-1, self.emb_dim])

        user_encoder = GrnnEncoder()
        user_enc = user_encoder.forward(user_emb)
        user_hid = fluid.layers.fc(input=user_enc,
                                   size=self.hidden_size,
                                   param_attr='user.w',
                                   bias_attr="user.b")
        user_exp = fluid.layers.expand(
            x=user_hid, expand_times=[1, self.vocab_size])
        user_re = fluid.layers.reshape(
            x=user_exp, shape=[-1, self.hidden_size])

        all_item_hid = fluid.layers.fc(input=all_item_emb_re,
                                       size=self.hidden_size,
                                       param_attr='item.w',
                                       bias_attr="item.b")
        cos_item = fluid.layers.cos_sim(X=all_item_hid, Y=user_re)
        all_pre_ = fluid.layers.reshape(
            x=cos_item, shape=[-1, self.vocab_size])
        acc = fluid.layers.accuracy(input=all_pre_, label=pos_label, k=20)

        self._infer_results['recall20'] = acc

    def _get_correct(self, x, y):
        less = tensor.cast(cf.less_than(x, y), dtype='float32')
        correct = fluid.layers.reduce_sum(less)
        return correct


class BowEncoder(object):
    """ bow-encoder """

    def __init__(self):
        self.param_name = ""

    def forward(self, emb):
        return fluid.layers.sequence_pool(input=emb, pool_type='sum')


class GrnnEncoder(object):
    """ grnn-encoder """

    def __init__(self, param_name="grnn", hidden_size=128):
        self.param_name = param_name
        self.hidden_size = hidden_size

    def forward(self, emb):
        fc0 = fluid.layers.fc(input=emb,
                              size=self.hidden_size * 3,
                              param_attr=self.param_name + "_fc.w",
                              bias_attr=False)

        gru_h = fluid.layers.dynamic_gru(
            input=fc0,
            size=self.hidden_size,
            is_reverse=False,
            param_attr=self.param_name + ".param",
            bias_attr=self.param_name + ".bias")
        return fluid.layers.sequence_pool(input=gru_h, pool_type='max')


class PairwiseHingeLoss(object):
    def __init__(self, margin=0.8):
        self.margin = margin

    def forward(self, pos, neg):
        loss_part1 = fluid.layers.elementwise_sub(
            tensor.fill_constant_batch_size_like(
                input=pos, shape=[-1, 1], value=self.margin, dtype='float32'),
            pos)
        loss_part2 = fluid.layers.elementwise_add(loss_part1, neg)
        loss_part3 = fluid.layers.elementwise_max(
            tensor.fill_constant_batch_size_like(
                input=loss_part2, shape=[-1, 1], value=0.0, dtype='float32'),
            loss_part2)
        return loss_part3
