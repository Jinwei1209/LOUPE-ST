"""
    Experiment on multi-echo kspace data reconstruction from GE scanner
"""
import os
import time
import torch
import math
import argparse
import numpy as np

from torch.utils import data
from loader.kdata_multi_echo_GE import kdata_multi_echo_GE
from utils.data import *
from models.unet import Unet
from models.initialization import *
from models.discriminator import Basic_D
from utils.train import *
from IPython.display import clear_output
from utils.loss import *
from models.dc_blocks import *
from models.unet_with_dc import *
from models.dc_with_prop_mask import *
from models.dc_with_straight_through_pmask import *
from models.dc_st_pmask import *
from models.resnet_with_dc import *
from utils.test import *
from utils.operators import *

if __name__ == '__main__':

    lrG_dc = 1e-3
    niter = 500
    batch_size = 1
    display_iters = 10
    gen_iterations = 1
    t0 = time.time()
    epoch = 0
    errL2_dc_sum = 0
    PSNRs_val = []
    Validation_loss = []
    ncoil = 12
    nrow = 206
    ncol = 80
    necho = 10
    lambda_dll2 = 1e-3
    
    # typein parameters
    parser = argparse.ArgumentParser(description='CardiacQSM')
    parser.add_argument('--gpu_id', type=str, default='0')
    parser.add_argument('--flag_train', type=int, default=1)  # 1 for training, 0 for testing
    parser.add_argument('--echo_cat', type=int, default=1)  # flag to concatenate echo dimension into channel
    parser.add_argument('--solver', type=int, default=0)  # 0 for deep Quasi-newton, 1 for deep ADMM,
                                                          # 2 for TV Quasi-newton, 3 for TV ADMM.
    parser.add_argument('--K', type=int, default=5)  # number of unrolls
    parser.add_argument('--loupe', type=int, default=0)  # flag to use loupe for sampling pattern optimization
    parser.add_argument('--precond', type=int, default=0)  # flag to use preconsitioning
    parser.add_argument('--att', type=int, default=0)  # flag to use attention-based denoiser
    parser.add_argument('--random', type=int, default=0)  # flag to multiply the input data with a random complex number
    parser.add_argument('--normalization', type=int, default=1)  # 0 for no normalization
    opt = {**vars(parser.parse_args())}
    K = opt['K']
    # concatenate echo dimension to the channel dimension for TV regularization
    if opt['solver'] > 1:
        opt['echo_cat'] = 1

    os.environ['CUDA_VISIBLE_DEVICES'] = opt['gpu_id']
    rootName = '/data/Jinwei/Multi_echo_slice_recon_GE'
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # load mask
    masks = np.real(readcfl(rootName+'/megre_slice_GE/mask'))
    # masks = np.ones(masks.shape)
    masks = masks[..., np.newaxis] # (nrow, ncol, 1)
    masks = torch.tensor(masks, device=device).float()
    # to complex data
    masks = torch.cat((masks, torch.zeros(masks.shape).to(device)),-1) # (nrow, ncol, 2)
    # add echo dimension
    masks = masks[None, ...] # (1, nrow, ncol, 2)
    masks = torch.cat(necho*[masks]) # (necho, nrow, ncol, 2)
    # add coil dimension
    masks = masks[None, ...] # (1, necho, nrow, ncol, 2)
    masks = torch.cat(ncoil*[masks]) # (ncoil, necho, nrow, ncol, 2)
    # add batch dimension
    masks = masks[None, ...] # (1, ncoil, necho, nrow, ncol, 2)
 
    # flip matrix
    flip = torch.ones([necho, nrow, ncol, 1]) 
    flip = torch.cat((flip, torch.zeros(flip.shape)), -1).to(device)
    flip[:, ::2, ...] = - flip[:, ::2, ...] 
    flip[:, :, ::2, ...] = - flip[:, :, ::2, ...]
    # add batch dimension
    flip = flip[None, ...] # (1, necho, nrow, ncol, 2)

    # training
    if opt['flag_train'] == 1:
        lossl1 = lossL1()

        dataLoader = kdata_multi_echo_GE(
            rootDir=rootName,
            contrast='MultiEcho', 
            split='train',
            normalization=opt['normalization'],
            echo_cat=opt['echo_cat']
        )
        trainLoader = data.DataLoader(dataLoader, batch_size=batch_size, shuffle=True, num_workers=1)

        dataLoader_val = kdata_multi_echo_GE(
            rootDir=rootName,
            contrast='MultiEcho', 
            split='val',
            normalization=opt['normalization'],
            echo_cat=opt['echo_cat']
        )
        valLoader = data.DataLoader(dataLoader_val, batch_size=batch_size, shuffle=True, num_workers=1)

        if opt['echo_cat'] == 1:
            netG_dc = Resnet_with_DC2(
                input_channels=2*necho,
                filter_channels=32*necho,
                lambda_dll2=lambda_dll2,
                K=K,
                echo_cat=1,
                flag_solver=opt['solver'],
                flag_precond=opt['precond'],
                flag_loupe=opt['loupe']
            )
        else:
            netG_dc = Resnet_with_DC2(
                input_channels=2,
                filter_channels=32,
                lambda_dll2=lambda_dll2,
                K=K,
                echo_cat=0,
                flag_solver=opt['solver'],
                flag_precond=opt['precond'],
                flag_loupe=opt['loupe']
            )   
        netG_dc.to(device)

        # optimizer
        optimizerG_dc = optim.Adam(netG_dc.parameters(), lr=lrG_dc, betas=(0.9, 0.999))

        while epoch < niter:

            epoch += 1

            # training phase
            netG_dc.train()
            metrices_train = Metrices()
            for idx, (kdatas, targets, csms, brain_masks) in enumerate(trainLoader):

                if torch.sum(brain_masks) == 0:
                    continue

                if gen_iterations%display_iters == 0:

                    print('epochs: [%d/%d], batchs: [%d/%d], time: %ds'
                    % (epoch, niter, idx, 600//batch_size, time.time()-t0))

                    print('echo_cat: {}, precond: {}'.format( \
                            opt['echo_cat'], opt['precond']))
                    
                    if opt['loupe']:
                        print('Sampling ratio cal: %f, Sampling ratio setup: %f, Pmask: %f' 
                        % (torch.mean(netG_dc.Mask), netG_dc.samplingRatio, torch.mean(netG_dc.Pmask)))

                    print('netG_dc --- loss_L2_dc: %f, lambda_dll2: %f, K: %d'
                        % (errL2_dc_sum/display_iters, netG_dc.lambda_dll2, K))

                    print('Average PSNR in Training dataset is %.2f' 
                    % (np.mean(np.asarray(metrices_train.PSNRs[-1-display_iters*batch_size:]))))
                    if epoch > 1:
                        print('Average PSNR in Validation dataset is %.2f' 
                        % (np.mean(np.asarray(metrices_val.PSNRs))))
                    
                    print(' ')

                    errL2_dc_sum = 0
                
                kdatas = kdatas.to(device)
                targets = targets.to(device)
                csms = csms.to(device)
                brain_masks = brain_masks.to(device)

                # operator = Back_forward_multiEcho(csms, masks, 0)
                # test_image = operator.AtA(targets, 0).cpu().detach().numpy()
                # save_mat(rootName+'/results/test_image.mat', 'test_image', test_image)

                optimizerG_dc.zero_grad()
                Xs = netG_dc(kdatas, csms, masks, flip)

                lossl2_sum = 0
                for i in range(len(Xs)):
                    lossl2_sum += lossl1(Xs[i]*brain_masks, targets*brain_masks)
                lossl2_sum.backward()
                optimizerG_dc.step()

                errL2_dc_sum += lossl2_sum.item()

                # calculating metrices
                metrices_train.get_metrices(Xs[-1]*brain_masks, targets*brain_masks)
                gen_iterations += 1

            
            # validation phase
            netG_dc.eval()
            metrices_val = Metrices()
            loss_total_list = []
            with torch.no_grad():  # to solve memory exploration issue
                for idx, (kdatas, targets, csms, brain_masks) in enumerate(valLoader):

                    if torch.sum(brain_masks) == 0:
                        continue

                    kdatas = kdatas.to(device)
                    targets = targets.to(device)
                    csms = csms.to(device)
                    brain_masks = brain_masks.to(device)

                    Xs = netG_dc(kdatas, csms, masks, flip)

                    metrices_val.get_metrices(Xs[-1]*brain_masks, targets*brain_masks)
                    targets = np.asarray(targets.cpu().detach())
                    brain_masks = np.asarray(brain_masks.cpu().detach())
                    temp = 0
                    for i in range(len(Xs)):
                        X = np.asarray(Xs[i].cpu().detach())
                        temp += abs(X - targets) * brain_masks
                    lossl2_sum = np.mean(temp)
                    loss_total_list.append(lossl2_sum)

                print('\n Validation loss: %f \n' 
                    % (sum(loss_total_list) / float(len(loss_total_list))))
                Validation_loss.append(sum(loss_total_list) / float(len(loss_total_list)))

            # save weights
            if Validation_loss[-1] == min(Validation_loss):
                torch.save(netG_dc.state_dict(), rootName+'/weights/echo_cat={}_precond={}_K={}.pt' \
                           .format(opt['echo_cat'], opt['precond'], opt['K']))

    
    # for test
    if opt['flag_train'] == 0:
        if opt['echo_cat'] == 1:
            netG_dc = Resnet_with_DC2(
                input_channels=2*necho,
                filter_channels=32*necho,
                lambda_dll2=lambda_dll2,
                K=K,
                echo_cat=1,
                flag_solver=opt['solver'],
                flag_precond=opt['precond'],
                flag_loupe=opt['loupe']
            )
        else:
            netG_dc = Resnet_with_DC2(
                input_channels=2,
                filter_channels=32,
                lambda_dll2=lambda_dll2,
                K=K,
                echo_cat=0,
                flag_solver=opt['solver'],
                flag_precond=opt['precond'],
                flag_loupe=opt['loupe']
            )
        # weights_dict = torch.load(rootName+'/weights/echo_cat={}_precond={}_K={}.pt' \
        #                         .format(opt['echo_cat'], opt['precond'], opt['K']))
        # netG_dc.load_state_dict(weights_dict)
        netG_dc.to(device)
        netG_dc.eval()

        Inputs = []
        Targets = []
        Recons = []
        preconds = []

        dataLoader_test = kdata_multi_echo_GE(
            rootDir=rootName,
            contrast='MultiEcho', 
            split='test',
            normalization=opt['normalization'],
            echo_cat=opt['echo_cat']
        )
        testLoader = data.DataLoader(dataLoader_test, batch_size=batch_size, shuffle=False)

        with torch.no_grad():
            for idx, (kdatas, targets, csms, brain_masks) in enumerate(testLoader):
                print(idx)
                if idx == 1 and opt['loupe'] == 1:
                    print('Saving sampling mask')
                    Mask = netG_dc.Mask.cpu().detach().numpy()
                    Mask[nrow//2-13:nrow//2+12, ncol//2-13:ncol//2+12] = 1
                    save_mat(rootName+'/results/Mask.mat', 'Mask', Mask)

                kdatas = kdatas.to(device)
                targets = targets.to(device)
                csms = csms.to(device)
                brain_masks = brain_masks.to(device)

                inputs = backward_multiEcho(kdatas, csms, masks, flip,
                                            opt['echo_cat'])
                Xs = netG_dc(kdatas, csms, masks, flip)
                precond = netG_dc.precond

                Inputs.append(inputs.cpu().detach())
                Targets.append(targets.cpu().detach())
                Recons.append(Xs[-1].cpu().detach())
                # preconds.append(precond.cpu().detach())

            Inputs = r2c(np.concatenate(Inputs, axis=0), opt['echo_cat'])
            Inputs = np.transpose(Inputs, [0, 2, 3, 1])
            Targets = r2c(np.concatenate(Targets, axis=0), opt['echo_cat'])
            Targets = np.transpose(Targets, [0, 2, 3, 1])
            Recons = r2c(np.concatenate(Recons, axis=0), opt['echo_cat'])
            Recons = np.transpose(Recons, [0, 2, 3, 1])

            save_mat(rootName+'/results/Inputs.mat', 'Inputs', Inputs)
            save_mat(rootName+'/results/Targets.mat', 'Targets', Targets)
            save_mat(rootName+'/results/Recons_echo_cat={}_precond={}_K={}.mat' \
              .format(opt['echo_cat'], opt['precond'], opt['K']), 'Recons', Recons)


