# coding:utf-8
# 网络训练

# 解决cudnn 初始化失败的东西: 使用GPU
from tensorflow.compat.v1 import ConfigProto
from tensorflow.compat.v1 import InteractiveSession
config = ConfigProto()
config.gpu_options.allow_growth = True
session = InteractiveSession(config=config)

import tensorflow as tf
from src.Data_voc import Data
from src.YOLO import YOLO
from os import path
import config
import time
import numpy as np
from src import Log
# for save pb file
from tensorflow.python.framework import graph_util

# 配置优化器
def config_optimizer(optimizer_name, lr_init, momentum=0.99):
    Log.add_log("message:配置优化器:'" + str(optimizer_name) + "'")
    if optimizer_name == 'momentum':
        return tf.compat.v1.train.MomentumOptimizer(lr_init, momentum=momentum)
    elif optimizer_name == 'adam':
        return tf.compat.v1.train.AdamOptimizer(learning_rate=lr_init)
    elif optimizer_name == 'sgd':
        return tf.compat.v1.train.GradientDescentOptimizer(learning_rate=lr_init)
    else:
        Log.add_log("error:不支持的优化器类型:'" + str(optimizer_name) + "'")
        raise ValueError(str(optimizer_name) + ":不支持的优化器类型")

# 配置学习率
def config_lr(lr_type, lr_init, epoch_globalstep=0):
    Log.add_log("message:配置学习率:'" + str(lr_type) + "', 初始学习率:"+str(lr_init))
    if lr_type == 'piecewise':
        lr = tf.compat.v1.train.piecewise_constant(epoch_globalstep, 
                                                    config.piecewise_boundaries, config.piecewise_values)
    elif lr_type == 'exponential':
        lr = tf.compat.v1.train.exponential_decay(learning_rate=lr_init,
                                                    global_step=epoch_globalstep, decay_steps=10, decay_rate=0.99, staircase=True)
    elif lr_type =='constant':
        lr = lr_init
    else:
        Log.add_log("error:不支持的学习率类型:'" + str(lr_type) + "'")
        raise ValueError(str(lr_type) + ":不支持的学习率类型")

    return tf.maximum(lr, config.lr_lower)

# tensor 计算当前 epoch 
def compute_curr_epoch(global_step, batch_size, imgs_num):
    '''
    global_step:当前步数
    batch_size:batch_size
    imgs_num:一共有多少图片
    '''
    epoch = global_step * batch_size / imgs_num
    return  tf.cast(epoch, tf.int32)

