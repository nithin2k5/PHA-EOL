"""
vision_engine/part_inspection/vision_controller.py
==================================================
Webcam-based part verification using Template Matching (Normalized Cross-Correlation).

Methodology:
  - Teach: the operator collects reference images and boxes the part in each one.
    The image patch inside each box becomes a 'Template'. Boxes are per image,
    because a single box applied to every reference crops background wherever the
    part happened to sit elsewhere — and a background template matches the live
    background, which would pass an empty fixture.
  - Inspect: capture a live frame and search for the Template across it with
    cv2.matchTemplate.
  - Result: if the best match score >= threshold the part is present and correct.

Frames are enhanced (see vision_engine/enhance.py) before templates are cut from them, and
live frames before they are searched, for parts taught that way.
"""
import json
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

import cv2
import numpy as np

from .. import camera
from ..config import (DEFAULT_MATCH_THRESHOLD, ROOT_DIR, load_camera_config,
                      load_vision_config, save_vision_config)
from ..enhance import MODEL_KEY as ENHANCED, enhance, for_model

_MODELS_DIR = os.path.join(ROOT_DIR, "vision_models")


@dataclass
class VisionResult:
    """Structured result from a vision inspection."""
    ok: bool
    judgement: str              # "OK", "NG", "ERROR"
    part_number: str = ""
    match_score: float = 0.0    # 1.0 = perfect match, lower = worse
    threshold: float = 0.0
    processing_time_ms: int = 0
    error: Optional[str] = None

    # Diagnostics for the settings-page test view. Production ignores these;
    # they exist so the UI can show *where* the match landed instead of only a
    # number, without duplicating the matching logic outside inspect().
    match_box: Optional[Tuple[int, int, int, int]] = None   # (x, y, w, h) in frame
    frame: Optional[np.ndarray] = None                      # frame that was judged


def get_vision_controller() -> "VisionController":
    return VisionController()


