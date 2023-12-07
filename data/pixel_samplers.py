# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
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

"""
Code for sampling pixels.
"""

import random
import cv2
import numpy as np
import math
from typing import Dict, Optional, Union

import torch
from jaxtyping import Int
from torch import Tensor


class PixelSampler:
    """Samples 'pixel_batch's from 'image_batch's.

    Args:
        num_rays_per_batch: number of rays to sample per batch
        keep_full_image: whether or not to include a reference to the full image in returned batch
    """

    def __init__(self, num_rays_per_batch: int, keep_full_image: bool = True, **kwargs) -> None:
        self.kwargs = kwargs
        self.num_rays_per_batch = num_rays_per_batch
        self.keep_full_image = keep_full_image

    def set_num_rays_per_batch(self, num_rays_per_batch: int):
        """Set the number of rays to sample per batch.

        Args:
            num_rays_per_batch: number of rays to sample per batch
        """
        self.num_rays_per_batch = num_rays_per_batch

    def sample_method(
        self,
        batch_size: int,
        num_images: int,
        image_height: int,
        image_width: int,
        mask: Optional[Tensor] = None,
        device: Union[torch.device, str] = "cpu",
        all_pixels: bool = False,
    ) -> Int[Tensor, "batch_size 3"]:
        """
        Naive pixel sampler, uniformly samples across all possible pixels of all possible images.

        Args:
            batch_size: number of samples in a batch
            num_images: number of images to sample over
            mask: mask of possible pixels in an image to sample from.
        """
        if isinstance(mask, torch.Tensor):
            nonzero_indices = torch.nonzero(mask[..., 0], as_tuple=False)
            chosen_indices = random.sample(range(len(nonzero_indices)), k=batch_size)
            indices = nonzero_indices[chosen_indices].to(device)
        elif all_pixels:
            interleave_num = batch_size // num_images
            num_imgs = torch.arange(0,num_images,device=device).repeat_interleave(interleave_num).unsqueeze(-1)
            interleave_num = batch_size // (image_height * num_images)
            num_height = torch.arange(0,image_height,device=device).repeat_interleave(interleave_num) #.unsqueeze(-1)
            num_height = num_height.repeat(num_images).unsqueeze(-1)
            interleave_num = batch_size // image_width
            num_width = torch.arange(0,image_width,device=device).repeat(interleave_num).unsqueeze(-1)
            indices = torch.cat([num_imgs,num_height,num_width],dim=1).long()
        else:
            indices = torch.floor(
                torch.rand((batch_size, 3), device=device)
                * torch.tensor([num_images, image_height, image_width], device=device)
            ).long()

        return indices

    def collate_image_dataset_batch(self, batch: Dict, num_rays_per_batch: int, keep_full_image: bool = False):
        """
        Operates on a batch of images and samples pixels to use for generating rays.
        Returns a collated batch which is input to the Graph.
        It will sample only within the valid 'mask' if it's specified.

        Args:
            batch: batch of images to sample from
            num_rays_per_batch: number of rays to sample per batch
            keep_full_image: whether or not to include a reference to the full image in returned batch
        """

        device = batch["image"].device
        #num_images, image_height, image_width, _ = batch["image"].shape

        indices_lst = []
        #divider = 8

        num_images, image_height, image_width, _ = batch["image"].shape            
        #image_height = image_height // divider
        #image_width = image_width // divider

        num_rays_per_batch = image_height * image_width

        if "mask" in batch:
            indices = self.sample_method(
                num_rays_per_batch, num_images, image_height, image_width, mask=batch["mask"], device=device
            )
        elif "time_mask" in batch and False:
            dynamic_num_rays_per_batch = 1024
            static_num_rays_per_batch = num_rays_per_batch - dynamic_num_rays_per_batch
            time_mask = torch.sum(batch["time_mask"],-1) > 10
            static_indices = self.sample_method(static_num_rays_per_batch, num_images, image_height, image_width, mask=~time_mask.unsqueeze(-1),device=device)
            dynamic_indices = self.sample_method(dynamic_num_rays_per_batch, num_images, image_height, image_width, mask=time_mask.unsqueeze(-1),device=device)
            indices = torch.cat([static_indices,dynamic_indices],dim=0)
        else:
            indices,dw,dh,select,xy_dim = self.sample_method(num_rays_per_batch, num_images, image_height, image_width, device=device, all_pixels=True)

        #c, y, x = (i.flatten() for i in torch.split(indices, 1, dim=-1))
        #c, y, x = c.cpu(), y.cpu(), x.cpu()
        #print(batch.keys())
        #print(indices.shape)
        #print(batch["image_idx"].shape)
        #print(batch["image_idx"][c])
        #exit(-1)
        #indices_lst.append(indices)
        #divider *= 2
        #collated_batch = {
        #    key: value[c, y, x] for key, value in batch.items() if key != "image_idx" and key != "image" and value is not None
        #}

        collated_batch = {}
        #assert collated_batch["image"].shape[0] == num_rays_per_batch
        #assert batch["image_idx"].shape[0] == 1
        # Needed to correct the random indices to their actual camera idx locations.

        #indices[:, 0] = batch["image_idx"][c]
        
        collated_batch["indices"] = indices  # with the abs camera indices
        
        if keep_full_image:
            collated_batch["full_image"] = batch["image"][select]
            image_arr = []
            for img_idx in range(dh.shape[0]):
                image_arr.append(collated_batch["full_image"][img_idx,dh[img_idx]:dh[img_idx]+xy_dim,dw[img_idx]:dw[img_idx]+xy_dim].unsqueeze(0))
            collated_batch["full_image"] = torch.concat(image_arr,dim=0)
            #print(collated_batch["full_image"].shape)
            m = collated_batch["full_image"]
            #f = m.reshape(-1,m.shape[2],m.shape[3])
            #f = f.detach().cpu().numpy()*255
            #f = f.astype(np.uint8)

            
            
            #cv2.imwrite("test_feature_img_in.png",f)
            m = m.reshape(m.shape[0],-1,m.shape[-1])
            m = m.mean(1)
            m = m - m.mean(0).unsqueeze(0)
            m = torch.nn.functional.softmax(torch.sqrt(torch.sum(m*m,1)),0)
            #print(m.shape)
            collated_batch["patch_weights"] = m

        return collated_batch

    def collate_image_dataset_batch_list(self, batch: Dict, num_rays_per_batch: int, keep_full_image: bool = False):
        """
        Does the same as collate_image_dataset_batch, except it will operate over a list of images / masks inside
        a list.

        We will use this with the intent of DEPRECIATING it as soon as we find a viable alternative.
        The intention will be to replace this with a more efficient implementation that doesn't require a for loop, but
        since pytorch's ragged tensors are still in beta (this would allow for some vectorization), this will do.

        Args:
            batch: batch of images to sample from
            num_rays_per_batch: number of rays to sample per batch
            keep_full_image: whether or not to include a reference to the full image in returned batch
        """

        device = batch["image"][0].device
        num_images = len(batch["image"])

        # only sample within the mask, if the mask is in the batch
        all_indices = []
        all_images = []

        if "mask" in batch:
            num_rays_in_batch = num_rays_per_batch // num_images
            for i in range(num_images):
                image_height, image_width, _ = batch["image"][i].shape

                if i == num_images - 1:
                    num_rays_in_batch = num_rays_per_batch - (num_images - 1) * num_rays_in_batch

                indices = self.sample_method(
                    num_rays_in_batch, 1, image_height, image_width, mask=batch["mask"][i], device=device
                )
                indices[:, 0] = i
                all_indices.append(indices)
                all_images.append(batch["image"][i][indices[:, 1], indices[:, 2]])

        else:
            num_rays_in_batch = num_rays_per_batch // num_images
            for i in range(num_images):
                image_height, image_width, _ = batch["image"][i].shape
                if i == num_images - 1:
                    num_rays_in_batch = num_rays_per_batch - (num_images - 1) * num_rays_in_batch
                indices = self.sample_method(num_rays_in_batch, 1, image_height, image_width, device=device)
                indices[:, 0] = i
                all_indices.append(indices)
                all_images.append(batch["image"][i][indices[:, 1], indices[:, 2]])

        indices = torch.cat(all_indices, dim=0)

        c, y, x = (i.flatten() for i in torch.split(indices, 1, dim=-1))
        collated_batch = {
            key: value[c, y, x]
            for key, value in batch.items()
            if key != "image_idx" and key != "image" and key != "mask" and value is not None
        }

        collated_batch["image"] = torch.cat(all_images, dim=0)

        assert collated_batch["image"].shape[0] == num_rays_per_batch

        # Needed to correct the random indices to their actual camera idx locations.
        indices[:, 0] = batch["image_idx"][c]
        collated_batch["indices"] = indices  # with the abs camera indices

        if keep_full_image:
            collated_batch["full_image"] = batch["image"]

        return collated_batch

    def sample(self, image_batch: Dict):
        """Sample an image batch and return a pixel batch.

        Args:
            image_batch: batch of images to sample from
        """
        if isinstance(image_batch["image"], list):
            image_batch = dict(image_batch.items())  # copy the dictionary so we don't modify the original
            pixel_batch = self.collate_image_dataset_batch_list(
                image_batch, self.num_rays_per_batch, keep_full_image=self.keep_full_image
            )
        elif isinstance(image_batch["image"], torch.Tensor):
            pixel_batch = self.collate_image_dataset_batch(
                image_batch, self.num_rays_per_batch, keep_full_image=self.keep_full_image
            )
        else:
            raise ValueError("image_batch['image'] must be a list or torch.Tensor")

        return pixel_batch


