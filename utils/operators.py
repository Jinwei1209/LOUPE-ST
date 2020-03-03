import torch
import torch.nn as nn
import numpy as np
from utils.data import *


'''
    forward and backward imaging model operator
'''
class Back_forward():

    def __init__(
        self,
        csm,
        mask,
        lambda_dll2,
    ):
        self.ncoil = csm.shape[1]
        self.nrow = csm.shape[2] 
        self.ncol = csm.shape[3] 
        self.csm = csm
        self.mask = mask
        self.lambda_dll2 = lambda_dll2

        device = self.csm.get_device()
        self.flip = torch.ones([self.nrow, self.ncol, 1]) 
        self.flip = torch.cat((self.flip, torch.zeros(self.flip.shape)), -1).to(device)
        self.flip[::2, ...] = - self.flip[::2, ...] 
        self.flip[:, ::2, ...] = - self.flip[:, ::2, ...]

    def AtA(
        self, 
        img, 
        use_dll2=1  # 1 for l2-x0 reg, 2 for l2-TV reg, 3 for l1-TV reg
    ):
        # forward
        img_new = img.permute(0, 2, 3, 1)
        img_new = img_new[:, None, ...]  # multiply order matters (in torch implementation)
        # coilImages = cplx_mlpy(self.csm, img_new)
        # coilImages = cplx_mlpy(coilImages, self.flip) # for GE kdata
        # coilImages = fft_shift_row(coilImages, self.nrow) # for GE kdata
        # kspace = torch.fft(coilImages, 2)  
        # temp = cplx_mlpy(kspace, self.mask)  
        temp = cplx_mlpy(torch.fft(fft_shift_row(cplx_mlpy(cplx_mlpy(self.csm, img_new), self.flip), self.nrow), 2), self.mask)
        # inverse
        coilImgs = torch.ifft(temp, 2)
        coilImgs = fft_shift_row(coilImgs, self.nrow) # for GE kdata
        coilImgs = cplx_mlpy(coilImgs, self.flip) # for GE kdata
        coilComb = torch.sum(
            cplx_mlpy(coilImgs, cplx_conj(self.csm)),
            dim=1,
            keepdim=False
        )
        coilComb = coilComb.permute(0, 3, 1, 2)
        if use_dll2 == 1:
            coilComb = coilComb + self.lambda_dll2*img
        elif use_dll2 == 2:
            coilComb = coilComb + self.lambda_dll2*divergence(gradient(img))
        elif use_dll2 == 3:
            coilComb = coilComb + self.lambda_dll2*divergence(gradient(img)/torch.sqrt(gradient(img)**2+3e-5))  #1e-4 best, 5e-5 to have consistent result to ADMM
            # print(torch.mean(gradient(img)**2))
        return coilComb


def forward_operator(img, csm, mask, ncoil, nrow, ncol):
    coilImages = np.tile(img, [ncoil, 1, 1]) * csm
    kspace = np.fft.fft2(coilImages) / np.sqrt(nrow*ncol)
    res = kspace[mask!=0]
    return res


def backward_operator(kspaceUnder, csm, mask, ncoil, nrow, ncol):
    # axis 0 as the channel dim
    temp = np.zeros((ncoil, nrow, ncol), dtype=np.complex64)
    temp[mask!=0] = kspaceUnder
    img = np.fft.ifft2(temp) * np.sqrt(nrow*ncol)
    coilComb = np.sum(img*np.conj(csm), axis=0).astype(np.complex64)
    return coilComb


"""
    for 4d data: (batchsize, real/imag dim, row dim, col dim)
"""
def gradient(x):
    dx = torch.cat((x[:, :, :, 1:], x[:, :, :, -1:]), dim=3) - x
    dy = torch.cat((x[:, :, 1:, :], x[:, :, -1:, :]), dim=2) - x
    return torch.cat((dx[..., None], dy[..., None]), dim=-1)


"""
    for 5d data: (batchsize, real/imag dim, row dim, col dim, gradient dim)
"""
def divergence(d):
    device = d.get_device()
    dx = d[..., 0]
    dy = d[..., 1]
    zerox = torch.zeros(dx.size()[:3] + (1,)).to(device)
    zeroy = torch.zeros(dy.size()[:2] + (1,) + dy.size()[-1:]).to(device)
    dxx = torch.cat((dx[:, :, :, :-1], zerox), dim=3) - torch.cat((zerox, dx[:, :, :, :-1]), dim=3) 
    dyy = torch.cat((dy[:, :, :-1, :], zeroy), dim=2) - torch.cat((zeroy, dy[:, :, :-1, :]), dim=2)
    return  - dxx - dyy

"""
    backward operator for CardiacQSM rawdata recon
"""
def backward_CardiacQSM(kdata, csm, mask, flip):
    nrows = kdata.size()[2]
    ncols = kdata.size()[3]
    temp = cplx_mlpy(kdata, mask)
    temp = torch.ifft(temp, 2)
    temp = fft_shift_row(temp, nrows)
    temp = fft_shift_col(temp, ncols)
    coilComb = torch.sum(
        cplx_mlpy(temp, cplx_conj(csm)),
        dim=1,
        keepdim=False
    )
    coilComb = cplx_mlpy(coilComb, flip)
    coilComb = coilComb.permute(0, 3, 1, 2)
    return coilComb

"""
    forward operator for CardiacQSM rawdata recon
"""
def forward_CardiacQSM(image, csm, mask, flip):
    image = image.permute(0, 2, 3, 1)
    nrows = csm.size()[2]
    ncols = csm.size()[3]
    temp = cplx_mlpy(image, flip)
    temp = temp[:, None, ...]
    temp = cplx_mlpy(csm, temp)
    temp = fft_shift_row(temp, nrows)
    temp = fft_shift_col(temp, ncols)
    temp = torch.fft(temp, 2)
    return cplx_mlpy(temp, mask)

"""
    AtA operator for CardiacQSM data
"""
class backward_forward_CardiacQSM():
    
    def __init__(
        self,
        csm,
        mask,
        lambda_dll2,
    ):
        self.ncoil = csm.shape[1]
        self.nrow = csm.shape[2] 
        self.ncol = csm.shape[3] 
        self.csm = csm
        self.mask = mask
        self.lambda_dll2 = lambda_dll2

        device = self.csm.get_device()
        self.flip = torch.ones([self.nrow, self.ncol, 1]) 
        self.flip = torch.cat((self.flip, torch.zeros(self.flip.shape)), -1).to(device)
        self.flip[::2, ...] = - self.flip[::2, ...] 
        self.flip[:, ::2, ...] = - self.flip[:, ::2, ...]

    def AtA(
        self, 
        img, 
        use_dll2=1
    ):
        # forward
        image = img.permute(0, 2, 3, 1)
        temp = cplx_mlpy(image, self.flip)
        temp = temp[:, None, ...]
        temp = cplx_mlpy(self.csm, temp)
        temp = fft_shift_row(temp, self.nrow)
        temp = fft_shift_col(temp, self.ncol)
        temp = torch.fft(temp, 2)
        temp = cplx_mlpy(temp, self.mask)
        # inverse
        temp = torch.ifft(temp, 2)
        temp = fft_shift_row(temp, self.nrow)
        temp = fft_shift_col(temp, self.ncol)
        coilComb = torch.sum(
            cplx_mlpy(temp, cplx_conj(self.csm)),
            dim=1,
            keepdim=False
        )
        coilComb = cplx_mlpy(coilComb, self.flip)
        coilComb = coilComb.permute(0, 3, 1, 2)
        if use_dll2 == 1:
            coilComb = coilComb + self.lambda_dll2*img
        return coilComb








