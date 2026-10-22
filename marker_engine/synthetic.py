"""Synthetic fixture scenes for development, unit tests and the evaluation harness.

The scenes imitate the reference setup: a straight cable with transverse
stripes lying in a metal fixture, with other wires, reflections and printed
characters nearby.  They are NOT a substitute for real images - accuracy
must be validated on images from the production camera (see README).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

COLOURS_BGR = {
    "black": (24, 24, 26),
    "grey": (128, 128, 128),
    "white": (232, 232, 230),
    "red": (40, 38, 200),
    "orange": (30, 130, 240),
    "yellow": (40, 215, 225),
    "green": (60, 160, 50),
    "blue": (185, 85, 30),
    "violet": (160, 50, 120),
    "brown": (40, 70, 120),
}


@dataclass
class SceneSpec:
    width: int = 1280
    height: int = 720
    body: str = "black"
    stripe: str = "white"
    stripe_colours: list[str] | None = None  # per-stripe override
    stripe_count: int = 3
    stripe_width: int = 9
    stripe_spacing: int = 42
    stripe_offset: float = 0.0     # shift of the stripe group along the axis (px)
    cable_width: int = 64
    angle_deg: float = 0.0
    shift: tuple[int, int] = (0, 0)
    text: bool = True              # printed characters on the cable
    text_in_region: bool = False   # put text inside the stripe inspection region
    glare: float = 0.0             # 0..1 strength of a saturating reflection on the cable
    occlusion: bool = False        # object crossing the cable inside the region
    no_cable: bool = False
    blur: float = 0.0              # gaussian sigma
    noise: float = 3.0
    brightness: float = 1.0        # global illumination factor
    seed: int = 0
    partial_stripe: bool = False   # one extra stripe cut by the region border
    extra_lines: int = 2           # other wires in the scene


@dataclass
class Scene:
    image: np.ndarray
    roi: tuple[int, int, int, int]
    inspection_region: tuple[int, int, int, int]
    stripe_centres: list[float] = field(default_factory=list)
    spec: SceneSpec | None = None


def _background(spec: SceneSpec, rng: np.random.Generator) -> np.ndarray:
    h, w = spec.height, spec.width
    base = np.linspace(95, 150, w, dtype=np.float32)[None, :].repeat(h, 0)
    base += np.linspace(-15, 15, h, dtype=np.float32)[:, None]
    brushed = cv2.GaussianBlur(rng.normal(0, 18, (h, w)).astype(np.float32), (61, 1), 0)
    img = np.dstack([base + brushed + 4, base + brushed + 2, base + brushed])
    # metal fixture clamps with bright edges
    for x0 in (int(w * 0.12), int(w * 0.80)):
        cv2.rectangle(img, (x0, int(h * 0.28)), (x0 + 110, int(h * 0.72)), (165, 168, 170), -1)
        cv2.rectangle(img, (x0 + 8, int(h * 0.30)), (x0 + 16, int(h * 0.70)), (250, 250, 250), -1)
        cv2.putText(img, "F-%d" % (x0 % 97), (x0 + 25, int(h * 0.36)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (40, 40, 40), 2, cv2.LINE_AA)
    # other wires
    wire_cols = [(30, 30, 190), (40, 160, 40), (200, 120, 30), (30, 200, 220)]
    for i in range(spec.extra_lines):
        y0 = int(h * (0.08 + 0.1 * i))
        cv2.line(img, (0, y0), (w, y0 + int(h * 0.12)), wire_cols[i % 4], 14, cv2.LINE_AA)
        y1 = int(h * (0.92 - 0.08 * i))
        cv2.line(img, (0, y1), (w, y1 - int(h * 0.05)), wire_cols[(i + 2) % 4], 12, cv2.LINE_AA)
    cv2.putText(img, "LOT 2417  STN 3", (int(w * 0.35), int(h * 0.2)),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (30, 30, 30), 2, cv2.LINE_AA)
    return img


def _shading_profile(n: int) -> np.ndarray:
    t = np.linspace(-1, 1, n, dtype=np.float32)
    return 0.62 + 0.38 * np.sqrt(np.clip(1 - t**2, 0, 1))


def render(spec: SceneSpec) -> Scene:
    rng = np.random.default_rng(spec.seed)
    h, w = spec.height, spec.width
    img = _background(spec, rng)
    cx, cy = w / 2 + spec.shift[0], h / 2 + spec.shift[1]
    cw = spec.cable_width
    length = int(w * 0.75)
    group = (spec.stripe_count - 1) * spec.stripe_spacing
    centres_local = [length / 2 - group / 2 + i * spec.stripe_spacing + spec.stripe_offset
                     for i in range(spec.stripe_count)]

    if not spec.no_cable:
        layer = np.zeros((cw, length, 3), np.float32)
        layer[:] = COLOURS_BGR[spec.body]
        stripe_cols = spec.stripe_colours or [spec.stripe] * spec.stripe_count
        all_centres = list(centres_local)
        if spec.partial_stripe:
            half_region = max(group / 2 + spec.stripe_spacing * 1.3, 120)
            all_centres.append(length / 2 + half_region - 2)
            stripe_cols = list(stripe_cols) + [spec.stripe]
        for c, name in zip(all_centres, stripe_cols):
            x0 = int(round(c - spec.stripe_width / 2))
            layer[:, max(0, x0):x0 + spec.stripe_width] = COLOURS_BGR[name]
        if spec.text:
            tx = int(length / 2 + group / 2 + 50) if not spec.text_in_region else int(length / 2 - 18)
            if spec.text_in_region:
                # characters printed between stripes and next to the group
                for k, s in enumerate(("2", "5", "AWG")):
                    px = int(centres_local[0] + 12 + k * spec.stripe_spacing) if k < 2 else int(centres_local[-1] + 20)
                    cv2.putText(layer, s, (px, int(cw * 0.66)), cv2.FONT_HERSHEY_SIMPLEX,
                                cw / 120.0, (150, 150, 150), 1, cv2.LINE_AA)
            else:
                cv2.putText(layer, "UL 1007 22AWG 80C", (tx, int(cw * 0.66)), cv2.FONT_HERSHEY_SIMPLEX,
                            cw / 110.0, (150, 150, 150), 1, cv2.LINE_AA)
                cv2.putText(layer, "UL 1007", (20, int(cw * 0.66)), cv2.FONT_HERSHEY_SIMPLEX,
                            cw / 110.0, (150, 150, 150), 1, cv2.LINE_AA)
        layer *= _shading_profile(cw)[:, None, None]
        # longitudinal specular highlight typical of a round cable
        hl_row = int(cw * 0.3)
        hl = np.exp(-((np.arange(cw) - hl_row) ** 2) / (2 * (cw * 0.05) ** 2)).astype(np.float32)
        layer += (35.0 + 220.0 * spec.glare) * hl[:, None, None]
        if spec.glare > 0.5:
            gx = int(length / 2 - 10)
            cv2.ellipse(layer, (gx, int(cw * 0.45)), (int(group * 0.7) + 30, int(cw * 0.45)), 0, 0, 360,
                        (255, 255, 255), -1)
        # composite rotated layer
        m = cv2.getRotationMatrix2D((length / 2, cw / 2), 0, 1.0)
        m[0, 2] += cx - length / 2
        m[1, 2] += cy - cw / 2
        rot = cv2.getRotationMatrix2D((cx, cy), spec.angle_deg, 1.0)
        full = np.vstack([rot, [0, 0, 1]]) @ np.vstack([m, [0, 0, 1]])
        warped = cv2.warpAffine(layer, full[:2], (w, h), flags=cv2.INTER_LINEAR)
        mask = cv2.warpAffine(np.ones((cw, length), np.float32), full[:2], (w, h), flags=cv2.INTER_LINEAR)
        img = img * (1 - mask[..., None]) + warped * mask[..., None]
    if spec.occlusion:
        ox = int(cx - group / 2 + spec.stripe_spacing * 0.5)
        pts = np.array([[ox - 14, int(cy - cw * 2.5)], [ox + 14, int(cy - cw * 2.5)],
                        [ox + 18, int(cy + cw * 0.3)], [ox - 18, int(cy + cw * 0.3)]], np.int32)
        cv2.fillConvexPoly(img, pts, (150, 175, 215), cv2.LINE_AA)

    img = img * spec.brightness
    if spec.noise > 0:
        img = img + rng.normal(0, spec.noise, img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if spec.blur > 0:
        img = cv2.GaussianBlur(img, (0, 0), spec.blur)

    # regions as an operator would draw them for the nominal (unrotated) position
    ncx, ncy = w / 2, h / 2
    roi = (int(ncx - length * 0.4), int(ncy - cw * 1.6), int(length * 0.8), int(cw * 3.2))
    half = max(group / 2 + spec.stripe_spacing * 1.3, 120)
    insp = (int(ncx - half), int(ncy - cw * 1.0), int(2 * half), int(cw * 2.0))
    centres_img = [cx - length / 2 + c for c in centres_local] if not spec.no_cable else []
    return Scene(img, roi, insp, centres_img, spec)


def make(**kwargs) -> Scene:
    return render(SceneSpec(**kwargs))
