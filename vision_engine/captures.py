"""
vision_engine/captures.py
=========================
Pictures the cameras took during each test, kept so a result can be looked
at again later on the Work Data page.

Each picture is stored as a JPEG under vision_captures/<date>/, with the
verdict written across its top and any found stripes outlined. The test
record holds its path relative to the application folder, so the folder can
move with the application.
"""
import os
import shutil
from datetime import datetime, timedelta
from typing import List, Optional

import cv2
import numpy as np

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURES_DIR = os.path.join(_ROOT_DIR, "vision_captures")

# Pictures older than this are deleted, so a busy line doesn't fill the disk
KEEP_DAYS = 30

# Stored no wider or taller than this; enough to see the stripes, a fraction
# of the size of a full camera frame
MAX_SIDE = 800

_VERDICT_BGR = {"PASS": (0, 160, 0), "NG": (0, 0, 220), "ERROR": (0, 140, 255)}


def outlined(frame: np.ndarray, outlines: Optional[List[np.ndarray]],
             verdict: str) -> np.ndarray:
    """A copy of `frame` with the shapes drawn in the verdict's colour."""
    img = frame.copy()
    colour = _VERDICT_BGR.get(verdict, (90, 90, 90))
    thick = max(2, round(max(img.shape[:2]) / 300))
    for shape in outlines or []:
        cv2.polylines(img, [np.round(shape).astype(np.int32)], True, colour, thick)
    return img


def save(frame: np.ndarray, camera_name: str, part_number: str, verdict: str,
         detail: str = "", outlines: Optional[List[np.ndarray]] = None) -> Optional[str]:
    """Store one camera's picture of a test. Returns its relative path.

    `outlines` are shapes in frame coordinates, such as the stripes found.
    Returns None if the picture could not be written; a test is never
    failed for want of its picture.
    """
    try:
        img = outlined(frame, outlines, verdict)
        colour = _VERDICT_BGR.get(verdict, (90, 90, 90))

        scale = min(1.0, MAX_SIDE / max(img.shape[:2]))
        if scale < 1.0:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

        # The verdict across the top, so the picture explains itself anywhere
        text = "%s  %s  %s" % (camera_name, verdict or "-", detail)
        bar = max(22, img.shape[0] // 18)
        cv2.rectangle(img, (0, 0), (img.shape[1], bar), colour, -1)
        cv2.putText(img, text.strip(), (8, int(bar * 0.72)), cv2.FONT_HERSHEY_SIMPLEX,
                    bar / 36, (255, 255, 255), max(1, bar // 14), cv2.LINE_AA)

        now = datetime.now()
        folder = os.path.join(CAPTURES_DIR, now.strftime("%Y-%m-%d"))
        os.makedirs(folder, exist_ok=True)
        safe_part = "".join(c if c.isalnum() or c in "-_." else "_" for c in part_number)
        name = "%s_%s_%s.jpg" % (now.strftime("%H%M%S_%f")[:-3], safe_part,
                                 camera_name.lower().replace(" ", ""))
        path = os.path.join(folder, name)
        ok, data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return None
        data.tofile(path)
        return os.path.relpath(path, _ROOT_DIR).replace("\\", "/")
    except Exception as e:
        print(f"[CAPTURE] could not save {camera_name} picture: {e}")
        return None


def full_path(relative: Optional[str]) -> Optional[str]:
    """Where a stored picture is on disk, or None if it is gone."""
    if not relative:
        return None
    path = os.path.join(_ROOT_DIR, relative)
    return path if os.path.isfile(path) else None


def load(relative: Optional[str]) -> Optional[np.ndarray]:
    path = full_path(relative)
    if path is None:
        return None
    try:
        return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        return None


def remove_old(keep_days: int = KEEP_DAYS) -> int:
    """Delete the day folders older than `keep_days`. Returns how many."""
    if not os.path.isdir(CAPTURES_DIR):
        return 0
    oldest = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed = 0
    for name in os.listdir(CAPTURES_DIR):
        path = os.path.join(CAPTURES_DIR, name)
        # Day folders only; their names sort the same way as their dates
        if os.path.isdir(path) and len(name) == 10 and name < oldest:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed
