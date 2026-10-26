"""
vision_engine/camera.py
=======================
Single owner per physical camera device.

A camera device accepts only one cv2.VideoCapture at a time, so the live
preview and the inspection path must share one stream rather than each opening
the device. Consumers acquire a reference-counted stream by index; the device is
opened on the first acquire and released when the last reference goes away.

Cameras are opened through Media Foundation, as the Windows Camera app opens
them: on the same camera it delivers twice DirectShow's frame rate at 1280x720,
so a moving part blurs less. DirectShow is kept for a device Media Foundation
can't open.

A camera with picture controls saved (camera 2, see config.py) has them set
on the device as it opens, and Camera Configuration changes them on the live
stream while the operator watches the picture.
"""
import threading
import time
from typing import Optional, Tuple

import cv2
import numpy as np

from .config import picture_controls_for

_REGISTRY_LOCK = threading.Lock()
_STREAMS: dict = {}

# ~1s of failed reads at the 20ms retry interval.
_MAX_CONSECUTIVE_READ_FAILURES = 50

# A camera that stops giving frames is closed and opened again, this many
# times in a row before the stream gives up on it
_MAX_REOPENS = 3

# Frames thrown away after a reopen, while auto-exposure settles again
_SETTLE_AFTER_REOPEN = 10

# Tried in this order (see the module docstring)
_BACKENDS = (cv2.CAP_MSMF, cv2.CAP_DSHOW)

# Frame sizes a camera may offer; probe() finds which it really delivers
SIZES = [(320, 240), (640, 480), (800, 600), (1024, 768), (1280, 720), (1280, 960),
         (1600, 1200), (1920, 1080), (2560, 1440), (3840, 2160)]


def open_capture(index: int, width: int = 0, height: int = 0):
    """An opened cv2.VideoCapture for `index`, asked for this frame size, or
    None if no backend opens the device."""
    for api in _BACKENDS:
        cap = cv2.VideoCapture(index, api)
        if not cap.isOpened():
            cap.release()
            continue
        if api == cv2.CAP_DSHOW:
            # DirectShow sends uncompressed frames unless told otherwise, and
            # many cameras send only small ones uncompressed over USB
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if width and height:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        return cap
    return None


# The camera properties behind each picture control (see config.py)
_PROPS = {"saturation": cv2.CAP_PROP_SATURATION,
          "exposure": cv2.CAP_PROP_EXPOSURE,
          "white_balance": cv2.CAP_PROP_WB_TEMPERATURE}


def apply_controls(cap, controls: dict):
    """Set picture controls on an open capture, best effort: cameras offer
    different ones. None leaves exposure and white balance to the camera's
    auto, and saturation as it is."""
    if "saturation" in controls and controls["saturation"] is not None:
        cap.set(cv2.CAP_PROP_SATURATION, float(controls["saturation"]))
    if "exposure" in controls:
        if controls["exposure"] is None:
            # DirectShow's convention, which Media Foundation reads as on/off
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
        else:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
            cap.set(cv2.CAP_PROP_EXPOSURE, float(controls["exposure"]))
    if "white_balance" in controls:
        if controls["white_balance"] is None:
            cap.set(cv2.CAP_PROP_AUTO_WB, 1)
        else:
            cap.set(cv2.CAP_PROP_AUTO_WB, 0)
            cap.set(cv2.CAP_PROP_WB_TEMPERATURE, float(controls["white_balance"]))


def read_controls(cap) -> dict:
    """The picture controls as the camera reports them."""
    seen = {}
    for name, prop in _PROPS.items():
        try:
            seen[name] = cap.get(prop)
        except cv2.error:
            pass
    return seen


