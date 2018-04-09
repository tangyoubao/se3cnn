# pylint: disable=C,R,E1101,W0221,W0511,W0601
'''
Architecture to predict the structural categories of proteins according to the CATH
classification (www.cathdb.info).

'''
import torch
import torch.nn as nn
import torch.utils.data
import torch.nn.functional as F

import numpy as np
import os
import time
import importlib

from experiments.datasets.cath.cath import Cath

from experiments.util import *



def get_output_shape(input_size, func):
    f = func(torch.autograd.Variable(torch.ones(2, *input_size)))
    return f.size()[1:]


def print_layer(layers, input_shape):
    """"Method for print architecture during model construction"""

    shape = get_output_shape(input_shape, layers)
    log_obj.write("layer %2d - %20s: %s [output size %s]" % (len(layers), list(layers.named_modules())[-1][0], tuple(shape), "{:,}".format(np.prod(shape))))


def train_loop(model, train_loader, optimizer, epoch):
    model.train()
    training_losses = []
    training_outs = []
    training_accs = []
    for batch_idx, (data, target) in enumerate(train_loader):
        time_start = time.perf_counter()

        target = torch.LongTensor(target)
        if use_gpu:
            data, target = data.cuda(), target.cuda()
        x = torch.autograd.Variable(data)
        y = torch.autograd.Variable(target)
        # forward and backward propagation
        out = model(x)
        losses = torch.nn.functional.cross_entropy(out, y, reduce=False)
        loss = losses.mean()
        loss.backward()
        if batch_idx % args.batchsize_multiplier == args.batchsize_multiplier-1:
            optimizer.step()
            optimizer.zero_grad()

        _, argmax = torch.max(out, 1)
        acc = (argmax.squeeze() == y).float().mean()

        training_losses.append(losses.data.cpu().numpy())
        training_outs.append(out.data.cpu().numpy())
        training_accs.append(acc.data[0])

        log_obj.write("[{}:{}/{}] loss={:.4} acc={:.2} time={:.2}".format(
            epoch, batch_idx, len(train_loader),
            float(loss.data[0]), float(acc.data[0]),
            time.perf_counter() - time_start))
    loss_avg = np.mean(training_losses)
    acc_avg = np.mean(training_accs)
    training_outs = np.concatenate(training_outs)
    training_losses = np.concatenate(training_losses)
    return loss_avg, acc_avg, training_outs, training_losses


def infer(model, loader):
    model.eval()
    losses = []
    outs = []
    ys = []
    for _, (data, target) in enumerate(loader):
        if use_gpu:
            data, target = data.cuda(), target.cuda()
        x = torch.autograd.Variable(data, volatile=True)
        y = torch.autograd.Variable(target)
        out = model(x)
        outs.append(out.data.cpu().numpy())
        ys.append(y.data.cpu().numpy())
        losses.append(torch.nn.functional.cross_entropy(out, y, reduce=False).data.cpu().numpy())
    outs = np.concatenate(outs)
    ys = np.concatenate(ys)
    return outs, ys, np.concatenate(losses)


