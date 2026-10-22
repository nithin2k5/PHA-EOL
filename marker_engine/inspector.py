"""High-level, UI-free entry point.

    from marker_engine import Inspector
    insp = Inspector.from_config("BLK-3xGRY", config_dir="path/to/config")
    result = insp.inspect(frame_bgr)          # one frame
    print(result.status, result.reasons)
    cv2.imwrite("annotated.png", insp.annotate(frame_bgr, result))

For a production line use ``InspectionCycle`` (several fresh, stable frames
per cable) or the threaded ``CameraWorker`` + ``InspectionProcessor`` pair;
this class judges single frames only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .annotate import annotate, magnified_view
from .calibration import TeachSession
from .detection import Measurement, detect
from .evaluation import Verdict, apply_identification, evaluate
from .presets import PresetStore
from .recipes import Recipe, RecipeStore


@dataclass
class InspectionResult:
    verdict: Verdict
    measurement: Measurement
    identified: str | None = None
    candidates: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.verdict.status

    @property
    def reasons(self) -> list[str]:
        return self.verdict.reasons

    def to_dict(self) -> dict:
        return self.verdict.to_dict()


class Inspector:
    """Judge frames against one recipe, optionally identifying the cable type."""

    def __init__(self, recipe: Recipe, presets: PresetStore | None = None,
                 all_recipes: list[Recipe] | None = None):
        self.recipe = recipe
        self.presets = presets or PresetStore()
        self.all_recipes = all_recipes or []

    @classmethod
    def from_config(cls, recipe_name: str, config_dir: str | Path | None = None,
                    identify: bool = False) -> "Inspector":
        """Load a recipe (and presets) from a config folder holding recipes.json / colour_presets.json."""
        if config_dir is None:
            store, presets = RecipeStore(), PresetStore()
        else:
            config_dir = Path(config_dir)
            store = RecipeStore(config_dir / "recipes.json")
            presets = PresetStore(config_dir / "colour_presets.json")
        recipe = store.get(recipe_name)
        if recipe is None:
            raise KeyError(f"recipe {recipe_name!r} not found (have: {store.names()})")
        return cls(recipe, presets, store.all() if identify else None)

    def inspect(self, frame_bgr: np.ndarray) -> InspectionResult:
        """Inspect one BGR frame (raises detection.SetupError if regions are missing)."""
        m = detect(frame_bgr, self.recipe)
        v = evaluate(m, self.recipe, self.presets)
        if self.all_recipes:
            apply_identification(v, m, self.all_recipes, self.presets)
        return InspectionResult(v, m, v.identified, v.identify_candidates)

    def annotate(self, frame_bgr: np.ndarray, result: InspectionResult) -> np.ndarray:
        return annotate(frame_bgr, result.measurement, result.status,
                        self.recipe.roi, self.recipe.inspection_region)

    def stripe_view(self, result: InspectionResult) -> np.ndarray | None:
        return magnified_view(result.measurement)

    def teach(self, good_frames: list[np.ndarray]) -> list[str]:
        """Teach colours/geometry from good frames into self.recipe; returns warnings."""
        session = TeachSession()
        for f in good_frames:
            session.add_auto(f, self.recipe)
        return session.build(self.recipe)
