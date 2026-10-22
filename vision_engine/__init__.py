"""
vision_engine
=============
The camera checks, each in its own folder:

  part_inspection/  camera 1: the right part is in the fixture
  stripes/          camera 2: the painted stripes on the cable

and what they share: opening cameras (camera.py), evening out the light
(enhance.py), the pictures kept with each test (captures.py) and the camera
and vision settings (config.py).
"""
import os

# As mainconsole.py sets it, for when the vision engine is loaded first
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

from .config import (
    load_camera_config,
    load_vision_config,
    save_camera_config,
    save_vision_config,
)
from .part_inspection import VisionController, VisionResult

__all__ = [
    "VisionController",
    "VisionResult",
    "load_camera_config",
    "load_vision_config",
    "save_camera_config",
    "save_vision_config",
]
