"""Cable localisation, presence verification and stripe detection.

Pipeline (all classical image processing):

1.  Crop the operator-drawn ROI and convert to Lab.
2.  Find the cable as a pair of strong, parallel edges.  The edge signal is
    the Lab gradient across the cable axis, so it works for any cable colour
    (we never threshold "black").  A small rotation search straightens the
    cable, tolerating fixture/rotation variation.
3.  Verify presence independently of colour: edge strength, signal-to-noise,
    agreement between slices along the cable, and plausible width.
4.  Build the cable mask geometrically (the band between the edges), so it
    contains the body AND the stripes whatever their colours.
5.  Compute a per-row body reference (median along the cable, robust to the
    few stripe/text pixels) and a per-pixel CIEDE2000 difference map.  This
    removes the cylindrical shading and the longitudinal highlight.
6.  Stripe candidates = columns where the difference covers most of the
    cable width (profile along the axis), merged across small gaps,
    then checked for width, fill, orientation and objects crossing the cable.
    Printed text, spots, longitudinal reflections and other wires fail these
    geometric tests.  Colour is only checked afterwards, so a stripe of the
    wrong colour is still found and reported.
7.  Measure body colour excluding stripes, text, glare and background, and
    each stripe's colour, width and position.

Results are a ``Measurement``; deciding PASS/FAIL is done in ``evaluation``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from .colors import bgr_to_lab, delta_e_2000, robust_lab
from .geometry import adapt_rect_to_image, crop, invert_affine, rotation_matrix, transform_points

PRESENT, ABSENT, UNCERTAIN = "PRESENT", "ABSENT", "UNCERTAIN"


@dataclass
class Stripe:
    index: int
    start: int                 # along-axis start/end in straightened ROI coordinates
    end: int
    centre_px: float           # along-axis centre relative to the inspection region start
    width_px: float
    coverage: float            # fraction of cable width covered
    lab: np.ndarray
    polygon: np.ndarray        # 4x2 image coordinates
    saturated: bool = False
    pixels: np.ndarray | None = None  # Lab pixels used for the colour (for teaching)
    colour_name: str = ""      # filled by evaluation
    colour_ok: bool | None = None


@dataclass
class Candidate:
    start: int
    end: int
    reason: str


@dataclass
class Measurement:
    presence: str = ABSENT
    issues: list[str] = field(default_factory=list)       # reasons for an unreliable image
    notes: list[str] = field(default_factory=list)        # informational
    angle_deg: float = 0.0
    edge_top: float = 0.0          # straightened ROI y of cable edges
    edge_bottom: float = 0.0
    cable_width_px: float = 0.0
    edge_strength: float = 0.0
    edge_snr: float = 0.0
    edge_consistency: float = 0.0
    occluded_frac: float = 0.0
    edge_width_px: float = 0.0     # cable outline transition width (blur estimate)
    stripe_edge_width_px: float = 0.0  # stripe boundary transition width (preferred blur estimate)
    glare_frac: float = 0.0
    background_l: float | None = None  # median L* of the fixture around the cable (lighting reference)
    body_lab: np.ndarray | None = None
    body_pixels: int = 0
    body_sample: np.ndarray | None = None     # subsample of clean body Lab pixels (for teaching)
    stripes: list[Stripe] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    cable_polygon: np.ndarray | None = None
    region_polygon: np.ndarray | None = None
    straight_view: np.ndarray | None = None   # straightened inspection crop (BGR) for magnified view
    straight_offset: tuple[int, int] = (0, 0)  # (x0, y0) of straight_view in straightened ROI
    profile: np.ndarray | None = None
    elapsed_ms: float = 0.0

    @property
    def reliable(self) -> bool:
        return self.presence == PRESENT and not self.issues

    def signature(self) -> tuple[float, float, float]:
        """Position signature used to check frame-to-frame stability."""
        return (self.angle_deg, self.edge_top, self.edge_bottom)


class SetupError(Exception):
    """The recipe has no usable regions for this image."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _gradient_across(lab: np.ndarray) -> np.ndarray:
    """Magnitude of the Lab gradient in the y direction (across a horizontal cable)."""
    g = np.zeros(lab.shape[:2], np.float32)
    for c, w in ((0, 1.0), (1, 0.7), (2, 0.7)):
        gy = cv2.Sobel(lab[..., c], cv2.CV_32F, 0, 1, ksize=3) / 4.0
        g += (w * gy) ** 2
    return np.sqrt(g)


def _rotate(img: np.ndarray, angle: float, interp=cv2.INTER_LINEAR) -> np.ndarray:
    h, w = img.shape[:2]
    m = rotation_matrix((w / 2.0, h / 2.0), angle)
    return cv2.warpAffine(img, m, (w, h), flags=interp, borderMode=cv2.BORDER_REPLICATE)


