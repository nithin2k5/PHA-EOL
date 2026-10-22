"""
vision_engine/config.py
=======================
Settings both camera checks share, kept beside the application: which device
each camera is and at what size (camera_cfg.ini), and the vision settings
(vision_config.json).
"""
import configparser
import json
import os
from typing import Tuple

# The application folder, where the settings files and taught models live
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VISION_CFG_PATH = os.path.join(ROOT_DIR, "vision_config.json")
_CAM_CFG_PATH = os.path.join(ROOT_DIR, "camera_cfg.ini")

DEFAULT_MATCH_THRESHOLD = 0.75


def _default_config() -> dict:
    return {
        "vision_enabled": True,
        "camera_source": "cam1",
        "match_threshold": DEFAULT_MATCH_THRESHOLD,
        "part_mapping": {},
    }


def load_vision_config() -> dict:
    cfg = _default_config()
    if os.path.exists(_VISION_CFG_PATH):
        try:
            with open(_VISION_CFG_PATH, "r") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    return cfg


def save_vision_config(cfg: dict):
    with open(_VISION_CFG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)
        f.write("\n")


def load_camera_config(source: str) -> Tuple[int, int, int]:
    """(index, width, height) saved for a camera source such as "cam1".

    An index of -1 means no device has been chosen for that source yet.
    """
    cfg = configparser.ConfigParser()
    if os.path.exists(_CAM_CFG_PATH):
        cfg.read(_CAM_CFG_PATH)
    return (
        cfg.getint("CAMERA", f"{source}_index", fallback=-1),
        cfg.getint("CAMERA", f"{source}_width", fallback=640),
        cfg.getint("CAMERA", f"{source}_height", fallback=480),
    )


def save_camera_config(source: str, index: int, width: int, height: int):
    """Store the device and resolution for one camera source.

    Other sources already in the file are kept as they are.
    """
    cfg = configparser.ConfigParser()
    if os.path.exists(_CAM_CFG_PATH):
        cfg.read(_CAM_CFG_PATH)
    if not cfg.has_section("CAMERA"):
        cfg.add_section("CAMERA")
    cfg.set("CAMERA", f"{source}_index", str(int(index)))
    cfg.set("CAMERA", f"{source}_width", str(int(width)))
    cfg.set("CAMERA", f"{source}_height", str(int(height)))
    with open(_CAM_CFG_PATH, "w") as f:
        cfg.write(f)
