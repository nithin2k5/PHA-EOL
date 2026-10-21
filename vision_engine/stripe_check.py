"""
vision_engine/stripe_check.py
=============================
Checks the painted stripes on a cable with the second camera ("cam2").

Each part number has one or more groups of painted bands round its cable,
for example three yellow stripes near one end and two red ones near the
other. A check passes when every group is found: the expected number of
stripes, in the expected colour, at the size the good part showed when it
was taught.

Each group is searched for in its own search region, drawn for the part.
A group without one is searched for in camera 2's detection region, or the
whole frame when that isn't drawn either. Inside its region the part need
not sit in a fixed place, and the cable may lie at any angle.

Methodology:
  - Teach: the operator boxes each stripe group on a good part, and may draw
    a search region round it. Each group's colour, count, size and spacing
    are read from its box and saved per part number. The box only says which
    stripes to learn from.
  - Inspect: pixels of the expected colour anywhere in the frame are grouped
    into bands. Bands far from the taught stripe size, such as specks on the
    background or a mat of the same colour, are dropped. Bands lying close
    together form a group, and each group is counted along its own cable
    direction, so stripes that touch still count separately.
  - Result: OK if, for every taught group, some group of bands in its
    region matches its count and length.

Frames are enhanced (see enhance.py) before stripes are read from them, both
when teaching and, for parts taught that way, when inspecting.
"""
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np

from . import camera
from .enhance import MODEL_KEY as ENHANCED, enhance, for_model
from .vision_controller import load_camera_config, load_vision_config, save_vision_config

CAMERA_SOURCE = "cam2"

# vision_config.json key holding the part of camera 2's frame that is searched
ROI_KEY = "cam2_roi"

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
_MODELS_DIR = os.path.join(_ROOT_DIR, "vision_models", "stripes")

COLOURS = ("white", "red", "yellow", "green", "blue", "purple")

# How far a live stripe may differ from the taught one before it counts as
# a different part. Hand-painted bands vary, so this is loose.
SIZE_TOLERANCE = 0.4

# White paint looks grey in poor light, so white also takes pixels clearly
# paler than what lies just round them. "Just round" is this many pixels
# across; anything narrower that is paler than its surroundings, such as a
# stripe, counts.
WHITE_REACH = 31

# Bands belong to one group while each is within this many stripe pitches
# of another
GROUP_LINK = 1.6

# Paint stands out from the cable and background round it; a bright spot
# on a pale bench or a glint on metal barely differs from its surroundings.
# A group must differ by at least this much (CIELAB distance), and by at
# least this share of what the good part showed when taught.
MIN_CONTRAST = 18.0
CONTRAST_SHARE = 0.35


@dataclass
class GroupResult:
    """How one taught stripe group fared."""
    ok: bool
    colour: str
    expected: int                   # stripes taught
    count: int = 0                  # stripes of its colour found
    seen: str = ""                  # e.g. "red x3"
    # The part of the frame searched, (x, y, w, h); None for all of it
    region: Optional[Tuple[int, int, int, int]] = None
    stripe_boxes: List[np.ndarray] = field(default_factory=list)


@dataclass
class StripeResult:
    """Outcome of one stripe check. With several groups, the colour, count
    and seen fields sum them up; `groups` has each one."""
    ok: bool
    judgement: str                  # "OK", "NG", "ERROR"
    part_number: str = ""
    expected_colour: str = ""
    expected_count: int = 0
    count: int = 0                  # stripes of the expected colour found
    seen: str = ""                  # what the frame actually holds, e.g. "red x3"
    processing_time_ms: int = 0
    error: Optional[str] = None
    # Diagnostics for the settings page: where the stripes were, and the
    # frame that was judged.
    stripe_boxes: List[np.ndarray] = field(default_factory=list)
    found_box: Optional[Tuple[int, int, int, int]] = None    # round the group judged
    frame: Optional[np.ndarray] = None
    groups: List[GroupResult] = field(default_factory=list)


# ── Colour masks ────────────────────────────────────────────────────────────

