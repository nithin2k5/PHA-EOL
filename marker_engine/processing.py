"""Processing thread: detection + evaluation + inspection cycle, off the UI thread.

The UI talks to this thread only through queues:

* commands in : ``("trigger",)``, ``("auto", bool)``, ``("recipe", Recipe)``,
  ``("recipes", [Recipe])``, ``("identify", bool)``, ``("stop",)``
* messages out: ``("frame", image, measurement, verdict)``, ``("result", CycleResult)``,
  ``("phase", str)``, ``("setup_error", str)``, ``("camera_error", str)``

Tk widgets are only touched by the UI thread, which drains the output
queue with ``after()``.
"""

from __future__ import annotations

import queue
import threading
import time

from .camera import FrameStore
from .cycle import ACQUIRING, FrameResult, InspectionCycle
from .detection import SetupError, detect
from .evaluation import apply_identification, evaluate


class InspectionProcessor(threading.Thread):
    def __init__(self, store: FrameStore, presets, out_queue: queue.Queue,
                 preview_fps: float = 15.0):
        super().__init__(daemon=True, name="processing")
        self.store = store
        self.presets = presets
        self.out = out_queue
        self.cmd: queue.Queue = queue.Queue()
        self.cycle = InspectionCycle()
        self.recipe = None
        self.recipes: list = []
        self.identify = False
        self.preview_interval = 1.0 / preview_fps
        self._halt = threading.Event()
        self._last_seq = 0
        self._last_preview = 0.0
        self._camera_was_ok = False
        self._expect_restart = False
        self._last_phase = None

    # -- API used by the UI thread -------------------------------------------
    def send(self, *command) -> None:
        self.cmd.put(command)

    def stop(self) -> None:
        self._halt.set()
        self.cmd.put(("stop",))

    # -- helpers ------------------------------------------------------------------
    def _post(self, *msg) -> None:
        # The UI drains the whole queue each tick and renders only the newest frame.
        self.out.put(msg)

    def _handle_commands(self) -> None:
        while True:
            try:
                c = self.cmd.get_nowait()
            except queue.Empty:
                return
            kind = c[0]
            if kind == "trigger":
                if self.recipe is None or not self.recipe.has_regions:
                    self._post("setup_error", "select a recipe and draw ROI + stripe region first")
                elif not self.store.camera_ok:
                    self._post("result", self.cycle.on_camera_error(
                        self.store.error or "camera not running", time.time()))
                else:
                    self.cycle.trigger(time.time(), self._last_seq, "manual")
            elif kind == "auto":
                self.cycle.set_auto(c[1])
            elif kind == "recipe":
                self.recipe = c[1]
                if self.recipe is not None:
                    self.cycle.configure(self.recipe.cycle)
                    self.cycle.set_recipe(self.recipe.name, self.recipe.version)
            elif kind == "recipes":
                self.recipes = list(c[1])
            elif kind == "identify":
                self.identify = bool(c[1])
            elif kind == "presets":
                self.presets = c[1]
            elif kind == "source_change":
                # deliberate switch to another image/video: not a camera failure
                self._expect_restart = True
                self.cycle.reset()

    def _emit_phase(self) -> None:
        if self.cycle.phase != self._last_phase:
            self._last_phase = self.cycle.phase
            self._post("phase", self.cycle.phase)

    def _finish(self, res) -> None:
        if res is None:
            return
        if self.identify and res.frame is not None and self.recipes:
            apply_identification(res.verdict, res.frame.measurement, self.recipes, self.presets)
        self._post("result", res)

    # -- main loop ------------------------------------------------------------------
    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self._step()
            except Exception as exc:  # noqa: BLE001 - never let the inspection thread die silently
                self._post("setup_error", f"internal error: {exc!r}")
                time.sleep(0.2)

    def _step(self) -> None:
        self._handle_commands()
        now = time.time()

        if not self.store.camera_ok:
            if self._expect_restart:
                time.sleep(0.02)
                return
            if self._camera_was_ok or self.cycle.phase == ACQUIRING:
                msg = self.store.error or "camera stopped"
                self._post("camera_error", msg)
                self._post("result", self.cycle.on_camera_error(msg, now))
            self._camera_was_ok = False
            self._emit_phase()
            time.sleep(0.05)
            return
        self._camera_was_ok = True
        self._expect_restart = False

        frame = self.store.wait_newer(self._last_seq, timeout=0.1)
        self._finish(self.cycle.tick(time.time()))
        if frame is None:
            self._emit_phase()
            return
        self._last_seq = frame.seq
        recipe = self.recipe
        m = v = None
        if recipe is not None and recipe.has_regions:
            try:
                m = detect(frame.image, recipe)
                v = evaluate(m, recipe, self.presets)
            except SetupError as exc:
                self._post("setup_error", str(exc))
            except Exception as exc:  # noqa: BLE001 - keep the line running, report it
                # the frame is not used; a running cycle ends INDETERMINATE on timeout
                self._post("setup_error", f"processing error: {exc!r}")
                m = v = None
            if m is not None and v is not None:
                fr = FrameResult(frame.seq, frame.timestamp, m, v, frame.image)
                self._finish(self.cycle.on_frame(fr, time.time()))
        self._emit_phase()
        if time.time() - self._last_preview >= self.preview_interval:
            self._last_preview = time.time()
            self._post("frame", frame.image, m, v)
