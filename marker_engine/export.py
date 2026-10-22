"""On-demand export of the current inspection result.

There is no automatic data logging: the operator presses "Export Result" to
save the latched result (JSON summary, raw frame and annotated frame) into a
folder of their choice.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import cv2

from .annotate import annotate, magnified_view
from .cycle import CycleResult


def _imwrite(path: Path, img) -> None:
    ok, buf = cv2.imencode(path.suffix, img)
    if ok:
        buf.tofile(str(path))  # works with non-ASCII paths on Windows


def export_result(result: CycleResult, folder: Path | str, recipe=None) -> list[Path]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{result.cycle_id}_{result.status.replace(' ', '_')}"
    written: list[Path] = []
    summary = {
        "cycle_id": result.cycle_id,
        "trigger": result.trigger,
        "started": datetime.fromtimestamp(result.started).isoformat(timespec="milliseconds"),
        "finished": datetime.fromtimestamp(result.finished).isoformat(timespec="milliseconds"),
        "frames_used": result.frames_used,
        "frame_statuses": result.frame_statuses,
        **result.verdict.to_dict(),
    }
    if recipe is not None:
        summary["recipe_settings"] = {
            "body_ref": recipe.body_ref, "stripe_ref": recipe.stripe_ref,
            "detection": recipe.detection, "geometry": recipe.geometry, "camera": recipe.camera,
        }
    fr = result.frame
    if fr is not None and fr.image is not None:
        raw = folder / f"{stem}_raw.png"
        _imwrite(raw, fr.image)
        written.append(raw)
        ann = annotate(fr.image, fr.measurement, result.status,
                       getattr(recipe, "roi", None), getattr(recipe, "inspection_region", None))
        p = folder / f"{stem}_annotated.png"
        _imwrite(p, ann)
        written.append(p)
        mag = magnified_view(fr.measurement)
        if mag is not None:
            p = folder / f"{stem}_stripes.png"
            _imwrite(p, mag)
            written.append(p)
        summary["measurement"] = {
            "presence": fr.measurement.presence,
            "issues": fr.measurement.issues,
            "edge_snr": round(fr.measurement.edge_snr, 2),
            "edge_consistency": round(fr.measurement.edge_consistency, 2),
            "occluded_frac": round(fr.measurement.occluded_frac, 2),
            "stripe_edge_width_px": round(fr.measurement.stripe_edge_width_px, 2),
            "rejected_candidates": [{"start": c.start, "end": c.end, "reason": c.reason}
                                    for c in fr.measurement.rejected],
        }
    js = folder / f"{stem}.json"
    js.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    written.append(js)
    return written
