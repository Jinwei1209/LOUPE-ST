"""
    Experiment on multi-echo kspace data reconstruction from GE scanner
"""
import os
import time
import torch
import math
import argparse
import scipy.io as sio
import numpy as np

from IPython.display import clear_output
from torch.utils import data
from loader.kdata_multi_echo_GE import kdata_multi_echo_GE
from utils.data import r2c, save_mat, readcfl, memory_pre_alloc, torch_channel_deconcate
from utils.loss import lossL1
from utils.test import Metrices
from utils.operators import backward_multiEcho
from models.resnet_with_dc import Resnet_with_DC2
from fits.fits import fit_R2_LM

if __name__ == '__main__':

    lrG_dc = 1e-3
    niter = 100
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
    parser = argparse.ArgumentParser(description='Multi_echo_GE')
    parser.add_argument('--gpu_id', type=str, default='0')
    parser.add_argument('--flag_train', type=int, default=1)  # 1 for training, 0 for testing
    parser.add_argument('--echo_cat', type=int, default=1)  # flag to concatenate echo dimension into channel
    parser.add_argument('--solver', type=int, default=1)  # 0 for deep Quasi-newton, 1 for deep ADMM,
                                                          # 2 for TV Quasi-newton, 3 for TV ADMM.
    parser.add_argument('--K', type=int, default=10)  # number of unrolls
    parser.add_argument('--loupe', type=int, default=0)  #-1: manually designed mask, 0 fixed learned mask
                                                         # 1: mask learning, same mask across echos, 2: mask learning, mask for each echo
    parser.add_argument('--norm_last', type=int, default=0)  # 0: norm+relu, 1: relu+norm
    parser.add_argument('--temporal_conv', type=int, default=0) # 0: no temporal, 1: center, 2: begining
    parser.add_argument('--1d_type', type=str, default='shear')  # 'shear' or 'random' sampling type of 1D mask
    parser.add_argument('--samplingRatio', type=float, default=0.2)

    parser.add_argument('--precond', type=int, default=0)  # flag to use preconsitioning
    parser.add_argument('--att', type=int, default=0)  # flag to use attention-based denoiser
    parser.add_argument('--random', type=int, default=0)  # flag to multiply the input data with a random complex number
    parser.add_argument('--normalization', type=int, default=1)  # 0 for no normalization
    opt = {**vars(parser.parse_args())}
    K = opt['K']
    norm_last = opt['norm_last']
    flag_temporal_conv = opt['temporal_conv']
    # concatenate echo dimension to the channel dimension for TV regularization
    if opt['solver'] > 1:
        opt['echo_cat'] = 1

    os.environ['CUDA_VISIBLE_DEVICES'] = opt['gpu_id']
    rootName = '/data/Jinwei/Multi_echo_slice_recon_GE'
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    if opt['loupe'] == -1:
        # load manually designed mask
        masks = np.real(readcfl(rootName+'/masks/mask_{}m'.format(opt['samplingRatio'])))
        # masks = np.real(readcfl(rootName+'/masks/mask_{}_1d_{}'.format(opt['samplingRatio'], opt['1d_type'])))
    elif opt['loupe'] == 0:
        # load fixed loupe optimized mask
        masks = np.real(readcfl(rootName+'/masks/mask_{}'.format(opt['samplingRatio'])))
        
    if opt['loupe'] < 1:
        # for 2D random sampling 
        masks = masks[..., np.newaxis] # (nrow, ncol, 1)
        
        # # for 1D echo-identical sampling
        # masks = masks[..., 0, np.newaxis] # (nrow, ncol, 1)
        # masks[nrow//2-13:nrow//2+12, ncol//2-13:ncol//2+12, ...] = 1 # add calibration region

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

        # # for 1D sampling
        # masks = masks[..., np.newaxis] # (nrow, ncol, necho, 1)
        # masks[nrow//2-13:nrow//2+12, ncol//2-13:ncol//2+12, ...] = 1 # add calibration region
        # masks = torch.tensor(masks, device=device).float()
        # # to complex data
        # masks = torch.cat((masks, torch.zeros(masks.shape).to(device)),-1) # (nrow, ncol, necho, 2)
        # # permute echo dimension
        # masks = masks.permute(2, 0, 1, 3) # (necho, nrow, ncol, 2)
        # # add coil dimension
        # masks = masks[None, ...] # (1, necho, nrow, ncol, 2)
        # masks = torch.cat(ncoil*[masks]) # (ncoil, necho, nrow, ncol, 2)
        # # add batch dimension
        # masks = masks[None, ...] # (1, ncoil, necho, nrow, ncol, 2)
    else:
        masks = []
 
    # flip matrix
    flip = torch.ones([necho, nrow, ncol, 1]) 
    flip = torch.cat((flip, torch.zeros(flip.shape)), -1).to(device)
    flip[:, ::2, ...] = - flip[:, ::2, ...] 
    flip[:, :, ::2, ...] = - flip[:, :, ::2, ...]
    # add batch dimension
    flip = flip[None, ...] # (1, necho, nrow, ncol, 2)

    # training
    if opt['flag_train'] == 1:
        # memory_pre_alloc(opt['gpu_id'])
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
                flag_loupe=opt['loupe'],
                samplingRatio=opt['samplingRatio'],
                norm_last=norm_last,
                flag_temporal_conv=flag_temporal_conv
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
                flag_loupe=opt['loupe'],
                samplingRatio=opt['samplingRatio']
            )
        netG_dc.to(device)
        if opt['loupe'] < 1:
            weights_dict = torch.load(rootName+'/weights/echo_cat={}_solver={}_K=2_loupe=1_ratio={}_{}{}.pt'
                        .format(opt['echo_cat'], opt['solver'], opt['samplingRatio'], norm_last, flag_temporal_conv))
            netG_dc.load_state_dict(weights_dict)

        # optimizer
        optimizerG_dc = torch.optim.Adam(netG_dc.parameters(), lr=lrG_dc, betas=(0.9, 0.999))

        while epoch < niter:

            epoch += 1

            # training phase
            netG_dc.train()
            metrices_train = Metrices()
            for idx, (kdatas, targets, targets_gen, csms, brain_masks) in enumerate(trainLoader):

                if torch.sum(brain_masks) == 0:
                    continue

                if gen_iterations%display_iters == 0:

                    print('epochs: [%d/%d], batchs: [%d/%d], time: %ds'
                    % (epoch, niter, idx, 600//batch_size, time.time()-t0))

                    print('echo_cat: {}, solver: {}, K: {}, loupe: {}'.format( \
                            opt['echo_cat'], opt['solver'], opt['K'], opt['loupe']))
                    
                    if opt['loupe'] > 0:
                        print('Sampling ratio cal: %f, Sampling ratio setup: %f, Pmask: %f' 
                        % (torch.mean(netG_dc.Mask), netG_dc.samplingRatio, torch.mean(netG_dc.Pmask)))
                    else:
                        print('Sampling ratio cal: %f' % (torch.mean(netG_dc.Mask)))

                    if opt['solver'] < 3:
                        print('netG_dc --- loss_L2_dc: %f, lambda_dll2: %f'
                            % (errL2_dc_sum/display_iters, netG_dc.lambda_dll2))
                    else:
                        print('netG_dc --- loss_L2_dc: %f, lambda_tv: %f, rho_penalty: %f'
                            % (errL2_dc_sum/display_iters, netG_dc.lambda_tv, netG_dc.rho_penalty))

                    print('Average PSNR in Training dataset is %.2f' 
                    % (np.mean(np.asarray(metrices_train.PSNRs[-1-display_iters*batch_size:]))))
                    if epoch > 1:
                        print('Average PSNR in Validation dataset is %.2f' 
                        % (np.mean(np.asarray(metrices_val.PSNRs))))
                    
                    print(' ')

                    errL2_dc_sum = 0

                # # check target and target_gen
                # save_mat(rootName+'/results/targets.mat', 'targets', targets.numpy())
                # save_mat(rootName+'/results/targets_gen.mat', 'targets_gen', targets_gen.numpy())
                
                kdatas = kdatas.to(device)
                targets = targets_gen.to(device)
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
                for idx, (kdatas, targets, targets_gen, csms, brain_masks) in enumerate(valLoader):

                    if torch.sum(brain_masks) == 0:
                        continue

                    kdatas = kdatas.to(device)
                    targets = targets_gen.to(device)
                    csms = csms.to(device)
                    brain_masks = brain_masks.to(device)

                    Xs = netG_dc(kdatas, csms, masks, flip)

                    metrices_val.get_metrices(Xs[-1]*brain_masks, targets*brain_masks)
                    targets = np.asarray(targets.cpu().detach())
                    brain_masks = np.asarray(brain_masks.cpu().detach())
                    temp = 0
                    # for i in range(len(Xs)):
                    #     X = np.asarray(Xs[i].cpu().detach())
                    #     temp += abs(X - targets) * brain_masks
                    X = np.asarray(Xs[-1].cpu().detach())
                    temp += abs(X - targets) * brain_masks
                    lossl2_sum = np.mean(temp)
                    loss_total_list.append(lossl2_sum)

                print('\n Validation loss: %f \n' 
                    % (sum(loss_total_list) / float(len(loss_total_list))))
                Validation_loss.append(sum(loss_total_list) / float(len(loss_total_list)))

            # save weights
            if Validation_loss[-1] == min(Validation_loss):
                torch.save(netG_dc.state_dict(), rootName+'/weights/echo_cat={}_solver={}_K={}_loupe={}_ratio={}_{}{}.pt' \
                .format(opt['echo_cat'], opt['solver'], opt['K'], opt['loupe'], opt['samplingRatio'], norm_last, flag_temporal_conv))
            torch.save(netG_dc.state_dict(), rootName+'/weights/echo_cat={}_solver={}_K={}_loupe={}_ratio={}_{}{}_last.pt' \
            .format(opt['echo_cat'], opt['solver'], opt['K'], opt['loupe'], opt['samplingRatio'], norm_last, flag_temporal_conv))

    
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
                flag_loupe=opt['loupe'],
                samplingRatio=opt['samplingRatio'],
                norm_last=norm_last,
                flag_temporal_conv=flag_temporal_conv
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
                flag_loupe=opt['loupe'],
                samplingRatio=opt['samplingRatio']
            )
        if opt['solver'] < 2:
            weights_dict = torch.load(rootName+'/weights/echo_cat={}_solver={}_K={}_loupe={}_ratio={}_{}{}.pt' \
            .format(opt['echo_cat'], opt['solver'], opt['K'], opt['loupe'], opt['samplingRatio'], norm_last, flag_temporal_conv))
            netG_dc.load_state_dict(weights_dict)
        netG_dc.to(device)
        netG_dc.eval()

        Inputs = []
        Targets = []
        R2s = []
        water = []
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
            for idx, (kdatas, targets, targets_gen, csms, brain_masks) in enumerate(testLoader):
                if idx == 1 and opt['loupe'] > 0:
                    Mask = netG_dc.Mask.cpu().detach().numpy()
                    print('Saving sampling mask: %', np.mean(Mask)*100)
                    save_mat(rootName+'/results/Mask_echo_cat={}_solver={}_K={}_loupe={}_ratio={}.mat' \
                            .format(opt['echo_cat'], opt['solver'], opt['K'], opt['loupe'], opt['samplingRatio']), 'Mask', Mask)
                if idx == 1:
                    print('Sampling ratio: {}%'.format(torch.mean(netG_dc.Mask)*100))
                if idx % 10 == 0:
                    print('Finish slice #', idx)
                kdatas = kdatas.to(device)
                targets = targets.to(device)
                csms = csms.to(device)
                brain_masks = brain_masks.to(device)

                # inputs = backward_multiEcho(kdatas, csms, masks, flip,
                                            # opt['echo_cat'])
                Xs_1 = netG_dc(kdatas, csms, masks, flip)[-1]
                precond = netG_dc.precond
                if opt['echo_cat']:
                    targets = torch_channel_deconcate(targets)
                    # inputs = torch_channel_deconcate(inputs)
                    Xs_1 = torch_channel_deconcate(Xs_1)
                    # y = fit_R2_LM(targets)

                # Inputs.append(inputs.cpu().detach())
                Targets.append(targets.cpu().detach())
                Recons.append(Xs_1.cpu().detach())
                # R2s.append(y[:, 0, ...].cpu().detach())
                # water.append(y[:, 2, ...].cpu().detach())
                # preconds.append(precond.cpu().detach())

            # write into .mat file
            Recons_ = np.squeeze(r2c(np.concatenate(Recons, axis=0), opt['echo_cat']))
            Recons_ = np.transpose(Recons_, [0, 2, 3, 1])
            if opt['loupe'] == -1:
                save_mat(rootName+'/results/iField_{}m.mat'.format(opt['samplingRatio']), 'Recons', Recons_)
            elif opt['loupe'] == 0:
                save_mat(rootName+'/results/iField_{}.mat'.format(opt['samplingRatio']), 'Recons', Recons_)

            # # write R2s into .mat file
            # R2s = np.concatenate(R2s, axis=0)
            # save_mat(rootName+'/results/R2s.mat', 'R2s', R2s)

            # # write water into .mat file
            # water = np.concatenate(water, axis=0)
            # save_mat(rootName+'/results/water.mat', 'water', water)

            # write into .bin file
            # (200, 2, 10, 206, 80) to (80, 206, 200, 10, 2)
            print('iField size is: ', np.concatenate(Recons, axis=0).shape)
            iField = np.transpose(np.concatenate(Recons, axis=0), [4, 3, 0, 2, 1])
            iField[:, :, 1::2, :, :] = - iField[:, :, 1::2, :, :]
            iField[..., 1] = - iField[..., 1]
            if os.path.exists(rootName+'/results_QSM/iField.bin'):
                os.remove(rootName+'/results_QSM/iField.bin')
            iField.tofile(rootName+'/results_QSM/iField.bin')
            print('Successfully save iField.bin')

            # run MEDIN
            os.system('medi ' + rootName + '/results_QSM/iField.bin' 
                    + ' --parameter ' + rootName + '/results_QSM/parameter.txt'
                    + ' --temp ' + rootName +  '/results_QSM/'
                    + ' --GPU ' + ' --device ' + opt['gpu_id'] 
                    + ' --CSF ' + ' -of QR')
            
            # read .bin files and save into .mat files
            QSM = np.fromfile(rootName+'/results_QSM/recon_QSM_10.bin', 'f4')
            QSM = np.transpose(QSM.reshape([80, 206, 200]), [2, 1, 0])

            iMag = np.fromfile(rootName+'/results_QSM/iMag.bin', 'f4')
            iMag = np.transpose(iMag.reshape([80, 206, 200]), [2, 1, 0])

            RDF = np.fromfile(rootName+'/results_QSM/RDF.bin', 'f4')
            RDF = np.transpose(RDF.reshape([80, 206, 200]), [2, 1, 0])

            R2star = np.fromfile(rootName+'/results_QSM/R2star.bin', 'f4')
            R2star = np.transpose(R2star.reshape([80, 206, 200]), [2, 1, 0])

            Mask = np.fromfile(rootName+'/results_QSM/Mask.bin', 'f4')
            Mask = np.transpose(Mask.reshape([80, 206, 200]), [2, 1, 0]) > 0

            adict = {}
            adict['QSM'], adict['iMag'], adict['RDF'] = QSM, iMag, RDF
            adict['R2star'], adict['Mask'] = R2star, Mask
            if opt['loupe'] == -1:
                sio.savemat(rootName+'/results/QSM_{}m.mat'.format(opt['samplingRatio']), adict)
            else:
                sio.savemat(rootName+'/results/QSM_{}_temporal.mat'.format(opt['samplingRatio']), adict)
            
            
            
            # # write into .mat file
            # Inputs = r2c(np.concatenate(Inputs, axis=0), opt['echo_cat'])
            # Inputs = np.transpose(Inputs, [0, 2, 3, 1])
            # Targets = r2c(np.concatenate(Targets, axis=0), opt['echo_cat'])
            # Targets = np.transpose(Targets, [0, 2, 3, 1])
            # Recons = r2c(np.concatenate(Recons, axis=0), opt['echo_cat'])
            # Recons = np.transpose(Recons, [0, 2, 3, 1])

            # save_mat(rootName+'/results/Inputs.mat', 'Inputs', Inputs)
            # save_mat(rootName+'/results/Targets.mat', 'Targets', Targets)
            # save_mat(rootName+'/results/Recons_echo_cat={}_solver={}_K={}_loupe={}.mat' \
            #   .format(opt['echo_cat'], opt['solver'], opt['K'], opt['loupe']), 'Recons', Recons)


