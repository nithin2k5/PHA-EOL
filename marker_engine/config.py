"""Paths and engine-wide defaults.

When marker_engine is copied into another project, PROJECT_ROOT is the folder
that contains the marker_engine package, so recipes/presets are looked up in
``<that folder>/config/``.  Override with the MARKER_ENGINE_CONFIG environment
variable, or pass explicit paths to RecipeStore(...) / PresetStore(...).
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("MARKER_ENGINE_CONFIG")
                  or os.environ.get("CABLE_INSPECTION_CONFIG")
                  or PROJECT_ROOT / "config")
RECIPES_FILE = CONFIG_DIR / "recipes.json"
PRESETS_FILE = CONFIG_DIR / "colour_presets.json"
EXPORT_DIR = PROJECT_ROOT / "exports"
SAMPLES_DIR = PROJECT_ROOT / "samples"

# Inspection cycle defaults (can be overridden per recipe under "cycle").
DEFAULT_CYCLE = {
    "frames_required": 5,        # fresh stable frames needed to finalise one inspection
    "cycle_timeout_s": 4.0,      # give up (INDETERMINATE) if no stable set within this time
    "presence_frames": 3,        # consecutive "cable present" frames before auto mode starts a cycle
    "removal_frames": 5,         # consecutive "no cable" frames before auto mode re-arms
    "stability_px": 4.0,         # max movement of cable edges between frames to count as stable
    "stability_deg": 1.5,        # max change of cable angle between frames
}

# Detection defaults (overridden per recipe under "detection").
DEFAULT_DETECTION = {
    "max_angle_deg": 12.0,         # search range for cable rotation
    "min_cable_width_frac": 0.15,  # cable width as fraction of ROI height, lower bound
    "max_cable_width_frac": 0.95,
    "width_tolerance": 0.20,       # allowed deviation from taught cable width (fraction)
    "edge_snr_min": 3.0,           # edge peak strength / median gradient required for presence
    "edge_consistency_min": 0.6,   # fraction of slices that must agree on cable edges
    "occlusion_max": 0.34,         # fraction of slices with missing edges tolerated
    "band_inset": 0.18,            # fraction of cable width ignored at each edge (shading)
    "stripe_delta_e_min": 12.0,    # minimum colour difference of stripe vs body (CIEDE2000)
    "stripe_mad_k": 5.0,           # adaptive threshold = median + k * MAD of body differences
    "ref_window_widths": 6.0,      # local body reference window, in cable widths (min 80px)
    "body_uniformity_max": 30.0,   # dE between body either side of the stripes -> INDETERMINATE
    "min_coverage": 0.65,          # stripe must cover this fraction of the cable width
    "min_stripe_width_px": 2,
    "max_stripe_width_px": 80,
    "merge_gap_px": 3,             # fragments closer than this along the axis are one stripe
    "min_fill": 0.5,               # pixel fill ratio inside a stripe band
    "group_stripes": True,         # with a taught pitch, ignore candidates far from the stripe group
    "max_edge_width_px": 6.0,      # stripe edge transition wider than this -> too blurred
    "min_edge_strength": 3.0,      # minimum Lab gradient of cable edges (colour-independent)
    "glare_max": 0.10,             # fraction of region columns blinded by reflection tolerated
    "glare_v": 245,                # pixel value treated as saturated
    "dark_max": 0.98,              # fraction of near-black pixels in whole ROI -> underexposed
    "lighting_tol_l": 10.0,        # fixture brightness change vs teach (L*) -> INDETERMINATE
    "marginal_factor": 1.35,       # colour distance between 1.0 and this -> INDETERMINATE
}

DEFAULT_GEOMETRY = {
    "check_spacing": False,
    "expected_spacing_px": None,   # centre-to-centre spacing; None -> learnt at teach time
    "spacing_tol_px": 6.0,
    "check_width": False,
    "expected_width_px": None,
    "width_tol_px": 4.0,
    "mm_per_px": None,             # physical scale; None -> report pixels only
}

DEFAULT_CAMERA = {
    "index": 0,
    "width": 1280,
    "height": 720,
    "fps": 30,
    "auto_exposure": True,
    "exposure": None,
    "auto_focus": True,
    "focus": None,
    "auto_white_balance": True,
    "white_balance": None,
    "gain": None,
}