def colour_masks(bgr: np.ndarray) -> dict:
    """One mask per stripe colour. OpenCV hue runs 0-180."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    # Shine on glossy paint keeps its hue but loses saturation, so very bright
    # pixels qualify at a lower saturation. Without this a highlight running
    # along the cable splits every stripe in two.
    shine = (s > 40) & (v > 200)
    vivid = ((s > 90) & (v > 45)) | shine
    # Purple and green paint is less saturated than the rest
    pale = ((s > 55) & (v > 45)) | shine
    # Saturation is unsteady on dark pixels, so grey is judged by how far
    # apart the channels are instead. A top-hat keeps pale detail narrower
    # than its kernel and drops broad pale areas such as a bench.
    chroma = bgr.max(axis=2).astype(np.int16) - bgr.min(axis=2)
    paler = cv2.morphologyEx(v, cv2.MORPH_TOPHAT, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (WHITE_REACH, WHITE_REACH)))
    masks = {
        "white": ((s < 55) & (v > 185)) | ((chroma < 40) & (v > 50) & (paler > 25)),
        "red": vivid & ((h < 9) | (h > 165)),
        "yellow": vivid & (h >= 18) & (h <= 38),
        "green": pale & (h >= 40) & (h <= 88),
        "blue": vivid & (h >= 95) & (h <= 128),
        "purple": pale & (h >= 129) & (h <= 165),
    }
    kernel = np.ones((3, 3), np.uint8)
    return {c: cv2.morphologyEx(m.astype(np.uint8) * 255, cv2.MORPH_OPEN, kernel)
            for c, m in masks.items()}


def masks_in(frame: np.ndarray, box: Tuple[int, int, int, int]) -> dict:
    """colour_masks() of one box of the frame, judged with the frame round it.

    Whether a pixel is paler than its surroundings depends on what lies
    beyond the box too: a pale bench cut down to a sliver by the box edge
    would otherwise look like a stripe.
    """
    x, y, w, h = box
    fh, fw = frame.shape[:2]
    x0, y0 = max(0, x - WHITE_REACH), max(0, y - WHITE_REACH)
    x1, y1 = min(fw, x + w + WHITE_REACH), min(fh, y + h + WHITE_REACH)
    masks = colour_masks(frame[y0:y1, x0:x1])
    return {c: m[y - y0:y - y0 + h, x - x0:x - x0 + w] for c, m in masks.items()}


def _paint_mask(masks: dict) -> np.ndarray:
    """Everything that is coloured paint, grown a little to cover its shine."""
    paint = np.zeros_like(masks["white"])
    for c in COLOURS:
        if c != "white":
            paint |= masks[c]
    return cv2.dilate(paint, np.ones((5, 5), np.uint8))


# ── Bands ───────────────────────────────────────────────────────────────────

def _bands(mask: np.ndarray, min_area: int = 12, max_extent: float = 0.0) -> list:
    """Solid colour patches: their pixels, centre and size across/along.

    Patches touching the edge of the mask are left out. Stripes sit inside
    the box drawn round them, or well inside the frame; a patch running off
    its edge is background, such as a green mat behind green stripes.
    With `max_extent`, patches whose bounding box is longer than that are
    skipped before they are measured, which keeps a whole frame of
    background colour cheap to look through.
    """
    mh, mw = mask.shape[:2]
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_area or x == 0 or y == 0 or x + w >= mw or y + h >= mh:
            continue
        if max_extent and max(w, h) > max_extent:
            continue
        ys, xs = np.nonzero(labels[y:y + h, x:x + w] == i)
        pts = np.column_stack([xs + x, ys + y]).astype(np.float32)
        (cx, cy), (w, h), ang = cv2.minAreaRect(pts)
        # A stripe is a filled band, not a hairline or a scattered smear
        if area / max(1.0, w * h) < 0.45:
            continue
        theta = math.radians(ang) if w >= h else math.radians(ang + 90)
        out.append({
            "pts": pts, "area": int(area), "centre": np.array([cx, cy]),
            "long": max(w, h), "short": max(1.0, min(w, h)),
            "dir": np.array([math.cos(theta), math.sin(theta)]),
            "box": cv2.boxPoints(((cx, cy), (w, h), ang)),
        })
    return out


def _cable_axis(bands: list) -> np.ndarray:
    """Unit vector along the cable, from the stripes themselves.

    Several stripes lie in a line along the cable. A single stripe crosses
    it, so the cable runs along its short side.
    """
    if len(bands) >= 2:
        cs = np.array([b["centre"] for b in bands], np.float32)
        vx, vy, _, _ = cv2.fitLine(cs, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
        axis = np.array([vx, vy])
    else:
        d = bands[0]["dir"]
        axis = np.array([-d[1], d[0]])
    # Fix the sign so a taught axis and a live one compare
    if axis[0] < 0 or (axis[0] == 0 and axis[1] < 0):
        axis = -axis
    return axis / np.linalg.norm(axis)


def _runs(bands: list, axis: np.ndarray) -> list:
    """Stretches along the cable that are painted, as (start, end) in pixels.

    All stripe pixels are projected onto the cable axis. A run is one stripe,
    or several that touch. A shine streak along the cable that splits a stripe
    in two doesn't split its run, because both halves project to the same
    positions.
    """
    pts = np.vstack([b["pts"] for b in bands])
    t = pts @ axis
    bins = np.bincount((t - t.min()).astype(int)).astype(float)
    bins = np.convolve(bins, np.ones(3) / 3, mode="same")
    # A position belongs to a stripe only if paint covers a fair share of the
    # cable's width there, not a stray pixel at an edge
    reach = np.median([b["long"] for b in bands])
    covered = bins >= max(1.5, reach * 0.3)

    runs = []
    start = None
    for i, c in enumerate(np.append(covered, False)):
        if c and start is None:
            start = i
        elif not c and start is not None:
            runs.append((float(start), float(i)))
            start = None
    return runs


def _count(runs: list, stripe_width: float, pitch: float = 0.0) -> Tuple[int, float]:
    """(stripes, group length) from the runs.

    A run of k touching stripes is k stripes plus the k-1 gaps they nearly
    closed, so with the taught pitch (one stripe plus one gap) it holds
    (length + gap) / pitch stripes. Without a pitch, as when teaching, the
    run is measured in stripe widths instead.
    """
    # Slivers much thinner than a stripe are paint edges, not stripes
    runs = [r for r in runs if r[1] - r[0] > stripe_width * 0.35]
    if not runs:
        return 0, 0.0
    if pitch > stripe_width:
        gap = pitch - stripe_width
        sizes = [(e - s + gap) / pitch for s, e in runs]
    else:
        sizes = [(e - s) / stripe_width for s, e in runs]
    count = sum(max(1, int(round(k))) for k in sizes)
    return count, runs[-1][1] - runs[0][0]


# ── Model storage ───────────────────────────────────────────────────────────

def _model_path(part_number: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', part_number)
    return os.path.join(_MODELS_DIR, f"{safe}.json")


def load_model(part_number: str) -> Optional[dict]:
    path = _model_path(part_number)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def groups_of(model: dict) -> List[dict]:
    """The stripe groups of a model.

    A model taught before parts could have several groups is itself the one
    group, with no search region of its own.
    """
    if "groups" in model:
        return list(model["groups"])
    return [dict(model, region=None)] if "colour" in model else []


def describe(model: dict) -> str:
    """The stripes a model expects, e.g. "yellow × 3 + red × 2"."""
    groups = groups_of(model)
    if not groups:
        return "?"
    return " + ".join("%s × %s" % (g.get("colour", "?"), g.get("count", "?"))
                      for g in groups)


def has_model(part_number: str) -> bool:
    return load_model(part_number) is not None


def taught_parts() -> List[str]:
    """Part numbers with a stripe model, as stored in the files.

    Read from inside each file, because a file name has had characters that
    are not allowed in file names replaced.
    """
    if not os.path.isdir(_MODELS_DIR):
        return []
    parts = []
    for name in sorted(os.listdir(_MODELS_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_MODELS_DIR, name), "r") as f:
                pno = json.load(f).get("part_number")
        except (OSError, ValueError):
            continue
        if pno:
            parts.append(pno)
    return parts


def save_model(model: dict):
    os.makedirs(_MODELS_DIR, exist_ok=True)
    with open(_model_path(model["part_number"]), "w") as f:
        json.dump(model, f, indent=4)
        f.write("\n")


def delete_model(part_number: str):
    path = _model_path(part_number)
    if os.path.exists(path):
        os.remove(path)


# ── Camera ──────────────────────────────────────────────────────────────────

def camera_settings() -> Tuple[int, int, int]:
    """(index, width, height) of camera 2; index -1 when none is chosen."""
    return load_camera_config(CAMERA_SOURCE)


def capture_frame() -> Optional[np.ndarray]:
    index, width, height = camera_settings()
    if index < 0:
        return None
    return camera.grab(index, width, height)


def load_roi() -> Optional[dict]:
    """The region of camera 2's frame that is searched, or None for all of it.

    Holds "x", "y", "width", "height" and the "frame_size" it was drawn on.
    """
    roi = load_vision_config().get(ROI_KEY)
    if not roi:
        return None
    try:
        return {"x": int(roi["x"]), "y": int(roi["y"]),
                "width": int(roi["width"]), "height": int(roi["height"]),
                "frame_size": [int(v) for v in roi["frame_size"]]}
    except (KeyError, TypeError, ValueError):
        return None


def save_roi(roi: Optional[dict], frame_size: Tuple[int, int]):
    """Search only this region of camera 2's frame; None searches all of it."""
    cfg = load_vision_config()
    if roi:
        cfg[ROI_KEY] = {k: int(roi[k]) for k in ("x", "y", "width", "height")}
        cfg[ROI_KEY]["frame_size"] = [int(frame_size[0]), int(frame_size[1])]
    else:
        cfg.pop(ROI_KEY, None)
    save_vision_config(cfg)


