"""Colour space conversion and perceptual colour differences.

All colour comparison is done in CIE L*a*b* (L 0..100, a/b roughly -128..127),
never on raw RGB values or hue alone.  Lab separates lightness from chroma so
black/grey/white cables (which have no meaningful hue) can be handled
separately from chromatic colours.
"""

from __future__ import annotations

import cv2
import numpy as np


def bgr_to_lab(bgr: np.ndarray) -> np.ndarray:
    """Convert a uint8 BGR image (or Nx3 pixel array) to float32 Lab."""
    arr = np.asarray(bgr)
    shape = arr.shape
    img = arr.reshape(-1, 1, 3) if arr.ndim == 2 else arr
    lab = cv2.cvtColor(img.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
    return lab.reshape(shape).astype(np.float32)


def lab_to_bgr(lab) -> tuple[int, int, int]:
    """Convert a single Lab triple to a displayable BGR uint8 tuple."""
    px = np.array(lab, dtype=np.float32).reshape(1, 1, 3)
    bgr = cv2.cvtColor(px, cv2.COLOR_LAB2BGR)[0, 0]
    return tuple(int(v) for v in np.clip(bgr * 255.0 + 0.5, 0, 255))


def chroma(lab: np.ndarray) -> np.ndarray:
    lab = np.asarray(lab, dtype=np.float32)
    return np.hypot(lab[..., 1], lab[..., 2])


def hue_deg(lab: np.ndarray) -> np.ndarray:
    lab = np.asarray(lab, dtype=np.float32)
    return np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) % 360.0


def delta_e_2000(lab1, lab2) -> np.ndarray:
    """Vectorised CIEDE2000 colour difference.  Inputs broadcast (..., 3)."""
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]

    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cbar = (C1 + C2) / 2.0
    G = 0.5 * (1 - np.sqrt(Cbar**7 / (Cbar**7 + 25.0**7)))
    a1p = (1 + G) * a1
    a2p = (1 + G) * a2
    C1p = np.hypot(a1p, b1)
    C2p = np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360.0
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360.0

    dLp = L2 - L1
    dCp = C2p - C1p
    dhp = h2p - h1p
    dhp = np.where(dhp > 180, dhp - 360, dhp)
    dhp = np.where(dhp < -180, dhp + 360, dhp)
    dhp = np.where(C1p * C2p == 0, 0.0, dhp)
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp) / 2.0)

    Lbarp = (L1 + L2) / 2.0
    Cbarp = (C1p + C2p) / 2.0
    hsum = h1p + h2p
    hbarp = np.where(np.abs(h1p - h2p) > 180, (hsum + 360) / 2.0, hsum / 2.0)
    hbarp = np.where(C1p * C2p == 0, hsum, hbarp)

    T = (1 - 0.17 * np.cos(np.radians(hbarp - 30))
         + 0.24 * np.cos(np.radians(2 * hbarp))
         + 0.32 * np.cos(np.radians(3 * hbarp + 6))
         - 0.20 * np.cos(np.radians(4 * hbarp - 63)))
    dtheta = 30 * np.exp(-(((hbarp - 275) / 25.0) ** 2))
    Rc = 2 * np.sqrt(Cbarp**7 / (Cbarp**7 + 25.0**7))
    Sl = 1 + (0.015 * (Lbarp - 50) ** 2) / np.sqrt(20 + (Lbarp - 50) ** 2)
    Sc = 1 + 0.045 * Cbarp
    Sh = 1 + 0.015 * Cbarp * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc

    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2
                   + Rt * (dCp / Sc) * (dHp / Sh))


