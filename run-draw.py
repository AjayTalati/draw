#!/usr/bin/env python

from __future__ import division, print_function

import logging

FORMAT = '[%(asctime)s] %(name)-15s %(message)s'
DATEFMT = "%H:%M:%S"
logging.basicConfig(format=FORMAT, datefmt=DATEFMT, level=logging.INFO)

import ipdb
import theano
import theano.tensor as T

from argparse import ArgumentParser
from collections import OrderedDict
from theano import tensor

from blocks.datasets.streams import DataStream
from blocks.datasets.schemes import SequentialScheme
from blocks.datasets.mnist import MNIST 

from blocks.algorithms import GradientDescent, RMSProp, Adam
from blocks.initialization import Constant, IsotropicGaussian, Orthogonal 
from blocks.filter import VariableFilter
from blocks.graph import ComputationGraph
from blocks.roles import WEIGHTS, BIASES, PARAMETER
from blocks.monitoring import aggregation
from blocks.extensions import FinishAfter, Timing, Printing  #, ProgressBar
from blocks.extensions.plot import Plot
from blocks.extensions.saveload import SerializeMainLoop
from blocks.extensions.monitoring import DataStreamMonitoring, TrainingDataMonitoring
from blocks.main_loop import MainLoop

from blocks.bricks.cost import BinaryCrossEntropy

from lib.progress_extension import ProgressBar

from draw import *