# ── Analysis ────────────────────────────────────────────────────────────────

def _crop(frame: np.ndarray, roi: dict, margin: float) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    fh, fw = frame.shape[:2]
    mx, my = int(roi["width"] * margin), int(roi["height"] * margin)
    x0 = max(0, roi["x"] - mx)
    y0 = max(0, roi["y"] - my)
    x1 = min(fw, roi["x"] + roi["width"] + mx)
    y1 = min(fh, roi["y"] + roi["height"] + my)
    return frame[y0:y1, x0:x1], (x0, y0, x1 - x0, y1 - y0)


def _colour_bands(masks: dict, colour: str, max_extent: float = 0.0) -> list:
    """Bands of one colour. White ones lying on coloured paint are its shine."""
    mask = masks[colour]
    if colour == "white":
        mask = cv2.bitwise_and(mask, cv2.bitwise_not(_paint_mask(masks)))
    return _bands(mask, max_extent=max_extent)


def dominant_colour(masks: dict) -> Tuple[Optional[str], int]:
    """The stripe colour covering most of a patch, and how many pixels it has.

    `masks` are the patch's colour masks. Only band-shaped patches count, so a
    background colour filling the whole patch, or scattered specks, don't win.
    """
    patch_area = masks["white"].size
    best, best_area = None, 0
    for colour in COLOURS:
        bands = [b for b in _colour_bands(masks, colour) if b["area"] < patch_area * 0.5]
        if not bands:
            continue
        biggest = max(b["area"] for b in bands)
        # Specks next to real stripes are far smaller than them
        area = sum(b["area"] for b in bands if b["area"] >= biggest * 0.25)
        if area > best_area:
            best, best_area = colour, area
    return best, best_area