def main():

    if args.mode == 'train':
        train_set = torch.utils.data.ConcatDataset([
            Cath(args.data_filename, split=i, download=True,
                 randomize_orientation=args.randomize_orientation,
                 discretization_bins=args.data_discretization_bins,
                 discretization_bin_size=args.data_discretization_bin_size) for i in range(7)])
        train_loader = torch.utils.data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True, num_workers=0, pin_memory=False, drop_last=True)
        n_input = train_set.datasets[0].n_atom_types
        n_output = len(train_set.datasets[0].label_set)
        log_obj.write("Training set: " + str([len(dataset) for dataset in train_set.datasets]))

    if args.mode in ['train', 'validate']:
        validation_set = Cath(
            args.data_filename, split=7,
            discretization_bins=args.data_discretization_bins,
            discretization_bin_size=args.data_discretization_bin_size)
        validation_loader = torch.utils.data.DataLoader(validation_set, batch_size=args.batch_size, shuffle=False, num_workers=0, pin_memory=False, drop_last=False)
        n_input = validation_set.n_atom_types
        n_output = len(validation_set.label_set)
        log_obj.write("Validation set: " + str(len(validation_set)))

    if args.mode == 'test':
        test_set = torch.utils.data.ConcatDataset([Cath(
            args.data_filename, split=i,
            discretization_bins=args.data_discretization_bins,
            discretization_bin_size=args.data_discretization_bin_size) for i in range(8, 10)])
        test_loader = torch.utils.data.DataLoader(test_set, batch_size=args.batch_size, shuffle=False, num_workers=0, pin_memory=False, drop_last=False)
        n_input = test_set.datasets[0].n_atom_types
        n_output = len(test_set.datasets[0].label_set)
        log_obj.write("Test set: " + str([len(dataset) for dataset in test_set.datasets]))

    model = network_module.network(n_input=n_input, n_output=n_output, args=args)
    if use_gpu:
        model.cuda()

    log_obj.write(str(model))
    log_obj.write("The model contains {} parameters".format(sum(p.numel() for p in model.parameters() if p.requires_grad)))

    param_groups = get_param_groups.get_param_groups(model, args)
    optimizer = optimizers_L1L2.Adam(param_groups, lr=args.initial_lr)
    optimizer.zero_grad()







    # Set up model dumping
    epoch_start_index = 0
    if args.read_from_checkpoint is not None:
        checkpoint_index = args.read_from_checkpoint
        checkpoint_basename = os.path.join(args.model_checkpoint_path,
                                           'model_%s' % (model.__class__.__name__))
        if checkpoint_index == -1:
            import glob
            checkpoint_filename = glob.glob(checkpoint_basename + '_*.ckpt')[-1]
            checkpoint_index = int(checkpoint_filename.split('.')[-2].split('_')[-1])
        else:
            checkpoint_filename = checkpoint_basename+'_%d.ckpt' % checkpoint_index
        log_obj.write("Restoring model from: " + checkpoint_filename)
        checkpoint = torch.load(checkpoint_filename)
        model.load_state_dict(checkpoint['state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])

        epoch_start_index = checkpoint_index+1





    # Set the logger
    if args.log_to_tensorboard:
        tf_logger, tensorflow_available = tensorflow_logger.get_tf_logger(path='networks/{:s}/tf_logs'.format(args.model))

    if args.mode == 'train':

        for epoch in range(epoch_start_index, args.training_epochs):

            # decay learning rate
            optimizer, _ = lr_schedulers.lr_scheduler_exponential(optimizer, epoch, args.initial_lr, args.lr_decay_start,
                                                                  args.lr_decay_base, verbose=True)

            loss_avg, acc_avg, training_outs, training_losses = train_loop(model, train_loader, optimizer, epoch)

            validation_outs, ys, validation_losses = infer(model, validation_loader)

            # compute the accuracy
            validation_acc = np.sum(validation_outs.argmax(-1) == ys) / len(ys)

            validation_loss_avg = np.mean(validation_losses)

            log_obj.write('TRAINING SET [{}:{}/{}] loss={:.4} acc={:.2}'.format(
                epoch, len(train_loader)-1, len(train_loader),
                loss_avg, acc_avg))
            log_obj.write('VALIDATION SET [{}:{}/{}] loss={:.4} acc={:.2}'.format(
                epoch, len(train_loader)-1, len(train_loader),
                validation_loss_avg, validation_acc))

            log_obj.write('VALIDATION losses: ' + str(validation_losses))

            if args.log_to_tensorboard and tensorflow_available:

                # ============ TensorBoard logging ============#
                # (1) Log the scalar values
                info = {
                    'training set avg loss': loss_avg,
                    'training set accuracy': acc_avg,
                    'validation set avg loss': validation_loss_avg,
                    'validation set accuracy': validation_acc,
                }

                step = epoch
                for tag, value in info.items():
                    tf_logger.scalar_summary(tag, value, step + 1)

                # (2) Log values and gradients of the parameters (histogram)
                for tag, value in model.named_parameters():
                    tag = tag.replace('.', '/')
                    tf_logger.histo_summary(tag, value.data.cpu().numpy(), step + 1)
                    tf_logger.histo_summary(tag + '/grad', value.grad.data.cpu().numpy(),
                                         step + 1)

                # (3) Log losses for all datapoints in validation and training set
                tf_logger.histo_summary("losses/validation/", validation_losses, step+1)
                tf_logger.histo_summary("losses/training", training_losses, step+1)

                # (4) Log losses for all datapoints in validation and training set
                for i in range(n_output):
                    tf_logger.histo_summary("logits/%d/validation" % i, validation_outs[:, i], step+1)
                    tf_logger.histo_summary("logits/%d/training" % i, training_outs[:, i], step+1)

            if args.save_checkpoints:
                checkpoint_filename = os.path.join(
                    args.model_checkpoint_path,
                    'model_%s_%d.ckpt' % (model.__class__.__name__, epoch))
                torch.save({'state_dict': model.state_dict(),
                            'optimizer': optimizer.state_dict()},
                           checkpoint_filename)
                log_obj.write("Model saved to %s" % checkpoint_filename)

    elif args.mode == 'validate':
        out, y, validation_loss_sum = infer(model, validation_loader)

        # compute the accuracy
        validation_acc = np.sum(out.argmax(-1) == y) / len(y)
        validation_loss_avg = validation_loss_sum / len(validation_loader.dataset)

        log_obj.write('VALIDATION SET: loss={:.4} acc={:.2}'.format(
            validation_loss_avg, validation_acc))

    elif args.mode == 'test':
        out, y, test_loss_sum = infer(model, test_loader)

        # compute the accuracy
        test_acc = np.sum(out.argmax(-1) == y) / len(y)
        test_loss_avg = test_loss_sum / len(test_loader.dataset)

        log_obj.write('VALIDATION SET: loss={:.4} acc={:.2}'.format(
            test_loss_avg, test_acc))


if __name__ == '__main__':

    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument("--data-filename", choices={"cath_3class.npz", "cath_10arch.npz", "cath_3class_backbone.npz", }, required=True,
                        help="The name of the data file (will automatically downloaded)")
    parser.add_argument("--data-discretization-bins", type=int, default=50,
                        help="Number of bins used in each dimension for the discretization of the input data")
    parser.add_argument("--data-discretization-bin-size", type=float, default=2.0,
                        help="Size of bins used in each dimension for the discretization of the input data")
    parser.add_argument("--model", required=True,
                        help="Which model definition to use")
    parser.add_argument("--training-epochs", default=100, type=int,
                        help="Which model definition to use")
    parser.add_argument("--randomize-orientation", action="store_true", default=False,
                        help="Whether to randomize the orientation of the structural input during training (default: %(default)s)")
    parser.add_argument("--batch-size", default=32, type=int,
                        help="Size of mini batches to use per iteration, can be accumulated via argument batchsize_multiplier (default: %(default)s)")
    parser.add_argument("--batchsize-multiplier", default=1, type=int,
                        help="number of minibatch iterations accumulated before applying the update step, effectively multiplying batchsize (default: %(default)s)")
    parser.add_argument("--log-to-tensorboard", action="store_true", default=False,
                        help="Whether to output log information in tensorboard format (default: %(default)s)")


    parser.add_argument("--checkpoint-name", type=str,
                        help="Checkpoint file name (file assumed to be in network folder)")
    parser.add_argument("--save-checkpoints", action="store_true", default=False,
                        help="Save model checkpoints at each epoch")
    parser.add_argument("--read-from-checkpoint", type=int, default=None,
                        help="Read model from checkpoint given by index")



    parser.add_argument("--mode", choices=['train', 'test', 'validate'], default="train",
                        help="Mode of operation (default: %(default)s)")
    parser.add_argument("--initial_lr", default=1e-3, type=float,
                        help="Initial learning rate (without decay)")
    parser.add_argument("--lr_decay_start", type=int, default=1,
                        help="epoch after which the exponential learning rate decay starts")
    parser.add_argument("--lr_decay_base", type=float, default=1,
                        help="exponential decay factor per epoch")
    # NEW
    parser.add_argument("--kernel_size", type=int, default=5,
                        help="convolution kernel size")
    parser.add_argument("--p-drop-conv", type=float, default=0,
                        help="convolution/capsule dropout probability")
    parser.add_argument("--p-drop-fully", type=float, default=0,
                        help="fully connected layer dropout probability")
    parser.add_argument("--bandlimit-mode", choices={"conservative", "compromise", "sfcnn"}, default="compromise",
                        help="bandlimiting heuristic for spherical harmonics")
    parser.add_argument("--SE3-nonlinearity", choices={"gated", "norm"}, default="gated",
                        help="Which nonlinearity to use for non-scalar capsules")
    # TODO: NOT IMPLEMENTED FOR CONVENTONAL NETWORKS YET!
    parser.add_argument("--downsample-by-pooling", action='store_true', default=False,
                        help="Switches from downsampling by striding to downsampling by pooling")
    # WEIGHTS
    parser.add_argument("--lamb_conv_weight_L1", default=0, type=float,
                        help="L1 regularization factor for convolution weights")
    parser.add_argument("--lamb_conv_weight_L2", default=0, type=float,
                        help="L2 regularization factor for convolution weights")
    parser.add_argument("--lamb_bn_weight_L1", default=0, type=float,
                        help="L1 regularization factor for batchnorm weights")
    parser.add_argument("--lamb_bn_weight_L2", default=0, type=float,
                        help="L2 regularization factor for batchnorm weights")
    parser.add_argument("--lamb_linear_weight_L1", default=0, type=float,
                        help="L1 regularization factor for fully connected layer weights (except last / classification layer)")
    parser.add_argument("--lamb_linear_weight_L2", default=0, type=float,
                        help="L2 regularization factor for fully connected layer weights (except last / classification layer)")
    parser.add_argument("--lamb_softmax_weight_L1", default=0, type=float,
                        help="L1 regularization factor for classification layer weights")
    parser.add_argument("--lamb_softmax_weight_L2", default=0, type=float,
                        help="L2 regularization factor for classification layer weights")
    # BIASES
    parser.add_argument("--lamb_conv_bias_L1", default=0, type=float,
                        help="L1 regularization factor for convolution biases")
    parser.add_argument("--lamb_conv_bias_L2", default=0, type=float,
                        help="L2 regularization factor for convolution biases")
    parser.add_argument("--lamb_norm_activ_bias_L1", default=0, type=float,
                        help="L1 regularization factor for norm activation biases")
    parser.add_argument("-lamb_norm_activ_bias_L2", default=0, type=float,
                        help="L2 regularization factor for norm activation biases")
    parser.add_argument("--lamb_bn_bias_L1", default=0, type=float,
                        help="L1 regularization factor for batchnorm biases")
    parser.add_argument("--lamb_bn_bias_L2", default=0, type=float,
                        help="L2 regularization factor for batchnorm biases")
    parser.add_argument("--lamb_linear_bias_L1", default=0, type=float,
                        help="L1 regularization factor for fully connected layer biases (except last / classification layer)")
    parser.add_argument("--lamb_linear_bias_L2", default=0, type=float,
                        help="L2 regularization factor for fully connected layer biases (except last / classification layer)")
    parser.add_argument("--lamb_softmax_bias_L1", default=0, type=float,
                        help="L1 regularization factor for classification layer biases")
    parser.add_argument("--lamb_softmax_bias_L2", default=0, type=float,
                        help="L2 regularization factor for classification layer biases")

    args, unparsed = parser.parse_known_args()

    if len(unparsed) != 0:
        print('\n{:d} unparsed (unknown arguments):'.format(len(unparsed)))
        for u in unparsed:
            print('  ', u)
        print()
        raise ValueError('unparsed / unknown arguments')

    network_module = importlib.import_module('networks.{:s}.{:s}'.format(args.model, args.model))

    # instantiate simple logger
    log_obj = logger.logger(path='networks/{:s}/logs'.format(args.model), network=args.model)
    log_obj.write('\n# Options')
    for key, value in sorted(vars(args).items()):
        log_obj.write('\t'+str(key)+'\t'+str(value))


    torch.backends.cudnn.benchmark = True
    use_gpu = torch.cuda.is_available()

    main()