def _peaks(profile: np.ndarray, min_rel: float = 0.12, min_sep: int = 3, max_peaks: int = 12) -> list[int]:
    p = profile
    if p.size < 3 or p.max() <= 0:
        return []
    thr = p.max() * min_rel
    idx = [i for i in range(1, len(p) - 1) if p[i] >= p[i - 1] and p[i] > p[i + 1] and p[i] >= thr]
    idx.sort(key=lambda i: -p[i])
    kept: list[int] = []
    for i in idx:
        if all(abs(i - k) >= min_sep for k in kept):
            kept.append(i)
            if len(kept) >= max_peaks:
                break
    return sorted(kept)


def _row_variation(lab: np.ndarray) -> np.ndarray:
    """Per-row robust variation along the axis (MAD of L*), insensitive to a few stripe columns."""
    L = lab[:, ::3, 0]
    med = np.median(L, axis=1, keepdims=True)
    return np.median(np.abs(L - med), axis=1)


def _signed_l_profile(lab: np.ndarray) -> np.ndarray:
    """Mean signed d(L*)/dy per row: distinguishes a step (boundary) from a thin ridge (highlight)."""
    return (cv2.Sobel(lab[..., 0], cv2.CV_32F, 0, 1, ksize=3) / 4.0).mean(axis=1)


def _is_ridge(signed: np.ndarray, p: int, reach: float) -> bool:
    """True if the edge at row p has an opposite-signed partner nearby (thin bright/dark line)."""
    v = signed[p]
    lo, hi = max(0, int(p - reach)), min(len(signed), int(p + reach) + 1)
    seg = signed[lo:hi]
    return bool(np.any(seg * np.sign(v) < -0.5 * abs(v)))


def _best_pair(profile: np.ndarray, wmin: float, wmax: float, row_var: np.ndarray | None = None,
               signed: np.ndarray | None = None, centre_weight: float = 0.15):
    """Choose the two edges (top, bottom) forming the most plausible cable.

    Besides edge strength, a real cable has (a) no longitudinal edge inside it
    comparable to its outline - stripes are transverse - and (b) an interior
    that is uniform along its length, unlike a strip of fixture background
    between two unrelated edges.
    """
    peaks = _peaks(profile)
    h = len(profile)
    pmax = float(profile.max()) if profile.size else 0.0
    reach = 0.3 * wmax
    ridge = {p: signed is not None and _is_ridge(signed, p, reach) for p in peaks}
    best = None
    for i, a in enumerate(peaks):
        for b in peaks[i + 1:]:
            width = b - a
            if width < wmin or width > wmax:
                continue
            edge = min(profile[a], profile[b])
            inner = [p for p in peaks if a + 0.15 * width < p < b - 0.15 * width and not ridge[p]]
            if inner and max(profile[p] for p in inner) > 0.6 * edge:
                continue
            score = edge
            if row_var is not None:
                lo, hi = int(a + 0.2 * width), int(b - 0.2 * width) + 1
                if hi > lo:
                    score = edge / (1.0 + float(np.median(row_var[lo:hi])) / 8.0)
            score -= centre_weight * pmax * abs((a + b) / 2.0 - h / 2.0) / (h / 2.0)
            if best is None or score > best[0]:
                best = (score, a, b)
    return best


def _subpixel(profile: np.ndarray, i: int) -> float:
    if 0 < i < len(profile) - 1:
        y0, y1, y2 = profile[i - 1], profile[i], profile[i + 1]
        den = y0 - 2 * y1 + y2
        if abs(den) > 1e-6:
            return i + 0.5 * (y0 - y2) / den
    return float(i)