class EquirectangularPixelSampler(PixelSampler):
    """Samples 'pixel_batch's from 'image_batch's. Assumes images are
    equirectangular and the sampling is done uniformly on the sphere.

    Args:
        num_rays_per_batch: number of rays to sample per batch
        keep_full_image: whether or not to include a reference to the full image in returned batch
    """

    # overrides base method
    def sample_method(
        self,
        batch_size: int,
        num_images: int,
        image_height: int,
        image_width: int,
        mask: Optional[Tensor] = None,
        device: Union[torch.device, str] = "cpu",
    ) -> Int[Tensor, "batch_size 3"]:
        if isinstance(mask, torch.Tensor):
            # Note: if there is a mask, sampling reduces back to uniform sampling, which gives more
            # sampling weight to the poles of the image than the equators.
            # TODO(kevinddchen): implement the correct mask-sampling method.

            indices = super().sample_method(batch_size, num_images, image_height, image_width, mask=mask, device=device)
        else:
            # We sample theta uniformly in [0, 2*pi]
            # We sample phi in [0, pi] according to the PDF f(phi) = sin(phi) / 2.
            # This is done by inverse transform sampling.
            # http://corysimon.github.io/articles/uniformdistn-on-sphere/
            num_images_rand = torch.rand(batch_size, device=device)
            phi_rand = torch.acos(1 - 2 * torch.rand(batch_size, device=device)) / torch.pi
            theta_rand = torch.rand(batch_size, device=device)
            indices = torch.floor(
                torch.stack((num_images_rand, phi_rand, theta_rand), dim=-1)
                * torch.tensor([num_images, image_height, image_width], device=device)
            ).long()

        return indices


