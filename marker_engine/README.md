# marker_engine

Standalone engine for checking a cable's **body colour**, **stripe count** and **stripe colour**
in a fixed camera fixture, using classical image processing. It returns PASS / FAIL /
INDETERMINATE / NO CABLE / CAMERA ERROR with specific reasons.

There is no UI code in this package. Copy the `marker_engine/` folder into any Python project.

**Requirements:** Python ≥ 3.10, `numpy`, `opencv-python` (see `requirements.txt`).

## Quick start

```python
import cv2
from marker_engine import Inspector

insp = Inspector.from_config("EXAMPLE-BLK-3xGRY", config_dir="config", identify=True)
frame = cv2.imread("frame.png")            # BGR image from your camera
result = insp.inspect(frame)

print(result.status)       # "PASS", "FAIL", "INDETERMINATE", "NO CABLE"
print(result.reasons)      # e.g. ["stripes 1, 2, 3 wrong colour: expected grey, observed red"]
print(result.identified)   # recipe name, "UNKNOWN" or "AMBIGUOUS" (when identify=True)
cv2.imwrite("annotated.png", insp.annotate(frame, result))
```

`config_dir` holds `recipes.json` and `colour_presets.json` (copy them from the original project's
`config/` folder). If you omit it, the engine looks in:

1. the `MARKER_ENGINE_CONFIG` environment variable, or else
2. a `config/` folder next to the `marker_engine` package.

### Creating a recipe in code

```python
from marker_engine import Inspector, Recipe

recipe = Recipe("BLK-3xWHT", body_colour="black", stripe_colour="white", stripe_count=3,
                roi=(250, 110, 700, 100),               # x, y, w, h around the cable
                inspection_region=(420, 120, 260, 80),  # smaller box containing the stripes
                image_size=(1920, 1080))                # resolution the boxes were drawn on
insp = Inspector(recipe)
warnings = insp.teach([good_frame_1, good_frame_2, good_frame_3])   # known-good cables
result = insp.inspect(new_frame)

from marker_engine import RecipeStore
RecipeStore("config/recipes.json").save(insp.recipe)    # persist (version auto-increments)
```

## One result per cable (production use)

`Inspector.inspect()` judges a single frame. On a line, use the cycle logic, which takes several
fresh, stable frames per cable, latches the result, and re-arms automatic mode only after the
cable has been removed:

```python
import queue, time
from marker_engine.camera import CameraWorker, FrameStore, make_source
from marker_engine.processing import InspectionProcessor

store, out = FrameStore(), queue.Queue()
CameraWorker(make_source(0, recipe.camera), store).start()   # USB camera 0 (or a video/image path)
proc = InspectionProcessor(store, presets, out)
proc.start()
proc.send("recipe", recipe)
proc.send("auto", True)              # or proc.send("trigger") for one manual inspection

while True:
    msg = out.get()
    if msg[0] == "result":           # CycleResult: one per cable / trigger
        res = msg[1]
        print(res.cycle_id, res.status, res.verdict.reasons)
```

The queue also carries `("frame", image, measurement, verdict)` previews, `("phase", state)`,
`("camera_error", msg)` and `("setup_error", msg)`. You can also drive `cycle.InspectionCycle`
yourself with your own frame source.

## Modules

| Module | Contents |
|---|---|
| `inspector.py` | `Inspector` facade (inspect / annotate / teach) |
| `detection.py` | `detect(frame, recipe) -> Measurement`: cable localisation, presence, stripes, colours |
| `evaluation.py` | `evaluate(measurement, recipe, presets) -> Verdict`, `identify(...)` |
| `recipes.py` | `Recipe` (regions, taught colours, thresholds, camera settings), `RecipeStore` (JSON) |
| `colors.py` | Lab conversion, CIEDE2000, `ColourReference` (achromatic/pale handling) |
| `presets.py` | editable named colours, `PresetStore` (JSON) |
| `calibration.py` | `TeachSession`: teach from good samples (auto or patches) |
| `cycle.py` | `InspectionCycle`: fresh stable frames, latching, auto re-arm, camera-error rules |
| `camera.py` | `CameraWorker` thread, `FrameStore`, USB/video/image sources |
| `processing.py` | `InspectionProcessor` thread (camera → detection → cycle → output queue) |
| `annotate.py` | overlay drawing and the magnified, straightened stripe view |
| `export.py` | save a result as JSON plus raw, annotated and stripe images |
| `geometry.py` | preview ↔ image coordinate mapping, rect helpers |
| `alerts.py` | repeating audible alarm (winsound on Windows, optional fallback callback) |
| `synthetic.py` | synthetic fixture scenes for tests and demos |
| `config.py` | default thresholds and config paths |

Thresholds are per recipe (`recipe.detection`, `recipe.geometry`, `recipe.cycle`,
`recipe.camera`); defaults are in `config.py`.

## Important notes

* Regions are in original-image pixels. If the camera position or framing changes, redraw them and
  teach again. A stripe region that misses the cable gives INDETERMINATE, never FAIL.
* Teach with the production camera and lighting, using fixed exposure, white balance and focus.
  Clipped (saturated) stripes give INDETERMINATE.
* Validate with many real good and bad cables before trusting the results (see the main project's
  `tools/evaluate.py` and README). One camera inspects only the visible side of the cable.
