"""Camera access on a background thread.

Sources:
* ``OpenCVSource`` - live USB camera (index) or a video file (development).
* ``ImageSource``  - a still image replayed as a stream (development).

``CameraWorker`` owns the source, publishes frames to a ``FrameStore`` and
reports state changes on an event queue.  Every frame gets an increasing
sequence number so the inspection cycle can insist on *fresh* frames, and a
lost camera is reported explicitly instead of silently re-using old images.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


@dataclass
class Frame:
    image: np.ndarray
    seq: int
    timestamp: float
    source: str


class FrameStore:
    """Latest-frame holder shared between capture, processing and UI threads."""

    def __init__(self):
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._seq = 0
        self.camera_ok = False
        self.error: str | None = None

    def put(self, image: np.ndarray, source: str) -> Frame:
        with self._cond:
            self._seq += 1
            self._frame = Frame(image, self._seq, time.time(), source)
            self.camera_ok = True
            self.error = None
            self._cond.notify_all()
            return self._frame

    def set_error(self, msg: str) -> None:
        with self._cond:
            self.camera_ok = False
            self.error = msg
            self._cond.notify_all()

    def clear(self) -> None:
        with self._cond:
            self._frame = None
            self.camera_ok = False
            self._cond.notify_all()

    def latest(self) -> Frame | None:
        with self._cond:
            return self._frame

    def wait_newer(self, seq: int, timeout: float) -> Frame | None:
        """Block until a frame newer than ``seq`` arrives (None on timeout/error)."""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                if self._frame is not None and self._frame.seq > seq and self.camera_ok:
                    return self._frame
                remaining = deadline - time.time()
                if remaining <= 0 or (not self.camera_ok and self.error):
                    return None
                self._cond.wait(remaining)


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

class OpenCVSource:
    def __init__(self, target: int | str, settings: dict | None = None, loop_video: bool = True):
        self.target = target
        self.settings = settings or {}
        self.loop_video = loop_video
        self.cap: cv2.VideoCapture | None = None
        self.is_live = isinstance(target, int)
        self.name = f"USB camera {target}" if self.is_live else Path(str(target)).name
        self.frame_interval = 0.0

    def open(self) -> None:
        if self.is_live:
            backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY
            self.cap = cv2.VideoCapture(int(self.target), backend)
        else:
            self.cap = cv2.VideoCapture(str(self.target))
        if not self.cap.isOpened():
            raise IOError(f"cannot open {self.name}")
        if self.is_live:
            apply_camera_settings(self.cap, self.settings)
        else:
            fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
            self.frame_interval = 1.0 / max(1.0, min(fps, 60.0))

    def read(self) -> np.ndarray | None:
        if self.cap is None:
            return None
        ok, img = self.cap.read()
        if not ok and not self.is_live and self.loop_video:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, img = self.cap.read()
        return img if ok else None

    def release(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class ImageSource:
    """A still image served repeatedly, like a stationary camera."""

    def __init__(self, image: np.ndarray | str, name: str | None = None, fps: float = 10.0):
        self._path = None if isinstance(image, np.ndarray) else str(image)
        self._image = image if isinstance(image, np.ndarray) else None
        self.name = name or (Path(self._path).name if self._path else "image")
        self.is_live = False
        self.frame_interval = 1.0 / fps

    def open(self) -> None:
        if self._image is None:
            data = np.fromfile(self._path, dtype=np.uint8)  # handles non-ASCII paths on Windows
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None:
                raise IOError(f"cannot read image {self._path}")
            self._image = img

    def read(self) -> np.ndarray | None:
        return None if self._image is None else self._image.copy()

    def release(self) -> None:
        pass


def make_source(target, settings: dict | None = None):
    """Build a source from a camera index, image path or video path."""
    if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
        return OpenCVSource(int(target), settings)
    if Path(str(target)).suffix.lower() in IMAGE_EXTS:
        return ImageSource(str(target))
    return OpenCVSource(str(target))


def apply_camera_settings(cap: cv2.VideoCapture, s: dict) -> None:
    """Best effort - UVC cameras/drivers support different subsets of properties."""
    def setp(prop, val):
        if val is not None:
            cap.set(prop, float(val))

    setp(cv2.CAP_PROP_FRAME_WIDTH, s.get("width"))
    setp(cv2.CAP_PROP_FRAME_HEIGHT, s.get("height"))
    setp(cv2.CAP_PROP_FPS, s.get("fps"))
    if s.get("auto_exposure", True):
        setp(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)  # DirectShow convention: 0.75 auto, 0.25 manual
    else:
        setp(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
        setp(cv2.CAP_PROP_EXPOSURE, s.get("exposure"))
    if s.get("auto_focus", True):
        setp(cv2.CAP_PROP_AUTOFOCUS, 1)
    else:
        setp(cv2.CAP_PROP_AUTOFOCUS, 0)
        setp(cv2.CAP_PROP_FOCUS, s.get("focus"))
    if s.get("auto_white_balance", True):
        setp(cv2.CAP_PROP_AUTO_WB, 1)
    else:
        setp(cv2.CAP_PROP_AUTO_WB, 0)
        setp(cv2.CAP_PROP_WB_TEMPERATURE, s.get("white_balance"))
    setp(cv2.CAP_PROP_GAIN, s.get("gain"))


# ---------------------------------------------------------------------------
# worker thread
# ---------------------------------------------------------------------------

class CameraWorker(threading.Thread):
    """Reads frames continuously; reconnects a lost live camera."""

    def __init__(self, source, store: FrameStore, events: queue.Queue | None = None,
                 reconnect_s: float = 1.0, max_failures: int = 5):
        super().__init__(daemon=True, name="camera")
        self.source = source
        self.store = store
        self.events = events or queue.Queue()
        self.reconnect_s = reconnect_s
        self.max_failures = max_failures
        self._halt = threading.Event()

    def _emit(self, kind: str, msg: str) -> None:
        try:
            self.events.put_nowait((kind, msg))
        except queue.Full:
            pass

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        opened = False
        failures = 0
        while not self._halt.is_set():
            if not opened:
                try:
                    self.source.open()
                    opened = True
                    failures = 0
                    self._emit("camera", f"connected: {self.source.name}")
                except Exception as exc:  # noqa: BLE001 - report any driver error
                    self.store.set_error(str(exc))
                    self._emit("camera_error", str(exc))
                    if self._halt.wait(self.reconnect_s):
                        break
                    continue
            t0 = time.time()
            try:
                img = self.source.read()
            except Exception as exc:  # noqa: BLE001
                img = None
                self._emit("camera_error", f"read error: {exc}")
            if img is None:
                failures += 1
                if failures >= self.max_failures:
                    self.store.set_error(f"camera lost: {self.source.name}")
                    self._emit("camera_error", f"camera lost: {self.source.name}")
                    self.source.release()
                    opened = False
                    if self._halt.wait(self.reconnect_s):
                        break
                else:
                    time.sleep(0.02)
                continue
            failures = 0
            self.store.put(img, self.source.name)
            wait = getattr(self.source, "frame_interval", 0.0) - (time.time() - t0)
            if wait > 0 and self._halt.wait(wait):
                break
        self.source.release()
        self.store.clear()
        self._emit("camera", "stopped")
