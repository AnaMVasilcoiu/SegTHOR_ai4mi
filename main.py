#!/usr/bin/env python3

# MIT License

# Copyright (c) 2024 Hoel Kervadec

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import argparse
import warnings
from datetime import datetime
from operator import itemgetter
from pathlib import Path
from plot import run as plot
from pprint import pprint
from shutil import copytree, rmtree
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn, Tensor
from torch.utils.data import DataLoader, WeightedRandomSampler
from torchvision import transforms
from torchvision.transforms import InterpolationMode
from torch.optim.lr_scheduler import ExponentialLR, StepLR 

from dataset import SliceDataset, SliceDatasetWithTransforms
from DeepLabV3 import DeepLabV3
from ENet import ENet
from ShallowNet import shallowCNN
from losses import (CrossEntropy, JaccardLoss, DiceLoss, LovaszSoftmaxLoss, CustomLoss, FocalLoss)
from ENet_less_layers import less_ENet
from ENet_more_layers import more_ENet
from ENet_kernelsize import kernel_ENet
from utils import (
    Dcm,
    class2one_hot,
    dice_coef,
    probs2class,
    probs2one_hot,
    save_images,
    tqdm_
)
from torch.utils.data import WeightedRandomSampler

from utils import (Dcm,
                   class2one_hot,
                   probs2one_hot,
                   probs2class,
                   tqdm_,
                   dice_coef,
                   save_images,
                   iou_coef,
                   assd_coef,
                   vol_sim_coef,
                   hausdorff_coef)


def compute_class_weights(train_set, K):
    """
    Compute class weights for the training dataset.
    This ensures that underrepresented classes (like classes 1 and 4) are sampled more frequently.
    """
    class_counts = np.zeros(K)

    # Loop through the dataset and count the number of pixels for each class
    for _, data in enumerate(train_set):
        gt = data['gts']  # Ground truth mask
        for k in range(K):
            class_counts[k] += (gt == k).sum().item()

    # Compute class weights (inverse of class frequencies)
    class_weights = 1.0 / np.maximum(class_counts, 1)  # Avoid division by zero

    return class_weights

datasets_params: dict[str, dict[str, Any]] = {}
# K for the number of classes
# Avoids the clases with C (often used for the number of Channel)
datasets_params["TOY2"] = {'K': 2, 'net': shallowCNN, 'B': 2}
datasets_params["SEGTHORCORRECT"] = {'K': 5, 'net': ENet, 'B': 8}


def setup(args) -> tuple[nn.Module, Any, Any, DataLoader, DataLoader, int]:
    # Networks and scheduler
    gpu: bool = args.gpu and torch.cuda.is_available()
    device = torch.device("cuda") if gpu else torch.device("cpu")
    print(f">> Picked {device} to run experiments")

    K: int = datasets_params[args.dataset]['K']
    if args.architecture == "normal":
        net = kernel_ENet(1, K, kernels = args.channels, kernelsize = args.kernelsize)
    elif args.architecture == "more":
        net = more_ENet(1, K, kernels = args.channels, kernelsize = args.kernelsize)
    elif args.architecture == "less":
        net = less_ENet(1, K, kernels = args.channels, kernelsize = args.kernelsize)

    if args.deeplabv3:
        net = DeepLabV3(K, pretrained=args.pretrained)
        net.to(device)
    else:
        net = datasets_params[args.dataset]['net'](1, K)
        net.init_weights()
        net.to(device)

    lr = args.lr
    if args.optimizer == 'sgd':
        optimizer = torch.optim.SGD(net.parameters(), lr=lr, momentum=0.9)
    elif args.optimizer == 'adamw':
        optimizer = torch.optim.AdamW(net.parameters(), lr=lr, betas=(0.9, 0.999))
    else:
        optimizer = torch.optim.Adam(net.parameters(), lr=lr, betas=(0.9, 0.999))

    
    if args.scheduler == "exp": 
        scheduler = ExponentialLR(optimizer, gamma=0.95)  

    elif args.scheduler == "steps": 
        scheduler = StepLR(optimizer, step_size=5, gamma=0.5)  


    # Dataset part
    B: int = datasets_params[args.dataset]['B']
    root_dir = Path("data") / args.dataset

    # Define the target size of the images (to fix after transformations)
    target_size = (256, 256)

    img_transform = transforms.Compose([
        transforms.Resize(target_size),  # Resize the image to the target size
        lambda img: img.convert('L'), # Convert to grayscale
        lambda img: np.array(img)[np.newaxis, ...], # Add one dimension to simulate batch
        lambda nd: nd / 255,  # max <= 1 # Normalize the image
        lambda nd: torch.tensor(nd, dtype=torch.float32) # Convert to tensor
    ])

    gt_transform = transforms.Compose([
        transforms.Resize(target_size, interpolation=InterpolationMode.NEAREST),  # Resize ground truth with NEAREST interpolation,
        lambda img: np.array(img)[...], # Convert to numpy array
        # The idea is that the classes are mapped to {0, 255} for binary cases
        # {0, 85, 170, 255} for 4 classes
        # {0, 51, 102, 153, 204, 255} for 6 classes
        # Very sketchy but that works here and that simplifies visualization
        lambda nd: nd / (255 / (K - 1)) if K != 5 else nd / 63,  # max <= 1 # Normalize the image
        lambda nd: torch.tensor(nd, dtype=torch.int64)[None, ...],  # Add one dimension to simulate batch
        lambda t: class2one_hot(t, K=K), # Convert to one-hot encoding
        itemgetter(0)
    ])

    if args.transformation == 'none':
        train_set = SliceDataset('train',
                             root_dir,
                             img_transform=img_transform,
                             gt_transform=gt_transform,
                             debug=args.debug,
                             remove_background=args.remove_background)
    else:
        # Define image and ground truth directories (original + augmented)
        if args.transformation == 'preprocessed':
            train_img_dirs = [root_dir / 'train' / 'img_preprocessed']
            train_gt_dirs = [root_dir / 'train' / 'gt_preprocessed']
        elif args.transformation == 'augmented':
            train_img_dirs = [root_dir / 'train' / 'img_spatial_aug', root_dir / 'train' / 'img_intensity_aug']
            train_gt_dirs = [root_dir / 'train' / 'gt_spatial_aug', root_dir / 'train' / 'gt_intensity_aug']
        elif args.transformation == 'preprocess_augment':
            train_img_dirs = [root_dir / 'train' / 'img_pre_spatial_aug', root_dir / 'train' / 'img_pre_intensity_aug']
            train_gt_dirs = [root_dir / 'train' / 'gt_pre_spatial_aug', root_dir / 'train' / 'gt_pre_intensity_aug']

        # Create the SliceDataset for training
        train_set = SliceDatasetWithTransforms(
            subset='train',
            img_dirs=train_img_dirs,
            gt_dirs=train_gt_dirs,
            img_transform=img_transform,
            gt_transform=gt_transform,
            debug=False,
            remove_background=args.remove_background
        )

    if args.class_aware_sampling:
        # Compute class weights for class-aware sampling
        class_weights = compute_class_weights(train_set, K)

        # Create sample weights for each image in the dataset based on the presence of each class
        sample_weights = []
        for data in train_set:
            gt = data['gts']
            sample_weight = 0
            for k in range(K):
                if torch.any(gt == k):
                    sample_weight += class_weights[k]
            sample_weights.append(sample_weight)

        sampler = WeightedRandomSampler(weights=sample_weights, num_samples=len(train_set), replacement=True)

        # Create DataLoader with the class-aware sampler
        train_loader = DataLoader(
            dataset=train_set,
            batch_size=B,
            num_workers=args.num_workers,
            sampler=sampler,
        )
    else:
        train_loader = DataLoader(train_set,
                                batch_size=B,
                                num_workers=args.num_workers,
                                shuffle=True)

    val_set = SliceDataset('val',
                           root_dir,
                           img_transform=img_transform,
                           gt_transform=gt_transform,
                           debug=args.debug)
    val_loader = DataLoader(val_set,
                            batch_size=B,
                            num_workers=args.num_workers,
                            shuffle=False)

    args.dest.mkdir(parents=True, exist_ok=True)

    if args.scheduler == "None":
        return (net, optimizer, device, train_loader, val_loader, K)
    else: 
        return (net, optimizer, device, train_loader, val_loader, K, scheduler) #ME -> added scheduler



