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


# ── Picture controls ────────────────────────────────────────────────────────
# The camera's own saturation, exposure and white balance, set on the device
# each time it is opened. Only camera 2 has them: its stripes are judged by
# colour, and a camera left on auto washes pale stripes out and shifts their
# tint from one part to the next.

TUNED_SOURCES = ("cam2",)
PICTURE_CONTROLS = ("saturation", "exposure", "white_balance")
_AUTO = "auto"


def load_picture_controls(source: str) -> dict:
    """The picture controls saved for a camera source, {} if none are.

    Each of PICTURE_CONTROLS is a number for the camera, or None: exposure
    and white balance left to the camera's auto, saturation as the camera
    has it.
    """
    cfg = configparser.ConfigParser()
    if os.path.exists(_CAM_CFG_PATH):
        cfg.read(_CAM_CFG_PATH)
    controls = {}
    for name in PICTURE_CONTROLS:
        value = cfg.get("CAMERA", f"{source}_{name}", fallback=None)
        if value is None:
            continue
        try:
            controls[name] = None if value == _AUTO else float(value)
        except ValueError:
            controls[name] = None
    return controls


def save_picture_controls(source: str, controls: dict):
    """Store a camera source's picture controls, as load_picture_controls() gives them."""
    cfg = configparser.ConfigParser()
    if os.path.exists(_CAM_CFG_PATH):
        cfg.read(_CAM_CFG_PATH)
    if not cfg.has_section("CAMERA"):
        cfg.add_section("CAMERA")
    for name in PICTURE_CONTROLS:
        value = controls.get(name)
        cfg.set("CAMERA", f"{source}_{name}", _AUTO if value is None else "%g" % value)
    with open(_CAM_CFG_PATH, "w") as f:
        cfg.write(f)


def picture_controls_for(index: int) -> dict:
    """The picture controls for the device at `index`, {} if it is no tuned camera."""
    for source in TUNED_SOURCES:
        if load_camera_config(source)[0] == index:
            return load_picture_controls(source)
    return {}