class VisionController:
    """Headless part verification against taught templates."""

    def __init__(self):
        self.config = load_vision_config()
        self._model_cache: Dict[str, dict] = {}

    def reload_config(self):
        self.config = load_vision_config()
        self._model_cache.clear()

    # ── Camera ──────────────────────────────────────────────────────────────

    def cam_settings(self) -> tuple:
        """(index, width, height) for the configured camera source."""
        return load_camera_config(self.config.get("camera_source", "cam1"))

    def _capture_frame(self) -> Optional[np.ndarray]:
        index, width, height = self.cam_settings()
        if index < 0:
            return None
        return camera.grab(index, width, height)

    def get_status(self) -> str:
        index, width, height = self.cam_settings()
        if index < 0:
            return "NO_CAMERA"
        return "READY" if camera.grab(index, width, height) is not None else "CAMERA_ERROR"

    # ── Model I/O ───────────────────────────────────────────────────────────

    def _model_path(self, part_number: str) -> Optional[str]:
        filename = self.config.get("part_mapping", {}).get(part_number)
        if not filename:
            return None
        path = os.path.join(_MODELS_DIR, filename)
        return path if os.path.exists(path) else None

    def _load_model(self, part_number: str) -> Optional[dict]:
        if part_number in self._model_cache:
            return self._model_cache[part_number]

        path = self._model_path(part_number)
        if path is None:
            return None

        try:
            data = np.load(path, allow_pickle=True)
            model_cfg = json.loads(str(data["config"]))

            templates = []
            i = 0
            while f"template_{i}" in data:
                templates.append(data[f"template_{i}"])
                i += 1

            model_cfg["templates"] = templates
            self._model_cache[part_number] = model_cfg
            return model_cfg
        except (OSError, ValueError, KeyError):
            return None

    def has_model(self, part_number: str) -> bool:
        return self._model_path(part_number) is not None

    def get_mapped_parts(self) -> dict:
        return dict(self.config.get("part_mapping", {}))

    # ── Production Inspection ───────────────────────────────────────────────

    def inspect(self, part_number: str, frame: Optional[np.ndarray] = None) -> VisionResult:
        """Judge one frame against a taught part.

        By default captures a fresh frame from the configured camera — the
        same path production uses. Pass `frame` to judge a still image
        instead, without needing the part in front of a camera at all.
        """
        start = time.time()

        def _error(msg: str) -> VisionResult:
            return VisionResult(
                ok=False, judgement="ERROR", part_number=part_number, error=msg,
                processing_time_ms=int((time.time() - start) * 1000),
            )

        if not self.config.get("vision_enabled", True):
            return _error("Vision inspection disabled")

        model = self._load_model(part_number)
        if model is None:
            return _error(f"No vision model found for '{part_number}'")

        templates = model.get("templates", [])
        if not templates:
            return _error("Model contains no templates")

        if frame is None:
            frame = self._capture_frame()
            if frame is None:
                return _error("Camera not available")

        # The frame as judged, which the result carries for display
        frame = for_model(frame, model)
        gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        best_score = -1.0
        best_box = None
        compared = 0
        for template in templates:
            if template.shape[0] > gray_frame.shape[0] or template.shape[1] > gray_frame.shape[1]:
                continue
            res = cv2.matchTemplate(gray_frame, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_score:
                best_score = max_val
                best_box = (max_loc[0], max_loc[1], template.shape[1], template.shape[0])
            compared += 1

        print(f"[VISION DEBUG] pno={part_number} frame={gray_frame.shape[1]}x{gray_frame.shape[0]} "
              f"brightness={gray_frame.mean():.1f} templates={[(t.shape[1], t.shape[0]) for t in templates]} "
              f"compared={compared} best_score={best_score:.3f}")

        if compared == 0:
            return _error(
                f"Every template is larger than this frame "
                f"({gray_frame.shape[1]}x{gray_frame.shape[0]}) — "
                f"re-teach the part at this resolution, or use a larger test image"
            )

        threshold = model.get("match_threshold", self.config.get("match_threshold", DEFAULT_MATCH_THRESHOLD))
        elapsed = int((time.time() - start) * 1000)

        if best_score >= threshold:
            return VisionResult(
                ok=True, judgement="OK", part_number=part_number,
                match_score=best_score, threshold=threshold,
                processing_time_ms=elapsed,
                match_box=best_box, frame=frame,
            )
        return VisionResult(
            ok=False, judgement="NG", part_number=part_number,
            match_score=best_score, threshold=threshold,
            processing_time_ms=elapsed,
            error=f"No match found (score {best_score:.2f} < {threshold})",
            match_box=best_box, frame=frame,
        )

    # ── Model Building ──────────────────────────────────────────────────────

    def build_and_save_model(
        self, part_number: str, images: List[np.ndarray],
        roi: Union[dict, List[dict]],
        match_threshold: float = DEFAULT_MATCH_THRESHOLD,
    ) -> str:
        """Crop one template per reference image and save them as the part's model.

        `roi` is either a single box applied to every image, or one box per image.
        Per-image boxes matter whenever the part is not rigidly fixtured: a shared
        box lands on background in any reference where the part sat elsewhere, and
        a background template matches the live background at a high score — which
        would pass an empty fixture.
        """
        rois = list(roi) if isinstance(roi, (list, tuple)) else [roi] * len(images)
        if len(rois) != len(images):
            raise ValueError(
                f"Got {len(rois)} regions for {len(images)} reference images."
            )

        templates = []
        for n, (img, r) in enumerate(zip(images, rois), start=1):
            if r is None:
                raise ValueError(f"Reference image {n} has no region marked.")
            x, y, w, h = r["x"], r["y"], r["width"], r["height"]
            if w < 10 or h < 10:
                raise ValueError(f"Region on reference image {n} is too small.")
            # Whole image, then crop: the enhancement depends on what is round
            # the part, as it will in the live frame
            img = enhance(img)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
            if y + h > gray.shape[0] or x + w > gray.shape[1]:
                raise ValueError(
                    f"Region on reference image {n} falls outside it."
                )
            templates.append(gray[y:y + h, x:x + w])

        model_cfg = {
            "part_number": part_number,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "rois": rois,
            "roi": rois[0],          # older readers expect a single region
            "match_threshold": match_threshold,
            "num_references": len(templates),
            ENHANCED: True,
        }

        os.makedirs(_MODELS_DIR, exist_ok=True)
        filename = f"{part_number}.npz"
        model_path = os.path.join(_MODELS_DIR, filename)

        save_dict = {"config": json.dumps(model_cfg)}
        for i, t in enumerate(templates):
            save_dict[f"template_{i}"] = t
        np.savez_compressed(model_path, **save_dict)

        self.config.setdefault("part_mapping", {})[part_number] = filename
        save_vision_config(self.config)
        self._model_cache.pop(part_number, None)

        return model_path

    def delete_model(self, part_number: str):
        path = self._model_path(part_number)
        if path and os.path.exists(path):
            os.remove(path)
        self.config.get("part_mapping", {}).pop(part_number, None)
        save_vision_config(self.config)
        self._model_cache.pop(part_number, None)

    def model_info(self, part_number: str) -> Optional[dict]:
        """Metadata for a taught part, or None if it has no usable model."""
        model = self._load_model(part_number)
        if model is None:
            return None
        templates = model.get("templates", [])
        roi = model.get("roi") or {}
        rois = model.get("rois") or ([roi] if roi else [])
        sizes = ([(t.shape[1], t.shape[0]) for t in templates] if templates else
                 [(r.get("width", 0), r.get("height", 0)) for r in rois])
        return {
            "references": model.get("num_references", len(templates)),
            "created": model.get("created", "—"),
            "threshold": model.get("match_threshold", self.config.get("match_threshold", DEFAULT_MATCH_THRESHOLD)),
            "roi": roi,
            "rois": rois,
            "template_size": sizes[0] if sizes else (0, 0),
            "template_sizes": sizes,
            "uniform_templates": len(set(sizes)) <= 1,
        }

    def set_model_threshold(self, part_number: str, threshold: float):
        """Rewrite a taught model's own threshold.

        A model carries the threshold it was taught with and that value wins over
        the global default at inspection time, so tuning a part has to reach into
        the .npz rather than the config file.
        """
        path = self._model_path(part_number)
        if path is None:
            raise ValueError(f"No model file mapped to '{part_number}'.")

        data = np.load(path, allow_pickle=True)
        model_cfg = json.loads(str(data["config"]))
        model_cfg["match_threshold"] = float(threshold)

        save_dict = {"config": json.dumps(model_cfg)}
        i = 0
        while f"template_{i}" in data:
            save_dict[f"template_{i}"] = data[f"template_{i}"]
            i += 1
        np.savez_compressed(path, **save_dict)
        self._model_cache.pop(part_number, None)

    def map_model_file(self, part_number: str, filename: str):
        """Point a part number at an existing model file in vision_models/."""
        if not os.path.exists(os.path.join(_MODELS_DIR, filename)):
            raise ValueError(f"Model file '{filename}' not found.")
        self.config.setdefault("part_mapping", {})[part_number] = filename
        save_vision_config(self.config)
        self._model_cache.pop(part_number, None)
