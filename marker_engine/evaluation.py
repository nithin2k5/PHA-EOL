"""Turn a ``Measurement`` into a verdict against a recipe.

Decision rules (in order):

* NO_CABLE      - presence check failed; never PASS.
* INDETERMINATE - the image is unreliable (blur, glare, obstruction, cut
                  stripe, uncertain presence) or a colour is marginal.
* FAIL          - a clear mismatch: wrong body colour, missing/extra stripe,
                  wrong stripe colour, spacing/width out of tolerance.
* PASS          - everything enabled matches.

CAMERA_ERROR is produced by the inspection cycle, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .colors import nearest_colour_name
from .detection import ABSENT, PRESENT, Measurement

PASS = "PASS"
FAIL = "FAIL"
INDETERMINATE = "INDETERMINATE"
NO_CABLE = "NO CABLE"
CAMERA_ERROR = "CAMERA ERROR"
UNKNOWN = "UNKNOWN"
AMBIGUOUS = "AMBIGUOUS"


@dataclass
class Verdict:
    status: str
    reasons: list[str] = field(default_factory=list)
    recipe_name: str = ""
    recipe_version: int = 0
    expected: dict = field(default_factory=dict)
    observed: dict = field(default_factory=dict)
    checks: list[tuple[str, bool | None, str]] = field(default_factory=list)
    identified: str | None = None
    identify_candidates: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return self.reasons[0] if self.reasons else self.status

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "recipe": self.recipe_name,
            "recipe_version": self.recipe_version,
            "expected": self.expected,
            "observed": self.observed,
            "checks": [{"check": n, "ok": ok, "detail": d} for n, ok, d in self.checks],
            "identified": self.identified,
            "identify_candidates": self.identify_candidates,
        }


def _reference_list(recipe, presets):
    """Named references for labelling observed colours (taught recipe colours win)."""
    refs = {r.name: r for r in presets.all()}
    for ref in (recipe.body_reference(presets), recipe.stripe_reference(presets)):
        if ref is not None:
            refs[ref.name] = ref
    return list(refs.values())


def _competitor(lab, expected_ref, refs) -> tuple[str, float] | None:
    """A different named colour that fits the observation better than the expected one."""
    d_exp = expected_ref.distance(lab)
    best = None
    for r in refs:
        if r.name == expected_ref.name:
            continue
        d = r.distance(lab)
        if d <= 1.0 and d < d_exp and (best is None or d < best[1]):
            best = (r.name, d)
    return best


def _lab_list(lab) -> list[float] | None:
    return None if lab is None else [round(float(v), 1) for v in np.asarray(lab)]


def evaluate(m: Measurement, recipe, presets) -> Verdict:
    cfg = recipe.detection
    geo = recipe.geometry
    marginal = float(cfg.get("marginal_factor", 1.35))
    v = Verdict(status=INDETERMINATE, recipe_name=recipe.name, recipe_version=recipe.version,
                expected={"body_colour": recipe.body_colour, "stripe_colour": recipe.stripe_colour,
                          "stripe_count": recipe.stripe_count})
    refs = _reference_list(recipe, presets)
    mm = geo.get("mm_per_px")

    # observed values (filled even when the verdict is not PASS/FAIL)
    obs = {"stripe_count": len(m.stripes), "angle_deg": round(m.angle_deg, 2),
           "cable_width_px": round(m.cable_width_px, 1), "body_lab": _lab_list(m.body_lab)}
    if m.body_lab is not None:
        obs["body_colour"] = nearest_colour_name(m.body_lab, refs)[0]
    stripes_obs = []
    prev = None
    for st in m.stripes:
        st.colour_name = nearest_colour_name(st.lab, refs)[0]
        d = {"index": st.index, "colour": st.colour_name, "lab": _lab_list(st.lab),
             "width_px": round(st.width_px, 1), "position_px": round(st.centre_px, 1),
             "coverage": round(st.coverage, 2)}
        if prev is not None:
            d["spacing_px"] = round(st.centre_px - prev.centre_px, 1)
        if mm:
            d["width_mm"] = round(st.width_px * mm, 2)
            d["position_mm"] = round(st.centre_px * mm, 2)
        stripes_obs.append(d)
        prev = st
    obs["stripes"] = stripes_obs
    v.observed = obs

    if m.presence == ABSENT:
        v.status = NO_CABLE
        v.reasons = ["no cable detected in the inspection region"] + m.notes
        return v
    issues = list(m.issues)
    ref_l = getattr(recipe, "lighting_ref_l", None)
    if ref_l is not None and m.background_l is not None:
        d_l = m.background_l - ref_l
        obs["background_l"] = round(m.background_l, 1)
        if abs(d_l) > float(cfg.get("lighting_tol_l", 10.0)):
            issues.append(f"illumination differs from teach (fixture L* {m.background_l:.0f} vs "
                          f"{ref_l:.0f}) - check lighting/exposure")
    if m.presence != PRESENT or issues:
        v.status = INDETERMINATE
        v.reasons = issues or ["cable presence uncertain"]
        return v

    fails: list[str] = []
    marg: list[str] = []

    # -- body colour --------------------------------------------------------
    body_ref = recipe.body_reference(presets)
    if body_ref is None:
        marg.append(f"no colour reference for body colour '{recipe.body_colour}'")
        v.checks.append(("body colour", None, "no reference"))
    else:
        d = body_ref.distance(m.body_lab)
        txt = f"observed {obs.get('body_colour', '?')} (score {d:.2f})"
        comp = _competitor(m.body_lab, body_ref, refs) if d <= 1.0 else None
        if comp:
            v.checks.append(("body colour", None, txt + f" - closer to {comp[0]}"))
            marg.append(f"body colour closer to {comp[0]} than to {recipe.body_colour}")
        elif d <= 1.0:
            v.checks.append(("body colour", True, txt))
        elif d <= marginal:
            v.checks.append(("body colour", None, txt + " - marginal"))
            marg.append(f"body colour marginal vs {recipe.body_colour} (score {d:.2f})")
        else:
            v.checks.append(("body colour", False, txt))
            fails.append(f"wrong cable colour: expected {recipe.body_colour}, observed {obs.get('body_colour')}")

    # -- stripe count -----------------------------------------------------------
    n, exp = len(m.stripes), int(recipe.stripe_count)
    if n == exp:
        v.checks.append(("stripe count", True, f"{n} of {exp}"))
    elif n < exp:
        v.checks.append(("stripe count", False, f"{n} of {exp}"))
        k = exp - n
        fails.append(f"missing stripe{'s' if k > 1 else ''}: found {n}, expected {exp}")
    else:
        v.checks.append(("stripe count", False, f"{n} of {exp}"))
        k = n - exp
        fails.append(f"extra stripe{'s' if k > 1 else ''}: found {n}, expected {exp}")

    # -- stripe colours ---------------------------------------------------------
    stripe_ref = recipe.stripe_reference(presets)
    if m.stripes and stripe_ref is None:
        marg.append(f"no colour reference for stripe colour '{recipe.stripe_colour}'")
    elif stripe_ref is not None:
        for st in m.stripes:
            d = stripe_ref.distance(st.lab)
            comp = _competitor(st.lab, stripe_ref, refs) if d <= 1.0 else None
            if comp:
                st.colour_ok = None
                marg.append(f"stripe {st.index} colour closer to {comp[0]} than to {recipe.stripe_colour}")
            elif d <= 1.0:
                st.colour_ok = True
            elif d <= marginal:
                st.colour_ok = None
                marg.append(f"stripe {st.index} colour marginal vs {recipe.stripe_colour} (score {d:.2f})")
            else:
                st.colour_ok = False
        # one reason per observed colour: "stripes 1, 2, 3 wrong colour: ..."
        wrong: dict[str, list[int]] = {}
        for st in m.stripes:
            if st.colour_ok is False:
                wrong.setdefault(st.colour_name, []).append(st.index)
        for colour, idx in wrong.items():
            which = f"stripe {idx[0]}" if len(idx) == 1 else "stripes " + ", ".join(map(str, idx))
            fails.append(f"{which} wrong colour: expected {recipe.stripe_colour}, observed {colour}")
        if m.stripes:
            bad = [st.index for st in m.stripes if st.colour_ok is False]
            v.checks.append(("stripe colours", not bad if all(s.colour_ok is not None for s in m.stripes) else None,
                             "all match" if not bad else f"wrong: {bad}"))

    # -- optional geometry --------------------------------------------------------
    if geo.get("check_spacing") and len(m.stripes) >= 2:
        exp_sp = geo.get("expected_spacing_px")
        tol = float(geo.get("spacing_tol_px", 6.0))
        if exp_sp:
            spacings = np.diff([st.centre_px for st in m.stripes])
            worst = float(np.max(np.abs(spacings - exp_sp)))
            ok = worst <= tol
            v.checks.append(("stripe spacing", ok, f"max deviation {worst:.1f}px (tol {tol:.1f})"))
            if not ok:
                fails.append(f"stripe spacing out of tolerance: deviation {worst:.1f}px > {tol:.1f}px")
        else:
            v.checks.append(("stripe spacing", None, "no expected spacing taught"))
    if geo.get("check_width") and m.stripes:
        exp_w = geo.get("expected_width_px")
        tol = float(geo.get("width_tol_px", 4.0))
        if exp_w:
            worst = max(abs(st.width_px - exp_w) for st in m.stripes)
            ok = worst <= tol
            v.checks.append(("stripe width", ok, f"max deviation {worst:.1f}px (tol {tol:.1f})"))
            if not ok:
                fails.append(f"stripe width out of tolerance: deviation {worst:.1f}px > {tol:.1f}px")
        else:
            v.checks.append(("stripe width", None, "no expected width taught"))

    if fails:
        v.status = FAIL
        v.reasons = fails + marg
    elif marg:
        v.status = INDETERMINATE
        v.reasons = marg
    else:
        v.status = PASS
        v.reasons = [f"{recipe.describe()} verified"]
    return v


def identify(m: Measurement, recipes, presets) -> tuple[str, list[str]]:
    """Compare a measurement with every recipe.

    Returns (type, matching recipe names).  A type is returned only when
    exactly one recipe passes; otherwise UNKNOWN (none) or AMBIGUOUS (several).
    An unreliable image yields INDETERMINATE and NO_CABLE stays NO_CABLE.

    The measurement comes from the selected recipe's regions, so recipes that
    are compared should share the same fixture position.
    """
    if m.presence == ABSENT:
        return NO_CABLE, []
    if m.presence != PRESENT or m.issues:
        return INDETERMINATE, []
    matches = []
    for r in recipes:
        if evaluate(m, r, presets).status == PASS:
            matches.append(r.name)
    if len(matches) == 1:
        return matches[0], matches
    return (AMBIGUOUS if matches else UNKNOWN), matches


def apply_identification(v: Verdict, m: Measurement, recipes, presets) -> Verdict:
    ident, cands = identify(m, recipes, presets)
    v.identified = ident
    v.identify_candidates = cands
    return v
