"""Inspection cycle state machine: one latched result per cable/trigger.

Rules implemented here:

* A cycle starts on a manual trigger, or in automatic mode when a cable has
  been present for ``presence_frames`` consecutive frames.
* Starting a cycle clears the previous result.
* Only frames captured *after* the trigger are used, and the result is
  finalised from ``frames_required`` consecutive stable frames (cable not
  moving, presence unchanged).  Disagreement between those frames yields
  INDETERMINATE.
* No stable set within ``cycle_timeout_s`` -> INDETERMINATE (timeout).
* The result is latched until the next cycle.  Automatic mode then waits for
  the cable to be removed (``removal_frames`` absent frames) before it can
  start again.
* A camera error aborts a running cycle and replaces any latched result with
  CAMERA ERROR, so an earlier PASS is never shown after the camera was lost.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from .config import DEFAULT_CYCLE
from .detection import ABSENT, Measurement
from .evaluation import CAMERA_ERROR, INDETERMINATE, NO_CABLE, Verdict

IDLE = "IDLE"                    # manual mode, waiting for a trigger
WAIT_CABLE = "WAIT FOR CABLE"    # auto mode armed
ACQUIRING = "ACQUIRING"          # collecting fresh stable frames
WAIT_REMOVAL = "WAIT FOR REMOVAL"  # auto mode, result latched, cable must leave
CAMERA_DOWN = "CAMERA ERROR"


@dataclass
class FrameResult:
    seq: int
    timestamp: float
    measurement: Measurement
    verdict: Verdict
    image: np.ndarray | None = None


@dataclass
class CycleResult:
    cycle_id: str
    trigger: str
    started: float
    finished: float
    verdict: Verdict
    frames_used: int = 0
    frame: FrameResult | None = None          # representative frame (for display/export)
    frame_statuses: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.verdict.status


class InspectionCycle:
    def __init__(self, cfg: dict | None = None):
        self.cfg = dict(DEFAULT_CYCLE)
        self.cfg.update(cfg or {})
        self.auto = False
        self.phase = IDLE
        self.latched: CycleResult | None = None
        self._counter = 0
        self._buffer: list[FrameResult] = []
        self._start_t = 0.0
        self._start_seq = 0
        self._trigger = "manual"
        self._present_run = 0
        self._absent_run = 0
        self._recipe = ("", 0)

    # -- configuration -----------------------------------------------------
    def configure(self, cfg: dict) -> None:
        self.cfg.update(cfg or {})

    def set_recipe(self, name: str, version: int) -> None:
        self._recipe = (name, version)

    def set_auto(self, on: bool) -> None:
        self.auto = bool(on)
        if self.phase == ACQUIRING:
            return
        if self.auto:
            # a cable already inspected must be removed first
            self.phase = WAIT_REMOVAL if self.latched is not None else WAIT_CABLE
        else:
            self.phase = IDLE
        self._present_run = self._absent_run = 0

    def reset(self) -> None:
        """Forget the latched result (operator switched to a different image/source)."""
        self.latched = None
        self._buffer = []
        self.phase = WAIT_CABLE if self.auto else IDLE
        self._present_run = self._absent_run = 0

    # -- triggers -------------------------------------------------------------
    def trigger(self, now: float, last_seq: int, source: str = "manual") -> None:
        """Start a new cycle; frames up to ``last_seq`` are considered stale."""
        self.latched = None
        self._buffer = []
        self._start_t = now
        self._start_seq = last_seq
        self._trigger = source
        self.phase = ACQUIRING

    @property
    def busy(self) -> bool:
        return self.phase == ACQUIRING

    # -- inputs -------------------------------------------------------------------
    def on_frame(self, fr: FrameResult, now: float) -> CycleResult | None:
        if self.phase == CAMERA_DOWN:
            # camera is back: a fresh inspection is required
            self.phase = WAIT_CABLE if self.auto else IDLE
            self._present_run = self._absent_run = 0
        absent = fr.measurement.presence == ABSENT

        if self.phase == WAIT_CABLE:
            self._present_run = 0 if absent else self._present_run + 1
            if self._present_run >= self.cfg["presence_frames"]:
                self.trigger(now, fr.seq - 1, "auto")
            else:
                return None
        elif self.phase == WAIT_REMOVAL:
            self._absent_run = self._absent_run + 1 if absent else 0
            if self._absent_run >= self.cfg["removal_frames"]:
                self.phase = WAIT_CABLE
                self._present_run = 0
            return None

        if self.phase != ACQUIRING or fr.seq <= self._start_seq:
            return None
        if now - self._start_t > self.cfg["cycle_timeout_s"]:
            return self._timeout(now)
        if self._buffer and not self._stable(self._buffer[-1], fr):
            self._buffer = []
        self._buffer.append(fr)
        if len(self._buffer) >= self.cfg["frames_required"]:
            return self._finalise(now)
        return None

    def tick(self, now: float) -> CycleResult | None:
        """Call periodically so a cycle times out even if no frames arrive."""
        if self.phase == ACQUIRING and now - self._start_t > self.cfg["cycle_timeout_s"]:
            return self._timeout(now)
        return None

    def on_camera_error(self, msg: str, now: float) -> CycleResult:
        v = Verdict(status=CAMERA_ERROR, reasons=[msg or "camera error"],
                    recipe_name=self._recipe[0], recipe_version=self._recipe[1])
        res = self._make_result(v, now, frames=[])
        self.phase = CAMERA_DOWN
        self._buffer = []
        return res

    # -- internals ----------------------------------------------------------------
    def _stable(self, a: FrameResult, b: FrameResult) -> bool:
        ma, mb = a.measurement, b.measurement
        if ma.presence != mb.presence:
            return False
        if mb.presence == ABSENT:
            return True
        da = abs(ma.angle_deg - mb.angle_deg)
        dy = max(abs(ma.edge_top - mb.edge_top), abs(ma.edge_bottom - mb.edge_bottom))
        if da > self.cfg["stability_deg"] or dy > self.cfg["stability_px"]:
            return False
        if len(ma.stripes) == len(mb.stripes) and ma.stripes:
            shift = max(abs(sa.centre_px - sb.centre_px) for sa, sb in zip(ma.stripes, mb.stripes))
            if shift > self.cfg["stability_px"]:
                return False
        return True

    def _new_id(self) -> str:
        self._counter += 1
        return f"{datetime.now():%Y%m%d-%H%M%S}-{self._counter:04d}"

    def _make_result(self, v: Verdict, now: float, frames: list[FrameResult]) -> CycleResult:
        rep = frames[len(frames) // 2] if frames else None
        res = CycleResult(cycle_id=self._new_id(), trigger=self._trigger, started=self._start_t or now,
                          finished=now, verdict=v, frames_used=len(frames), frame=rep,
                          frame_statuses=[f.verdict.status for f in frames])
        self.latched = res
        return res

    def _after_result(self, status: str) -> None:
        if self.auto:
            self.phase = WAIT_CABLE if status == NO_CABLE else WAIT_REMOVAL
        else:
            self.phase = IDLE
        self._present_run = self._absent_run = 0

    def _timeout(self, now: float) -> CycleResult:
        v = Verdict(status=INDETERMINATE,
                    reasons=[f"timeout: no {self.cfg['frames_required']} consecutive stable frames "
                             f"within {self.cfg['cycle_timeout_s']:.1f}s (cable moving or camera stalled)"],
                    recipe_name=self._recipe[0], recipe_version=self._recipe[1])
        frames = self._buffer
        res = self._make_result(v, now, frames)
        self._buffer = []
        self._after_result(v.status)
        return res

    def _finalise(self, now: float) -> CycleResult:
        frames = self._buffer
        statuses = [f.verdict.status for f in frames]
        counts = Counter(statuses)
        rep = frames[len(frames) // 2]
        if len(counts) == 1:
            v = rep.verdict
        else:
            detail = ", ".join(f"{n} x {s}" for s, n in counts.most_common())
            v = Verdict(status=INDETERMINATE, reasons=[f"inconsistent results across frames ({detail})"]
                        + [r for f in frames for r in f.verdict.reasons if f.verdict.status != "PASS"][:3],
                        recipe_name=rep.verdict.recipe_name, recipe_version=rep.verdict.recipe_version,
                        expected=rep.verdict.expected, observed=rep.verdict.observed,
                        checks=rep.verdict.checks)
        res = self._make_result(v, now, frames)
        self._buffer = []
        self._after_result(v.status)
        return res