# 训练
def backward():
    class_num = config.voc_class_num
    yolo = YOLO(class_num, config.voc_anchors, width=config.width, height=config.height)
    data = Data(config.voc_root_dir, config.voc_dir_ls, "./data/voc.names", class_num, config.batch_size, config.voc_anchors, config.data_augment, config.width, config.height, data_debug=config.data_debug)

    inputs = tf.compat.v1.placeholder(dtype=tf.float32, shape=[config.batch_size, None, None, 3])
    y1_true = tf.compat.v1.placeholder(dtype=tf.float32, shape=[config.batch_size, None, None, 3, 4+1+class_num])
    y2_true = tf.compat.v1.placeholder(dtype=tf.float32, shape=[config.batch_size, None, None, 3, 4+1+class_num])
    y3_true = tf.compat.v1.placeholder(dtype=tf.float32, shape=[config.batch_size, None, None, 3, 4+1+class_num])
    
    feature_y1, feature_y2, feature_y3 = yolo.forward(inputs, weight_decay=config.weight_decay, isTrain=True)

    global_step = tf.Variable(0, trainable=False)
    
    # 损失 yolov4
    loss = yolo.get_loss_v4(feature_y1, feature_y2, feature_y3, y1_true, y2_true, y3_true, 
                                                        config.cls_normalizer, config.ignore_thresh, config.prob_thresh, config.score_thresh)
    l2_loss = tf.compat.v1.losses.get_regularization_loss()
    
    epoch = compute_curr_epoch(global_step, config.batch_size, data.num_imgs)
    lr = config_lr(config.lr_type, config.lr_init, epoch)
    optimizer = config_optimizer(config.optimizer_type, lr, config.momentum)

    update_ops = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
        gvs = optimizer.compute_gradients(loss+l2_loss)
        clip_grad_var = [gv if gv[0] is None else[tf.clip_by_norm(gv[0], 100.), gv[1]] for gv in gvs]
        train_step = optimizer.apply_gradients(clip_grad_var, global_step=global_step)

    # 初始化
    init = tf.compat.v1.global_variables_initializer()

    saver = tf.compat.v1.train.Saver()
    with tf.compat.v1.Session() as sess:
        sess.run(init)
        step = 0
        
        ckpt = tf.compat.v1.train.get_checkpoint_state(config.voc_model_path)
        if ckpt and ckpt.model_checkpoint_path:
            saver.restore(sess, ckpt.model_checkpoint_path)
            step = ckpt.model_checkpoint_path.split('/')[-1].split('-')[-1]
            step = eval(step)
            Log.add_log("message:存在 ckpt 模型, global_step=" + str(step))
        else:
            Log.add_log("message:不存在 ckpt 模型")
        
        # 一共迭代这么多次
        total_steps = np.ceil(config.total_epoch * data.num_imgs / config.batch_size)
        while step < total_steps:
            start = time.perf_counter()
            batch_img, y1, y2, y3 = next(data)
            _, loss_, step, lr_ = sess.run([train_step, loss, global_step, lr],
                                    feed_dict={inputs:batch_img, y1_true:y1, y2_true:y2, y3_true:y3})
            end = time.perf_counter()
            print("step: %6d, loss: %.5g\t, w: %3d, h: %3d, lr:%.5g\t, time: %5f s"
                         %(step, loss_, data.width, data.height, lr_, end-start))
            
            if (loss_ > 1e3) and (step > 1e3):
                Log.add_log("error:loss exception, loss_value = "+str(loss_))
                ''' break the process or lower learning rate '''
                raise ValueError("error:loss exception, loss_value = "+str(loss_)+", please lower your learning rate")
                # lr = tf.math.maximum(tf.math.divide(lr, 10), config.lr_lower)

            if step % 5 == 2:
                Log.add_loss(str(step) + "\t" + str(loss_))

            if (step+1) % config.save_step == 0:
                if config.save_ckpt_model:
                    # save ckpt model
                    Log.add_log("message: save ckpt model, step=" + str(step) +", lr=" + str(lr_))
                    saver.save(sess, path.join(config.voc_model_path, config.voc_model_name), global_step=step)                    
                if config.save_pb_model:
                    Log.add_log("message: save pb model, step="+str(step) +", lr=" + str(lr_))
                    pb_model_name = path.join(config.voc_model_path, config.voc_model_name) + '-' + str(step) + ".pb"
                    constant_graph = graph_util.convert_variables_to_constants(sess, sess.graph_def, ['yolo/Conv_1/BiasAdd', 'yolo/Conv_9/BiasAdd', 'yolo/Conv_17/BiasAdd'])
                    # save  PB model
                    with tf.gfile.FastGFile(pb_model_name, mode='wb') as f:
                        f.write(constant_graph.SerializeToString())

        # save ckpt model
        if config.save_ckpt_model:
            Log.add_log("message: save final ckpt model, step=" + str(step))
            saver.save(sess, path.join(config.voc_model_path, config.voc_model_name), global_step=step)

        # save pb model
        if config.save_pb_model:
            Log.add_log("message: save final pb model, step="+str(step))
            pb_model_name = path.join(config.voc_model_path, config.voc_model_name)+ '-' + str(step) + ".pb"
            constant_graph = graph_util.convert_variables_to_constants(sess, sess.graph_def, ['yolo/Conv_1/BiasAdd', 'yolo/Conv_9/BiasAdd', 'yolo/Conv_17/BiasAdd'])
            # save  PB model
            with tf.gfile.FastGFile(pb_model_name, mode='wb') as f:
                f.write(constant_graph.SerializeToString())
    return 0


if __name__ == "__main__":
    Log.add_log("message:进入 VOC backward 函数")
    Log.add_loss("###########")
    backward()
