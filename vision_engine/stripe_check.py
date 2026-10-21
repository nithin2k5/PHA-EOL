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

# Colours found anywhere by their own look (see colour_masks)
MASK_COLOURS = ("white", "red", "yellow", "green", "blue", "purple")
# Grey is found only along a cable boxed tightly, by being paler than the
# cable, as white can be there too: grey anywhere else is half the picture
CABLE_COLOURS = ("white", "grey")
COLOURS = MASK_COLOURS + ("grey",)

# How far a live stripe may differ from the taught one before it counts as
# a different part. Hand-painted bands vary, so this is loose.
SIZE_TOLERANCE = 0.4

# A box at least this many times longer than it is wide is drawn tight round
# the cable, and stripes may touch its long sides (see _bands)
CABLE_BOX_RATIO = 3

# White stripes on a cable drawn round tightly are found by how much paler
# they are than the cable itself (see _cable_runs): at least this much, in
# brightness levels, and at least this share of what the good part showed
MIN_CABLE_CONTRAST = 15
CABLE_CONTRAST_SHARE = 0.35
# ... and colourless: a coloured mark on the cable is not a white stripe
MAX_CABLE_WHITE_SATURATION = 60

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
    masks = {
        "white": (s < 55) & (v > 185),
        "red": vivid & ((h < 9) | (h > 165)),
        "yellow": vivid & (h >= 18) & (h <= 38),
        "green": pale & (h >= 40) & (h <= 88),
        "blue": vivid & (h >= 95) & (h <= 128),
        "purple": pale & (h >= 129) & (h <= 165),
    }
    kernel = np.ones((3, 3), np.uint8)
    return {c: cv2.morphologyEx(m.astype(np.uint8) * 255, cv2.MORPH_OPEN, kernel)
            for c, m in masks.items()}