def read_stripes(frame: np.ndarray, roi: dict, colour: Optional[str] = None,
                 count: Optional[int] = None) -> Tuple[dict, List[np.ndarray]]:
    """Read a good part's stripes from the box, without saving anything.

    Returns the model it would save (less the part number) and the outline
    of each stripe in frame coordinates, so the operator can see what was
    found before saving. `colour` and `count` override what was read from the
    image. Raises ValueError with an operator-readable reason when no stripes
    are found. `frame` is as captured; it is enhanced here.
    """
    if roi["width"] < 8 or roi["height"] < 8:
        raise ValueError("The box is too small. Draw it round the whole stripe group.")
    frame = enhance(frame)
    patch, box = _crop(frame, roi, 0.0)
    x0, y0 = box[0], box[1]
    masks = masks_in(frame, box)

    if colour is None:
        colour, _ = dominant_colour(masks)
        if colour is None:
            raise ValueError("No stripe colour found in the box.")
    elif colour not in COLOURS:
        raise ValueError(f"Unknown stripe colour '{colour}'.")

    bands = _colour_bands(masks, colour)
    if not bands:
        raise ValueError(f"No {colour} stripes found in the box.")
    biggest = max(b["area"] for b in bands)
    # The box is drawn round the stripes, so anything far smaller is a speck
    bands = [b for b in bands if b["area"] >= biggest * 0.25]

    axis = _cable_axis(bands)
    runs = _runs(bands, axis)
    # Most runs are single stripes, so the middle length is one stripe's
    # width. If they all touch, the operator corrects the count.
    width = float(np.median([e - s for s, e in runs]))
    found, span = _count(runs, width)
    if found == 0:
        raise ValueError(f"No {colour} stripes found in the box.")
    count = int(count) if count else found
    if count != found:
        # The operator corrected the count, so the runs weren't single
        # stripes. Share the group out evenly; the gaps can't be told apart
        # from the paint, so they count as part of each stripe.
        width = span / count

    model = {
        "roi": {k: int(roi[k]) for k in ("x", "y", "width", "height")},
        "frame_size": [int(frame.shape[1]), int(frame.shape[0])],
        "colour": colour,
        "count": count,
        "axis": [float(axis[0]), float(axis[1])],
        "stripe_width": width,
        "stripe_length": float(np.median(_stripe_lengths(
            bands, axis, [r for r in runs if r[1] - r[0] > width * 0.35]))),
        "span": span,
        # Start of one stripe to the start of the next
        "pitch": (span - width) / (count - 1) if count > 1 and count == found else 0.0,
        "size_tolerance": SIZE_TOLERANCE,
        "contrast": _contrast(cv2.cvtColor(patch, cv2.COLOR_BGR2LAB).astype(np.float32), bands),
    }
    boxes = [b["box"] + np.array([x0, y0], np.float32) for b in bands]
    return model, boxes


