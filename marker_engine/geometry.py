"""Coordinate mapping between the resized preview and the original image,
plus rectangle and rotation helpers."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class ViewTransform:
    """Letterboxed fit of an image (img_w x img_h) into a view (view_w x view_h).

    The preview keeps the aspect ratio, so the image is scaled by ``scale`` and
    offset by (``ox``, ``oy``).  All ROI selections made on the preview must go
    through ``view_to_image`` so they refer to original-resolution pixels.
    """
    img_w: int
    img_h: int
    view_w: int
    view_h: int

    @property
    def scale(self) -> float:
        return min(self.view_w / self.img_w, self.view_h / self.img_h)

    @property
    def disp_size(self) -> tuple[int, int]:
        return (max(1, int(round(self.img_w * self.scale))),
                max(1, int(round(self.img_h * self.scale))))

    @property
    def offset(self) -> tuple[int, int]:
        dw, dh = self.disp_size
        return (self.view_w - dw) // 2, (self.view_h - dh) // 2

    def view_to_image(self, vx: float, vy: float, clamp: bool = True) -> tuple[float, float]:
        ox, oy = self.offset
        x = (vx - ox) / self.scale
        y = (vy - oy) / self.scale
        if clamp:
            x = min(max(x, 0.0), float(self.img_w))
            y = min(max(y, 0.0), float(self.img_h))
        return x, y

    def image_to_view(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self.offset
        return x * self.scale + ox, y * self.scale + oy

    def view_rect_to_image(self, x0, y0, x1, y1) -> Rect:
        ax, ay = self.view_to_image(x0, y0)
        bx, by = self.view_to_image(x1, y1)
        return normalize_rect(ax, ay, bx, by)

    def image_rect_to_view(self, rect: Rect) -> tuple[float, float, float, float]:
        x, y, w, h = rect
        a = self.image_to_view(x, y)
        b = self.image_to_view(x + w, y + h)
        return a[0], a[1], b[0], b[1]


def normalize_rect(x0, y0, x1, y1) -> Rect:
    """Rect from two arbitrary corner points (handles dragging in any direction)."""
    xa, xb = sorted((x0, x1))
    ya, yb = sorted((y0, y1))
    x, y = int(round(xa)), int(round(ya))
    return x, y, max(0, int(round(xb)) - x), max(0, int(round(yb)) - y)


def clamp_rect(rect: Rect, img_w: int, img_h: int) -> Rect:
    x, y, w, h = rect
    x0 = min(max(x, 0), img_w)
    y0 = min(max(y, 0), img_h)
    x1 = min(max(x + w, 0), img_w)
    y1 = min(max(y + h, 0), img_h)
    return x0, y0, x1 - x0, y1 - y0


def scale_rect(rect: Rect, sx: float, sy: float) -> Rect:
    x, y, w, h = rect
    return (int(round(x * sx)), int(round(y * sy)), int(round(w * sx)), int(round(h * sy)))


def adapt_rect_to_image(rect: Rect, drawn_size, img_w: int, img_h: int) -> Rect:
    """Rescale a rect drawn on an image of ``drawn_size`` to the current resolution."""
    if drawn_size and tuple(drawn_size) != (img_w, img_h):
        rect = scale_rect(rect, img_w / drawn_size[0], img_h / drawn_size[1])
    return clamp_rect(rect, img_w, img_h)


def rect_contains(outer: Rect, inner: Rect) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ix >= ox and iy >= oy and ix + iw <= ox + ow and iy + ih <= oy + oh


def crop(img: np.ndarray, rect: Rect) -> np.ndarray:
    x, y, w, h = rect
    return img[y:y + h, x:x + w]


def rotation_matrix(center: tuple[float, float], angle_deg: float) -> np.ndarray:
    """2x3 affine matrix rotating by ``angle_deg`` (counter-clockwise, image y-down)."""
    return cv2.getRotationMatrix2D(center, angle_deg, 1.0)


def rotate_image(img: np.ndarray, angle_deg: float, interp=cv2.INTER_LINEAR) -> np.ndarray:
    h, w = img.shape[:2]
    m = rotation_matrix((w / 2.0, h / 2.0), angle_deg)
    return cv2.warpAffine(img, m, (w, h), flags=interp, borderMode=cv2.BORDER_REPLICATE)


def transform_points(points, m: np.ndarray) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    ones = np.ones((len(pts), 1))
    return np.hstack([pts, ones]) @ np.asarray(m, dtype=np.float64).T


def invert_affine(m: np.ndarray) -> np.ndarray:
    return cv2.invertAffineTransform(np.asarray(m, dtype=np.float64))
