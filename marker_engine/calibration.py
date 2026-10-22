"""'Teach from good sample': build colour references and geometry from known-good cables.

Two ways to collect samples, which can be combined:

* Manual patches - the operator drags rectangles over clean areas of the
  cable body and of the stripes.  Saturated pixels and outliers (print,
  dirt) inside a patch are discarded.
* Auto sample  - run the detector on a good cable whose stripe count is
  correct and take its body/stripe pixels and geometry.

Each patch/frame is kept as a separate sample so the tolerance reflects the
variation *between* samples.  Use several cables and positions for teaching,
and keep those images separate from the images used to evaluate accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .colors import ColourReference, bgr_to_lab, delta_e_2000, robust_lab
from .detection import detect
from .geometry import clamp_rect, crop

BODY, STRIPE = "body", "stripe"
MIN_PATCH_PIXELS = 30
MIN_BODY_STRIPE_DE = 15.0


def patch_pixels(frame: np.ndarray, rect, sat_level: int = 245) -> np.ndarray:
    """Clean Lab pixels from a rectangle (image coords): no saturation, no outliers."""
    h, w = frame.shape[:2]
    rect = clamp_rect(rect, w, h)
    bgr = crop(frame, rect).reshape(-1, 3)
    if len(bgr) == 0:
        return np.empty((0, 3), np.float32)
    bgr = bgr[bgr.max(axis=1) < sat_level]
    if len(bgr) < MIN_PATCH_PIXELS:
        return np.empty((0, 3), np.float32)
    lab = bgr_to_lab(bgr)
    med, _ = robust_lab(lab)
    d = delta_e_2000(lab, med)
    mad = np.median(np.abs(d - np.median(d))) * 1.4826
    keep = d <= np.median(d) + 3.0 * max(mad, 1.0)
    return lab[keep]


@dataclass
class TeachSession:
    body_samples: list[np.ndarray] = field(default_factory=list)
    stripe_samples: list[np.ndarray] = field(default_factory=list)
    cable_widths: list[float] = field(default_factory=list)
    spacings: list[float] = field(default_factory=list)
    stripe_widths: list[float] = field(default_factory=list)
    background_l: list[float] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    # -- collecting ------------------------------------------------------------
    def add_patch(self, frame: np.ndarray, rect, kind: str) -> int:
        px = patch_pixels(frame, rect)
        if len(px) < MIN_PATCH_PIXELS:
            self.log.append(f"{kind} patch rejected: too few clean pixels")
            return 0
        (self.body_samples if kind == BODY else self.stripe_samples).append(px)
        med = robust_lab(px)[0].round(1).tolist()
        self.log.append(f"{kind} patch: {len(px)} px, Lab {med}")
        return len(px)

    def add_auto(self, frame: np.ndarray, recipe) -> tuple[bool, str]:
        """Sample a good cable automatically using the detector."""
        m = detect(frame, recipe, expected_width=None)
        if not m.reliable:
            msg = "auto sample rejected: " + ("; ".join(m.issues) or m.presence)
            self.log.append(msg)
            return False, msg
        if len(m.stripes) != recipe.stripe_count:
            msg = (f"auto sample rejected: found {len(m.stripes)} stripes, "
                   f"recipe expects {recipe.stripe_count}")
            self.log.append(msg)
            return False, msg
        if m.body_sample is not None and len(m.body_sample) >= MIN_PATCH_PIXELS:
            self.body_samples.append(m.body_sample)
        for st in m.stripes:
            if st.pixels is not None and len(st.pixels) >= 10 and not st.saturated:
                self.stripe_samples.append(st.pixels)
            self.stripe_widths.append(st.width_px)
        self.cable_widths.append(m.cable_width_px)
        if m.background_l is not None:
            self.background_l.append(m.background_l)
        if len(m.stripes) >= 2:
            self.spacings.extend(np.diff([s.centre_px for s in m.stripes]).tolist())
        msg = (f"auto sample: width {m.cable_width_px:.1f}px, {len(m.stripes)} stripes, "
               f"angle {m.angle_deg:.1f} deg")
        self.log.append(msg)
        return True, msg

    def clear(self) -> None:
        self.__init__()

    @property
    def counts(self) -> tuple[int, int]:
        return len(self.body_samples), len(self.stripe_samples)

    # -- building ----------------------------------------------------------------
    def build(self, recipe, tol_scale: float = 1.0) -> list[str]:
        """Write references/geometry into ``recipe``.  Returns warnings."""
        warnings: list[str] = []
        if not self.body_samples or not self.stripe_samples:
            raise ValueError("need at least one body sample and one stripe sample")
        body = ColourReference.from_samples(recipe.body_colour, self.body_samples, tol_scale)
        stripe = ColourReference.from_samples(recipe.stripe_colour, self.stripe_samples, tol_scale)
        de = float(delta_e_2000(body.lab, stripe.lab))
        if de < MIN_BODY_STRIPE_DE:
            warnings.append(f"body and stripe colours are very similar (dE {de:.1f}); "
                            "stripes may not be detectable")
        if len(self.body_samples) < 3 or len(self.stripe_samples) < 3:
            warnings.append("few samples - teach from several cables/positions for robust tolerances")
        recipe.body_ref = body.to_dict()
        recipe.stripe_ref = stripe.to_dict()
        if self.cable_widths:
            recipe.cable_width_px = float(np.median(self.cable_widths))
        if self.background_l:
            recipe.lighting_ref_l = round(float(np.median(self.background_l)), 1)
        if self.spacings:
            recipe.geometry["expected_spacing_px"] = round(float(np.median(self.spacings)), 1)
        if self.stripe_widths:
            recipe.geometry["expected_width_px"] = round(float(np.median(self.stripe_widths)), 1)
        return warnings


def preset_from_patches(name: str, frame: np.ndarray, rects, tol_scale: float = 1.0) -> ColourReference:
    """Calibrate a named colour preset from one or more patches on the live image."""
    samples = [p for p in (patch_pixels(frame, r) for r in rects) if len(p) >= MIN_PATCH_PIXELS]
    if not samples:
        raise ValueError("no clean pixels in the selected patches")
    return ColourReference.from_samples(name, samples, tol_scale)
