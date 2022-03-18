"""
    Proximal Policy Optimization (PPO) Algorithm

"""

import tensorflow as tf
import numpy as np
from sklearn.utils import shuffle


class PPO_PEVF(object):
    def __init__(self, s_dim, a_dim, sess,
                 pr_dim, h_dim=64,
                 # FIXME 1027 - 0 for concat, 1 for dot prod, 2 for hypernetwork
                 prev_type=0,
                 policy_logvar=-1.0,
                 lr_a=0.0001, lr_c=0.001, gamma=0.99,
                 lr_pc=0.0001, lr_aux=0.001,
                 k=2,
                 epsilon=0.2, batch_size=256,
                 c_epochs=10, a_epochs=10, clipping_range=0.2,
                 memory_size=100000, policy_size=10000,
                 # - pair_data_num is for aux training, pair_num is for surface pr
                 pair_data_num=1000,
                 pair_num=200,
                 sample_ratio=0.8,
                 aux_policy_batch_size=8, aux_batch_size=128,
                 pr_model=None,
                 ):
        self.sess = sess

        self.replay_buffer_x = None
        self.replay_buffer_y = None
        self.c_epochs, self.a_epochs = c_epochs, a_epochs

        self.s_dim, self.a_dim = s_dim, a_dim
        self.pr_dim = pr_dim
        self.h_dim = h_dim
        self.prev_type = prev_type
        self.lr_a, self.lr_c = lr_a, lr_c
        self.lr_pc = lr_pc
        self.lr_aux = lr_aux
        self.gamma = gamma
        self.epsilon = epsilon
        self.policy_logvar = policy_logvar
        self.batch_size = batch_size
        self.k = k

        self.aux_batch_size = aux_batch_size
        self.aux_policy_batch_size = aux_policy_batch_size

        self.policy_logvar = policy_logvar
        self.clipping_range = clipping_range

        # FIXME 1029 - init memory for PREV
        self.memory_size = memory_size
        self.policy_size = policy_size
        self.memory = np.zeros((memory_size, s_dim + 1 + 1), dtype=np.float32)
        self.mem_ptr = 0

        self.pair_data_num = pair_data_num
        self.policy_data_memory = np.zeros((policy_size, pair_data_num, s_dim + a_dim), dtype=np.float32)
        # self.pd_mem_ptr = 0

        # FIXME 0813  - this memory for surface policy representation
        self.policy_memory = np.zeros((policy_size, pair_num, s_dim + a_dim), dtype=np.float32)
        self.pmem_ptr = 0

        # FIXME 0102
        self.pair_num = pair_num
        self.sample_ratio = sample_ratio
        self.sample_pair_num = int(pair_num * sample_ratio)
        self.pr_model = pr_model
        self.cur_pr = None

        self.train_cnt = 0

        self._placeholders()

        # actor
        self.means, self.log_vars = self._build_policy_net(self.s_ph, 'policy', trainable=True)
        self.logp, self.logp_old = self._logprob()

        self.sampled_act = self.means + tf.exp(self.log_vars / 2.0) * tf.random_normal(shape=[1, self.a_dim])

        # actor parameters
        self.ae_params = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='policy')

        # FIXME 0508 - build pr network
        self.surface_pr = self._build_spr_encoder(self.pair_ph, scope='Encoder')
        self.pr_params = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='Encoder')

        # FIXME 0729 - build predictor
        self.dy_a_means, self.dy_a_logvars = self._build_virtual_policy(self.s_ph, self.surface_pr, trainable=True)
        dy_logp = -0.5 * tf.reduce_sum(self.dy_a_logvars)
        dy_logp += -0.5 * tf.reduce_sum(tf.square(self.a_ph - self.dy_a_means) /
                                     tf.exp(self.dy_a_logvars), axis=1, keepdims=True)

        # FIXME 1029 - build prev V
        if self.prev_type == 0:
            self.prev_v = self._build_value_net_concat(self.s_ph, self.surface_pr, scope='PREV', trainable=True)
        elif self.prev_type == 1:
            self.prev_v = self._build_value_net_dot_prod(self.s_ph, self.surface_pr, scope='PREV', trainable=True)
        elif self.prev_type == 2:
            self.prev_v = self._build_value_net_hyper(self.s_ph, self.surface_pr, scope='PREV', trainable=True)

        self.vf_params = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='PEVF')


        # FIXME 1029 - train PREV
        self.prev_td_error = 0.5 * tf.reduce_mean((self.prev_v - self.val_ph)**2)
        self.prev_ctrain = tf.train.AdamOptimizer(self.lr_pc).minimize(self.prev_td_error,
                                                                       # var_list=self.vf_params + self.pr_params
                                                                       )

        # - clipped surrogate objective
        pg_ratio = tf.exp(self.logp - self.logp_old)
        clipped_pg_ratio = tf.clip_by_value(pg_ratio, 1 - self.clipping_range, 1 + self.clipping_range)
        surrogate_loss = tf.minimum(self.adv_ph * pg_ratio, self.adv_ph * clipped_pg_ratio)
        self.a_loss = - tf.reduce_mean(surrogate_loss)
        self.a_train_op = tf.train.AdamOptimizer(self.lr_a).minimize(self.a_loss)

        # - auxiliary learning loss
        self.aux_loss = tf.reduce_mean(-dy_logp)
        self.aux_train_op = tf.train.AdamOptimizer(self.lr_aux).minimize(self.aux_loss)

        # - target/momentum net replacement

        self.sess.run(tf.global_variables_initializer())

        self._print_hyperparams()

        print('-- INFO: PPO PEVF aux w/ SPR ad initialized.')
        print('==========================')

    def _print_hyperparams(self):
        print('------------------- Hyperparameters ----------------------')
        print('-- S_Dim:', self.s_dim)
        print('-- A_Dim:', self.a_dim)
        print('-- K:', self.k)
        print('-- PR_Dim:', self.pr_dim)
        print('-- PREV_Type:', self.prev_type)
        print('-- LR_V:', self.lr_c)
        print('-- LR_PEVF:', self.lr_pc)
        print('-- LR_Actor:', self.lr_a)
        print('-- LR_AUX:', self.lr_aux)
        print('-- AUX_batch_size:', self.aux_batch_size)
        print('-- AUX_policy_batch_size:', self.aux_policy_batch_size)
        print('-- Gamma:', self.gamma)
        print('-- Batch_size:', self.batch_size)
        print('-- Memory_size:', self.memory_size)
        print('-- Policy_memory_size:', self.policy_size)
        print('-- Sample_ratio:', self.sample_ratio)
        print('--')

    def _placeholders(self):
        """ Input placeholders"""
        # observations, actions and advantages:
        self.s_ph = tf.placeholder(tf.float32, [None, self.s_dim], 'state')
        self.a_ph = tf.placeholder(tf.float32, [None, self.a_dim], 'action')
        self.adv_ph = tf.placeholder(tf.float32, [None, 1], 'advantages')
        self.val_ph = tf.placeholder(tf.float32, [None, 1], 'val_valfunc')
        # self.pr_ph = tf.placeholder(tf.float32, [None, self.pr_dim], 'policy_representation')
        self.pair_ph = tf.placeholder(tf.float32, [None, self.pair_num, self.s_dim + self.a_dim], 'sa_pairs')

        self.old_log_vars_ph = tf.placeholder(tf.float32, [1, self.a_dim], 'old_log_vars')
        self.old_means_ph = tf.placeholder(tf.float32, [None, self.a_dim], 'old_means')

        # FIXME 0722
        self.label_ph = tf.placeholder(tf.int32, [None, None], 'cl_label')

    def _build_spr_encoder(self, pairs, scope, reuse=False):
        # trainable = True if not reuse else False

        pairs = tf.transpose(pairs, [1, 0, 2])
        pairs = tf.random_shuffle(pairs)[:self.sample_pair_num]
        pairs = tf.transpose(pairs, [1, 0, 2])
        pairs = tf.reshape(pairs, [-1, self.s_dim + self.a_dim])
        trainable = True
        with tf.variable_scope(scope, reuse=reuse):
            e = tf.layers.dense(pairs, self.pr_dim, activation=tf.nn.relu, name='ef1', trainable=trainable,
                                   kernel_initializer=tf.random_normal_initializer(
                                       stddev=np.sqrt(1 / (self.s_dim + self.a_dim))))
            # e = tf.layers.dense(e, self.pr_dim, activation=tf.nn.relu, name='ef2', trainable=trainable,
            #                       kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / (self.pr_dim))))
            # e = tf.layers.dense(e, self.pr_dim, activation=None, name='encoding', trainable=trainable,
            #                        kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / self.pr_dim)))
            pr = tf.reduce_mean(tf.reshape(e, [-1, self.sample_pair_num, self.pr_dim]), axis=1,
                                name='pair_policy_representation')

            return pr

    def _build_virtual_policy(self, s, pr, trainable):
        with tf.variable_scope('Virtual_Actor'):
            # TODO 0729
            pr = tf.reshape(tf.tile(tf.reshape(pr, [-1, 1, self.pr_dim]),
                                    [1, int(self.aux_batch_size // self.aux_policy_batch_size), 1]),
                            [-1, self.pr_dim])
            fc1 = tf.layers.dense(tf.concat([s, pr], axis=1), 128, activation=tf.nn.relu, name='l1', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / (self.s_dim + self.pr_dim))))
            fc2 = tf.layers.dense(fc1, 128, activation=tf.nn.relu, name='l2', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / 128)))
            means = tf.layers.dense(fc2, self.a_dim, activation=tf.nn.tanh, name='means', trainable=trainable,
                                    kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / 128)))
            logvar_speed = (10 * 64) // 48
            spd_log_vars = tf.get_variable('spd_logvars', [logvar_speed, self.a_dim], tf.float32,
                                           tf.constant_initializer(0.0))
            log_vars = tf.reduce_sum(spd_log_vars, axis=0, keepdims=True) + self.policy_logvar
            return means, log_vars

    def _build_policy_net(self, s, scope, trainable):
        with tf.variable_scope(scope):
            fc1 = tf.layers.dense(s, self.k, activation=tf.nn.relu, name='fc1', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / self.s_dim)))
            fc2 = tf.layers.dense(fc1, self.k, activation=tf.nn.relu, name='fc2', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / self.k)))
            means = tf.layers.dense(fc2, self.a_dim, activation=tf.nn.tanh, name='means', trainable=trainable,
                                    kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / self.k)))
            logvar_speed = (10 * 64) // 48
            spd_log_vars = tf.get_variable('spd_logvars', [logvar_speed, self.a_dim], tf.float32,
                                           tf.constant_initializer(0.0))
            log_vars = tf.reduce_sum(spd_log_vars, axis=0, keepdims=True) + self.policy_logvar

        return means, log_vars

    def _build_value_net_hyper(self, s, pr, scope, trainable):
        with tf.variable_scope(scope):
            # FIXME 1006 - use h_dim to control the dimension of main stream
            fcl = tf.layers.dense(s, self.h_dim, activation=tf.nn.relu,
                                  name='fcl', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(
                                      stddev=np.sqrt(1 / self.s_dim)))
            fcr = tf.layers.dense(pr, 64, activation=tf.nn.relu,
                                  name='fcr', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(
                                      stddev=np.sqrt(1 / self.pr_dim)))

            # FIXME 0930 - hypernetwork
            w1 = tf.layers.dense(fcr, self.h_dim * self.h_dim, activation=None,
                                 name='w1', trainable=trainable,
                                 kernel_initializer=tf.random_normal_initializer(
                                     stddev=np.sqrt(1 / self.h_dim)))
            b1 = tf.layers.dense(fcr, self.h_dim, activation=None,
                                 name='b1', trainable=trainable,
                                 kernel_initializer=tf.random_normal_initializer(
                                     stddev=np.sqrt(1 / self.h_dim)))

            w1_resh = tf.reshape(w1, shape=[-1, self.h_dim, self.h_dim], name='w1_resh')
            b1_resh = tf.reshape(b1, shape=[-1, 1, self.h_dim], name='b1_resh')

            l1 = tf.matmul(tf.reshape(fcl, shape=[-1, 1, self.h_dim]), w1_resh) + b1_resh
            a1 = tf.nn.relu(l1, name='a1')
            fc1 = tf.reshape(a1, [-1, self.h_dim], name='fc1')
            fc2 = tf.layers.dense(fc1, 128, activation=tf.nn.relu, name='fc2', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / self.h_dim)))
            q = tf.layers.dense(fc2, 1, activation=None,
                                name='q', trainable=trainable,
                                kernel_initializer=tf.random_normal_initializer(
                                    stddev=np.sqrt(1 / 128)))

        return q

    def _build_value_net_dot_prod(self, s, pr, scope, trainable):
        with tf.variable_scope(scope):
            fcl = tf.layers.dense(s, 64, activation=tf.nn.relu,
                                  name='fcl', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(
                                      stddev=np.sqrt(1 / self.s_dim)))
            # fcr = tf.layers.dense(pr, 64, activation=tf.nn.tanh,
            fcr = tf.layers.dense(pr, 64, activation=tf.nn.relu,
                                  name='fcr', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(
                                      stddev=np.sqrt(1 / self.pr_dim)))
            fc1 = fcl * fcr
            fc2 = tf.layers.dense(fc1, 128, activation=tf.nn.relu, name='fc2', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / 128)))
            q = tf.layers.dense(fc2, 1, activation=None, name='q_value', trainable=trainable,
                                kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / 128)))
        return q

    def _build_value_net_concat(self, s, pr, scope, trainable):
        with tf.variable_scope(scope):
            fcl = tf.layers.dense(s, 64, activation=tf.nn.relu,
                                  name='fcl', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(
                                      stddev=np.sqrt(1 / self.s_dim)))
            fcr = tf.layers.dense(pr, 64, activation=tf.nn.relu,
                                  name='fcr', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(
                                      stddev=np.sqrt(1 / self.pr_dim)))
            fc1 = tf.concat([fcl, fcr], axis=1, name='fc1')
            # fc1 = fcl
            fc2 = tf.layers.dense(fc1, 128, activation=tf.nn.relu, name='fc2', trainable=trainable,
                                  kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / 128)))
            v = tf.layers.dense(fc2, 1, activation=None, name='v_value', trainable=trainable,
                                kernel_initializer=tf.random_normal_initializer(stddev=np.sqrt(1 / 128)))
        return v

    def _logprob(self):
        """ Calculate log probabilities of a batch of observations & actions

        Calculates log probabilities using previous step's model parameters and
        new parameters being trained.
        """
        logp = -0.5 * tf.reduce_sum(self.log_vars)
        logp += -0.5 * tf.reduce_sum(tf.square(self.a_ph - self.means) /
                                     tf.exp(self.log_vars), axis=1, keepdims=True)

        logp_old = -0.5 * tf.reduce_sum(self.old_log_vars_ph)
        logp_old += -0.5 * tf.reduce_sum(tf.square(self.a_ph - self.old_means_ph) /
                                         tf.exp(self.old_log_vars_ph), axis=1, keepdims=True)

        return logp, logp_old

    def choose_action(self, s):
        s = s[np.newaxis, :]
        a = self.sess.run(self.sampled_act, feed_dict={self.s_ph: s})[0]
        return np.clip(a, -1, 1)

    def choose_action_batch(self, s):
        a = self.sess.run(self.sampled_act, feed_dict={self.s_ph: s})
        return np.clip(a, -1, 1)

    def store_transition(self, s, r, policy_idx):
        transition = np.hstack((s, [r, policy_idx]))
        index = self.mem_ptr % self.memory_size
        self.memory[index, :] = transition
        self.mem_ptr += 1

    def store_policy_data(self, policy_pairs, policy_pair_data):
        index = self.pmem_ptr % self.policy_size
        self.policy_memory[index, :] = policy_pairs
        self.policy_data_memory[index, :] = policy_pair_data
        self.pmem_ptr += 1

        return index

    def predict_v_prev(self, s, policy_pairs):
        """ Predict method """
        y_hat = self.sess.run(self.prev_v, feed_dict={self.s_ph: s, self.pair_ph: policy_pairs,})

        return np.squeeze(y_hat)

    def update_p(self, observes, actions, advantages):
        """ Update policy based on observations, actions and advantages

        Args:
            observes: observations, shape = (N, obs_dim)
            actions: actions, shape = (N, act_dim)
            advantages: advantages, shape = (N,)
        """
        feed_dict = {self.s_ph: observes,
                     self.a_ph: actions,
                     self.adv_ph: advantages.reshape(-1, 1),
                     }

        old_means_np, old_log_vars_np = self.sess.run([self.means, self.log_vars], feed_dict)
        feed_dict[self.old_log_vars_ph] = old_log_vars_np
        feed_dict[self.old_means_ph] = old_means_np

        a_loss = 0
        for e in range(self.a_epochs):
            # TODO: need to improve data pipeline - re-feeding data every epoch
            self.sess.run(self.a_train_op, feed_dict)
            a_loss = self.sess.run(self.a_loss, feed_dict)

        # FIXME 0102 - update cur_pr
        # if self.pr_model is not None:
        #     self.cur_pr = self.calc_cur_params()
        # else:
        #     self.cur_pr = np.random.random(self.pr_dim) * 2 - 1

        return a_loss

    def update_v(self, x, y, z):
        num_batches = max(x.shape[0] // self.batch_size, 1)
        batch_size = x.shape[0] // num_batches

        x_train, y_train = x, y
        self.replay_buffer_x = x
        self.replay_buffer_y = y

        # cur_params = self.cur_pr
        b_pair = np.tile(z[np.newaxis, :, :], [batch_size, 1, 1])
        for e in range(self.c_epochs):
            x_train, y_train = shuffle(x_train, y_train)
            for j in range(num_batches):
                start = j * batch_size
                end = (j + 1) * batch_size
                bs = x_train[start:end, :]
                bval = y_train[start:end].reshape(-1, 1)
                # FIXME 1029 - train PREV V
                # bpr = np.tile(np.reshape(cur_params, [1, -1]), [bs.shape[0], 1])
                prev_l_, _ = self.sess.run([self.prev_td_error, self.prev_ctrain], feed_dict={self.s_ph: bs,
                                                                                              self.val_ph: bval,
                                                                                              self.pair_ph: b_pair,
                                                                                              })

        # cur_bpr = np.tile(np.reshape(cur_params, [1, -1]), [x.shape[0], 1])
        cur_bpr = np.tile(z[np.newaxis, :, :], [x.shape[0], 1, 1])
        prev_y_hat = self.predict_v_prev(x, cur_bpr)
        prev_c_loss = np.mean(np.square(prev_y_hat - y))

        return prev_c_loss

    def update_pevf(self, batch_size=None):
        """
            off-policy training of PEVF with replay buffer
        """
        if batch_size is None:
            batch_size = self.batch_size
        # FIXME 1201
        indices = np.random.choice(min(self.memory_size, self.mem_ptr), size=batch_size)
        bt = self.memory[indices, :]
        bs = bt[:, :self.s_dim]
        bdsc_re = bt[:, self.s_dim:self.s_dim + 1]
        bp_idx = bt[:, -1].astype(int)
        bpairs = self.policy_memory[bp_idx, :, :]

        prev_l, _ = self.sess.run([self.prev_td_error, self.prev_ctrain], {self.s_ph: bs,
                                                                           self.val_ph: bdsc_re,
                                                                           self.pair_ph: bpairs,
                                                                           })

        # return c_loss, prev_c_loss, c_loss_, prev_c_loss_

    def update_aux(self, batch_size=None):
        """
            auxiliary training of policy representation
        """
        # if batch_size is None:
        #     batch_size = self.aux_batch_size
        # if self.pmem_ptr < batch_size:
        #     print('- No sufficient policy data for training.')
        #     return
        # FIXME 0813
        # - first sample policies
        bp_idx = np.random.choice(min(self.policy_size, self.pmem_ptr), size=self.aux_policy_batch_size)
        bpairs = self.policy_memory[bp_idx, :]
        bpairs4pr = self.policy_data_memory[bp_idx, :, :]

        # - then sample policy data for training
        sub_batch_size = int(self.aux_batch_size // self.aux_policy_batch_size)
        pd_indices = np.random.choice(self.pair_data_num, size=sub_batch_size)
        bpairs4pred = bpairs4pr[:, pd_indices, :]
        bpairs4pred_resh = np.reshape(bpairs4pred, [-1, self.s_dim + self.a_dim])
        b_s = bpairs4pred_resh[:, :self.s_dim]
        b_a = bpairs4pred_resh[:, -self.a_dim:]

        aux_loss, _ = self.sess.run([self.aux_loss, self.aux_train_op],
                                    feed_dict={self.pair_ph: bpairs,
                                               self.s_ph: b_s,
                                               self.a_ph: b_a,
                                               }
                                    )

        return aux_loss

    def get_pairs(self, is4pr=False):
        sample_pair_num = self.pair_num if is4pr else self.pair_data_num
        indices = np.random.choice(min(self.memory_size, self.mem_ptr), size=sample_pair_num)
        bt = self.memory[indices, :]
        bs = bt[:, :self.s_dim]
        ba = self.choose_action_batch(bs)
        cur_pairs = np.concatenate([bs, ba], axis=1)

        return cur_pairs