#----------------------------------------------------------------------------
def main(name, epochs, batch_size, learning_rate, n_iter, enc_dim, dec_dim, z_dim):
    """ Run a Reweighted Wake Sleep experiment """

    if name is None:
        name = "mnist-enc%d-dec%d-z%d" % (enc_dim, dec_dim, z_dim)

    print("\nRunning experiment %s" % name)
    print("         learning rate: %5.3f" % learning_rate) 
    print("     encoder dimension: %d" % enc_dim)
    print("           z dimension: %d" % z_dim)
    print("     decoder dimension: %d" % dec_dim)
    print()


    #------------------------------------------------------------------------

    x_dim = 28*28
    read_dim = 2*x_dim
    
    inits = {
        #'weights_init': Orthogonal(),
        'weights_init': IsotropicGaussian(0.0001),
        'biases_init': Constant(0.),
    }
    

    prior_mu = T.zeros([z_dim])
    prior_log_sigma = T.zeros([z_dim])

    reader = Reader(x_dim=x_dim, dec_dim=dec_dim, **inits)
    writer = Writer(input_dim=dec_dim, output_dim=x_dim, **inits)
    encoder = RNN(name="RNN_enc", state_dim=enc_dim, input_dim=(read_dim+dec_dim), **inits)
    decoder = RNN(name="RNN_dec", state_dim=dec_dim, input_dim=z_dim, **inits)
    q_sampler = Qsampler(input_dim=enc_dim, output_dim=z_dim, **inits)
        
    for brick in [reader, writer, encoder, decoder, q_sampler]:
        brick.initialize()

    #------------------------------------------------------------------------
    x = tensor.matrix('features')

    # This is one iteration 
    def one_iteration(c, h_enc, z_mean, z_log_sigma, z, h_dec, x):
        x_hat = x-T.nnet.sigmoid(c)
        r = reader.apply(x, x_hat, h_dec)
        h_enc = encoder.apply(h_enc, T.concatenate([r, h_dec], axis=1))
        z_mean, z_log_sigma, z = q_sampler.apply(h_enc)
        h_dec = decoder.apply(h_dec, z)
        c = c + writer.apply(h_dec)
        return c, h_enc, z_mean, z_log_sigma, z, h_dec

    outputs_info = [
            T.zeros([batch_size, x_dim]),     # c
            T.zeros([batch_size, enc_dim]),   # h_enc
            T.zeros([batch_size, z_dim]),     # z_mean
            T.zeros([batch_size, z_dim]),     # z_log_sigma
            T.zeros([batch_size, z_dim]),     # z
            T.zeros([batch_size, dec_dim]),   # h_dec
        ]
    
    outputs, scan_updates = theano.scan(fn=one_iteration, 
                                sequences=[],
                                outputs_info=outputs_info,
                                non_sequences=[x],
                                n_steps=n_iter)

    c, h_enc, z_mean, z_log_sigma, z, h_dec = outputs

    kl_terms = (
        prior_log_sigma - z_log_sigma
        + 0.5 * (
            tensor.exp(2 * z_log_sigma) + (z_mean - prior_mu) ** 2
        ) / tensor.exp(2 * prior_log_sigma)
        - 0.5
    ).sum(axis=-1)
    
    x_hat = T.nnet.sigmoid(c[-1,:,:])
    recons_term = BinaryCrossEntropy().apply(x, x_hat)
    recons_term.name = "recons_term"

    cost = (recons_term + kl_terms.sum(axis=0)).mean()
    cost.name = "nll_bound"

    #------------------------------------------------------------------------
    # Setup monitors
    monitors = [cost]
    for t in range(n_iter):
        kl_term_t = kl_terms[t,:].mean()
        kl_term_t.name = "kl_term_%d" % t

        x_hat_t = T.nnet.sigmoid(c[t,:,:])
        recons_term_t = BinaryCrossEntropy().apply(x, x_hat_t)
        recons_term_t = recons_term_t.mean()
        recons_term_t.name = "recons_term_%d" % t

        monitors +=[kl_term_t, recons_term_t]

    # Live plotting...
    plot_channels = [
        ["train_nll_bound"],
        ["train_kl_term_%d" % t for t in range(n_iter)],
        ["train_recons_term_%d" % t for t in range(n_iter)]
    ]

    #------------------------------------------------------------
    cg = ComputationGraph([cost])
    params = VariableFilter(roles=[PARAMETER])(cg.variables)

    algorithm = GradientDescent(
        cost=cost, 
        params=params,
        step_rule=Adam(learning_rate)
        #step_rule=RMSProp(learning_rate),
        #step_rule=Momentum(learning_rate=learning_rate, momentum=0.95)
    )
    algorithm.add_updates(scan_updates)

    #------------------------------------------------------------

    #mnist_train = BinarizedMNIST("train", sources=['features'])
    #mnist_test = BinarizedMNIST("test", sources=['features'])
    mnist_train = MNIST("train", binary=True, sources=['features'])
    mnist_test = MNIST("test", binary=True, sources=['features'])

    main_loop = MainLoop(
        model=None,
        data_stream=DataStream(mnist_train,
                        iteration_scheme=SequentialScheme(
                        mnist_train.num_examples, batch_size)),
        algorithm=algorithm,
        extensions=[
            Timing(),
            ProgressBar(),
            FinishAfter(after_n_epochs=epochs),
            #DataStreamMonitoring(
            #    monitors,
            #    DataStream(mnist_test,
            #        iteration_scheme=SequentialScheme(
            #        mnist_test.num_examples, batch_size)),
            #        prefix="test"),
            TrainingDataMonitoring(
                monitors, 
                prefix="train",
                after_every_epoch=True),
            SerializeMainLoop(name+".pkl"),
            Plot(name, channels=plot_channels),
            Printing()])
    main_loop.run()

#-----------------------------------------------------------------------------

if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--name", type=str, dest="name",
                default=None, help="Name for this experiment")
    parser.add_argument("--epochs", type=int, dest="epochs",
                default=25, help="Number of training epochs to do")
    parser.add_argument("--bs", "--batch-size", type=int, dest="batch_size",
                default=100, help="Size of each mini-batch")
    parser.add_argument("--lr", "--learning-rate", type=float, dest="learning_rate",
                default=1e-3, help="Learning rate")
    parser.add_argument("--niter", type=int, dest="n_iter",
                default=5, help="No. of iterations")
    parser.add_argument("--enc-dim", type=int, dest="enc_dim",
                default=200, help="Encoder RNN state dimension")
    parser.add_argument("--dec-dim", type=int, dest="dec_dim",
                default=200, help="Decoder  RNN state dimension")
    parser.add_argument("--z-dim", type=int, dest="z_dim",
                default=50, help="Z-vector dimension")
    args = parser.parse_args()

    main(**vars(args))