def probe(index: int) -> Optional[dict]:
    """What a camera delivers: {"index", "sizes"}, the SIZES it gives frames
    at, smallest first. None if it gives no frame at all.

    A camera asked for a size it lacks keeps its old one, and in Media
    Foundation a capture left that way fails on the next size change. So
    after each size refused the device is opened afresh.
    """
    cap = open_capture(index)
    if cap is None:
        return None
    sizes = []
    try:
        ok, _ = cap.read()
        if not ok:
            return None
        for w, h in SIZES:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            if (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))) == (w, h):
                ok, frame = cap.read()
                if ok and frame.shape[1::-1] == (w, h):
                    sizes.append((w, h))
            else:
                cap.release()
                cap = open_capture(index)
                if cap is None:
                    break
    finally:
        if cap is not None:
            cap.release()
    return {"index": index, "sizes": sizes} if sizes else None


class CameraStream:
    """Background reader for one camera index. Serves the most recent frame."""

    def __init__(self, index: int, width: int, height: int):
        self.index = index
        self._width = width
        self._height = height
        self._cap = None
        self._frame = None
        self._frames_read = 0
        self._frame_lock = threading.Lock()
        self._running = False
        self._refs = 0
        self._opened = threading.Event()
        self._open_ok = False
        self._thread = None
        # Picture controls waiting to be set, set by the reader between
        # frames: a capture mustn't be touched from two threads at once
        self._controls_lock = threading.Lock()
        self._pending = None
        self._controls_at_open = {}
        self._controls_seen = {}
        # Every control set on the live stream, set again after a reopen
        self._controls_set = {}

    # ── lifecycle ───────────────────────────────────────────────────────────

    def _start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        cap = open_capture(self.index, self._width, self._height)
        if cap is not None:
            self._cap = cap
            self._open_ok = True
            self._controls_at_open = read_controls(cap)
            saved = picture_controls_for(self.index)
            if saved:
                apply_controls(cap, saved)
            self._controls_seen = read_controls(cap)
        else:
            self._running = False
        self._opened.set()

        # A device can stop delivering frames (a USB stall, power saving,
        # another program taking it) or never deliver any (unplugged, held
        # by another process). It is opened afresh a few times, then given
        # up on rather than let a reader block until its timeout - an
        # inspection stalling the test cycle is worse than a fast "camera
        # unavailable".
        consecutive_failures = 0
        reopens = 0
        skip = 0
        while self._running:
            self._apply_pending()
            ret, frame = self._cap.read()
            if ret:
                consecutive_failures = 0
                if skip:
                    skip -= 1
                    continue
                reopens = 0
                with self._frame_lock:
                    self._frame = frame
                    self._frames_read += 1
            else:
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_READ_FAILURES:
                    if reopens >= _MAX_REOPENS or not self._reopen():
                        print(f"[CAMERA] Camera {self.index} gives no frames; giving up on it")
                        self._running = False
                        break
                    reopens += 1
                    consecutive_failures = 0
                    skip = _SETTLE_AFTER_REOPEN
                    continue
                time.sleep(0.02)

        if self._cap is not None:
            # Controls asked for as the stream closes, e.g. put back on cancel
            self._apply_pending()
            self._cap.release()
            self._cap = None

    def _reopen(self) -> bool:
        """Close the device and open it again, with its picture controls."""
        print(f"[CAMERA] Camera {self.index} stopped giving frames; reopening it")
        self._cap.release()
        self._cap = None
        time.sleep(0.5)
        if not self._running:
            return False
        cap = open_capture(self.index, self._width, self._height)
        if cap is None:
            return False
        self._cap = cap
        controls = {**picture_controls_for(self.index), **self._controls_set}
        if controls:
            apply_controls(cap, controls)
        self._controls_seen = read_controls(cap)
        return True

    def _apply_pending(self):
        with self._controls_lock:
            controls, self._pending = self._pending, None
        if controls is not None and self._cap is not None:
            apply_controls(self._cap, controls)
            self._controls_set.update(controls)
            self._controls_seen = read_controls(self._cap)

    # ── picture controls ────────────────────────────────────────────────────

    def set_controls(self, controls: dict):
        """Set picture controls (see apply_controls) on the device, between frames."""
        with self._controls_lock:
            self._pending = {**(self._pending or {}), **controls}

    def controls(self) -> dict:
        """The picture controls as the camera last reported them."""
        return dict(self._controls_seen)

    def controls_at_open(self) -> dict:
        """The picture controls the camera had before any were set on it."""
        return dict(self._controls_at_open)

    def wait_until_open(self, timeout: float = 5.0) -> bool:
        self._opened.wait(timeout)
        return self._open_ok

    def is_alive(self) -> bool:
        """False once the reader has given up on the device."""
        return self._running

    def release(self):
        with _REGISTRY_LOCK:
            self._refs -= 1
            if self._refs > 0:
                return
            _STREAMS.pop(self.index, None)
        self._running = False
        # Block until this device's cv2.VideoCapture is actually closed before
        # returning. A device tolerates only one capture at a time -- without
        # this, a caller that immediately re-acquires the same index (a new
        # CameraStream, on a new thread) can open a second cv2.VideoCapture
        # while this one is still mid-read or mid-release, which is a known way
        # to hard-crash (segfault) the process rather than raise a catchable
        # Python exception.
        # A reopen under way takes longer to finish.
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # ── frames ──────────────────────────────────────────────────────────────

    def latest(self) -> Optional[np.ndarray]:
        """Most recent frame without waiting, or None if none captured yet."""
        with self._frame_lock:
            return self._frame.copy() if self._frame is not None else None

    def read(self, timeout: float = 5.0, settle_frames: int = 30) -> Optional[np.ndarray]:
        """Newest frame, once the device has produced at least `settle_frames`.

        The settle count lets auto-exposure stabilise on a stream that has just
        been opened; an already-running preview satisfies it immediately because
        the counter is cumulative over the stream's life. 30 frames is ~1s at
        30fps -- judging a cold device sooner than that matches a template
        against a still-darkening frame and scores it far below what the same
        part scores once exposure settles.
        """
        if not self.wait_until_open(timeout):
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._frame_lock:
                if self._frames_read >= settle_frames and self._frame is not None:
                    return self._frame.copy()
            if not self._running:
                return None
            time.sleep(0.02)
        with self._frame_lock:
            return self._frame.copy() if self._frame is not None else None

    def read_after(self, count: int, timeout: float = 2.0) -> Tuple[Optional[np.ndarray], int]:
        """The first frame read after the `count`-th, and its number.

        Gives (None, count) if none arrives in `timeout`. Number 0 asks for
        the next frame once the device has settled, as read() does, so a
        caller can collect several fresh frames one after another.
        """
        if not self.wait_until_open(timeout):
            return None, count
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._frame_lock:
                if self._frame is not None and self._frames_read > count \
                        and self._frames_read >= 30:
                    return self._frame.copy(), self._frames_read
            if not self._running:
                break
            time.sleep(0.005)
        return None, count


def acquire(index: int, width: int = 640, height: int = 480) -> Optional[CameraStream]:
    """Reference-counted handle to the stream for `index`. Call release() when done."""
    if index < 0:
        return None
    with _REGISTRY_LOCK:
        stream = _STREAMS.get(index)
        if stream is None:
            stream = CameraStream(index, width, height)
            _STREAMS[index] = stream
            stream._refs = 1
            stream._start()
        else:
            stream._refs += 1
    return stream


def is_live(index: int) -> bool:
    with _REGISTRY_LOCK:
        return index in _STREAMS


def grab(index: int, width: int = 640, height: int = 480,
         timeout: float = 5.0) -> Optional[np.ndarray]:
    """One frame from `index`, reusing a live stream if one is already open."""
    stream = acquire(index, width, height)
    if stream is None:
        return None
    try:
        return stream.read(timeout=timeout)
    finally:
        stream.release()