def _paint_mask(masks: dict) -> np.ndarray:
    """Everything that is coloured paint, grown a little to cover its shine."""
    paint = np.zeros_like(masks["white"])
    for c in MASK_COLOURS:
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

    A mask far longer than it is wide is a box drawn tight round the cable,
    whose long sides are the cable's edges. Stripes run right across the
    cable, so there patches may touch the long sides; only those touching
    the ends, where the box cuts the cable off, are left out.
    """
    mh, mw = mask.shape[:2]
    along_x = mw >= CABLE_BOX_RATIO * mh
    along_y = mh >= CABLE_BOX_RATIO * mw
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_area:
            continue
        if not along_y and (x == 0 or x + w >= mw):
            continue
        if not along_x and (y == 0 or y + h >= mh):
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


def dominant_colour(patch: np.ndarray) -> Tuple[Optional[str], int]:
    """The stripe colour covering most of a patch, and how many pixels it has.

    Only band-shaped patches count, so a background colour filling the whole
    patch, or scattered specks, don't win.
    """
    masks = colour_masks(patch)
    patch_area = patch.shape[0] * patch.shape[1]
    best, best_area = None, 0
    for colour in MASK_COLOURS:
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
    patch, (x0, y0, _, _) = _crop(frame, roi, 0.0)

    if colour is None:
        colour, _ = dominant_colour(patch)
        if colour is None and _is_cable_box(roi):
            colour = "white"
        if colour is None:
            raise ValueError("No stripe colour found in the box.")
    elif colour not in COLOURS:
        raise ValueError(f"Unknown stripe colour '{colour}'.")
    if colour in CABLE_COLOURS and _is_cable_box(roi):
        return _read_cable(frame, roi, count, colour)
    if colour not in MASK_COLOURS:
        raise ValueError(f"{colour.capitalize()} stripes are found only on a cable: "
                         f"draw the box tight round it.")

    masks = colour_masks(patch)
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


# ── White or grey stripes along a cable ────────────────────────────────────
#
# A white stripe on a black cable can look only grey to the camera, and far
# darker than a pale bench or sheet behind the cable, so no brightness of its
# own marks it as white. What does is that it is paler than the cable round
# it. When the box is drawn tight round the cable, the cable is followed along
# its length as its darkest band, and the brightness is read along the middle
# of that band, clear of the background at the cable's edges. The stripes are
# the stretches clearly paler than the cable either side of them. Nothing off
# the cable is looked at, so pale things beside it can't count. Grey stripes
# are found the same way; which of the two the part has is the operator's
# word for them.

CABLE_MODE = "cable"

# The cable is found in sections this many pixels long, so a cable lying at
# a slant or bowed a little is still followed
CABLE_SECTION = 16

# The cable must be about as dark against what lies beside it as when taught
# (see _darkness), or it isn't there: no paler than this many times as pale,
# give or take this much. A good cable stays within about 0.06 of its
# taught share through changes of light; an empty fixture is far paler.
CABLE_DARKNESS_FACTOR = 1.4
CABLE_DARKNESS_SLACK = 0.05


def _is_cable_box(box: dict) -> bool:
    """True for a box drawn tight round a stretch of cable (see _bands)."""
    w, h = box["width"], box["height"]
    return max(w, h) >= CABLE_BOX_RATIO * max(1, min(w, h))


def _brightness(frame: np.ndarray, box: Tuple[int, int, int, int],
                along_x: bool) -> Tuple[np.ndarray, np.ndarray]:
    """(brightness, saturation) inside a box, as float rows across the cable
    and columns along it."""
    x, y, w, h = box
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV).astype(np.float32)
    v, s = hsv[..., 2], hsv[..., 1]
    return (v, s) if along_x else (v.T, s.T)


def _cable_band(v: np.ndarray) -> Tuple[int, int]:
    """(first row, rows) of the cable in a box drawn round it: the longest
    run of rows darker than halfway between the darkest and the lightest."""
    med = np.median(v, axis=1)
    dark = med <= (med.min() + med.max()) / 2
    best, start = (0, 0), None
    for i, d in enumerate(np.append(dark, False)):
        if d and start is None:
            start = i
        elif not d and start is not None:
            if i - start > best[1]:
                best = (start, i - start)
            start = None
    return best


def _track(v: np.ndarray, rows: int) -> np.ndarray:
    """The middle row of the cable at each column: a band `rows` thick.

    In each short section the darkest band that thick is taken, judged by
    each row's median so the stripes, a small part of a section, don't move
    it. A straight line is fitted through the sections, leaving out those far
    off it, such as where something dark lies beside the cable.
    """
    h, w = v.shape
    rows = max(1, min(rows, h))
    xs, ys = [], []
    for c0 in range(0, max(1, w - CABLE_SECTION // 2), CABLE_SECTION // 2):
        med = np.median(v[:, c0:c0 + CABLE_SECTION], axis=1)
        sums = np.convolve(med, np.ones(rows), mode="valid")
        xs.append(c0 + min(CABLE_SECTION, w - c0) / 2)
        ys.append(int(np.argmin(sums)) + rows / 2)
    xs, ys = np.array(xs), np.array(ys)
    keep = np.ones(len(xs), bool)
    for _ in range(3):
        if keep.sum() < 2:
            break
        slope, icept = np.polyfit(xs[keep], ys[keep], 1)
        keep = np.abs(ys - (slope * xs + icept)) <= max(2.0, rows / 2)
    if keep.sum() >= 2:
        slope, icept = np.polyfit(xs[keep], ys[keep], 1)
    else:
        slope, icept = 0.0, float(np.median(ys))
    return slope * np.arange(w) + icept


def _profile(v: np.ndarray, centres: np.ndarray, rows: int) -> np.ndarray:
    """`v` along the middle of the cable, smoothed a little.

    Only the middle three fifths of its thickness are used: at its edges the
    cable blurs into whatever lies behind it.
    """
    h = v.shape[0]
    half = max(0.5, rows * 0.3)
    profile = np.empty(v.shape[1], np.float32)
    for c, mid in enumerate(centres):
        r0 = int(np.clip(round(mid - half), 0, h - 1))
        r1 = int(np.clip(round(mid + half), r0 + 1, h))
        profile[c] = v[r0:r1, c].mean()
    return np.convolve(profile, np.array([1, 2, 1]) / 4, mode="same")


def _darkness(v: np.ndarray, centres: np.ndarray, rows: int) -> Optional[float]:
    """How dark the cable is next to what lies just beside it, as a share
    (0 black on white, 1 no darker), or None if nothing beside it is in view.
    """
    h = v.shape[0]
    inside, beside = [], []
    gap, reach = rows / 2 + 2, rows / 2 + 2 + max(2, rows / 2)
    for c, mid in enumerate(centres):
        r0, r1 = int(round(mid - rows * 0.3)), int(round(mid + rows * 0.3)) + 1
        if 0 <= r0 and r1 <= h:
            inside.append(v[r0:r1, c])
        for a, b in ((mid - reach, mid - gap), (mid + gap, mid + reach)):
            a, b = int(round(a)), int(round(b))
            if 0 <= a < b <= h:
                beside.append(v[a:b, c])
    if not inside or not beside:
        return None
    return float(np.median(np.concatenate(inside))) / max(1.0, float(np.median(np.concatenate(beside))))


def _white_only(runs: list, saturation: np.ndarray) -> list:
    """The stretches that are colourless, as white paint is."""
    return [r for r in runs
            if saturation[r[0]:r[1]].mean() <= MAX_CABLE_WHITE_SATURATION]


def _clusters(runs: list, link: float) -> List[list]:
    """Stretches whose middles lie within `link` of the next, together."""
    clusters, current = [], []
    for r in runs:
        if current and (r[0] + r[1]) / 2 - (current[-1][0] + current[-1][1]) / 2 > link:
            clusters.append(current)
            current = []
        current.append(r)
    return clusters + ([current] if current else [])


def _cable_runs(profile: np.ndarray, min_contrast: float) -> list:
    """Stretches of the cable clearly paler than the cable either side.

    Each is (start, end, how much paler). The cable is not evenly dark, as
    where it crosses something pale behind it, so each stretch is judged
    against its own surroundings. It must rise by at least `min_contrast`
    above the lows either side of it: the darkest before anything paler, and
    the dip right next to it, as stripes side by side only dip part of the
    way between them. It is measured where it is more than halfway up from
    the higher of the dips next to it, so its width doesn't take in a paler
    stretch of cable beside it. Stretches running off either end are left
    out, being cut off by the box.
    """
    n = len(profile)
    runs = []
    i = 1
    while i < n - 1:
        # A peak, or the middle of a flat top
        j = i
        while j + 1 < n and profile[j + 1] == profile[i]:
            j += 1
        if profile[i] > profile[i - 1] and (j + 1 >= n or profile[j + 1] < profile[i]):
            top = profile[i]
            peak = (i + j) // 2
            left = i
            while left > 0 and profile[left - 1] <= top:
                left -= 1
            right = j
            while right + 1 < n and profile[right + 1] <= top:
                right += 1
            rise = float(top - max(profile[left:i + 1].min(), profile[j:right + 1].min()))
            dip = max(_dip(profile, i, -1, min_contrast), _dip(profile, j, 1, min_contrast))
            if rise >= min_contrast and top - dip >= min_contrast:
                half = top - (top - dip) / 2
                s0, s1 = peak, peak + 1
                while s0 > 0 and profile[s0 - 1] > half:
                    s0 -= 1
                while s1 < n and profile[s1] > half:
                    s1 += 1
                if s0 > 0 and s1 < n and s1 - s0 >= 2:
                    runs.append((s0, s1, rise))
        i = j + 1
    return runs


def _dip(profile: np.ndarray, start: int, step: int, min_contrast: float) -> float:
    """The low next to a peak at `start`, going `step` (-1 or 1) from it.

    Followed downhill until the cable turns paler again by more than a
    wiggle of noise.
    """
    wiggle = max(3.0, min_contrast * 0.25)
    low = float(profile[start])
    k = start + step
    while 0 <= k < len(profile) and profile[k] <= low + wiggle:
        low = min(low, float(profile[k]))
        k += step
    return low


def _run_outlines(runs: list, centres: np.ndarray, rows: int,
                  box: Tuple[int, int, int, int], along_x: bool) -> List[np.ndarray]:
    """Each stretch as a rectangle across the cable, in frame coordinates."""
    x, y = box[0], box[1]
    out = []
    for s0, s1, _ in runs:
        mid = float(centres[(s0 + s1) // 2])
        a, b = mid - rows / 2, mid + rows / 2
        if along_x:
            pts = [(x + s0, y + a), (x + s1, y + a), (x + s1, y + b), (x + s0, y + b)]
        else:
            pts = [(x + a, y + s0), (x + b, y + s0), (x + b, y + s1), (x + a, y + s1)]
        out.append(np.array(pts, np.float32))
    return out


def _read_cable(frame: np.ndarray, roi: dict, count: Optional[int],
                colour: str = "white") -> Tuple[dict, List[np.ndarray]]:
    """read_stripes() for white or grey stripes in a box drawn tight round the
    cable. Both are found the same way; `colour` is what the operator calls
    them."""
    box = (roi["x"], roi["y"], roi["width"], roi["height"])
    along_x = roi["width"] >= roi["height"]
    v, sat = _brightness(frame, box, along_x)
    _, rows = _cable_band(v)
    if rows < 2:
        raise ValueError("No cable found in the box. Draw it tight round the cable.")
    centres = _track(v, rows)
    runs = _cable_runs(_profile(v, centres, rows), MIN_CABLE_CONTRAST)
    runs = _white_only(runs, _profile(sat, centres, rows))
    if not runs:
        raise ValueError(f"No {colour} stripes found on the cable.")
    # A box along the cable can take in other pale marks, such as where the
    # cable crosses a bracket. The stripes are the ones lying close together:
    # within half as far again as the closest two
    if len(runs) > 1:
        mids = [(s + e) / 2 for s, e, _ in runs]
        clusters = _clusters(runs, min(np.diff(mids)) * 1.5)
        wanted = int(count) if count else None
        runs = max(clusters, key=lambda c: (len(c) == wanted, len(c), sum(r[2] for r in c)))
    if count and int(count) != len(runs):
        raise ValueError(f"{len(runs)} {colour} stripes found together on the cable, "
                         f"not {int(count)}.")

    widths = [e - s for s, e, _ in runs]
    mids = [(s + e) / 2 for s, e, _ in runs]
    model = {
        "roi": {k: int(roi[k]) for k in ("x", "y", "width", "height")},
        "frame_size": [int(frame.shape[1]), int(frame.shape[0])],
        "colour": colour,
        "count": len(runs),
        "mode": CABLE_MODE,
        "axis": [1.0, 0.0] if along_x else [0.0, 1.0],
        "stripe_width": float(np.median(widths)),
        "stripe_length": float(rows),
        "cable_width": rows,
        "span": float(runs[-1][1] - runs[0][0]),
        # Middle of one stripe to the middle of the next
        "pitch": float(np.mean(np.diff(mids))) if len(runs) > 1 else 0.0,
        "size_tolerance": SIZE_TOLERANCE,
        "contrast": float(np.median([r[2] for r in runs])),
    }
    # How dark the cable is against what lies beside it, measured where a
    # check will look for it, so an empty fixture isn't taken for a cable
    window = _cable_window(frame, model, None)
    wv, _ = _brightness(frame, window, along_x)
    model["cable_darkness"] = _darkness(wv, _track(wv, rows), rows)
    return model, _run_outlines(runs, centres, rows, box, along_x)


def _cable_window(frame: np.ndarray, group: dict,
                  region: Optional[dict]) -> Tuple[int, int, int, int]:
    """Where to look for a cable group: its own area if it has one, else the
    box it was taught in, widened enough for the cable to have moved a
    little. Camera 2's region isn't used: it is not drawn round this cable,
    and other dark things in it could be taken for it."""
    fh, fw = frame.shape[:2]
    if region is not None:
        x, y, w, h = region["x"], region["y"], region["width"], region["height"]
    else:
        r = group["roi"]
        along_x = group["axis"][0] >= group["axis"][1]
        pad_along = int(max(r["width"], r["height"]) * 0.1)
        pad_across = int(min(r["width"], r["height"])) * 2
        px, py = (pad_along, pad_across) if along_x else (pad_across, pad_along)
        x, y, w, h = r["x"] - px, r["y"] - py, r["width"] + 2 * px, r["height"] + 2 * py
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(fw, x + w), min(fh, y + h)
    return x0, y0, max(0, x1 - x0), max(0, y1 - y0)


def _inspect_cable(frame: np.ndarray, group: dict, region: Optional[dict]) -> GroupResult:
    """_inspect_group() for white or grey stripes taught on a cable drawn
    round tightly.

    The cable is followed through the window as the darkest band as thick as
    it was taught, and must be about as dark against what lies beside it as
    it was then. Its stripes are counted along it: they must be as many as
    taught, about as wide, and spread over about the same length.
    """
    box = _cable_window(frame, group, region)
    expected = group["count"]
    along_x = group["axis"][0] >= group["axis"][1]
    colour = group["colour"]
    result = GroupResult(ok=False, colour=colour, expected=expected, seen="none",
                         region=box if region is not None else None)
    if box[2] < 8 or box[3] < 8:
        return result

    v, sat = _brightness(frame, box, along_x)
    rows = group.get("cable_width", 3)
    centres = _track(v, rows)
    taught_dark = group.get("cable_darkness")
    if taught_dark is not None:
        dark = _darkness(v, centres, rows)
        if dark is None or dark > taught_dark * CABLE_DARKNESS_FACTOR + CABLE_DARKNESS_SLACK:
            result.seen = "no cable"
            return result

    taught = group.get("contrast", 0.0)
    min_contrast = max(MIN_CABLE_CONTRAST * 0.8, CABLE_CONTRAST_SHARE * taught)
    runs = _cable_runs(_profile(v, centres, rows), min_contrast)
    runs = _white_only(runs, _profile(sat, centres, rows))

    tol = group.get("size_tolerance", SIZE_TOLERANCE)
    width = group["stripe_width"]
    pitch = group.get("pitch", 0.0)
    # Slivers are noise; a run much wider than a stripe is stripes that touch,
    # counted by how many pitches it covers
    runs = [r for r in runs if r[1] - r[0] >= width * (1 - tol) * 0.6]

    def stripes_in(run):
        w = run[1] - run[0]
        if pitch > width and w > width / (1 - tol):
            return max(1, int(round((w - width) / pitch + 1)))
        return 1

    # Stripes lying close together form a group, as when taught; the group
    # nearest the taught count is the one judged
    link = (pitch if pitch else width * 2) * 1.5
    best = min(_clusters(runs, link),
               key=lambda c: (abs(sum(map(stripes_in, c)) - expected), -sum(x[2] for x in c)),
               default=[])

    count = sum(map(stripes_in, best))
    span = best[-1][1] - best[0][0] if best else 0.0
    sized = all(_within(r[1] - r[0], width, tol) for r in best if stripes_in(r) == 1)
    spread = expected == 1 or _within(span, group["span"], tol)
    result.ok = count == expected and sized and spread
    result.count = count
    result.seen = "none" if not count else f"{colour} x{count}" + (
        " of the wrong size or spacing" if count == expected and not result.ok else "")
    result.stripe_boxes = _run_outlines(best, centres, rows, box, along_x)
    return result


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
    if group.get("mode") == CABLE_MODE:
        return _inspect_cable(frame, group, group.get("region"))
    area, x0, y0, box = frame, 0, 0, None
    if region is not None:
        area, box = _crop(frame, region, 0.0)
        x0, y0 = box[0], box[1]
    colour, expected = group["colour"], group["count"]
    if area.shape[0] < 8 or area.shape[1] < 8:
        return GroupResult(ok=False, colour=colour, expected=expected, seen="none",
                           region=box)

    masks = colour_masks(area)
    lab = cv2.cvtColor(area, cv2.COLOR_BGR2LAB).astype(np.float32)
    count, ok, bands = _judge(masks, lab, colour, group)

    seen = f"{colour} x{count}" if count else "none"
    if not ok and count == expected:
        # The right number, but not laid out like the taught stripes
        seen += " of the wrong size or spacing"
    if not ok:
        # Name what is there instead, so the operator sees "red x3", not just NG
        for other in MASK_COLOURS:
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
