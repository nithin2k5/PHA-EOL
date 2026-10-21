"""
vision_engine/enhance.py
========================
Evens out the light in a camera frame before it is taught or judged.

Light on the line changes through the day, and a part sitting in shadow or
glare at one end looks unlike the good part it was taught from. Contrast is
equalised tile by tile on the lightness alone (CLAHE), so dim areas are lifted
and bright ones held back while colours keep their hue.

A part taught from enhanced frames must be judged on enhanced frames, and one
taught before enhancement on plain ones, so each model records which it was.
"""
import cv2
import numpy as np

# Saved in a model taught from enhanced frames
MODEL_KEY = "enhanced"

# How far contrast may be raised in a tile; higher lifts dim areas more but
# also lifts sensor noise
CLIP_LIMIT = 2.0
TILES = (8, 8)


def enhance(frame: np.ndarray) -> np.ndarray:
    """A copy of a BGR (or grey) frame with its light evened out."""
    clahe = cv2.createCLAHE(clipLimit=CLIP_LIMIT, tileGridSize=TILES)
    if frame.ndim == 2:
        return clahe.apply(frame)
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    lab[..., 0] = clahe.apply(lab[..., 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def for_model(frame: np.ndarray, model: dict) -> np.ndarray:
    """The frame as a model expects it: enhanced if it was taught that way."""
    return enhance(frame) if model.get(MODEL_KEY) else frame
