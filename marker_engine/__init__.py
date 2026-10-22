"""marker_engine - cable body-colour and stripe inspection (classical machine vision).

Self-contained: depends only on numpy and opencv-python (no UI code).
See README.md in this folder for usage.
"""

__version__ = "0.2.0"

from .colors import ColourReference, delta_e_2000  # noqa: E402
from .detection import ABSENT, PRESENT, UNCERTAIN, Measurement, SetupError, Stripe, detect  # noqa: E402
from .evaluation import (AMBIGUOUS, CAMERA_ERROR, FAIL, INDETERMINATE, NO_CABLE, PASS,  # noqa: E402
                         UNKNOWN, Verdict, evaluate, identify)
from .inspector import InspectionResult, Inspector  # noqa: E402
from .presets import PresetStore  # noqa: E402
from .recipes import Recipe, RecipeStore  # noqa: E402

__all__ = [
    "Inspector", "InspectionResult", "Recipe", "RecipeStore", "PresetStore", "ColourReference",
    "detect", "evaluate", "identify", "Measurement", "Stripe", "Verdict", "SetupError",
    "PASS", "FAIL", "INDETERMINATE", "NO_CABLE", "CAMERA_ERROR", "UNKNOWN", "AMBIGUOUS",
    "PRESENT", "ABSENT", "UNCERTAIN", "delta_e_2000",
]
