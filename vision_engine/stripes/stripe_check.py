"""
vision_engine/stripes/stripe_check.py
=====================================
Checks the painted stripes on a cable with the second camera ("cam2").

Each part number has one or more groups of painted bands round its cable,
for example three yellow stripes near one end and two red ones near the
other. A check passes when every group is found: the cable's body colour,
the expected number of stripes, and each stripe in the expected colour.

The judging itself is done by marker_engine (see marker_engine/README.md);
this module keeps each part's groups and turns the engine's verdicts into the
OK / NG / ERROR the test console shows.

Methodology:
  - Teach: on a good part, the operator draws two boxes for each group: one
    round its stripes, with a little cable either side, and a longer one round
    the cable there, with some of the bench above and below it. The engine
    finds the cable's edges in the cable box, and the stripes across it in the
    stripe box, and the body and stripe colours of the good part are taught
    from what it sees.
  - Inspect: each group is judged on its own two boxes. The cable may sit a
    little higher or lower, or lie a few degrees off, but must run along the
    long side of its cable box.
  - Result: OK if every group passes. A group whose colours, count or cable
    are wrong is NG; one the engine cannot judge reliably (blur, glare,
    something over the cable, light unlike the teach) is an ERROR.

Parts taught with the earlier stripe check are kept in the list, but are
judged ERROR until they are re-taught.
"""
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

import cv2
import numpy as np

from marker_engine import (FAIL, NO_CABLE, PASS, PresetStore, Recipe, SetupError,
                           detect, evaluate)
from marker_engine.calibration import TeachSession
from marker_engine.colors import nearest_colour_name
from marker_engine.detection import PRESENT

from .. import camera
from ..config import ROOT_DIR, load_camera_config

CAMERA_SOURCE = "cam2"

_MODELS_DIR = os.path.join(ROOT_DIR, "vision_models", "stripes")

# Saved in every model taught with marker_engine; a model without it was
# taught with the earlier stripe check and has to be re-taught
ENGINE = "marker_engine"

# The engine's named colours. The cable and the stripes are taught from the
# good part, so these name what was taught rather than define it.
_PRESETS = PresetStore(os.path.join(ROOT_DIR, "vision_models", "colour_presets.json"))
COLOURS = tuple(_PRESETS.names())

# The smallest boxes the engine can work in (see marker_engine/detection.py):
# the cable box's long and short sides, and the stripe box's sides
MIN_CABLE_BOX = (20, 12)
MIN_STRIPE_BOX = 8


@dataclass
class GroupResult:
    """How one taught stripe group fared."""
    ok: bool
    colour: str
    expected: int                   # stripes taught
    count: int = 0                  # stripes found
    seen: str = ""                  # e.g. "red x3"
    # The cable box the group was judged in, (x, y, w, h)
    region: Optional[Tuple[int, int, int, int]] = None
    stripe_boxes: List[np.ndarray] = field(default_factory=list)
    status: str = ""                # the engine's verdict, e.g. "FAIL"
    reasons: List[str] = field(default_factory=list)


@dataclass
class StripeResult:
    """Outcome of one stripe check. With several groups, the colour, count
    and seen fields sum them up; `groups` has each one."""
    ok: bool
    judgement: str                  # "OK", "NG", "ERROR"
    part_number: str = ""
    expected_colour: str = ""
    expected_count: int = 0
    count: int = 0                  # stripes found
    seen: str = ""                  # what the frame actually holds, e.g. "red x3"
    processing_time_ms: int = 0
    error: Optional[str] = None
    # Diagnostics for the settings page: where the stripes were, and the
    # frame that was judged.
    stripe_boxes: List[np.ndarray] = field(default_factory=list)
    found_box: Optional[Tuple[int, int, int, int]] = None    # round the stripes found
    frame: Optional[np.ndarray] = None
    groups: List[GroupResult] = field(default_factory=list)


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


def needs_reteach(model: dict) -> bool:
    """True for a model taught with the earlier stripe check."""
    return model.get("engine") != ENGINE


def groups_of(model: dict) -> List[dict]:
    """The stripe groups of a model, each a marker_engine recipe as saved."""
    if needs_reteach(model):
        return []
    return list(model.get("groups", []))


def describe(model: dict) -> str:
    """The stripes a model expects, e.g. "black: yellow × 3 + red × 2"."""
    if needs_reteach(model):
        return "re-teach needed"
    groups = groups_of(model)
    if not groups:
        return "?"
    bodies = {g.get("body_colour") for g in groups}
    stripes = " + ".join("%s × %s" % (g.get("stripe_colour", "?"), g.get("stripe_count", "?"))
                         for g in groups)
    return "%s: %s" % (bodies.pop(), stripes) if len(bodies) == 1 else stripes


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


# ── Boxes ───────────────────────────────────────────────────────────────────
# The settings pages draw boxes as {"x", "y", "width", "height"}; the engine
# takes (x, y, w, h).