def _contains(outer: dict, inner: dict) -> bool:
    return (outer["x"] <= inner["x"] and outer["y"] <= inner["y"]
            and inner["x"] + inner["width"] <= outer["x"] + outer["width"]
            and inner["y"] + inner["height"] <= outer["y"] + outer["height"])


def read_group(frame: np.ndarray, roi: dict, region: Optional[dict] = None,
               colour: Optional[str] = None,
               count: Optional[int] = None) -> Tuple[dict, List[np.ndarray]]:
    """One stripe group as it would be saved, and its stripes' outlines.

    `region` is where the group is searched for on the line, or None for
    camera 2's detection region. Raises ValueError with an operator-readable
    reason if the group can't be taught.
    """
    if region is not None:
        if region["width"] < 8 or region["height"] < 8:
            raise ValueError("The search region is too small.")
        if not _contains(region, roi):
            raise ValueError("The stripe box must lie inside the search region, "
                             "or the stripes would never be found there.")
    group, boxes = read_stripes(frame, roi, colour, count)
    group.pop("frame_size", None)
    group["region"] = ({k: int(region[k]) for k in ("x", "y", "width", "height")}
                       if region is not None else None)
    return group, boxes


def teach(part_number: str, frame: np.ndarray, groups: List[dict]) -> dict:
    """Read each stripe group of a good part and save them for the part.

    `groups` lists one dict per group: "roi", the box round its stripes, and
    optionally "region", "colour" and "count", as for read_group().
    """
    if not groups:
        raise ValueError("Box at least one group of stripes.")
    taught = []
    for n, g in enumerate(groups, start=1):
        try:
            group, _ = read_group(frame, g["roi"], g.get("region"),
                                  g.get("colour"), g.get("count"))
        except ValueError as e:
            raise (ValueError(f"Group {n}: {e}") if len(groups) > 1 else e)
        taught.append(group)
    model = {
        "part_number": part_number,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "frame_size": [int(frame.shape[1]), int(frame.shape[0])],
        "groups": taught,
        ENHANCED: True,
    }
    save_model(model)
    return model


