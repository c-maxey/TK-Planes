
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

"""Data parser for blender dataset"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Type

import imageio
import numpy as np
import torch

from nerfstudio.cameras.cameras import Cameras, CameraType
from nerfstudio.data.dataparsers.base_dataparser import (
    DataParser,
    DataParserConfig,
    DataparserOutputs,
)
from nerfstudio.data.scene_box import SceneBox
from nerfstudio.utils.colors import get_color
from nerfstudio.utils.io import load_from_json


@dataclass
class OkutamaDataParserConfig(DataParserConfig):
    """D-NeRF dataset parser config"""

    _target: Type = field(default_factory=lambda: Okutama)
    """target class to instantiate"""
    data: Path = Path("data/practice_set2/train")
    """Directory specifying location of data."""
    scale_factor: float = 1.0
    """How much to scale the camera origins by."""
    height: float = 720.0
    width: float = 1280.0
    alpha_color: str = "white"
    """alpha color of background"""


@dataclass
class Okutama(DataParser):
    """Okutama Dataset"""

    config: OkutamaDataParserConfig
    includes_time: bool = True

    def __init__(self, config: OkutamaDataParserConfig):
        super().__init__(config=config)
        self.data: Path = config.data
        self.scale_factor: float = config.scale_factor
        self.alpha_color = config.alpha_color
        self.width = config.width
        self.height = config.height

    def _generate_dataparser_outputs(self, split="train"):
        if self.alpha_color is not None:
            alpha_color_tensor = get_color(self.alpha_color)
        else:
            alpha_color_tensor = None

        meta = load_from_json(self.data / f"transforms_{split}.json")
        image_filenames = []
        mask_filenames = []
        poses = []
        times = []
        for frame in meta["frames"]:
            fname = self.data / Path(frame["file_path"].replace("./", "") + ".jpg")
            mname = self.data / Path(frame["file_path"].replace("./", "").replace("images","masks").replace("frame","mask") + ".png")
            image_filenames.append(fname)
            mask_filenames.append(mname)
            poses.append(np.array(frame["transform_matrix"]))
            times.append(frame["time"])
        poses = np.array(poses).astype(np.float32)
        times = torch.tensor(times, dtype=torch.float32)

        #img_0 = imageio.imread(image_filenames[0])
        #image_height, image_width = img_0.shape[:2]
        #camera_angle_x = float(meta["camera_angle_x"])
        #focal_length = 0.5 * image_width / np.tan(0.5 * camera_angle_x)

        fx = meta['fl_x']
        fy = meta['fl_y']
        
        cx = self.width / 2 #meta['cx']
        cy = self.height / 2 #meta['cy']
        camera_to_world = torch.from_numpy(poses[:, :3])  # camera to world transform

        # in x,y,z order
        camera_to_world[..., 3] *= self.scale_factor
        #scene_box = SceneBox(aabb=torch.tensor([[-4, -4, -3.75], [4, 4, 0.25]], dtype=torch.float32))
        scene_box = SceneBox(aabb=torch.tensor([[-20, -20, -35], [20, 20, 5]], dtype=torch.float32))        

        cameras = Cameras(
            camera_to_worlds=camera_to_world,
            fx=fx, #focal_length,
            fy=fy, #focal_length,
            cx=cx,
            cy=cy,
            #width=int(self.width),
            #height=int(self.height),
            camera_type=CameraType.PERSPECTIVE,
            times=times,
        )

        dataparser_outputs = DataparserOutputs(
            image_filenames=image_filenames,
            #mask_filenames=mask_filenames,
            metadata={"time_masks":mask_filenames},
            cameras=cameras,
            alpha_color=alpha_color_tensor,
            scene_box=scene_box,
            dataparser_scale=self.scale_factor,
        )

        return dataparser_outputs
