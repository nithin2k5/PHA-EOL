"""Drawing overlays: regions, cable mask, labelled stripes, rejected candidates."""

from __future__ import annotations

import cv2
import numpy as np

from .colors import lab_to_bgr
from .detection import Measurement
from .evaluation import CAMERA_ERROR, FAIL, INDETERMINATE, NO_CABLE, PASS

STATUS_BGR = {
    PASS: (60, 170, 40),
    FAIL: (40, 40, 220),
    INDETERMINATE: (0, 170, 245),
    NO_CABLE: (150, 150, 150),
    CAMERA_ERROR: (160, 60, 160),
}
FONT = cv2.FONT_HERSHEY_SIMPLEX


def _rect(img, rect, colour, label=None, thickness=2):
    if rect is None:
        return
    x, y, w, h = [int(v) for v in rect]
    cv2.rectangle(img, (x, y), (x + w, y + h), colour, thickness, cv2.LINE_AA)
    if label:
        cv2.putText(img, label, (x + 3, max(14, y - 6)), FONT, 0.5, colour, 1, cv2.LINE_AA)


def _label(img, text, org, colour, scale=0.5):
    x, y = int(org[0]), int(org[1])
    (tw, th), _ = cv2.getTextSize(text, FONT, scale, 1)
    cv2.rectangle(img, (x - 2, y - th - 3), (x + tw + 2, y + 3), (0, 0, 0), -1)
    cv2.putText(img, text, (x, y), FONT, scale, colour, 1, cv2.LINE_AA)


def draw_regions(img: np.ndarray, roi=None, region=None) -> np.ndarray:
    _rect(img, roi, (255, 200, 0), "ROI")
    _rect(img, region, (255, 0, 255), "stripes")
    return img


def annotate(frame: np.ndarray, m: Measurement | None, status: str | None = None,
             roi=None, region=None) -> np.ndarray:
    img = frame.copy()
    draw_regions(img, roi, region)
    if m is not None:
        if m.cable_polygon is not None:
            cv2.polylines(img, [m.cable_polygon.astype(np.int32)], True, (0, 255, 255), 1, cv2.LINE_AA)
        for st in m.stripes:
            ok = st.colour_ok
            col = (0, 220, 0) if ok else (0, 0, 255) if ok is False else (0, 200, 255)
            cv2.polylines(img, [st.polygon.astype(np.int32)], True, col, 2, cv2.LINE_AA)
            top = st.polygon[:, 1].min()
            cx = st.polygon[:, 0].mean()
            _label(img, str(st.index), (cx - 4, top - 6), col)
    if status:
        col = STATUS_BGR.get(status, (200, 200, 200))
        cv2.rectangle(img, (0, 0), (img.shape[1], 34), col, -1)
        cv2.putText(img, status, (10, 25), FONT, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
    return img


def magnified_view(m: Measurement | None, target_w: int = 640, target_h: int = 220) -> np.ndarray | None:
    """Straightened, enlarged stripe region with labels for every stripe and rejected candidate."""
    if m is None or m.straight_view is None or m.straight_view.size == 0:
        return None
    src = m.straight_view
    sh, sw = src.shape[:2]
    scale = min(target_w / sw, (target_h - 72) / sh)
    scale = max(scale, 1.0)
    big = cv2.resize(src, (int(sw * scale), int(sh * scale)), interpolation=cv2.INTER_NEAREST)
    canvas = np.full((big.shape[0] + 72, max(big.shape[1], 10), 3), 30, np.uint8)
    canvas[:big.shape[0], :big.shape[1]] = big
    ox, oy = m.straight_offset
    y_top = (m.edge_top - oy) * scale
    y_bot = (m.edge_bottom - oy) * scale
    cv2.line(canvas, (0, int(y_top)), (big.shape[1], int(y_top)), (0, 255, 255), 1)
    cv2.line(canvas, (0, int(y_bot)), (big.shape[1], int(y_bot)), (0, 255, 255), 1)
    for c in m.rejected:
        x0, x1 = (c.start - ox) * scale, (c.end - ox) * scale
        cv2.rectangle(canvas, (int(x0), int(y_top)), (int(x1), int(y_bot)), (130, 130, 130), 1)
    for st in m.stripes:
        x0, x1 = (st.start - ox) * scale, (st.end - ox) * scale
        ok = st.colour_ok
        col = (0, 220, 0) if ok else (0, 0, 255) if ok is False else (0, 200, 255)
        cv2.rectangle(canvas, (int(x0), int(y_top) - 2), (int(x1), int(y_bot) + 2), col, 2)
        _label(canvas, str(st.index), ((x0 + x1) / 2 - 4, max(14, y_top - 6)), col)
        sw_px = lab_to_bgr(st.lab)
        yb = big.shape[0] + 6
        cv2.rectangle(canvas, (int(x0), yb), (int(x1) + 1, yb + 12), sw_px, -1)
        cv2.rectangle(canvas, (int(x0), yb), (int(x1) + 1, yb + 12), col, 1)
        stagger = 0 if st.index % 2 else 24
        for k, txt in enumerate((st.colour_name or "?", f"{st.width_px:.0f}px")):
            cv2.putText(canvas, txt, (int(x0) - 4, yb + 26 + stagger + 11 * k), FONT, 0.36,
                        (230, 230, 230), 1, cv2.LINE_AA)
    return canvas