def robust_lab(pixels_lab: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Median Lab and per-channel MAD (scaled to sigma) of an Nx3 pixel set."""
    px = np.asarray(pixels_lab, dtype=np.float32).reshape(-1, 3)
    if px.size == 0:
        raise ValueError("no pixels")
    med = np.median(px, axis=0)
    mad = np.median(np.abs(px - med), axis=0) * 1.4826
    return med, mad


# ---------------------------------------------------------------------------
# Colour references
# ---------------------------------------------------------------------------

ACHROMATIC_CHROMA = 9.0     # Lab chroma below which a colour is treated as black/grey/white
MIN_TOL_CHROMATIC = 8.0     # minimum CIEDE2000 tolerance for chromatic colours
MIN_TOL_LIGHTNESS = 12.0    # minimum lightness tolerance for achromatic colours
PALE_CHROMA = 25.0          # chromatic colours below this chroma get an extra hue check


class ColourReference:
    """A named reference colour with a tolerance.

    ``distance()`` returns a normalised score: <= 1.0 means "matches".

    * Achromatic references (black, grey, white, silver) are compared by
      lightness, and the sample must also be nearly colourless.  Hue is
      meaningless for these colours and is ignored.
    * Chromatic references use CIEDE2000.  Pale colours (low chroma, e.g.
      pale yellow vs white) additionally require the hue angle to be close,
      since a small CIEDE2000 distance can hide a different hue at low chroma.
    """

    def __init__(self, name: str, lab, tol: float = 12.0, achromatic: bool | None = None,
                 tol_l: float = 14.0, max_chroma: float | None = None,
                 hue_tol_deg: float | None = None):
        self.name = name
        self.lab = np.array(lab, dtype=np.float32)
        c = float(chroma(self.lab))
        self.achromatic = (c < ACHROMATIC_CHROMA) if achromatic is None else bool(achromatic)
        self.tol = float(tol)
        self.tol_l = float(tol_l)
        self.max_chroma = float(max_chroma) if max_chroma is not None else max(12.0, c + 8.0)
        if hue_tol_deg is None and not self.achromatic and c < PALE_CHROMA:
            hue_tol_deg = 30.0
        self.hue_tol_deg = hue_tol_deg

    # -- comparison --------------------------------------------------------
    def distance(self, lab) -> float:
        lab = np.asarray(lab, dtype=np.float32)
        c = float(chroma(lab))
        if self.achromatic:
            d_l = abs(float(lab[0]) - float(self.lab[0])) / self.tol_l
            d_c = c / self.max_chroma
            return max(d_l, d_c)
        d = float(delta_e_2000(lab, self.lab)) / self.tol
        if c < ACHROMATIC_CHROMA * 0.6:
            # A colourless sample cannot be this chromatic colour.
            d = max(d, 1.0 + (ACHROMATIC_CHROMA - c) / ACHROMATIC_CHROMA)
        if self.hue_tol_deg is not None and c >= ACHROMATIC_CHROMA * 0.6:
            dh = abs(float(hue_deg(lab)) - float(hue_deg(self.lab)))
            dh = min(dh, 360.0 - dh)
            d = max(d, dh / self.hue_tol_deg * 0.999)
        return d

    def matches(self, lab) -> bool:
        return self.distance(lab) <= 1.0

    # -- construction -------------------------------------------------------
    @classmethod
    def from_samples(cls, name: str, samples: list[np.ndarray],
                     tol_scale: float = 1.0) -> "ColourReference":
        """Build a reference from one or more pixel sets (each Nx3 Lab).

        Each element of ``samples`` is one patch/frame.  The reference is the
        median of all pixels; the tolerance covers both the variation between
        samples (lighting drift, cable batches) and within-patch spread.
        """
        sets = [np.asarray(s, dtype=np.float32).reshape(-1, 3) for s in samples if len(s)]
        if not sets:
            raise ValueError("no samples for colour " + name)
        all_px = np.concatenate(sets)
        med, _ = robust_lab(all_px)
        sample_meds = np.array([robust_lab(s)[0] for s in sets])
        c = float(chroma(med))
        achromatic = c < ACHROMATIC_CHROMA
        if achromatic:
            between = float(np.max(np.abs(sample_meds[:, 0] - med[0]))) if len(sets) > 1 else 0.0
            within = float(np.percentile(np.abs(all_px[:, 0] - med[0]), 90))
            tol_l = max(MIN_TOL_LIGHTNESS, 2.5 * between, min(0.3 * within, 15.0)) * tol_scale
            c_px = np.percentile(chroma(all_px), 90)
            max_chroma = max(12.0, float(c_px) + 6.0)
            return cls(name, med, tol=MIN_TOL_CHROMATIC, achromatic=True,
                       tol_l=tol_l, max_chroma=max_chroma)
        de_between = delta_e_2000(sample_meds, med)
        between = float(np.max(de_between)) if len(sets) > 1 else 0.0
        within = float(np.percentile(delta_e_2000(all_px, med), 90))
        tol = max(MIN_TOL_CHROMATIC, 2.5 * between, min(0.3 * within, 15.0)) * tol_scale
        return cls(name, med, tol=tol, achromatic=False)

    # -- serialisation ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lab": [round(float(v), 2) for v in self.lab],
            "tol": round(self.tol, 2),
            "achromatic": self.achromatic,
            "tol_l": round(self.tol_l, 2),
            "max_chroma": round(self.max_chroma, 2),
            "hue_tol_deg": self.hue_tol_deg,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ColourReference":
        return cls(d["name"], d["lab"], tol=d.get("tol", 12.0), achromatic=d.get("achromatic"),
                   tol_l=d.get("tol_l", 14.0), max_chroma=d.get("max_chroma"),
                   hue_tol_deg=d.get("hue_tol_deg"))

    def display_bgr(self) -> tuple[int, int, int]:
        return lab_to_bgr(self.lab)

    def __repr__(self) -> str:
        kind = "achromatic" if self.achromatic else "chromatic"
        return f"ColourReference({self.name!r}, lab={self.lab.round(1).tolist()}, {kind})"


def rank_colours(lab, refs) -> list[tuple[str, float]]:
    """Return (name, normalised distance) for every reference, best first."""
    scored = [(r.name, r.distance(lab)) for r in refs]
    return sorted(scored, key=lambda t: t[1])


def nearest_colour_name(lab, refs, ambiguity_ratio: float = 0.85) -> tuple[str, float]:
    """Best-matching name, or 'unknown' / 'A|B' when no or several references match."""
    ranked = rank_colours(lab, refs)
    if not ranked or ranked[0][1] > 1.0:
        return "unknown", ranked[0][1] if ranked else float("inf")
    if len(ranked) > 1 and ranked[1][1] <= 1.0 and ranked[0][1] / max(ranked[1][1], 1e-6) > ambiguity_ratio:
        return f"{ranked[0][0]}|{ranked[1][0]}", ranked[0][1]
    return ranked[0][0], ranked[0][1]