def _rect(box: dict) -> Tuple[int, int, int, int]:
    return int(box["x"]), int(box["y"]), int(box["width"]), int(box["height"])


def _box(rect) -> dict:
    x, y, w, h = (int(v) for v in rect)
    return {"x": x, "y": y, "width": w, "height": h}


def _contains(outer, inner) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ix + iw <= ox + ow and iy + ih <= oy + oh


# The engine looks for a cable running across its box, give or take a few
# degrees. A cable box taller than it is wide holds an upright cable, so the
# frame is turned a quarter clockwise for it, and what is found turned back.

def _upright(recipe: Recipe) -> bool:
    return recipe.roi[3] > recipe.roi[2]


def _turn_rect(rect, frame_h: int):
    x, y, w, h = rect
    return frame_h - (y + h), x, h, w


def _turn(frame: np.ndarray, recipe: Recipe) -> Tuple[np.ndarray, Recipe]:
    """The frame and recipe as the engine should see them."""
    if not _upright(recipe):
        return frame, recipe
    fh = frame.shape[0]
    turned = recipe.copy()
    turned.roi = _turn_rect(recipe.roi, fh)
    turned.inspection_region = _turn_rect(recipe.inspection_region, fh)
    if recipe.image_size:
        turned.image_size = (recipe.image_size[1], recipe.image_size[0])
    return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE), turned


def _turn_back(points: np.ndarray, recipe: Recipe, frame_h: int) -> np.ndarray:
    """Points found in the turned frame, in the frame as captured."""
    if not _upright(recipe):
        return points
    pts = np.asarray(points, np.float32)
    return np.stack([pts[:, 1], frame_h - pts[:, 0]], axis=1)


def _outlines(m, recipe: Recipe, frame_h: int) -> List[np.ndarray]:
    return [_turn_back(st.polygon, recipe, frame_h) for st in m.stripes]


def _seen(m) -> str:
    """What the engine found, e.g. "white x3" or "white x2, red x1"."""
    if m.presence != PRESENT:
        return "no cable" if m.presence == "ABSENT" else "cable unclear"
    if not m.stripes:
        return "none"
    names = Counter(st.colour_name or "?" for st in m.stripes)
    return ", ".join("%s x%d" % (name, n) for name, n in names.most_common())


# ── Teaching ────────────────────────────────────────────────────────────────

def _recipe(name: str, frame: np.ndarray, group: dict) -> Recipe:
    fh, fw = frame.shape[:2]
    return Recipe(name, body_colour=group.get("body") or "black",
                  stripe_colour=group.get("colour") or "white",
                  stripe_count=int(group.get("count") or 1),
                  roi=_rect(group["area"]), inspection_region=_rect(group["roi"]),
                  image_size=(fw, fh))


def _check_boxes(box: Optional[dict], area: Optional[dict]):
    if box is None:
        raise ValueError("Draw a box round the stripes.")
    if area is None:
        raise ValueError("Draw a box round the cable.")
    if box["width"] < MIN_STRIPE_BOX or box["height"] < MIN_STRIPE_BOX:
        raise ValueError("The stripe box is too small. Draw it round the whole stripe group.")
    if max(area["width"], area["height"]) < MIN_CABLE_BOX[0] or \
            min(area["width"], area["height"]) < MIN_CABLE_BOX[1]:
        raise ValueError("The cable box is too small.")
    if not _contains(_rect(area), _rect(box)):
        raise ValueError("The stripe box must lie inside the cable box.")


def read_group(frame: np.ndarray, roi: dict, area: Optional[dict] = None,
               body: Optional[str] = None, colour: Optional[str] = None,
               count: Optional[int] = None) -> Tuple[dict, List[np.ndarray]]:
    """What the engine sees of one group on a good part, without saving it.

    `roi` is the box round the stripes and `area` the longer one round the
    cable. Returns what was read, with "body" and "colour" named from the
    engine's colours unless given, "count", "stripe_width" and "span", and the
    outline of each stripe in frame coordinates. With `count`, that many
    stripes must be found. Raises ValueError with an operator-readable reason
    when the group can't be taught from this picture.
    """
    _check_boxes(roi, area)
    recipe = _recipe("read", frame, {"roi": roi, "area": area})
    turned_frame, turned = _turn(frame, recipe)
    try:
        m = detect(turned_frame, turned)
    except SetupError as e:
        raise ValueError(str(e).capitalize() + ".")
    if m.presence != PRESENT:
        raise ValueError("No cable found in the cable box. Draw it round the cable, "
                         "with some of the bench above and below it.")
    if m.issues:
        raise ValueError(m.issues[0][0].upper() + m.issues[0][1:] + ".")
    if not m.stripes:
        raise ValueError("No stripes found in the stripe box.")
    if count is not None and len(m.stripes) != count:
        raise ValueError("%d stripes found, but %d are expected. Check the boxes and the "
                         "lighting; stripes that touch can't be told apart."
                         % (len(m.stripes), count))
    refs = _PRESETS.all()
    names = Counter(nearest_colour_name(st.lab, refs)[0] for st in m.stripes)
    found = {
        "body": body or nearest_colour_name(m.body_lab, refs)[0],
        "colour": colour or names.most_common(1)[0][0],
        "count": len(m.stripes),
        "stripe_width": float(np.median([st.width_px for st in m.stripes])),
        "span": float(m.stripes[-1].end - m.stripes[0].start),
    }
    return found, _outlines(m, recipe, frame.shape[0])


