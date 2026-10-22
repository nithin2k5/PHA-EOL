"""Editable named colour presets.

The shipped values are generic starting points only.  They must be
re-calibrated with the real camera and lighting ("Calibrate preset from
patch" in the UI) before they are trusted - colour as seen by a camera
depends strongly on illumination, white balance and exposure.
"""

from __future__ import annotations

import json
from pathlib import Path

from .colors import ColourReference
from .config import PRESETS_FILE

DEFAULT_PRESETS = [
    ColourReference("black", [14, 0, 0], achromatic=True, tol_l=14, max_chroma=12),
    ColourReference("grey", [50, 0, 0], achromatic=True, tol_l=15, max_chroma=12),
    ColourReference("white", [90, 0, 0], achromatic=True, tol_l=13, max_chroma=12),
    ColourReference("red", [44, 60, 40], tol=14),
    ColourReference("orange", [64, 40, 64], tol=14),
    ColourReference("yellow", [86, -4, 78], tol=14),
    ColourReference("green", [50, -48, 28], tol=14),
    ColourReference("blue", [38, 18, -58], tol=14),
    ColourReference("violet", [36, 42, -42], tol=14),
    ColourReference("brown", [36, 18, 26], tol=12),
    ColourReference("pink", [72, 38, 4], tol=12),
    ColourReference("pale yellow", [92, -4, 24], tol=9),
]


class PresetStore:
    def __init__(self, path: Path | str = PRESETS_FILE):
        self.path = Path(path)
        self._refs: dict[str, ColourReference] = {}
        self.load()

    def load(self) -> None:
        self._refs = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for d in data.get("colours", []):
                ref = ColourReference.from_dict(d)
                self._refs[ref.name] = ref
        if not self._refs:
            for ref in DEFAULT_PRESETS:
                self._refs[ref.name] = ColourReference.from_dict(ref.to_dict())

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "note": "Lab (L 0-100). Calibrate with the production camera and lighting.",
            "colours": [r.to_dict() for r in self._refs.values()],
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def names(self) -> list[str]:
        return list(self._refs)

    def get(self, name: str) -> ColourReference | None:
        return self._refs.get(name)

    def all(self) -> list[ColourReference]:
        return list(self._refs.values())

    def put(self, ref: ColourReference) -> None:
        self._refs[ref.name] = ref

    def remove(self, name: str) -> None:
        self._refs.pop(name, None)