class PatchPixelSampler(PixelSampler):
    """Samples 'pixel_batch's from 'image_batch's. Samples square patches
    from the images randomly. Useful for patch-based losses.

    Args:
        num_rays_per_batch: number of rays to sample per batch
        keep_full_image: whether or not to include a reference to the full image in returned batch
        patch_size: side length of patch. This must be consistent in the method
        config in order for samples to be reshaped into patches correctly.
    """

    def __init__(self, num_rays_per_batch: int, keep_full_image: bool = False, **kwargs) -> None:
        self.patch_size = kwargs["patch_size"]
        num_rays = (num_rays_per_batch // (self.patch_size**2)) * (self.patch_size**2)
        super().__init__(num_rays, keep_full_image, **kwargs)

    def set_num_rays_per_batch(self, num_rays_per_batch: int):
        """Set the number of rays to sample per batch. Overridden to deal with patch-based sampling.

        Args:
            num_rays_per_batch: number of rays to sample per batch
        """
        self.num_rays_per_batch = (num_rays_per_batch // (self.patch_size**2)) * (self.patch_size**2)

    # overrides base method
    def sample_method(
        self,
        batch_size: int,
        num_images: int,
        image_height: int,
        image_width: int,
        mask: Optional[Tensor] = None,
        device: Union[torch.device, str] = "cpu",
            all_pixels: bool = False
    ) -> Int[Tensor, "batch_size 3"]:
        if isinstance(mask, Tensor):
            # Note: if there is a mask, sampling reduces back to uniform sampling
            indices = super().sample_method(batch_size, num_images, image_height, image_width, mask=mask, device=device)
        else:
            sub_bs = batch_size // (self.patch_size**2)
            indices = torch.rand((sub_bs, 3), device=device) * torch.tensor(
                [num_images, image_height - self.patch_size, image_width - self.patch_size],
                device=device,
            )
            print(indices.shape)
            print(self.patch_size)
            print(batch_size)
            exit(-1)
            
            indices = indices.view(sub_bs, 1, 1, 3).broadcast_to(sub_bs, self.patch_size, self.patch_size, 3).clone()

            yys, xxs = torch.meshgrid(
                torch.arange(self.patch_size, device=device), torch.arange(self.patch_size, device=device)
            )
            indices[:, ..., 1] += yys
            indices[:, ..., 2] += xxs

            indices = torch.floor(indices).long()
            indices = indices.flatten(0, 2)

        return indices

class TieredFeaturePatchPixelSampler(PixelSampler):
    """Samples 'pixel_batch's from 'image_batch's. Samples square patches
    from the images randomly. Useful for patch-based losses.

    Args:
        num_rays_per_batch: number of rays to sample per batch
        keep_full_image: whether or not to include a reference to the full image in returned batch
        patch_size: side length of patch. This must be consistent in the method
        config in order for samples to be reshaped into patches correctly.
    """

    def __init__(self, num_rays_per_batch: int, keep_full_image: bool = False, **kwargs) -> None:
        self.patch_size = kwargs["patch_size"]
        self.num_tiers = kwargs["num_tiers"]
        self.feature_patch_size = kwargs["feature_patch_size"]

        self.select_idxs = [i for i in range(153)]
        random.shuffle(self.select_idxs)
        #self.num_imgs = kwargs["num_imgs"]
        num_rays = (num_rays_per_batch // (self.patch_size**2)) * (self.patch_size**2)
        self.keep_full_image = keep_full_image
        self.indices = []
        self.num_to_select = 1
        dh_lst = [[] for _ in range(self.num_to_select)]
        for idx in range(self.num_to_select):
            dh_start = random.randint(0,self.patch_size - 1)
            for dh_idx in range((720 // self.patch_size) - 1):
                dh_lst[idx].append(dh_start + dh_idx*self.patch_size)
            random.shuffle(dh_lst[idx])
        dw_lst = [[] for _ in range(self.num_to_select)]
        for idx in range(self.num_to_select):
            dw_start = random.randint(0,self.patch_size - 1)
            for dw_idx in range((1280 // self.patch_size) - 1):
                dw_lst[idx].append(dw_start + dw_idx*self.patch_size)
            random.shuffle(dw_lst[idx])
        self.dw_lst = torch.tensor(dw_lst)
        self.dh_lst = torch.tensor(dh_lst)

        num_images = 153
        curr_dim = self.patch_size# // 2
        #self.init_dim = curr_dim
        for idx in range(4):
            batch_size = (curr_dim**2)*num_images
            curr_indices = super().sample_method(batch_size, num_images, curr_dim, curr_dim, mask=None, all_pixels=True) #device="cuda:0",all_pixels=True)
            self.indices.append(curr_indices)
            curr_dim = curr_dim // 2
        #select = torch.randn(153) > 1.5
        #select = select.repeat_interleave(curr_dim**2)
        #print(indices1.shape)
        #print(indices1[select].shape)

        super().__init__(num_rays, keep_full_image, **kwargs)

    def set_num_rays_per_batch(self, num_rays_per_batch: int):
        """Set the number of rays to sample per batch. Overridden to deal with patch-based sampling.

        Args:
            num_rays_per_batch: number of rays to sample per batch
        """
        self.num_rays_per_batch = (num_rays_per_batch // (self.patch_size**2)) * (self.patch_size**2)

    # overrides base method
    def sample_method(
        self,
        batch_size: int,
        num_images: int,
        image_height: int,
        image_width: int,
        mask: Optional[Tensor] = None,
        device: Union[torch.device, str] = "cpu",
        all_pixels: bool = False,            
    ) -> Int[Tensor, "batch_size 3"]:
        if isinstance(mask, Tensor):
            # Note: if there is a mask, sampling reduces back to uniform sampling
            indices = super().sample_method(batch_size, num_images, image_height, image_width, mask=mask, device=device)
            print('MASK NOT HANDLED FOR TIERED FEATURE SAMPLER, EXITTING')
            exit(-1)
        else:
            #select = torch.randn(153) > 1.5

            if len(self.select_idxs) < self.num_to_select:
                self.select_idxs = [i for i in range(153)]
                random.shuffle(self.select_idxs)

            curr_select_idxs = self.select_idxs[:self.num_to_select]
            self.select_idxs = self.select_idxs[self.num_to_select:]
            select = torch.zeros(153,device=self.indices[0].device).to(bool)
            select[curr_select_idxs] = True
            curr_dim = self.patch_size #self.init_dim * 2
            indices = []
            #dw = random.randint(0,image_width - curr_dim - 1)
            #dh = random.randint(0,image_height - curr_dim - 1)
            #dw = (torch.rand(self.num_to_select,device=self.indices[0].device) * (image_width - curr_dim - 1)).to(int)
            #dh = (torch.rand(self.num_to_select,device=self.indices[0].device) * (image_height - curr_dim - 1)).to(int)
            if self.dw_lst.shape[1] == 0:
                dw_lst = [[] for _ in range(self.num_to_select)]
                for idx in range(self.num_to_select):
                    dw_start = random.randint(0,self.patch_size - 1)
                    for dw_idx in range((1280 // self.patch_size) - 1):
                        dw_lst[idx].append(dw_start + dw_idx*self.patch_size)
                    random.shuffle(dw_lst[idx])
                self.dw_lst = torch.tensor(dw_lst)
            if self.dh_lst.shape[1] == 0:
                dh_lst = [[] for _ in range(self.num_to_select)]
                for idx in range(self.num_to_select):
                    dh_start = random.randint(0,self.patch_size - 1)
                    for dh_idx in range((720 // self.patch_size) - 1):
                        dh_lst[idx].append(dh_start + dh_idx*self.patch_size)
                    random.shuffle(dh_lst[idx])
                self.dh_lst = torch.tensor(dh_lst)
            dw = self.dw_lst[:,0]
            dh = self.dh_lst[:,0]

            self.dw_lst = self.dw_lst[:,1:]
            self.dh_lst = self.dh_lst[:,1:]            
            #dw = dw.repeat_interleave(3).repeat(2)
            dw_og = dw #list(dw.numpy())
            dh_og = dh #list(dh.numpy())
            for index in self.indices:
                curr_dw = dw.repeat_interleave(curr_dim**2)
                curr_dh = dh.repeat_interleave(curr_dim**2)

                curr_select = select.repeat_interleave(curr_dim**2)
                curr_index = index[curr_select]
                curr_index[:,1] += curr_dh
                curr_index[:,2] += curr_dw
                #divvy = math.sqrt(curr_index.shape[0] / self.num_to_select)
                indices.append(curr_index)
                curr_dim = curr_dim // 2
                dw = torch.ceil(dw / 2).to(int)
                dh = torch.ceil(dh / 2).to(int)

        return indices,dw_og,dh_og,select,self.patch_size #self.init_dim*2
