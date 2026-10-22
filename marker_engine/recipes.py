"""Cable recipes: expected colours/stripe count plus calibration and settings.

Each recipe carries everything needed to reproduce an inspection:
regions of interest, taught colour references, camera settings and
detection thresholds.  The version number increments on every save so an
exported result can be traced back to the exact settings used.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from .colors import ColourReference
from .config import (DEFAULT_CAMERA, DEFAULT_CYCLE, DEFAULT_DETECTION, DEFAULT_GEOMETRY,
                     RECIPES_FILE)

Rect = tuple[int, int, int, int]  # x, y, w, h in original image pixels


def _merged(defaults: dict, values: dict | None) -> dict:
    out = copy.deepcopy(defaults)
    out.update(values or {})
    return out


@dataclass
class Recipe:
    name: str
    body_colour: str = "black"
    stripe_colour: str = "white"
    stripe_count: int = 3
    version: int = 0
    roi: Rect | None = None                 # region around the cable
    inspection_region: Rect | None = None   # smaller region containing the stripes
    image_size: tuple[int, int] | None = None  # (w, h) of the image the regions were drawn on
    body_ref: dict | None = None            # taught ColourReference dicts
    stripe_ref: dict | None = None
    cable_width_px: float | None = None     # taught cable width (across axis)
    lighting_ref_l: float | None = None     # taught fixture background lightness (L*)
    camera: dict = field(default_factory=dict)
    detection: dict = field(default_factory=dict)
    geometry: dict = field(default_factory=dict)
    cycle: dict = field(default_factory=dict)
    updated: str = ""
    notes: str = ""

    def __post_init__(self):
        self.camera = _merged(DEFAULT_CAMERA, self.camera)
        self.detection = _merged(DEFAULT_DETECTION, self.detection)
        self.geometry = _merged(DEFAULT_GEOMETRY, self.geometry)
        self.cycle = _merged(DEFAULT_CYCLE, self.cycle)
        for attr in ("roi", "inspection_region", "image_size"):
            v = getattr(self, attr)
            if v is not None:
                setattr(self, attr, tuple(int(x) for x in v))

    # -- colour references ----------------------------------------------------
    def body_reference(self, presets) -> ColourReference | None:
        """Calibrated body colour if taught for this recipe, else the named preset."""
        if self.body_ref and self.body_ref.get("name") == self.body_colour:
            return ColourReference.from_dict(self.body_ref)
        return presets.get(self.body_colour)

    def stripe_reference(self, presets) -> ColourReference | None:
        if self.stripe_ref and self.stripe_ref.get("name") == self.stripe_colour:
            return ColourReference.from_dict(self.stripe_ref)
        return presets.get(self.stripe_colour)

    @property
    def is_calibrated(self) -> bool:
        return bool(self.body_ref and self.stripe_ref)

    @property
    def has_regions(self) -> bool:
        return self.roi is not None and self.inspection_region is not None

    def describe(self) -> str:
        return f"{self.body_colour} body / {self.stripe_count} x {self.stripe_colour} stripes"

    # -- serialisation ----------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Recipe":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def copy(self) -> "Recipe":
        return Recipe.from_dict(copy.deepcopy(self.to_dict()))


class RecipeStore:
    def __init__(self, path: Path | str = RECIPES_FILE):
        self.path = Path(path)
        self._recipes: dict[str, Recipe] = {}
        self.load()

    def load(self) -> None:
        self._recipes = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for d in data.get("recipes", []):
                r = Recipe.from_dict(d)
                self._recipes[r.name] = r

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"recipes": [r.to_dict() for r in self._recipes.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def names(self) -> list[str]:
        return list(self._recipes)

    def get(self, name: str) -> Recipe | None:
        r = self._recipes.get(name)
        return r.copy() if r else None

    def all(self) -> list[Recipe]:
        return [r.copy() for r in self._recipes.values()]

    def save(self, recipe: Recipe) -> Recipe:
        """Persist a recipe, bumping its version.  Returns the stored copy."""
        stored = recipe.copy()
        prev = self._recipes.get(stored.name)
        stored.version = (prev.version if prev else stored.version) + 1
        stored.updated = datetime.now().isoformat(timespec="seconds")
        self._recipes[stored.name] = stored
        self._write()
        return stored.copy()

    def delete(self, name: str) -> None:
        if self._recipes.pop(name, None) is not None:
            self._write()