def _runs(mask_1d: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) runs of True values."""
    m = np.concatenate([[False], mask_1d.astype(bool), [False]])
    d = np.diff(m.astype(np.int8))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def _merge_runs(runs, gap: int):
    out: list[list[int]] = []
    for s, e in runs:
        if out and s - out[-1][1] <= gap:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [tuple(r) for r in out]


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def detect(frame: np.ndarray, recipe, expected_width: float | None = None) -> Measurement:
    """Analyse one BGR frame using the regions and thresholds of ``recipe``."""
    t0 = time.perf_counter()
    if frame is None or frame.ndim != 3:
        raise SetupError("invalid frame")
    if not recipe.has_regions:
        raise SetupError("draw the cable ROI and stripe inspection region first")
    cfg = recipe.detection
    ih, iw = frame.shape[:2]
    roi = adapt_rect_to_image(recipe.roi, recipe.image_size, iw, ih)
    insp = adapt_rect_to_image(recipe.inspection_region, recipe.image_size, iw, ih)
    if roi[2] < 20 or roi[3] < 12 or insp[2] < 8:
        raise SetupError("regions too small or outside the image")
    if expected_width is None:
        expected_width = recipe.cable_width_px
        if expected_width and recipe.image_size and recipe.image_size[1] != ih:
            expected_width *= ih / recipe.image_size[1]
    cfg = dict(cfg)
    spacing = recipe.geometry.get("expected_spacing_px")
    if spacing and recipe.image_size and recipe.image_size[0] != iw:
        spacing *= iw / recipe.image_size[0]
    cfg["_expected_spacing"] = spacing if cfg.get("group_stripes", True) else None

    m = Measurement()
    roi_bgr = crop(frame, roi)
    rh, rw = roi_bgr.shape[:2]

    # -- exposure ----------------------------------------------------------
    grey = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    if np.mean(grey < 8) > cfg["dark_max"]:
        m.issues.append("image too dark (check lighting/exposure)")
        m.presence = UNCERTAIN
        m.elapsed_ms = (time.perf_counter() - t0) * 1000
        return m
    if np.mean(grey > 250) > 0.6:
        m.issues.append("image overexposed (reduce exposure/gain)")
        m.presence = UNCERTAIN
        m.elapsed_ms = (time.perf_counter() - t0) * 1000
        return m

    lab = bgr_to_lab(cv2.GaussianBlur(roi_bgr, (0, 0), 1.2))

    # -- width bounds -----------------------------------------------------------
    wmin = cfg["min_cable_width_frac"] * rh
    wmax = cfg["max_cable_width_frac"] * rh
    if expected_width:
        tol = cfg["width_tolerance"]
        wmin = max(wmin, expected_width * (1 - tol))
        wmax = min(wmax, expected_width * (1 + tol))
        wmin, wmax = min(wmin, wmax), max(wmin, wmax)

    # -- rotation search (half resolution) ------------------------------------
    # coarse search on roughly 400 x 60 px, but keep the cable at least ~6 px wide
    ds = int(max(1, min(np.ceil(max(rw / 400.0, rh / 60.0)), round(wmin / 6.0))))
    lab_s = cv2.resize(lab, (rw // ds, rh // ds), interpolation=cv2.INTER_AREA) if ds > 1 else lab

    def pair_score(img, angle, w_lo, w_hi):
        rimg = _rotate(img, angle)
        prof = _gradient_across(rimg).mean(axis=1)
        return _best_pair(prof, w_lo, w_hi, _row_variation(rimg), _signed_l_profile(rimg)), prof

    max_a = cfg["max_angle_deg"]
    best = None
    for a in np.arange(-max_a, max_a + 1e-6, 1.5):
        bp, _ = pair_score(lab_s, a, wmin / ds, wmax / ds)
        if bp and (best is None or bp[0] > best[0]):
            best = (bp[0], a)
    width_fallback = False
    if best is None and expected_width:
        # The cable may be the wrong gauge: retry without the taught width.
        wmin = cfg["min_cable_width_frac"] * rh
        wmax = cfg["max_cable_width_frac"] * rh
        width_fallback = True
        for a in np.arange(-max_a, max_a + 1e-6, 1.5):
            bp, _ = pair_score(lab_s, a, wmin / ds, wmax / ds)
            if bp and (best is None or bp[0] > best[0]):
                best = (bp[0], a)
    if best is None:
        m.presence = ABSENT
        m.notes.append("no pair of parallel cable edges found")
        m.elapsed_ms = (time.perf_counter() - t0) * 1000
        return m
    # refine at low resolution, then confirm the final +-0.25 deg at full
    # resolution so the chosen angle and the final edge pair use the same data
    for a in np.arange(best[1] - 1.0, best[1] + 1.0 + 1e-6, 0.25):
        bp, _ = pair_score(lab_s, a, wmin / ds, wmax / ds)
        if bp and bp[0] > best[0]:
            best = (bp[0], a)
    coarse = best[1]
    best = None
    for a in (coarse - 0.25, coarse, coarse + 0.25) if ds > 1 else (coarse,):
        bp, _ = pair_score(lab, a, wmin, wmax)
        if bp and (best is None or bp[0] > best[0]):
            best = (bp[0], a)
    angle = best[1] if best else coarse

    # -- straightened ROI --------------------------------------------------------
    rot_m = rotation_matrix((rw / 2.0, rh / 2.0), angle)
    inv_m = invert_affine(rot_m)
    s_lab = _rotate(lab, angle)
    s_bgr = _rotate(roi_bgr, angle)
    grad = _gradient_across(s_lab)
    prof = grad.mean(axis=1)
    bp = _best_pair(prof, wmin, wmax, _row_variation(s_lab), _signed_l_profile(s_lab))
    if bp is None:
        m.presence = UNCERTAIN
        m.issues.append("cable edges found at low resolution only - presence uncertain")
        m.elapsed_ms = (time.perf_counter() - t0) * 1000
        return m
    _, ia, ib = bp
    top, bot = _subpixel(prof, ia), _subpixel(prof, ib)
    width = bot - top
    m.angle_deg = float(-angle)  # cable angle in the image (counter-clockwise positive)
    m.edge_top, m.edge_bottom, m.cable_width_px = float(top), float(bot), float(width)
    m.edge_strength = float(min(prof[ia], prof[ib]))
    m.edge_snr = float(m.edge_strength / max(np.median(prof), 1e-3))

    margin = int(0.3 * width) + 2
    bg_rows = np.r_[0:max(0, ia - margin), min(rh, ib + margin):rh]
    if len(bg_rows):
        m.background_l = float(np.median(s_lab[bg_rows, :, 0]))

    # -- inspection region along the axis ----------------------------------
    corners = np.array([[insp[0], insp[1]], [insp[0] + insp[2], insp[1]],
                        [insp[0] + insp[2], insp[1] + insp[3]], [insp[0], insp[1] + insp[3]]], float)
    corners -= [roi[0], roi[1]]
    sc = transform_points(corners, rot_m)
    x0 = int(np.clip(np.floor(sc[:, 0].min()), 0, rw - 1))
    x1 = int(np.clip(np.ceil(sc[:, 0].max()), x0 + 1, rw))

    # -- per-slice edge agreement (presence + occlusion) ----------------------
    def slice_edges(c0, c1):
        sp = grad[:, c0:c1].mean(axis=1)
        ok = []
        for e in (ia, ib):
            lo, hi = max(0, e - 3), min(len(sp), e + 4)
            ok.append(sp[lo:hi].max() >= 0.4 * prof[e])
        return ok

    def slice_ok(c0, c1):
        return all(slice_edges(c0, c1))

    def slice_hidden(c0, c1):
        # one missing edge usually means poor contrast against the background
        # (e.g. black cable over a dark slot); both missing means something covers it
        return not any(slice_edges(c0, c1))

    n_slices = 12
    edges = np.linspace(0, rw, n_slices + 1).astype(int)
    consistency = np.mean([slice_ok(a, b) for a, b in zip(edges[:-1], edges[1:]) if b - a > 2])
    n_ins = max(3, (x1 - x0) // 12)
    ins_edges = np.linspace(x0, x1, n_ins + 1).astype(int)
    ins_hidden = [slice_hidden(a, b) for a, b in zip(ins_edges[:-1], ins_edges[1:]) if b - a > 1]
    m.edge_consistency = float(consistency)
    m.occluded_frac = float(np.mean(ins_hidden)) if ins_hidden else 1.0

    # -- edge sharpness (blur) -----------------------------------------------
    inside = s_lab[int(top + width * 0.25):int(bot - width * 0.25) + 1]
    above = s_lab[max(0, ia - 8):max(1, ia - 3)]
    below = s_lab[min(rh - 1, ib + 3):min(rh, ib + 8)]
    steps = []
    for outside, e in ((above, ia), (below, ib)):
        if outside.size and inside.size:
            w_ch = np.array([1.0, 0.7, 0.7], np.float32)
            diff_o = (np.median(outside.reshape(-1, 3), 0) - np.median(inside.reshape(-1, 3), 0)) * w_ch
            steps.append((float(np.linalg.norm(diff_o)), prof[e]))
    if steps:
        m.edge_width_px = float(np.median([d / max(p, 1e-3) for d, p in steps]))

    # -- presence decision ---------------------------------------------------
    strong = (m.edge_strength >= cfg["min_edge_strength"] and m.edge_snr >= cfg["edge_snr_min"])
    consistent = consistency >= cfg["edge_consistency_min"]
    if strong and consistent:
        m.presence = PRESENT
    elif m.edge_strength < cfg["min_edge_strength"] or m.edge_snr < 0.6 * cfg["edge_snr_min"] \
            or consistency < 0.3:
        m.presence = ABSENT
        m.notes.append("edges too weak or inconsistent for a cable")
    else:
        m.presence = UNCERTAIN
        m.issues.append("cable presence uncertain (weak/inconsistent edges)")
    if width_fallback and m.presence == PRESENT:
        m.issues.append(f"cable width {width:.0f}px differs from taught {expected_width:.0f}px")

    # geometry for display
    m.cable_polygon = _to_image([[0, top], [rw, top], [rw, bot], [0, bot]], inv_m, roi)

    # Local edge refinement: a real cable sags/bends slightly, so one straight
    # line over the whole ROI can be 1-2 px off at the stripes.  Re-locate both
    # edges using only the columns around the inspection region.
    pad = int(max(x1 - x0, 3 * width) // 2)
    lprof = grad[:, max(0, x0 - pad):min(rw, x1 + pad)].mean(axis=1)
    reach = max(3, int(round(0.3 * width)))
    local = []
    for e in (ia, ib):
        lo, hi = max(1, e - reach), min(rh - 1, e + reach + 1)
        k = lo + int(np.argmax(lprof[lo:hi]))
        local.append((k, _subpixel(lprof, k)))
    lw = local[1][1] - local[0][1]
    if abs(lw - width) <= 0.25 * width and lprof[local[0][0]] > 0.5 * prof[ia] \
            and lprof[local[1][0]] > 0.5 * prof[ib]:
        (ia, top), (ib, bot) = local
        prof = lprof
        width = bot - top
        m.edge_top, m.edge_bottom, m.cable_width_px = float(top), float(bot), float(width)

    m.region_polygon = _to_image([[x0, top], [x1, top], [x1, bot], [x0, bot]], inv_m, roi)
    vy0 = max(0, int(top - width * 0.6))
    vy1 = min(rh, int(bot + width * 0.6) + 1)
    m.straight_view = s_bgr[vy0:vy1, x0:x1].copy()
    m.straight_offset = (x0, vy0)

    if m.presence == ABSENT:
        m.elapsed_ms = (time.perf_counter() - t0) * 1000
        return m
    if m.occluded_frac > cfg["occlusion_max"]:
        m.issues.append(f"cable obstructed in inspection region ({m.occluded_frac:.0%} of edges missing)")
    # The stripe region must actually contain the cable; otherwise "0 stripes"
    # would be a setup error reported as a missing-stripe FAIL.
    ry0, ry1 = float(sc[:, 1].min()), float(sc[:, 1].max())
    overlap = max(0.0, min(ry1, bot) - max(ry0, top))
    if overlap < 0.5 * width:
        m.issues.append("cable is outside the stripe region - redraw the stripe region over the stripes")

    # per-column silhouette edge strength relative to the mean edge (for the crossing test)
    edge_cols = []
    for e in (ia, ib):
        lo, hi = max(0, e - 2), min(rh, e + 3)
        edge_cols.append(grad[lo:hi].max(axis=0) / max(float(prof[e]), 1e-3))
    _measure_band(m, s_lab, s_bgr, top, bot, x0, x1, inv_m, roi, cfg, rh, edge_cols)
    # Sharpness: painted stripe edges are true steps; the cable outline is
    # softened by its round profile, so it gets a looser limit.
    if m.stripe_edge_width_px > 0:
        if m.stripe_edge_width_px > cfg["max_edge_width_px"]:
            m.issues.append(f"image blurred (stripe edge width {m.stripe_edge_width_px:.1f}px)"
                            " - check focus/motion")
    elif m.edge_width_px > cfg["max_edge_width_px"] * 1.5:
        m.issues.append(f"image blurred (cable edge width {m.edge_width_px:.1f}px) - check focus/motion")
    m.elapsed_ms = (time.perf_counter() - t0) * 1000
    return m


def _to_image(pts, inv_m, roi) -> np.ndarray:
    p = transform_points(pts, inv_m)
    p += [roi[0], roi[1]]
    return p.astype(np.float32)


def _measure_band(m: Measurement, s_lab, s_bgr, top, bot, x0, x1, inv_m, roi, cfg, rh, edge_cols):
    width = bot - top
    inset = cfg["band_inset"] * width
    by0 = int(np.ceil(top + inset))
    by1 = int(np.floor(bot - inset)) + 1
    if by1 - by0 < 3:
        m.issues.append("cable too thin in image for reliable stripe detection")
        return
    band_lab = s_lab[by0:by1]          # full ROI length - body estimate
    band_bgr = s_bgr[by0:by1]
    bh, bw = band_lab.shape[:2]

    # -- glare / saturation ------------------------------------------------
    vmax = band_bgr.max(axis=2)
    vmin = band_bgr.min(axis=2)
    glare = (vmax >= cfg["glare_v"]) & (vmin >= cfg["glare_v"] - 40)
    glare = cv2.dilate(glare.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    valid = ~glare

    # -- local body reference & difference map ---------------------------------
    # Real cables pick up reflections of the fixture, so brightness drifts along
    # the cable.  The reference is a running median along the axis (per row,
    # handling cylindrical shading), with a window wide enough that stripes and
    # print stay a minority of its pixels.
    win = int(max(cfg["ref_window_widths"] * width, 80))
    ref = _local_reference(band_lab, valid, win)
    diff = delta_e_2000(band_lab, ref).astype(np.float32)
    # noise statistics near the inspection region only
    n0, n1 = max(0, x0 - win // 2), min(bw, x1 + win // 2)
    dv = diff[:, n0:n1][valid[:, n0:n1]]
    if dv.size:
        med = float(np.median(dv))
        mad = float(np.median(np.abs(dv - med))) * 1.4826
    else:
        med, mad = 0.0, 0.0
    thr = max(cfg["stripe_delta_e_min"], med + cfg["stripe_mad_k"] * mad)
    raw = (diff > thr) & valid

    # restrict stripe search to the inspection region
    sub = raw[:, x0:x1]
    sub_valid = valid[:, x0:x1]
    sub_glare = glare[:, x0:x1]
    # A longitudinal highlight blinds a few rows of every column but leaves the
    # stripes measurable; glare is a problem when it blinds whole columns.
    blinded = sub_glare.mean(axis=0) >= 0.5
    # bridge small gaps across the cable (highlight row) and along it (fragments)
    gap = int(cfg["merge_gap_px"])
    closed = cv2.morphologyEx(sub.astype(np.uint8), cv2.MORPH_CLOSE,
                              np.ones((5, 1), np.uint8)).astype(bool)
    n_valid = np.maximum(sub_valid.sum(axis=0), 1)
    cov = (closed & sub_valid).sum(axis=0) / n_valid
    glare_cov = sub_glare.mean(axis=0)
    sat_cols = glare_cov >= cfg["min_coverage"]
    profile = np.where(sat_cols, 1.0, cov)
    m.profile = profile.astype(np.float32)

    # saturated runs narrow enough to be a stripe are kept (flagged later);
    # wider ones are reflections
    for s_, e_ in _runs(sat_cols):
        if e_ - s_ > cfg["max_stripe_width_px"]:
            sat_cols[s_:e_] = False
    profile = np.where(sat_cols, 1.0, cov)
    m.glare_frac = float((blinded & ~sat_cols).mean())
    if m.glare_frac > cfg["glare_max"]:
        m.issues.append(f"reflection/glare over the stripe area ({m.glare_frac:.0%} of region blinded)")
        profile = np.where(blinded & ~sat_cols, 0.0, profile)

    min_cov = cfg["min_coverage"]
    strong_runs = _merge_runs(_runs(profile >= min_cov), gap)
    weak_runs = _merge_runs(_runs(profile >= 0.25), gap)

    # crossing-object test needs background just outside the cable
    out_gap = 3
    above = s_lab[max(0, int(top) - out_gap - 5):max(1, int(top) - out_gap)]
    below = s_lab[min(rh - 1, int(bot) + out_gap + 1):min(rh, int(bot) + out_gap + 6)]

    if any(s_ == 0 or e_ == len(profile) for s_, e_ in strong_runs):
        m.issues.append("stripe-like mark cut by the inspection region border - reposition cable or enlarge region")
    n_lab, labels, stats, _ = cv2.connectedComponentsWithStats(closed.astype(np.uint8), connectivity=8)

    accepted: list[tuple[int, int, float, bool]] = []
    for s, e in strong_runs:
        w = e - s
        seg = closed[:, s:e]
        fill = float((seg | sub_glare[:, s:e]).mean()) if seg.size else 0.0
        c = float(profile[s:e].mean())
        reason = None
        if w < cfg["min_stripe_width_px"]:
            reason = "too narrow (spot/print)"
        elif w > cfg["max_stripe_width_px"]:
            reason = "too wide for a stripe"
        elif fill < cfg["min_fill"]:
            reason = "low fill (print/texture)"
        elif _part_of_glyph(labels, stats, s, e, gap):
            reason = "part of a printed character/marking"
        else:
            tilt = _tilt_deg(seg)
            if tilt is not None and abs(tilt) > 30:
                reason = f"oblique ({tilt:.0f} deg) - not a transverse stripe"
            elif _crosses_cable(above, below, x0 + s, x0 + e, bw, edge_cols=edge_cols, cand_px=
                                band_lab[:, x0 + s:x0 + e][seg & valid[:, x0 + s:x0 + e]]):
                reason = "object crossing the cable"
        if reason:
            m.rejected.append(Candidate(x0 + s, x0 + e, reason))
            if reason == "object crossing the cable":
                if "object crossing the cable in inspection region" not in m.issues:
                    m.issues.append("object crossing the cable in inspection region")
            if reason == "too wide for a stripe":
                m.issues.append("unexpected wide band of different colour in region")
            continue
        accepted.append((s, e, c, bool(sat_cols[s:e].mean() > 0.5)))

    accepted = _keep_stripe_group(accepted, cfg.get("_expected_spacing"), m, x0)

    for s, e in weak_runs:
        if not any(a[0] < e and s < a[1] for a in accepted) and \
                not any(r.start - x0 < e and s < r.end - x0 for r in m.rejected):
            m.rejected.append(Candidate(x0 + s, x0 + e, "partial coverage (print/spot/reflection)"))

    # -- stripe measurement ---------------------------------------------------
    for i, (s, e, c, sat) in enumerate(accepted):
        px_mask = closed[:, s:e] & sub[:, s:e]
        pix = band_lab[:, x0 + s:x0 + e][px_mask]
        if sat or len(pix) < 3:
            pix = band_lab[:, x0 + s:x0 + e].reshape(-1, 3)
        lab_med, _ = robust_lab(pix)
        poly = _to_image([[x0 + s, top], [x0 + e, top], [x0 + e, bot], [x0 + s, bot]], inv_m, roi)
        m.stripes.append(Stripe(index=i + 1, start=x0 + s, end=x0 + e, centre_px=(s + e) / 2.0,
                                width_px=float(e - s), coverage=c, lab=lab_med, polygon=poly,
                                saturated=sat, pixels=pix[:: max(1, len(pix) // 1500)]))
        if sat:
            m.issues.append(f"stripe {i + 1} saturated (overexposed) - reduce exposure")

    m.stripe_edge_width_px = _stripe_edge_width(band_lab, valid, m.stripes)

    # -- body colour: band minus stripes, print, glare ---------------------------
    body_mask = valid & ~(diff > thr)
    excl = np.zeros(bw, bool)
    for st in m.stripes:
        excl[max(0, st.start - 3):min(bw, st.end + 3)] = True
    for r in m.rejected:
        excl[max(0, r.start - 2):min(bw, r.end + 2)] = True
    body_mask[:, excl] = False
    # measure near the stripes: lighting/reflections vary along a real cable
    span = max(x1 - x0, win)
    near = np.zeros(bw, bool)
    near[max(0, x0 - span):min(bw, x1 + span)] = True
    body_mask[:, ~near] = False
    body_px = band_lab[body_mask]
    m.body_pixels = int(len(body_px))
    if m.body_pixels < 100:
        m.issues.append("too few clean body pixels to measure cable colour")
        return
    m.body_lab, _ = robust_lab(body_px)
    m.body_sample = body_px[:: max(1, len(body_px) // 3000)]
    # uniformity along the cable (mixed/obstructed cable)
    left_mask = body_mask.copy()
    left_mask[:, x0:] = False
    right_mask = body_mask.copy()
    right_mask[:, :x1] = False
    left, right = band_lab[left_mask], band_lab[right_mask]
    if len(left) > 50 and len(right) > 50:
        d = float(delta_e_2000(robust_lab(left)[0], robust_lab(right)[0]))
        if d > cfg["body_uniformity_max"]:
            m.issues.append(f"body colour not uniform along cable (dE {d:.0f})")


def _local_reference(band_lab: np.ndarray, valid: np.ndarray, win: int) -> np.ndarray:
    """Per-row running median along the axis, sampled on a coarse grid and interpolated.

    Saturated pixels are replaced by their row median first so plain (fast)
    medians can be used.
    """
    bh, bw = band_lab.shape[:2]
    data = band_lab.astype(np.float32, copy=True)
    if not valid.all():
        row_med = np.median(band_lab, axis=1)  # (bh, 3)
        inv = ~valid
        data[inv] = np.broadcast_to(row_med[:, None, :], data.shape)[inv]
    step = max(4, win // 3)
    centres = np.arange(0, bw, step)
    if centres[-1] != bw - 1:
        centres = np.append(centres, bw - 1)
    samples = np.empty((bh, len(centres), 3), np.float32)
    for i, c in enumerate(centres):
        a, b = max(0, c - win // 2), min(bw, c + win // 2 + 1)
        samples[:, i] = np.median(data[:, a:b], axis=1)
    xs = np.arange(bw, dtype=np.float32)
    idx = np.clip(np.searchsorted(centres, xs, side="right") - 1, 0, len(centres) - 2)
    c0 = centres[idx].astype(np.float32)
    c1 = centres[idx + 1].astype(np.float32)
    t = ((xs - c0) / np.maximum(c1 - c0, 1.0))[None, :, None]
    return samples[:, idx] * (1 - t) + samples[:, idx + 1] * t


def _stripe_edge_width(band_lab, valid, stripes) -> float:
    """Median transition width (step / peak gradient) of stripe boundaries along the axis."""
    good = [st for st in stripes if not st.saturated and st.width_px >= 4]
    if not good:
        return 0.0
    w = np.array([1.0, 0.7, 0.7], np.float32)
    bw = band_lab.shape[1]
    cols = np.zeros((bw, 3), np.float32)
    need = np.zeros(bw, bool)
    for st in good:
        need[max(0, st.start - 10):min(bw, st.end + 10)] = True
    for x in np.flatnonzero(need):
        px = band_lab[:, x][valid[:, x]]
        cols[x] = np.median(px, 0) if len(px) else np.median(band_lab[:, x], 0)
    grad = np.linalg.norm(np.diff(cols, axis=0) * w, axis=1)
    widths = []
    for st in good:
        inner = cols[st.start + 1:st.end - 1]
        if not len(inner):
            continue
        centre = np.median(inner, 0)
        for edge, outside in ((st.start, cols[max(0, st.start - 8):max(1, st.start - 4)]),
                              (st.end, cols[st.end + 4:st.end + 8])):
            if not len(outside):
                continue
            step = float(np.linalg.norm((centre - np.median(outside, 0)) * w))
            lo, hi = max(0, edge - 4), min(len(grad), edge + 4)
            peak = float(grad[lo:hi].max()) if hi > lo else 0.0
            if step > 10 and peak > 0:
                widths.append(step / peak)
    return float(np.median(widths)) if widths else 0.0


def _keep_stripe_group(accepted, spacing, m: Measurement, x0: int):
    """Keep the main group of stripes when the stripe pitch has been taught.

    Printed legends repeat along the cable at random positions relative to
    the stripes, so they can fall inside the inspection region.  Candidates
    more than ``2.5 x pitch`` away from the largest group are reported as
    rejected instead of being counted.  (Trade-off: an extra stripe far from
    the pattern is also excluded; disable with detection.group_stripes.)
    """
    if not spacing or len(accepted) < 2:
        return accepted

    def fit(g):
        """How well a group matches the taught pitch (gaps near integer multiples)."""
        if len(g) < 2:
            return 0.0
        cs = [(a[0] + a[1]) / 2.0 for a in g]
        errs = [abs(d / spacing - round(d / spacing)) if round(d / spacing) >= 1 else 1.0
                for d in np.diff(cs)]
        return float(np.mean([e < 0.2 for e in errs]))

    groups: list[list] = [[accepted[0]]]
    for a in accepted[1:]:
        prev = groups[-1][-1]
        if (a[0] + a[1]) / 2.0 - (prev[0] + prev[1]) / 2.0 > 2.5 * spacing:
            groups.append([a])
        else:
            groups[-1].append(a)
    if len(groups) == 1:
        return accepted
    ranked = sorted(groups, key=lambda g: (len(g), fit(g)), reverse=True)
    best = ranked[0]
    if (len(ranked[1]), fit(ranked[1])) == (len(best), fit(best)):
        m.issues.append("several separate stripe groups in region - ambiguous (print or misplaced stripes)")
        return accepted
    for g in groups:
        if g is not best:
            for a in g:
                m.rejected.append(Candidate(x0 + a[0], x0 + a[1], "outside the stripe group (print/marking)"))
    return best


def _part_of_glyph(labels, stats, s, e, gap) -> bool:
    """True if the run belongs to a connected shape clearly wider than the run itself.

    A stripe is its own component.  The upright of a printed 'H' or 'C' is
    joined to the rest of the character, so the component extends well beyond
    the high-coverage columns.
    """
    ids, counts = np.unique(labels[:, s:e], return_counts=True)
    run_area = labels[:, s:e].size
    lo, hi = s, e
    for i, c in zip(ids, counts):
        if i == 0 or c < 0.05 * run_area:
            continue
        x, w = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_WIDTH]
        lo, hi = min(lo, x), max(hi, x + w)
    margin = max(gap + 1, 0.5 * (e - s))
    return (s - lo) > margin or (hi - e) > margin


def _tilt_deg(seg: np.ndarray) -> float | None:
    """Tilt of a stripe from the across-cable direction, from per-row centroids."""
    rows, xs = [], []
    for y in range(seg.shape[0]):
        cols = np.flatnonzero(seg[y])
        if len(cols):
            rows.append(y)
            xs.append(cols.mean())
    if len(rows) < max(4, seg.shape[0] // 2):
        return None
    slope = np.polyfit(rows, xs, 1)[0]
    return float(np.degrees(np.arctan(slope)))


def _crosses_cable(above, below, s, e, bw, cand_px, edge_cols, de_thr: float = 14.0) -> bool:
    """True if something continues from the candidate out across a cable edge.

    A painted stripe keeps the cable silhouette and does not change the
    fixture next to the cable.  An object lying over the cable (finger, tool,
    another wire) hides the silhouette edge where it crosses it and continues
    outside in its own colour.  All three must hold on at least one side;
    a cluttered background alone is not enough.
    """
    if above.size == 0 or below.size == 0 or not len(cand_px):
        return False
    cand = np.median(cand_px, 0)
    pad = max(12, e - s)
    for band, edge in ((above, edge_cols[0]), (below, edge_cols[1])):
        # only the columns where the silhouette is actually lost matter: an
        # object may cover just part of a wide candidate
        lost_cols = np.flatnonzero(edge[s:e] < 0.35) + s
        if len(lost_cols) < max(3, 0.25 * (e - s)):
            continue
        here = band[:, lost_cols].reshape(-1, 3)
        l0, l1 = max(0, s - pad - 8), max(0, s - 8)
        r0, r1 = min(bw, e + 8), min(bw, e + 8 + pad)
        ref = np.concatenate([band[:, l0:l1].reshape(-1, 3), band[:, r0:r1].reshape(-1, 3)])
        if len(here) == 0 or len(ref) == 0:
            continue
        here_med = np.median(here, 0)
        changed = float(delta_e_2000(here_med, np.median(ref, 0))) > de_thr
        continues = float(delta_e_2000(here_med, cand)) < de_thr
        if changed and continues:
            return True
    return False
