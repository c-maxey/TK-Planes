# Copyright 2022 The Nerfstudio Team. All rights reserved.
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
Implementation of K-Planes (https://sarafridov.github.io/K-Planes/).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Type

import numpy as np
import cv2
import random
import torch
import torch.nn.functional as F
from torch.nn import Parameter
from torchmetrics import PeakSignalNoiseRatio
from torchmetrics.functional import structural_similarity_index_measure
from torchmetrics.image.lpip import LearnedPerceptualImagePatchSimilarity
from typing_extensions import Literal

from nerfstudio.cameras.rays import RayBundle
from nerfstudio.configs.config_utils import to_immutable_dict
from nerfstudio.engine.callbacks import (
    TrainingCallback,
    TrainingCallbackAttributes,
    TrainingCallbackLocation,
)
from nerfstudio.field_components.field_heads import FieldHeadNames
from nerfstudio.field_components.spatial_distortions import SceneContraction
from nerfstudio.model_components.losses import MSELoss, distortion_loss, interlevel_loss
from nerfstudio.model_components.ray_samplers import (
    ProposalNetworkSampler,
    UniformLinDispPiecewiseSampler,
    UniformSampler,
)
from nerfstudio.model_components.renderers import (
    AccumulationRenderer,
    DepthRenderer,
    RGBRenderer,
)
from nerfstudio.model_components.scene_colliders import NearFarCollider
from nerfstudio.models.base_model import Model, ModelConfig
from nerfstudio.utils import colormaps, misc

from .kplanes_field import KPlanesDensityField, KPlanesField
from .LimitGradLayer import LimitGradLayer


@dataclass
class KPlanesModelConfig(ModelConfig):
    """K-Planes Model Config"""

    _target: Type = field(default_factory=lambda: KPlanesModel)

    near_plane: float = 2.0
    """How far along the ray to start sampling."""

    far_plane: float = 6.0
    """How far along the ray to stop sampling."""

    grid_base_resolution: List[int] = field(default_factory=lambda: [128, 128, 128])
    """Base grid resolution."""

    grid_feature_dim: int = 32
    """Dimension of feature vectors stored in grid."""

    multiscale_res: List[int] = field(default_factory=lambda: [1, 2, 4])
    """Multiscale grid resolutions."""

    is_contracted: bool = False
    """Whether to use scene contraction (set to true for unbounded scenes)."""

    concat_features_across_scales: bool = True
    """Whether to concatenate features at different scales."""

    linear_decoder: bool = False
    """Whether to use a linear decoder instead of an MLP."""

    linear_decoder_layers: Optional[int] = 1
    """Number of layers in linear decoder"""

    # proposal sampling arguments
    num_proposal_iterations: int = 2
    """Number of proposal network iterations."""

    use_same_proposal_network: bool = False
    """Use the same proposal network. Otherwise use different ones."""

    proposal_net_args_list: List[Dict] = field(
        default_factory=lambda: [
            {"num_output_coords": 8, "resolution": [64, 64, 64]},
            {"num_output_coords": 8, "resolution": [128, 128, 128]},
        ]
    )
    """Arguments for the proposal density fields."""

    num_proposal_samples: Optional[Tuple[int]] = (256, 128)
    """Number of samples per ray for each proposal network."""

    num_samples: Optional[int] = 48
    """Number of samples per ray used for rendering."""

    single_jitter: bool = False
    """Whether use single jitter or not for the proposal networks."""

    proposal_warmup: int = 5000
    """Scales n from 1 to proposal_update_every over this many steps."""

    proposal_update_every: int = 5
    """Sample every n steps after the warmup."""

    use_proposal_weight_anneal: bool = True
    """Whether to use proposal weight annealing."""

    proposal_weights_anneal_slope: float = 10.0
    """Slope of the annealing function for the proposal weights."""

    proposal_weights_anneal_max_num_iters: int = 1000
    """Max num iterations for the annealing function."""

    appearance_embedding_dim: int = 0
    """Dimension of appearance embedding. Set to 0 to disable."""

    use_average_appearance_embedding: bool = True
    """Whether to use average appearance embedding or zeros for inference."""

    background_color: Literal["random", "last_sample", "black", "white"] = "white"
    """The background color as RGB."""

    loss_coefficients: Dict[str, float] = to_immutable_dict(
        {
            "img": 1.0,
            "interlevel": 1.0,
            "distortion": 0.001,
            "plane_tv": 0.0001,
            "plane_tv_proposal_net": 0.0001,
            "l1_time_planes": 0.0001,
            "l1_time_planes_proposal_net": 0.0001,
            "time_smoothness": 0.1,
            "time_smoothness_proposal_net": 0.001,
        }
    )
    """Loss coefficients."""


class KPlanesModel(Model):
    config: KPlanesModelConfig
    """K-Planes model

    Args:
        config: K-Planes configuration to instantiate model
    """

    def populate_modules(self):
        """Set the fields and modules."""
        super().populate_modules()

        if self.config.is_contracted:
            scene_contraction = SceneContraction(order=float("inf"))
        else:
            scene_contraction = None

        # Fields
        self.field = KPlanesField(
            self.scene_box.aabb,
            num_images=self.num_train_data,
            grid_base_resolution=self.config.grid_base_resolution,
            grid_feature_dim=self.config.grid_feature_dim,
            concat_across_scales=self.config.concat_features_across_scales,
            multiscale_res=self.config.multiscale_res,
            spatial_distortion=scene_contraction,
            appearance_embedding_dim=self.config.appearance_embedding_dim,
            use_average_appearance_embedding=self.config.use_average_appearance_embedding,
            linear_decoder=self.config.linear_decoder,
            linear_decoder_layers=self.config.linear_decoder_layers,
        )

        self.vol_tvs = None
        self.cosine_idx = 0
        self.vol_tv_mult = 0.0001
        self.conv_vol_tv_mult = 0.0001
        self.mask_layer = LimitGradLayer.apply
        self.conv_switch = 500
        
        grad_bool = False
        self.conv_train_bool = False
        self.conv_train_idx = 0
        self.conv_comp = torch.nn.ModuleList([])
        self.conv_mlp = torch.nn.ModuleList([])
        start_layers = 3
        curr_dim_mult = 2
        for conv_idx in range(len(self.config.multiscale_res)):
            curr_dim = self.config.grid_feature_dim            
            curr_seq = torch.nn.Sequential()
            curr_seq.append(torch.nn.Conv2d(curr_dim,curr_dim,3,1,1,padding_mode='replicate',bias=False))
            for conv_jdx in range(start_layers): # + conv_idx):
                next_dim = int(curr_dim*curr_dim_mult)
                #curr_seq.append(torch.nn.InstanceNorm2d(curr_dim))                
                curr_seq.append(torch.nn.ReLU())
                curr_seq.append(torch.nn.Conv2d(curr_dim,next_dim,3,2,1,padding_mode='replicate',bias=False))
                #curr_seq.append(torch.nn.InstanceNorm2d(next_dim))      
                curr_seq.append(torch.nn.ReLU())
                curr_seq.append(torch.nn.Conv2d(next_dim,next_dim,3,1,1,padding_mode='replicate',bias=False))
                curr_dim = next_dim

            self.conv_comp.append(curr_seq)
            curr_mlp_seq = torch.nn.Sequential()
            #curr_mlp_seq.append(torch.nn.Dropout(0.3))

            for conv_jdx in range(start_layers): # + conv_idx):
                next_dim = int(curr_dim / curr_dim_mult)
                curr_mlp_seq.append(torch.nn.Linear(curr_dim, next_dim, bias=False))
                #curr_mlp_seq.append(torch.nn.LayerNorm(next_dim))
                curr_mlp_seq.append(torch.nn.ReLU())
                curr_dim = next_dim
                
            curr_mlp_seq.append(torch.nn.Linear(curr_dim, 2,bias=False))
            curr_mlp_seq.append(torch.nn.Softmax(1))
            
            self.conv_mlp.append(curr_mlp_seq)
        
        #self.conj = torch.nn.Parameter(torch.tensor([[1,-1,-1,-1]]),requires_grad=False)
        #self.quat = torch.nn.Parameter(torch.tensor([[1.0,0.0,0.0,0.0]]))
        rot_angs = torch.nn.Parameter(torch.zeros(3,153))
        pos_diff = torch.nn.Parameter(torch.zeros(153,3))
        self.pos_idx = 0
        self.camera_pose_delts = torch.nn.ParameterList([rot_angs,pos_diff])
        
        self.density_fns = []
        num_prop_nets = self.config.num_proposal_iterations
        # Build the proposal network(s)
        self.proposal_networks = torch.nn.ModuleList()
        if self.config.use_same_proposal_network:
            assert len(self.config.proposal_net_args_list) == 1, "Only one proposal network is allowed."
            prop_net_args = self.config.proposal_net_args_list[0]
            network = KPlanesDensityField(
                self.scene_box.aabb,
                spatial_distortion=scene_contraction,
                linear_decoder=self.config.linear_decoder,
                **prop_net_args,
            )
            self.proposal_networks.append(network)
            self.density_fns.extend([network.density_fn for _ in range(num_prop_nets)])
        else:
            for i in range(num_prop_nets):
                prop_net_args = self.config.proposal_net_args_list[min(i, len(self.config.proposal_net_args_list) - 1)]
                network = KPlanesDensityField(
                    self.scene_box.aabb,
                    spatial_distortion=scene_contraction,
                    linear_decoder=self.config.linear_decoder,
                    **prop_net_args,
                )
                self.proposal_networks.append(network)
            self.density_fns.extend([network.density_fn for network in self.proposal_networks])

        # Samplers
        def update_schedule(step):
            return np.clip(
                np.interp(step, [0, self.config.proposal_warmup], [0, self.config.proposal_update_every]),
                1,
                self.config.proposal_update_every,
            )

        if self.config.is_contracted:
            initial_sampler = UniformLinDispPiecewiseSampler(single_jitter=self.config.single_jitter)
        else:
            initial_sampler = UniformSampler(single_jitter=self.config.single_jitter)

        self.proposal_sampler = ProposalNetworkSampler(
            num_nerf_samples_per_ray=self.config.num_samples,
            num_proposal_samples_per_ray=self.config.num_proposal_samples,
            num_proposal_network_iterations=self.config.num_proposal_iterations,
            single_jitter=self.config.single_jitter,
            update_sched=update_schedule,
            initial_sampler=initial_sampler,
        )

        # Collider
        self.collider = NearFarCollider(near_plane=self.config.near_plane, far_plane=self.config.far_plane)

        # renderers
        self.renderer_rgb = RGBRenderer(background_color=self.config.background_color)
        self.renderer_accumulation = AccumulationRenderer()
        self.renderer_depth = DepthRenderer()

        # losses
        self.rgb_loss = MSELoss()
        self.similarity_loss = torch.nn.CosineSimilarity(dim=2)
        self.grid_similarity_loss = torch.nn.CosineSimilarity(dim=0)        
        self.conv_mlp_loss = torch.nn.CrossEntropyLoss()
        self.mask_layer = LimitGradLayer.apply
        
        # metrics
        self.psnr = PeakSignalNoiseRatio(data_range=1.0)
        self.ssim = structural_similarity_index_measure
        self.lpips = LearnedPerceptualImagePatchSimilarity(normalize=True)
        self.temporal_distortion = len(self.config.grid_base_resolution) == 4  # for viewer

    def get_param_groups(self) -> Dict[str, List[Parameter]]:
        param_groups = {
            "proposal_networks": list(self.proposal_networks.parameters()),
            "fields": list(self.field.parameters()),
            "pose_delts": self.camera_pose_delts,
            "conv_comp": list(self.conv_comp.parameters()),
            "conv_mlp": list(self.conv_mlp.parameters())
        }
        return param_groups

    def get_training_callbacks(
        self, training_callback_attributes: TrainingCallbackAttributes
    ) -> List[TrainingCallback]:
        callbacks = []
        if self.config.use_proposal_weight_anneal:
            # anneal the weights of the proposal network before doing PDF sampling
            N = self.config.proposal_weights_anneal_max_num_iters

            def set_anneal(step):
                # https://arxiv.org/pdf/2111.12077.pdf eq. 18
                train_frac = np.clip(step / N, 0, 1)
                bias = lambda x, b: (b * x) / ((b - 1) * x + 1)
                anneal = bias(train_frac, self.config.proposal_weights_anneal_slope)
                self.proposal_sampler.set_anneal(anneal)

            callbacks.append(
                TrainingCallback(
                    where_to_run=[TrainingCallbackLocation.BEFORE_TRAIN_ITERATION],
                    update_every_num_iters=1,
                    func=set_anneal,
                )
            )
            callbacks.append(
                TrainingCallback(
                    where_to_run=[TrainingCallbackLocation.AFTER_TRAIN_ITERATION],
                    update_every_num_iters=1,
                    func=self.proposal_sampler.step_cb,
                )
            )
        return callbacks

    def quat_mult(self,q1,q2):
        a = (q1[:,0]*q2[:,0] - q1[:,1]*q2[:,1] - q1[:,2]*q2[:,2] - q1[:,3]*q2[:,3]).unsqueeze(-1)
        b = (q1[:,0]*q2[:,1] - q1[:,1]*q2[:,0] - q1[:,2]*q2[:,3] - q1[:,3]*q2[:,2]).unsqueeze(-1)
        c = (q1[:,0]*q2[:,2] - q1[:,1]*q2[:,3] - q1[:,2]*q2[:,0] - q1[:,3]*q2[:,1]).unsqueeze(-1)
        d = (q1[:,0]*q2[:,3] - q1[:,1]*q2[:,2] - q1[:,2]*q2[:,1] - q1[:,3]*q2[:,0]).unsqueeze(-1)        

        p = torch.cat([a,b,c,d],dim=1)

        if torch.sum(torch.abs(p[:,0])) > 0.00001:
            print('WHATTTTTT')
            exit(-1)
        
        return p

    def get_rot_mat_torch(self,angs): #roll,yaw,pitch):
        #roll = torch.clip(roll,-3,3) * torch.pi / 180
        #yaw = yaw * torch.pi / 180
        #pitch = torch.clip(pitch,-75,-40) * torch.pi / 180
        roll = angs[0]
        yaw = angs[1]
        pitch = angs[2]
        rz = torch.stack([torch.stack([torch.cos(roll), -torch.sin(roll), torch.zeros_like(roll)]),
                          torch.stack([torch.sin(roll), torch.cos(roll), torch.zeros_like(roll)]),
                          torch.stack([torch.zeros_like(roll),torch.zeros_like(roll),torch.ones_like(roll)])])
        ry = torch.stack([torch.stack([torch.cos(yaw), torch.zeros_like(roll), torch.sin(yaw)]),
                          torch.stack([torch.zeros_like(roll), torch.ones_like(roll), torch.zeros_like(roll)]),
                          torch.stack([-torch.sin(yaw), torch.zeros_like(roll), torch.cos(yaw)])])
        rx = torch.stack([torch.stack([torch.ones_like(roll), torch.zeros_like(roll), torch.zeros_like(roll)]),
                          torch.stack([torch.zeros_like(roll), torch.cos(pitch), -torch.sin(pitch)]),
                          torch.stack([torch.zeros_like(roll), torch.sin(pitch), torch.cos(pitch)])])
        

        rz = rz.permute(2,0,1)
        ry = ry.permute(2,0,1)
        rx = rx.permute(2,0,1)

        R = torch.matmul(rz.squeeze(),torch.matmul(ry.squeeze(),rx.squeeze()))

        return R
    
    def get_outputs(self, ray_bundle: RayBundle):
        density_fns = self.density_fns
        if ray_bundle.times is not None:
            density_fns = [functools.partial(f, times=ray_bundle.times) for f in density_fns]

        
        rot_angs = self.camera_pose_delts[0]
        pos_diff = self.camera_pose_delts[1]
        
        R = self.get_rot_mat_torch(torch.clip(rot_angs,-0.01,0.01))
        selected_R = R[ray_bundle.camera_indices.squeeze()]
        new_dirs = torch.matmul(selected_R,ray_bundle.directions.unsqueeze(-1)).squeeze()
        selected_delts = pos_diff[ray_bundle.camera_indices.squeeze()]
        new_origins = ray_bundle.origins + torch.clip(selected_delts,-0.2,0.2)
        ray_bundle.origins = new_origins
        ray_bundle.directions = new_dirs #/ torch.norm(new_dirs,2,1).unsqueeze(-1)
        
        
        ray_samples, weights_list, ray_samples_list = self.proposal_sampler(
            ray_bundle, density_fns=density_fns
        )
        field_outputs = self.field(ray_samples)

        weights = ray_samples.get_weights(field_outputs[FieldHeadNames.DENSITY])
        weights_list.append(weights)
        ray_samples_list.append(ray_samples)

        rgb = self.renderer_rgb(rgb=field_outputs[FieldHeadNames.RGB], weights=weights)
        depth = self.renderer_depth(weights=weights, ray_samples=ray_samples)
        accumulation = self.renderer_accumulation(weights=weights)

        self.vol_tvs = field_outputs["vol_tvs"]
        
        outputs = {
            "rgb": rgb,
            "accumulation": accumulation,
            "depth": depth,
        }

        # These use a lot of GPU memory, so we avoid storing them for eval.
        if self.training:
            outputs["weights_list"] = weights_list
            outputs["ray_samples_list"] = ray_samples_list

        for i in range(self.config.num_proposal_iterations):
            outputs[f"prop_depth_{i}"] = self.renderer_depth(
                weights=weights_list[i], ray_samples=ray_samples_list[i]
            )

        return outputs

    def get_metrics_dict(self, outputs, batch):
        image = batch["image"].to(self.device)

        metrics_dict = {
            "psnr": self.psnr(outputs["rgb"], image)
        }
        if self.training:
            metrics_dict["interlevel"] = interlevel_loss(outputs["weights_list"], outputs["ray_samples_list"])
            metrics_dict["distortion"] = distortion_loss(outputs["weights_list"], outputs["ray_samples_list"])

            prop_grids = [p.grids.plane_coefs for p in self.proposal_networks]
            field_grids = [g.plane_coefs for g in self.field.grids]

            metrics_dict["plane_tv"] = space_tv_loss(field_grids)
            metrics_dict["plane_tv_proposal_net"] = space_tv_loss(prop_grids)

            if len(self.config.grid_base_resolution) == 4:
                metrics_dict["l1_time_planes"] = l1_time_planes(field_grids)
                metrics_dict["l1_time_planes_proposal_net"] = l1_time_planes(prop_grids)
                metrics_dict["time_smoothness"] = time_smoothness(field_grids)
                metrics_dict["time_smoothness_proposal_net"] = time_smoothness(prop_grids)

        return metrics_dict

    def get_dist_loss(self, outputs):
        weights = outputs['weights_list'][-1]
        ray_samples = outputs['ray_samples_list'][-1]

        num_samples = weights.shape[1]

        num_rays = 1000
        dist_loss = 0.0
        for i in range(num_samples-1):
            for j in range(num_samples-1):
                if i != j:
                    dist_loss += weights[:num_rays,i,0]*weights[:num_rays,j,0]*torch.abs((ray_samples.spacing_starts[:num_rays,i,0] +
                                                                                  ray_samples.spacing_starts[:num_rays,i+1,0]) -
                                                                                 (ray_samples.spacing_starts[:num_rays,j,0] +
                                                                                  ray_samples.spacing_starts[:num_rays,j+1,0])) / 2

        for i in range(0,num_samples-1):
            dist_loss += (weights[:num_rays,i,0]**2)*(ray_samples.spacing_starts[:num_rays,i+1,0] - ray_samples.spacing_starts[:num_rays,i,0]) / 3

        return dist_loss
                    
    def get_loss_dict(self, outputs, batch, metrics_dict=None):
        image = batch["image"].to(self.device)

        mask_image = batch["time_mask"].to(image.dtype)

        weights_lst = outputs['weights_list']#[-1]
        
        centers = torch.tensor([(255,0,0),(0,255,0),(0,0,255),(255,255,0),(255,0,255),(0,255,255),(255,255,255),(125,125,125),(50,50,50),(125,0,125)])
        center_num = centers.shape[0]
        centers = centers.to("cuda:0")
        centers = centers.unsqueeze(0)
        #mask_image = mask_image.unsqueeze(1)
        dists = torch.sqrt(torch.sum((mask_image.unsqueeze(1) - centers)**2,dim=-1))
        dists = dists == 0

        #outtie = self.vol_tvs[0][0].reshape(-1,48,32)
        #nonzero0 = torch.nonzero(dists[:,0]).squeeze()
        #new_outtie = outtie[nonzero0].transpose(0,1)
        #new_outtie_comp = torch.matmul(new_outtie,new_outtie.transpose(-1,-2))

        
        image_mask = torch.sum(mask_image,-1).unsqueeze(-1).to(image.dtype)
        #print("SUM: {}".format(torch.sum(image_mask)))
        #exit(-1)
        image_mask_bool = (image_mask > 10).to(image.dtype)

        mask_image = mask_image / 255.
        #image = image*(1 - image_mask_bool) + mask_image*image_mask_bool
        #image = (1 - image_mask) + red_image*image_mask        
        loss_dict = {"rgb": self.rgb_loss(image, outputs["rgb"])}
        #loss_dict = {"rgb": self.rgb_loss(self.field.masks*image, outputs["rgb"])}        
        self.cosine_idx += 1

        if self.training:
            for key in self.config.loss_coefficients:
                if key in metrics_dict:
                    loss_dict[key] = metrics_dict[key].clone()

            loss_dict = misc.scale_dict(loss_dict, self.config.loss_coefficients)

            outputs_lst = self.vol_tvs
            vol_tvs = 0.0
            time_mask_loss = 0.0
            time_mask_alt = image_mask_bool.unsqueeze(-1).expand(-1,48,-1)
            num_comps = outputs_lst[0][0].shape[-1]

            for output_idx,_outputs in enumerate(outputs_lst):
                time_mask_loss += self.rgb_loss((_outputs[2][:,num_comps:]).reshape(-1,48,1),time_mask_alt)
                time_mask_loss += self.rgb_loss((_outputs[4][:,num_comps:]).reshape(-1,48,1),time_mask_alt)
                time_mask_loss += self.rgb_loss((_outputs[5][:,num_comps:]).reshape(-1,48,1),time_mask_alt)


            
            field_grids = [g.plane_coefs for g in self.field.grids]
            grid_norm = 0.0
            reshape_num = 16
            conv_mlp = 0.0
            conv_vol_tvs = 0.0
            local_vol_tvs = 0.0
            temporal_simm = 0.0
            self.conv_train_idx = (self.conv_train_idx + 1) % self.conv_switch
            if self.conv_train_idx == 0:
                for m in self.conv_comp:
                    for param in m.parameters():
                        param.requires_grad = self.conv_train_bool

                self.conv_switch = 100 if self.conv_switch == 500 else 500
                self.conv_train_bool = not self.conv_train_bool
                
            weights = weights_lst[-1].detach()
            for odx,_outputs in enumerate(outputs_lst):
                #continue
                #for tdx0,tdx1,tdx2,tdx3 in [(0,2,4,6),(1,2,5,7),(3,4,5,8)]:
                #    spatial = _outputs[tdx0].reshape(-1,48,32)
                #    temporal = (_outputs[tdx3]*_outputs[tdx1][:,:num_comps]*_outputs[tdx2][:,:num_comps]).reshape(-1,48,32).transpose(-1,-2)
                #    local_vol_tvs += torch.abs(torch.matmul(spatial,temporal)).mean()
                #local_vol_tvs += torch.abs(self.similarity_loss(_outputs[0],_outputs[2][:,:num_comps]*_outputs[4][:,:num_comps]*_outputs[6])).mean()
                #local_vol_tvs += torch.abs(self.similarity_loss(_outputs[1],_outputs[2][:,:num_comps]*_outputs[5][:,:num_comps]*_outputs[7])).mean()
                #local_vol_tvs += torch.abs(self.similarity_loss(_outputs[3],_outputs[4][:,:num_comps]*_outputs[5][:,:num_comps]*_outputs[8])).mean()
                #local_vol_tvs += torch.abs(self.similarity_loss(_outputs[0].detach(),_outputs[6])).mean()
                #local_vol_tvs += torch.abs(self.similarity_loss(_outputs[1].detach(),_outputs[7])).mean()
                #local_vol_tvs += torch.abs(self.similarity_loss(_outputs[3].detach(),_outputs[8])).mean()

                cdxs = [cdx for cdx in range(center_num)]
                random.shuffle(cdxs)
                for cdx in cdxs[:3]:
                    alt_cdxs = [xdx for xdx in range(center_num) if cdx != xdx]
                    random.shuffle(alt_cdxs)
                    alt_cdx = 2 #alt_cdxs[0]

                    for tdx0,tdx1,tdx2,tdx3 in [(6,2,4,0),(7,2,5,1),(8,4,5,3)]:
                        spatial = _outputs[tdx3].reshape(-1,48,32)
                        temporal = (_outputs[tdx0]*_outputs[tdx1][:,:num_comps]*_outputs[tdx2][:,:num_comps]).reshape(-1,48,32)
                        nonzero = torch.nonzero(dists[:,cdx]).squeeze()
                        alt_nonzero = torch.nonzero(dists[:,alt_cdx]).squeeze()
                        if alt_nonzero.shape[0] == 0:
                            alt_nonzero = nonzero
                        
                        select_weights = weights[nonzero].reshape(-1,1)#.transpose(0,1) #48, num_select, 1
                        select_alt_weights = weights[alt_nonzero].reshape(-1,1)#.transpose(0,1) #48, num_select, 1                        

                        select_temporal = temporal[nonzero].reshape(-1,temporal.shape[-1])#.transpose(0,1) #* select_weights #48, num_select, 32
                        select_alt_temporal = temporal[alt_nonzero].reshape(-1,temporal.shape[-1])#.transpose(0,1) #* select_weights #48, num_select, 32  
                        select_spatial = spatial[nonzero].reshape(-1,spatial.shape[-1])#.transpose(0,1) #* select_weights
                        #sim_loss = self.similarity_loss(select_temporal,select_temporal) 115 x 48  ==> 115 x 48 x 1 matmul 1 x 1 (115 x 48)
                        select_alt_weights = torch.matmul(select_weights,select_alt_weights.transpose(-1,-2)) 
                        select_weights = torch.matmul(select_weights,select_weights.transpose(-1,-2))

                        #matmul_mask = torch.ones_like(select_weights)
                        if cdx == alt_cdx:
                            for matidx in range(0,select_alt_weights.shape[0],48):
                                select_alt_weights[matidx:matidx+48,matidx:matidx+48] = 0

                        select_weights = F.softmax(select_weights,dim=1)
                        select_alt_weights = F.softmax(select_alt_weights,dim=1)
                        
                        temporal_sim_norms = torch.sqrt(torch.sum(select_temporal**2,dim=-1))
                        alt_temporal_sim_norms = torch.sqrt(torch.sum(select_alt_temporal**2,dim=-1))                        
                        spatial_sim_norms = torch.sqrt(torch.sum(select_spatial**2,dim=-1))
                        spatial_sim_norms = temporal_sim_norms.unsqueeze(-1)*spatial_sim_norms.unsqueeze(-2) + 1e-8
                        #temporal_sim_norms = temporal_sim_norms.unsqueeze(-1)*temporal_sim_norms.unsqueeze(-2) + 1e-8
                        temporal_sim_norms = temporal_sim_norms.unsqueeze(-1)*alt_temporal_sim_norms.unsqueeze(-2) + 1e-8

                        temporal_similarity = torch.matmul(select_temporal,select_alt_temporal.transpose(-1,-2)) / temporal_sim_norms
                        #temporal_similarity = torch.matmul(select_temporal,select_temporal.transpose(-1,-2)) / temporal_sim_norms                       
                        spatial_temporal_comp = torch.abs(torch.matmul(select_spatial,select_temporal.transpose(-1,-2)) / spatial_sim_norms) # 48, num_select, num_select
                        #local_vol_tvs += -self.mask_layer(temporal_similarity,select_weights).mean()
                        local_vol_tvs += -0.1*self.mask_layer(temporal_similarity,select_alt_weights).mean()                        
                        #local_vol_tvs += spatial_temporal_comp.mean()
                        local_vol_tvs += self.mask_layer(spatial_temporal_comp,select_weights).mean()                        
                        #local_vol_tvs += -torch.sum(self.mask_layer(temporal_similarity,select_weights))#.mean()
                        #local_vol_tvs += torch.sum(self.mask_layer(spatial_temporal_comp,select_weights))#.mean()                        
                
            for grid_idx,grids in enumerate(field_grids):
                #continue
                grid_norm += torch.abs(1 - torch.norm(grids[0],2,0)).mean()
                grid_norm += torch.abs(1 - torch.norm(grids[1],2,0)).mean()
                grid_norm += torch.abs(1 - torch.norm(grids[3],2,0)).mean()
                #grid_norm += 0.1*torch.abs(1 - torch.norm(grids[6],2,0)).mean()
                #grid_norm += 0.1*torch.abs(1 - torch.norm(grids[7],2,0)).mean()
                #grid_norm += 0.1*torch.abs(1 - torch.norm(grids[8],2,0)).mean()                
                #grid_norm += torch.norm(grids[6],2,0).mean()
                #grid_norm += torch.norm(grids[7],2,0).mean()
                #grid_norm += torch.norm(grids[8],2,0).mean()                
                
                #continue
                g2 = grids[2]                
                g4 = grids[4]
                g5 = grids[5]
                g6 = grids[6]
                c,w,h = g6.shape
                g7 = grids[7]
                g8 = grids[8]

                #g6 = y - x

                #g2 = t - x

                #g4 = t - y

                #g4_t = y - t

                #g42 = g4_t * g2 = (y - t) * (t - x) = y - x

                g24 = torch.matmul(g4[:num_comps].transpose(-1,-2),g2[:num_comps])
                #g24_mask = torch.matmul(g4[num_comps:].transpose(-1,-2),g2[num_comps:])

                g24 = g24 * g6 #* g24_mask
                #g24 = g6
                #g24 = self.mask_layer(g6,g24mask)
                #g24 = F.normalize(g24,2,0)
                #g24mlp = self.conv_comp(g24.detach())
                if not self.conv_train_bool:
                    g24 = g24.detach()                
                g24 = self.conv_comp[grid_idx](g24)

                g24c, g24h, g24w = g24.shape
                #g24 = g24.reshape(g24c, (g24h // reshape_num),reshape_num, (g24w // reshape_num),reshape_num)
                #g24 = g24.permute(0,1,3,2,4)
                #g24 = g24.reshape(c,-1,reshape_num**2)
                #g24 = g24.permute(1,2,0)
                #temporal_simm += compute_plane_tv(g24)
                #g24 = F.normalize(g24.reshape(g24c,-1),2,0)
                #g24mlp = F.normalize(g24mlp.reshape(g24c,-1),2,0)                
                g24 = g24.reshape(g24c,-1)
                
                g25 = torch.matmul(g5[:num_comps].transpose(-1,-2),g2[:num_comps])
                #g25_mask = torch.matmul(g5[num_comps:].transpose(-1,-2),g2[num_comps:])                

                g25 = g25 * g7 #* g25_mask
                #g25 = g7
                #g25 = self.mask_layer(g7,g25mask)
                #g25 = F.normalize(g25,2,0)
                #g25mlp = self.conv_comp(g25.detach())
                if not self.conv_train_bool:
                    g25 = g25.detach()
                g25 = self.conv_comp[grid_idx](g25)                
                g25c, g25h, g25w = g25.shape
                #g25 = g25.reshape(g25c, (g25h // reshape_num),reshape_num, (g25w // reshape_num),reshape_num)
                #g25 = g25.permute(0,1,3,2,4)
                #g25 = g25.reshape(c,-1,reshape_num**2)
                #g25 = g25.permute(1,2,0)
                #temporal_simm += compute_plane_tv(g25)                
                #g25 = F.normalize(g25.reshape(g25c,-1),2,0)
                #g25mlp = F.normalize(g25mlp.reshape(g25c,-1),2,0)                
                g25 = g25.reshape(g25c,-1)
                
                g45 = torch.matmul(g5[:num_comps].transpose(-1,-2),g4[:num_comps])
                #g45_mask = torch.matmul(g5[num_comps:].transpose(-1,-2),g4[num_comps:])                

                g45 = g45 * g8 #* g45_mask
                #g45 = g8
                #g45 = self.mask_layer(g8,g45mask)
                #g45 = F.normalize(g45,2,0)
                #g45mlp = self.conv_comp(g45.detach())
                if not self.conv_train_bool:
                    g45 = g45.detach()
                g45 = self.conv_comp[grid_idx](g45)     
                g45c, g45h, g45w = g45.shape
                #g45 = g45.reshape(g45c, (g45h // reshape_num),reshape_num, (g45w // reshape_num),reshape_num)
                #g45 = g45.permute(0,1,3,2,4)
                #g45 = g45.reshape(c,-1,reshape_num**2)
                #g45 = g45.permute(1,2,0)
                #g45mlp = F.normalize(g45mlp.reshape(g45c,-1),2,0)
                #temporal_simm += compute_plane_tv(g45)
                #g45 = F.normalize(g45.reshape(g45c,-1),2,0)
                g45 = g45.reshape(g45c,-1)

                #if self.conv_train_bool:

                if not self.conv_train_bool:
                    mlp24 = self.conv_mlp[grid_idx](g24.transpose(-1,-2))
                    mlp25 = self.conv_mlp[grid_idx](g25.transpose(-1,-2))
                    mlp45 = self.conv_mlp[grid_idx](g45.transpose(-1,-2))
                    conv_mlp += self.conv_mlp_loss(mlp24,torch.ones_like(mlp24))
                    conv_mlp += self.conv_mlp_loss(mlp25,torch.ones_like(mlp25))
                    conv_mlp += self.conv_mlp_loss(mlp45,torch.ones_like(mlp45))
                else:
                    temporal_simm += -self.grid_similarity_loss(g24,g24).mean()
                    temporal_simm += -self.grid_similarity_loss(g25,g25).mean()
                    temporal_simm += -self.grid_similarity_loss(g45,g45).mean()


                c,h,w = grids[0].shape

                g0 = grids[0]
                #g0 = F.normalize(g0,2,0)
                #g0mlp = self.conv_comp(g0.detach())
                if not self.conv_train_bool:
                    g0 = g0.detach()
                g0 = self.conv_comp[grid_idx](g0) 
                g0c,g0h,g0w = g0.shape
                #g0 = g0.reshape(g0c, (g0h // reshape_num),reshape_num, (g0w // reshape_num),reshape_num)
                #g0 = g0.permute(0,1,3,2,4)
                #g0 = g0.reshape(c,-1,reshape_num**2)                
                #g0 = g0.permute(1,2,0)
                #g0 = F.normalize(g0.reshape(g0c,-1),2,0)
                #g0mlp = F.normalize(g0mlp.reshape(g0c,-1),2,0)                
                g0 = g0.reshape(g0c,-1)
                
                g1 = grids[1]
                #g1 = F.normalize(g1,2,0)
                if not self.conv_train_bool:
                    g1 = g1.detach()                
                #g1mlp = self.conv_comp(g1.detach())
                g1 = self.conv_comp[grid_idx](g1)
                g1c,g1h,g1w = g1.shape
                #g1 = g1.reshape(g1c, (g1h // reshape_num),reshape_num, (g1w // reshape_num),reshape_num)
                #g1 = g1.permute(0,1,3,2,4)
                #g1 = g1.reshape(c,-1,reshape_num**2)
                #g1 = g1.permute(1,2,0)
                #g1 = F.normalize(g1.reshape(g1c,-1),2,0)
                #g1mlp = F.normalize(g1mlp.reshape(g1c,-1),2,0)                
                g1 = g1.reshape(g1c,-1)
                
                g3 = grids[3]
                #g3 = F.normalize(g3,2,0)
                if not self.conv_train_bool:
                    g3 = g3.detach()                
                #g3mlp = self.conv_comp(g3.detach())
                g3 = self.conv_comp[grid_idx](g3)
                g3c,g3h,g3w = g3.shape
                #g3 = g3.reshape(g3c, (g3h // reshape_num),reshape_num, (g3w // reshape_num),reshape_num)
                #g3 = g3.permute(0,1,3,2,4)
                #g3 = g3.reshape(c,-1,reshape_num**2)                                
                #g3 = g3.permute(1,2,0)
                #g3 = F.normalize(g3.reshape(g3c,-1),2,0)
                #g3mlp = F.normalize(g3mlp.reshape(g3c,-1),2,0)                
                g3 = g3.reshape(g3c,-1)
                
                g6 = g24
                g7 = g25
                g8 = g45

                if not self.conv_train_bool:
                    mlp0 = self.conv_mlp[grid_idx](g0.transpose(-1,-2))
                    mlp1 = self.conv_mlp[grid_idx](g1.transpose(-1,-2))
                    mlp3 = self.conv_mlp[grid_idx](g3.transpose(-1,-2))
                    conv_mlp += self.conv_mlp_loss(mlp0,torch.zeros_like(mlp0))
                    conv_mlp += self.conv_mlp_loss(mlp1,torch.zeros_like(mlp1))
                    conv_mlp += self.conv_mlp_loss(mlp3,torch.zeros_like(mlp3))                                              
                else:
                    vol_tvs += torch.abs(self.grid_similarity_loss(g0,g6)).mean()
                    vol_tvs += torch.abs(self.grid_similarity_loss(g1,g7)).mean()
                    vol_tvs += torch.abs(self.grid_similarity_loss(g3,g8)).mean()
                    temporal_simm += -self.grid_similarity_loss(g0,g0).mean()
                    temporal_simm += -self.grid_similarity_loss(g1,g1).mean()
                    temporal_simm += -self.grid_similarity_loss(g3,g3).mean()                    
                                               

                reshape_num *= 2
                
                #vol_tvs += torch.abs(torch.matmul(grids[1].reshape(c,-1).transpose(-1,-2),grids[7].reshape(c,-1))).mean()
                #vol_tvs += torch.abs(torch.matmul(grids[3].reshape(c,-1).transpose(-1,-2),grids[8].reshape(c,-1))).mean()                
                
                #vol_tvs += torch.abs(self.grid_similarity_loss(grids[0],grids[6])).mean()
                #vol_tvs += torch.abs(self.grid_similarity_loss(grids[1],grids[7])).mean()
                #vol_tvs += torch.abs(self.grid_similarity_loss(grids[3],grids[8])).mean()

            if self.cosine_idx % 3000 == 0:
                self.vol_tv_mult = np.clip(self.vol_tv_mult * 2,0,0.01)
                self.conv_vol_tv_mult = np.clip(self.conv_vol_tv_mult*2,0,0.01)
            
            if self.conv_train_bool:
                #loss_dict["vol_tvs"] = self.vol_tv_mult*(vol_tvs / (3*len(outputs_lst)))
                #loss_dict["temporal_simm"] = self.conv_vol_tv_mult*temporal_simm / (3*len(outputs_lst))
                loss_dict["vol_tvs"] = 0.01*(vol_tvs / (3*len(outputs_lst)))
                loss_dict["temporal_simm"] = 0.01*temporal_simm / (3*len(outputs_lst))                                
            else:
                loss_dict["conv_mlp"] = conv_mlp / (6*len(outputs_lst))
            
            loss_dict["camera_delts"] = (torch.abs(self.camera_pose_delts[0]).mean() + torch.abs(self.camera_pose_delts[1]).mean())
            loss_dict["local_vol_tvs"] = 0.01*local_vol_tvs / (3*len(outputs_lst))
            #loss_dict["grid_norm"] = 0.01*grid_norm / (6*len(outputs_lst))
            
            loss_dict["time_masks"] = time_mask_loss
            
        return loss_dict

    def get_image_metrics_and_images(
        self, outputs: Dict[str, torch.Tensor], batch: Dict[str, torch.Tensor]
    ) -> Tuple[Dict[str, float], Dict[str, torch.Tensor]]:
        image = batch["image"].to(self.device)

        rgb = outputs["rgb"]
        acc = colormaps.apply_colormap(outputs["accumulation"])
        depth = colormaps.apply_depth_colormap(outputs["depth"], accumulation=outputs["accumulation"])

        combined_rgb = torch.cat([image, rgb], dim=1)
        combined_acc = torch.cat([acc], dim=1)
        combined_depth = torch.cat([depth], dim=1)

        # Switch images from [H, W, C] to [1, C, H, W] for metrics computations
        image = torch.moveaxis(image, -1, 0)[None, ...]
        rgb = torch.moveaxis(rgb, -1, 0)[None, ...]

        # all of these metrics will be logged as scalars
        metrics_dict = {
            "psnr": float(self.psnr(image, rgb).item()),
            "ssim": float(self.ssim(image, rgb)),
            "lpips": float(self.lpips(image, rgb))
        }
        images_dict = {"img": combined_rgb, "accumulation": combined_acc, "depth": combined_depth}

        for i in range(self.config.num_proposal_iterations):
            key = f"prop_depth_{i}"
            prop_depth_i = colormaps.apply_depth_colormap(
                outputs[key],
                accumulation=outputs["accumulation"],
            )
            images_dict[key] = prop_depth_i

        return metrics_dict, images_dict


def compute_plane_tv(t: torch.Tensor, only_w: bool = False) -> float:
    """Computes total variance across a plane.

    Args:
        t: Plane tensor
        only_w: Whether to only compute total variance across w dimension

    Returns:
        Total variance
    """
    _, h, w = t.shape
    w_tv = torch.square(t[..., :, 1:] - t[..., :, : w - 1]).mean()
    
    if only_w:
        #w_tv2 = torch.square(t[..., 1:, 1:] - t[..., : h - 1, : w - 1]).mean()
        #w_tv3 = torch.square(t[..., : h - 1, 1:] - t[..., 1:, : w - 1]).mean()    
        
        return w_tv #+ 0.5*(w_tv2 + w_tv3)

    h_tv = torch.square(t[..., 1:, :] - t[..., : h - 1, :]).mean()
    #h_tv2 = torch.square(t[..., 1:, 1:] - t[..., : h - 1, : w - 1]).mean()
    #h_tv3 = torch.square(t[..., 1:, : w - 1] - t[..., : h - 1, 1:]).mean()    
    return h_tv + w_tv #+ 0.5*(w_tv2 + w_tv3) + 0.5*(h_tv2 + h_tv3)


def space_tv_loss(multi_res_grids: List[torch.Tensor]) -> float:
    """Computes total variance across each spatial plane in the grids.

    Args:
        multi_res_grids: Grids to compute total variance over

    Returns:
        Total variance
    """

    total = 0.0
    num_planes = 0
    num_comps = multi_res_grids[0][0].shape[0]
    for grids in multi_res_grids:
        if len(grids) == 3:
            spatial_planes = {0, 1, 2}
        else:
            spatial_planes = {0, 1, 3, 6, 7, 8} #3}

        for grid_id, grid in enumerate(grids):
            if grid_id in spatial_planes:
                total += compute_plane_tv(grid)
            else:
                # Space is the last dimension for space-time planes.
                total += compute_plane_tv(grid[:num_comps], only_w=True)
            num_planes += 1
    return total / num_planes


def l1_time_planes(multi_res_grids: List[torch.Tensor]) -> float:
    """Computes the L1 distance from the multiplicative identity (1) for spatiotemporal planes.

    Args:
        multi_res_grids: Grids to compute L1 distance over

    Returns:
         L1 distance from the multiplicative identity (1)
    """
    time_planes = [2, 4, 5, 6, 7, 8]  # These are the spatiotemporal planes
    #time_planes = [4, 5, 7, 8, 10, 11]  # These are the spatiotemporal planes    
    total = 0.0
    num_comps = multi_res_grids[0][0].shape[0]
    num_planes = 0
    for grids in multi_res_grids:
        for grid_id in time_planes:
            #total += torch.abs(1 - grids[grid_id]).mean()
            total += torch.abs(grids[grid_id][:num_comps]).mean()            
            num_planes += 1

    return total / num_planes


def compute_plane_smoothness(t: torch.Tensor) -> float:
    """Computes smoothness across the temporal axis of a plane

    Args:
        t: Plane tensor

    Returns:
        Time smoothness
    """
    _, h, w = t.shape
    # Convolve with a second derivative filter, in the time dimension which is dimension 2
    first_difference = t[..., 1:, :] - t[..., : h - 1, :]  # [c, h-1, w]
    second_difference = first_difference[..., 1:, :] - first_difference[..., : h - 2, :]  # [c, h-2, w]
    #first_difference2 = t[..., 1:, 1:] - t[..., : h - 1, : w - 1]  # [c, h-1, w-1]
    #second_difference2 = first_difference2[..., 1:, 1:] - first_difference2[..., : h - 2, : w - 2]  # [c, h-2, w-2]
    #first_difference3 = t[..., 1:, : w - 1] - t[..., : h - 1, 1:]  # [c, h-1, w-1]
    #second_difference3 = first_difference3[..., 1:, : w - 2] - first_difference3[..., : h - 2, 1:]  # [c, h-2, w-2]
    # Take the L2 norm of the result
    return torch.square(second_difference).mean() #+ torch.square(second_difference2).mean() + torch.square(second_difference3).mean()


def time_smoothness(multi_res_grids: List[torch.Tensor]) -> float:
    """Computes smoothness across each time plane in the grids.

    Args:
        multi_res_grids: Grids to compute time smoothness over

    Returns:
        Time smoothness
    """
    total = 0.0
    num_planes = 0
    num_comps = multi_res_grids[0][0].shape[0]    
    for grids in multi_res_grids:
        time_planes = [2, 4, 5]  # These are the spatiotemporal planes
        #time_planes = [4, 5, 7, 8, 10, 11]  # These are the spatiotemporal planes            
        for grid_id in time_planes:
            total += compute_plane_smoothness(grids[grid_id][:num_comps])
            num_planes += 1

    return total / num_planes