def _within(value: float, taught: float, tolerance: float) -> bool:
    """`value` is within `tolerance` of `taught`, either way round.

    0.4 accepts 0.6x to 1/0.6 = 1.67x, so a stripe half again as big passes
    as readily as one a third smaller.
    """
    return taught > 0 and (1 - tolerance) <= value / taught <= 1 / (1 - tolerance)


def _groups(bands: list, link: float) -> List[list]:
    """Bands that lie close together, each list one candidate stripe group."""
    n = len(bands)
    parent = list(range(n))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cs = np.array([b["centre"] for b in bands])
    for i in range(n):
        d = np.linalg.norm(cs[i + 1:] - cs[i], axis=1)
        for j in np.nonzero(d <= link)[0]:
            parent[root(i)] = root(i + 1 + j)
    groups = {}
    for i in range(n):
        groups.setdefault(root(i), []).append(bands[i])
    return list(groups.values())


def _axes(group: list, taught: np.ndarray) -> List[np.ndarray]:
    """Directions the cable might run through a group.

    The line through the stripes' centres is usually right, but a shine
    streak that splits each stripe in two adds centres across the cable, and
    a single stripe has no line at all. Across the biggest band, and the
    direction taught, cover those cases.
    """
    biggest = max(group, key=lambda b: b["area"])
    across = np.array([-biggest["dir"][1], biggest["dir"][0]])
    axes = [_cable_axis(group), across, taught]
    out = []
    for a in axes:
        a = a / np.linalg.norm(a)
        if not any(abs(float(a @ b)) > 0.99 for b in out):
            out.append(a)
    return out


def _stripe_lengths(group: list, axis: np.ndarray, runs: list) -> List[float]:
    """How far each stripe reaches across the cable, from all of its paint.

    Measured per stripe rather than per band, so a stripe that a shine streak
    has split into two halves still measures its full length.
    """
    pts = np.vstack([b["pts"] for b in group])
    t = pts @ axis
    t = t - t.min()
    across = pts @ np.array([-axis[1], axis[0]])
    lengths = []
    for s, e in runs:
        a = across[(t >= s) & (t < e)]
        if a.size:
            lengths.append(float(np.percentile(a, 97) - np.percentile(a, 3) + 1))
    return lengths


def _contrast(lab: np.ndarray, group: list) -> float:
    """How far the group's paint colour is from the colour just round it."""
    mask = np.zeros(lab.shape[:2], np.uint8)
    pts = np.vstack([b["pts"] for b in group]).astype(int)
    mask[pts[:, 1], pts[:, 0]] = 255
    kernel = np.ones((3, 3), np.uint8)
    # Skip the blurred edge of the paint, then take a thin ring beyond it
    edge = cv2.dilate(mask, kernel, iterations=2)
    ring = cv2.dilate(mask, kernel, iterations=6) & ~edge
    if not ring.any():
        return 0.0
    paint = np.median(lab[mask > 0], axis=0)
    around = np.median(lab[ring > 0], axis=0)
    return float(np.linalg.norm(paint - around))


def _lines_up(group: list, axis: np.ndarray, model: dict) -> bool:
    """True if a group is laid out like stripes painted round one cable.

    Searching a whole frame turns up scatters of background bits, such as
    marks on a bench or glare on metal, that happen to be the stripes' size
    and number. Real stripes sit one after another on a line along the
    cable, evenly spaced at the taught pitch.

    Each stripe is judged by all of its paint together: a shine streak can
    split one into halves either side of the line, but their middle is still
    on it. A band's own shape says nothing about the cable's direction, as a
    stripe can be wider along the cable than across it.
    """
    tol = model.get("size_tolerance", SIZE_TOLERANCE)
    normal = np.array([-axis[1], axis[0]])
    pts = np.vstack([b["pts"] for b in group])
    t = pts @ axis
    t = t - t.min()
    across = pts @ normal
    runs = [r for r in _runs(group, axis) if r[1] - r[0] > model["stripe_width"] * 0.35]
    if not runs:
        return False

    # Each stripe's middle on one line along the cable
    middles = [float(np.median(across[(t >= s) & (t < e)])) for s, e in runs]
    if max(middles) - min(middles) > model["stripe_length"] * 0.5:
        return False

    # Each stripe reaches as far across the cable as the taught ones
    if not all(_within(n, model["stripe_length"], tol)
               for n in _stripe_lengths(group, axis, runs)):
        return False

    # Evenly spaced at the taught pitch
    pitch = model.get("pitch", 0.0)
    if pitch > 0 and len(runs) == model["count"]:
        starts = [s for s, _ in runs]
        if not all(_within(b - a, pitch, tol) for a, b in zip(starts, starts[1:])):
            return False
    return True