def runTraining(args):
    print(f">>> Setting up to train on {args.dataset} with {args.mode}")
    if args.scheduler == "None": #ME
        net, optimizer, device, train_loader, val_loader, K = setup(args) #ME
    else:   #ME
        net, optimizer, device, train_loader, val_loader, K, scheduler =setup(args) #ME -> added scheduler


    if args.mode == "full":
        idk = list(range(K))
    elif args.mode in ["partial"] and args.dataset in ['SEGTHOR', 'SEGTHOR_STUDENTS']:
        idk = [0, 1, 3, 4]
    else:
        raise ValueError(args.mode, args.dataset)

    if args.loss == "ce":
        loss_fn = CrossEntropy(idk=idk)
    elif args.loss == "jaccard":
        loss_fn = JaccardLoss(idk=idk)
    elif args.loss == "dice":
        loss_fn = DiceLoss(idk=idk)
    elif args.loss == "lovasz":
        loss_fn = LovaszSoftmaxLoss(idk=idk)
    elif args.loss == "custom":
        loss_fn = CustomLoss(idk=idk)
    elif args.loss == "focal":
        loss_fn = FocalLoss(idk=idk, gamma=args.focal_loss_gamma, focal_loss_weights=args.focal_loss_weights)
    else:
        raise ValueError(args.loss)

    # Notice one has the length of the _loader_, and the other one of the _dataset_
    log_loss_tra: Tensor = torch.zeros((args.epochs, len(train_loader)))
    log_dice_tra: Tensor = torch.zeros((args.epochs, len(train_loader.dataset), K))
    log_loss_val: Tensor = torch.zeros((args.epochs, len(val_loader)))
    log_dice_val: Tensor = torch.zeros((args.epochs, len(val_loader.dataset), K))

    # Additional metrics
    log_iou_tra = torch.zeros((args.epochs, len(train_loader.dataset), K))
    log_hausdorff_tra = torch.zeros((args.epochs, len(train_loader.dataset), K))
    log_assd_tra = torch.zeros((args.epochs, len(train_loader.dataset), K))
    log_volsim_tra = torch.zeros((args.epochs, len(train_loader.dataset), K))
    log_iou_val = torch.zeros((args.epochs, len(val_loader.dataset), K))
    log_hausdorff_val = torch.zeros((args.epochs, len(val_loader.dataset), K))
    log_assd_val = torch.zeros((args.epochs, len(val_loader.dataset), K))
    log_volsim_val = torch.zeros((args.epochs, len(val_loader.dataset), K))

    best_dice: float = 0

    for e in range(args.epochs):
        for m in ['train', 'val']:
            if m == 'train':
                net.train()
                opt = optimizer
                cm = Dcm
                desc = f">> Training   ({e: 4d})"
                loader = train_loader
                log_loss = log_loss_tra
                log_dice = log_dice_tra
                log_iou = log_iou_tra
                log_hausdorff = log_hausdorff_tra
                log_assd = log_assd_tra
                log_volsim = log_volsim_tra
            else:
                net.eval()
                opt = None
                cm = torch.no_grad
                desc = f">> Validation ({e: 4d})"
                loader = val_loader
                log_loss = log_loss_val
                log_dice = log_dice_val
                log_iou = log_iou_val
                log_hausdorff = log_hausdorff_val
                log_assd = log_assd_val
                log_volsim = log_volsim_val

            with cm():  # Either dummy context manager, or the torch.no_grad for validation
                j = 0
                tq_iter = tqdm_(enumerate(loader), total=len(loader), desc=desc)
                for i, data in tq_iter:
                    img = data['images'].to(device)
                    gt = data['gts'].to(device)

                    if opt:  # So only for training
                        opt.zero_grad()

                    # Sanity tests to see we loaded and encoded the data correctly
                    assert 0 <= img.min() and img.max() <= 1
                    B, _, W, H = img.shape

                    pred_logits = net(img)
                    pred_probs = F.softmax(1 * pred_logits, dim=1)  # 1 is the temperature parameter

                    # Metrics computation, not used for training
                    pred_seg = probs2one_hot(pred_probs)
                    log_dice[e, j:j + B, :] = dice_coef(pred_seg, gt)  # One DSC value per sample and per class

                    # IoU (Jaccard Index)
                    log_iou[e, j:j + B, :] = iou_coef(pred_seg, gt)

                    # Hausdorff Distance
                    log_hausdorff[e, j:j + B, :] = hausdorff_coef(pred_seg, gt).transpose(0, 1)

                    # ASSD (Average Symmetric Surface Distance)
                    log_assd[e, j:j + B, :] = assd_coef(pred_seg, gt).transpose(0, 1)

                    # Volumetric Similarity
                    log_volsim[e, j:j + B, :] = vol_sim_coef(pred_seg, gt)

                    loss = loss_fn(pred_probs, gt)
                    log_loss[e, i] = loss.item()  # One loss value per batch (averaged in the loss)

                    if opt:  # Only for training
                        loss.backward()
                        opt.step()

                    if m == 'val':
                        with warnings.catch_warnings():
                            warnings.filterwarnings('ignore', category=UserWarning)
                            predicted_class: Tensor = probs2class(pred_probs)
                            mult: int = 63 if K == 5 else (255 / (K - 1))
                            if not args.dont_save_predictions and log_dice[e, :, 1:].mean().item() > best_dice:
                                save_images(predicted_class * mult,
                                            data['stems'],
                                            args.dest / f"iter{e:03d}" / m)

                    j += B  # Keep in mind that _in theory_, each batch might have a different size
                    # For the DSC average: do not take the background class (0) into account:
                    postfix_dict: dict[str, str] = {"Dice": f"{log_dice[e, :j, 1:].mean():05.3f}",
                                                    "IoU": f"{log_iou[e, :j, 1:].mean():05.3f}",
                                                    "Hausdorff": f"{log_hausdorff[e, :j, 1:].mean():05.3f}",
                                                    "ASSD": f"{log_assd[e, :j, 1:].mean():05.3f}",
                                                    "VolSim": f"{log_volsim[e, :j, 1:].mean():05.3f}",
                                                    "Loss": f"{log_loss[e, :i + 1].mean():5.2e}"}
                    if K > 2:
                        postfix_dict |= {f"Dice-{k}": f"{log_dice[e, :j, k].mean():05.3f}"
                                         for k in range(1, K)}
                    tq_iter.set_postfix(postfix_dict)

        
        if args.scheduler != "None":
            scheduler.step()

        # I save it at each epochs, in case the code crashes or I decide to stop it early
        np.save(args.dest / "loss_tra.npy", log_loss_tra)
        np.save(args.dest / "dice_tra.npy", log_dice_tra)
        np.save(args.dest / "loss_val.npy", log_loss_val)
        np.save(args.dest / "dice_val.npy", log_dice_val)

        np.save(args.dest / "iou_tra.npy", log_iou_tra)
        np.save(args.dest / "hausdorff_tra.npy", log_hausdorff_tra)
        np.save(args.dest / "assd_tra.npy", log_assd_tra)
        np.save(args.dest / "volsim_tra.npy", log_volsim_tra)
        np.save(args.dest / "iou_val.npy", log_iou_val)
        np.save(args.dest / "hausdorff_val.npy", log_hausdorff_val)
        np.save(args.dest / "assd_val.npy", log_assd_val)
        np.save(args.dest / "volsim_val.npy", log_volsim_val)

        current_dice: float = log_dice_val[e, :, 1:].mean().item()
        if current_dice > best_dice:
            print(f">>> Improved dice at epoch {e}: {best_dice:05.3f}->{current_dice:05.3f} DSC")
            best_dice = current_dice
            with open(args.dest / "best_epoch.txt", 'w') as f:
                    f.write(str(e))

            if not args.dont_save_predictions:
                best_folder = args.dest / "best_epoch"
                if best_folder.exists():
                        rmtree(best_folder)
                copytree(args.dest / f"iter{e:03d}", Path(best_folder))

            torch.save(net, args.dest / "bestmodel.pkl")
            torch.save(net.state_dict(), args.dest / "bestweights.pt")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument('--epochs', default=25, type=int)
    parser.add_argument('--dataset', default='TOY2', choices=datasets_params.keys())
    parser.add_argument('--mode', default='full', choices=['partial', 'full'])
    parser.add_argument('--dest', type=Path, required=True,
                        help="Destination directory to save the results (predictions and weights).")

    parser.add_argument('--num_workers', type=int, default=5)
    parser.add_argument('--gpu', action='store_true')
    parser.add_argument('--debug', action='store_true',
                        help="Keep only a fraction (10 samples) of the datasets, "
                             "to test the logic around epochs and logging easily.")
    parser.add_argument('--deeplabv3', action='store_true', help="Use DeepLabV3 instead of the default model")
    parser.add_argument('--pretrained', action='store_true', help="Use a pretrained deeplabv3 model")
    parser.add_argument('--lr', type=float, default=0.0005)
    parser.add_argument('--optimizer', default='adam', choices=['adam', 'sgd', 'adamw'])
    parser.add_argument('--remove_background', action='store_true', default=False,
                        help="If set, remove slices that contain only background.")
    parser.add_argument('--transformation', default='none', choices=['none', 'preprocessed', 'augmented', 'preprocess_augment'])

    parser.add_argument('--class_aware_sampling', action='store_true', default=False,
                        help="If set, samples batches so that every batch has a balanced representation of all classes.")
    parser.add_argument('--plot_results', action='store_true', default=False)
    parser.add_argument('--dont_save_predictions', action='store_true', default=False)
    parser.add_argument('--focal_loss_gamma', type=float, default=2.0)
    parser.add_argument('--focal_loss_weights', type=float, nargs=5, default=[1.0, 1.0, 1.0, 1.0, 1.0],
                        help="Weights for the classes in the following order background, esophagus, heart, trachea, aorta")
    parser.add_argument("--loss", type=str, choices=["ce", "jaccard", "dice", "lovasz", "custom", "focal"],default='ce',
                        help="Loss function to be used.")
    
    parser.add_argument('--architecture', default='normal', choices=['normal', 'more', 'less'])
    parser.add_argument('--scheduler', default='None', choices=['None', 'exp', 'steps'])
    parser.add_argument('--kernelsize', default=3, type=int)
    parser.add_argument('--channels', default=16, type=int)

    args = parser.parse_args()

    args.dest = Path(args.dest) / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    pprint(args)

    runTraining(args)
    
    if args.plot_results:
        plot_args = argparse.Namespace(metric_file=args.dest / "dice_val.npy", dest=args.dest / "dice_val.png", headless=True)
        plot(plot_args)


if __name__ == '__main__':
    main()