def teach(part_number: str, frame: np.ndarray, groups: List[dict]) -> dict:
    """Teach each stripe group of a good part and save them for the part.

    `groups` lists one dict per group: "roi", the box round its stripes,
    "area", the box round the cable, and "body", "colour" and "count", as
    read_group() gives or the operator corrected them. Raises ValueError,
    naming the group, if one can't be taught.
    """
    if not groups:
        raise ValueError("Add at least one stripe group.")
    fh, fw = frame.shape[:2]
    recipes = []
    for n, group in enumerate(groups, start=1):
        try:
            _check_boxes(group.get("roi"), group.get("area"))
            if not group.get("body") or not group.get("colour") or not group.get("count"):
                raise ValueError("Choose the cable colour, stripe colour and count.")
            recipe = _recipe(f"{part_number} G{n}", frame, group)
            turned_frame, turned = _turn(frame, recipe)
            session = TeachSession()
            ok, msg = session.add_auto(turned_frame, turned)
            if not ok:
                raise ValueError(msg.replace("auto sample rejected: ", "").capitalize() + ".")
            session.build(turned)
        except ValueError as e:
            raise ValueError(f"Group {n}: {e}")
        # Taught on the turned frame; the boxes are kept as drawn
        turned.roi, turned.inspection_region = recipe.roi, recipe.inspection_region
        turned.image_size = recipe.image_size
        recipes.append(turned.to_dict())
    model = {
        "part_number": part_number,
        "engine": ENGINE,
        "created": datetime.now().isoformat(timespec="seconds"),
        "frame_size": [fw, fh],
        "groups": recipes,
    }
    save_model(model)
    return model


# ── Inspection ──────────────────────────────────────────────────────────────

def _inspect_group(frame: np.ndarray, recipe: Recipe) -> GroupResult:
    """Judge one taught group in its own boxes."""
    result = GroupResult(ok=False, colour=recipe.stripe_colour,
                         expected=recipe.stripe_count, region=tuple(recipe.roi))
    turned_frame, turned = _turn(frame, recipe)
    try:
        m = detect(turned_frame, turned)
    except SetupError as e:
        result.status, result.seen, result.reasons = "ERROR", "not judged", [str(e)]
        return result
    verdict = evaluate(m, turned, _PRESETS)
    result.ok = verdict.status == PASS
    result.status = verdict.status
    result.reasons = list(verdict.reasons)
    result.count = len(m.stripes)
    result.seen = _seen(m)
    result.stripe_boxes = _outlines(m, recipe, frame.shape[0])
    return result


def inspect(part_number: str, frame: Optional[np.ndarray] = None) -> StripeResult:
    """Check one part's stripes: every taught group must pass.

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
    if needs_reteach(model):
        return _error(f"'{part_number}' was taught with the earlier stripe check. "
                      f"Re-teach it.")
    groups = [Recipe.from_dict(g) for g in groups_of(model)]
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

    results = [_inspect_group(frame, g) for g in groups]
    ok = all(r.ok for r in results)
    several = len(results) > 1

    # A wrong part is NG; a group that couldn't be judged makes it an ERROR
    # unless another group is clearly wrong
    if ok:
        judgement = "OK"
    elif any(r.status in (FAIL, NO_CABLE) for r in results):
        judgement = "NG"
    else:
        judgement = "ERROR"
    failed = [("Group %d: " % n if several else "")
              + f"Expected {r.colour} x{r.expected}, found {r.seen}"
              + (f" ({r.reasons[0]})" if r.reasons else "")
              for n, r in enumerate(results, start=1) if not r.ok]
    boxes = [b for r in results for b in r.stripe_boxes]
    found_box = (cv2.boundingRect(np.vstack(boxes).astype(np.int32)) if boxes else None)
    return StripeResult(
        ok=ok, judgement=judgement, part_number=part_number,
        expected_colour=results[0].colour if not several else describe(model),
        expected_count=sum(r.expected for r in results),
        count=sum(r.count for r in results),
        seen=" + ".join(r.seen for r in results),
        processing_time_ms=int((time.time() - start) * 1000),
        error=None if ok else "; ".join(failed),
        stripe_boxes=boxes, found_box=found_box, frame=frame, groups=results,
    )