def _judge(masks: dict, lab: np.ndarray, colour: str, model: dict) -> Tuple[int, bool, list]:
    """(count, matches the taught part, bands of the group) for one colour.

    Every group of bands the taught stripe size in the frame is counted; the
    first that matches the taught count and length wins. With none matching,
    the group whose count comes closest is reported.
    """
    tol = model.get("size_tolerance", SIZE_TOLERANCE)
    length = model["stripe_length"]
    width = model["stripe_width"]
    pitch = model.get("pitch", 0.0)
    expected = model["count"]
    # Anything bigger than the whole taught group can't be one of its stripes
    extent = (max(model["span"], length) + width) * 2
    # A band may be a whole stripe or a piece of one split by shine, so only
    # bands too big for a stripe, or mere specks, are dropped here; each
    # stripe's full length is checked once the group is put together
    bands = [b for b in _colour_bands(masks, colour, max_extent=extent)
             if length * (1 - tol) * 0.4 <= b["long"] <= length / (1 - tol)]
    if not bands:
        return 0, False, []

    link = max(pitch, width, length * 0.5) * GROUP_LINK
    min_contrast = max(MIN_CONTRAST, CONTRAST_SHARE * model.get("contrast", 0.0))
    taught_axis = np.array(model["axis"])
    best = None                     # (miss, -area, count, bands)
    for group in _groups(bands, link):
        for axis in _axes(group, taught_axis):
            found, span = _count(_runs(group, axis), width, pitch)
            # One stripe has no group length to compare; its size already matched
            span_ok = expected == 1 or _within(span, model["span"], tol)
            if (found == expected and span_ok and _lines_up(group, axis, model)
                    and _contrast(lab, group) >= min_contrast):
                return found, True, group
            key = (abs(found - expected), -sum(b["area"] for b in group), found, group)
            if best is None or key[:2] < best[:2]:
                best = key
    return best[2], False, best[3]


def set_group_regions(part_number: str, regions: List[Optional[dict]]) -> dict:
    """Give each of a taught part's stripe groups a new search region.

    `regions` has one entry per group, in order; None searches camera 2's
    detection region. Nothing else about the groups changes, so the part is
    not re-taught. Raises ValueError if a region would leave out its group's
    stripes.
    """
    model = load_model(part_number)
    if model is None:
        raise ValueError(f"'{part_number}' has no stripe check.")
    groups = groups_of(model)
    if len(regions) != len(groups):
        raise ValueError(f"'{part_number}' has {len(groups)} stripe groups, "
                         f"not {len(regions)}.")
    for n, (group, region) in enumerate(zip(groups, regions), start=1):
        if region is not None:
            if region["width"] < 8 or region["height"] < 8:
                raise ValueError(f"Group {n}: the area is too small.")
            if not _contains(region, group["roi"]):
                raise ValueError(f"Group {n}: the area must contain the stripes "
                                 f"taught for it, or they would never be found.")
        group["region"] = ({k: int(region[k]) for k in ("x", "y", "width", "height")}
                           if region is not None else None)
    if "groups" not in model:
        # Taught before parts could have several groups: store it the new way
        frame_size = model.pop("frame_size", None)
        model = {"part_number": model.get("part_number", part_number),
                 "created": model.get("created", ""), "frame_size": frame_size,
                 "groups": [{k: v for k, v in g.items()
                             if k not in ("part_number", "created")} for g in groups]}
    else:
        model["groups"] = groups
    save_model(model)
    return model


def _inspect_group(frame: np.ndarray, group: dict, region: Optional[dict]) -> GroupResult:
    """Judge one taught group inside `region` of the frame (None: all of it).

    Only the region is searched, so paint elsewhere in the frame, such as
    another group or another cable, is never counted. Stripes crossing its
    edge are left out, as at the edge of the frame.
    """
    area, x0, y0, box = frame, 0, 0, None
    if region is not None:
        area, box = _crop(frame, region, 0.0)
        x0, y0 = box[0], box[1]
    colour, expected = group["colour"], group["count"]
    if area.shape[0] < 8 or area.shape[1] < 8:
        return GroupResult(ok=False, colour=colour, expected=expected, seen="none",
                           region=box)

    masks = masks_in(frame, box) if box is not None else colour_masks(area)
    lab = cv2.cvtColor(area, cv2.COLOR_BGR2LAB).astype(np.float32)
    count, ok, bands = _judge(masks, lab, colour, group)

    seen = f"{colour} x{count}" if count else "none"
    if not ok and count == expected:
        # The right number, but not laid out like the taught stripes
        seen += " of the wrong size or spacing"
    if not ok:
        # Name what is there instead, so the operator sees "red x3", not just NG
        for other in COLOURS:
            if other == colour:
                continue
            n, other_ok, _ = _judge(masks, lab, other, group)
            if n and (other_ok or not count):
                seen = f"{other} x{n}"
                if other_ok:
                    break

    return GroupResult(ok=ok, colour=colour, expected=expected, count=count, seen=seen,
                       region=box,
                       stripe_boxes=[b["box"] + np.array([x0, y0], np.float32)
                                     for b in bands])


def inspect(part_number: str, frame: Optional[np.ndarray] = None) -> StripeResult:
    """Check one part's stripes: every taught group must be found.

    By default grabs a fresh frame from camera 2. Pass `frame` to judge a
    still image instead.
    """
    start = time.time()

    def _error(msg: str) -> StripeResult:
        return StripeResult(
            ok=False, judgement="ERROR", part_number=part_number, error=msg,
            processing_time_ms=int((time.time() - start) * 1000),
        )

    model = load_model(part_number)
    if model is None:
        return _error(f"No stripe model for '{part_number}'")
    groups = groups_of(model)
    if not groups:
        return _error(f"The stripe model for '{part_number}' has no stripe groups")

    if frame is None:
        frame = capture_frame()
        if frame is None:
            return _error("Camera 2 not available")

    fw, fh = model.get("frame_size", [frame.shape[1], frame.shape[0]])
    if (frame.shape[1], frame.shape[0]) != (fw, fh):
        return _error(f"Camera 2 gives {frame.shape[1]}x{frame.shape[0]} frames, "
                      f"but this part was taught at {fw}x{fh}. Re-teach it.")

    # Groups without a search region of their own use camera 2's
    camera_roi = None
    if any(not g.get("region") for g in groups):
        camera_roi = load_roi()
        if camera_roi is not None and \
                tuple(camera_roi["frame_size"]) != (frame.shape[1], frame.shape[0]):
            return _error(f"Camera 2's detection region was drawn on "
                          f"{camera_roi['frame_size'][0]}x{camera_roi['frame_size'][1]} "
                          f"frames, but it gives {frame.shape[1]}x{frame.shape[0]}. "
                          f"Redraw the region.")

    # The frame as judged, which the result carries for display
    frame = for_model(frame, model)
    results = [_inspect_group(frame, g, g.get("region") or camera_roi) for g in groups]
    ok = all(r.ok for r in results)
    several = len(results) > 1

    failed = [("Group %d: " % n if several else "")
              + f"Expected {r.colour} x{r.expected}, found {r.seen}"
              for n, r in enumerate(results, start=1) if not r.ok]
    boxes = [b for r in results for b in r.stripe_boxes]
    found_box = (cv2.boundingRect(np.vstack(boxes).astype(np.int32)) if boxes else None)
    elapsed = int((time.time() - start) * 1000)
    return StripeResult(
        ok=ok, judgement="OK" if ok else "NG", part_number=part_number,
        expected_colour=results[0].colour if not several else describe(model),
        expected_count=sum(r.expected for r in results),
        count=sum(r.count for r in results),
        seen=" + ".join(r.seen for r in results),
        processing_time_ms=elapsed,
        error=None if ok else "; ".join(failed),
        stripe_boxes=boxes, found_box=found_box, frame=frame, groups=results,
    )
