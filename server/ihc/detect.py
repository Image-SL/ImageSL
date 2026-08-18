from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from skimage.measure import label as _label

try:
    from scipy.ndimage import uniform_filter as _uniform_filter
    from scipy.ndimage import gaussian_filter as _gaussian
    from scipy.ndimage import sobel as _sobel
    _HAVE_SCIPY = True
except Exception:
    _HAVE_SCIPY = False
    _gaussian = None
    _sobel = None

DAB_OD     = np.array([0.270, 0.570, 0.780])
HEMA_OD    = np.array([0.650, 0.700, 0.290])
RED_OD     = np.array([0.070, 0.990, 0.110])
NEUTRAL_OD = np.array([0.577, 0.577, 0.577])

BG_WIN_FRAC      = 0.06
BG_WIN_MIN       = 33
BG_COARSE_MULT   = 4
BG_ITERS         = 2
BG_FG_MAX_FRAC   = 0.55
BG_GLOBAL_FG_K   = 6.0

MATERIAL_OD_MIN  = 0.15
MATERIAL_TISSUE_FRAC = 0.35

BROWN_SEED       = 0.22
BROWN_GROW       = -0.05
NEUTRAL_SAT_MAX  = 0.12

BLUE_OVER_GREEN_MIN = -0.15
CHROMOGEN_SHARE_MIN = 0.35
OBJ_BROWN_MEAN   = 0.14

OBJ_RAW_BROWN    = 0.22
OBJ_RAW_WARM     = 0.20
OBJ_RAW_SAT      = 0.30

DEBRIS_SAT_MAX   = 0.22
DEBRIS_HUE_MIN   = 30.0

CHROMO_FRAC_MIN = 0.25

UNMISTAKABLE_BROWN = 0.28
UNMISTAKABLE_SIG_K = 5.0
UNMISTAKABLE_PCT   = 10.0
UNMISTAKABLE_MIN_N = 6
BAR_NOTE_RATIO     = 0.67
SEED_RAW_BROWN   = 0.14
SEED_RAW_BOG     = 0.04
DENSE_SIG_K      = 4.0
OBJ_SEED_FRAC    = 0.06
OBJ_SEED_MIN_PX  = 2

SMOOTH_SIGMA     = 0.80
SMOOTH_REF_EDGE  = 1024.0
CHROMA_SMOOTH    = 2.2

NOISE_MULT       = 1.6
ABS_MIN_EXCESS   = 0.025

PEAK_BAR_ABS_MIN = 0.06
PEAK_BAR_SIG_MIN = 2.6
PEAK_BAR_MAX     = 1.20
PEAK_BIMODAL_MIN = 0.22
PEAK_MIN_SPREAD  = 2.30
HIST_SMOOTH_BINS = 3.0
PEAK_DIFFUSE_LO  = 0.50
PEAK_DIFFUSE_HI  = 0.58
BAR_DIFFUSE_PCT  = 78.0
BAR_MIN_AREA_FRAC = 3.5e-5
OTSU_PLATEAU     = 0.90

EXTENT_PEAK_FRAC      = 0.26
EXTENT_PEAK_FRAC_FINE = 0.34
EXTENT_FINE_AREA_FRAC = 1.3e-3
EXTENT_BULK_AREA_FRAC = 4.6e-3
EXTENT_FLOOR_MULT = 1.0

MIN_AREA_FRAC    = 6e-6

N_LEVELS         = 201
AUTO_LEVEL       = 100

LEVEL_CANDIDATE  = 254
LEVEL_NEVER      = 255
LADDER_STRICT    = 4.0
LADDER_LOOSE     = 0.25
LADDER_END_MARGIN = 1.02

@dataclass
class Detection:
    positive: np.ndarray
    level_map: np.ndarray
    levels: list
    level: int
    auto_level: int
    areas: list
    evidence: np.ndarray
    excess: np.ndarray
    brownness: np.ndarray
    background_od: np.ndarray
    sigma: float
    bar: float = 0.0
    ladder_hi: float = LADDER_STRICT
    ladder_lo: float = LADDER_LOOSE
    floor: float = 0.0
    separability: float = 0.0
    bar_discard: float = 0.0
    single_population: bool = False
    chromogen_share: float = 1.0
    objects: int = 0
    object_areas: list = field(default_factory=list)
    notes: list = field(default_factory=list)

def _mask_weights(keep: np.ndarray, win: int) -> tuple[np.ndarray, np.ndarray]:
    k = keep.astype(np.float32)
    return (_uniform_filter(k, win),
            _uniform_filter(k, win * BG_COARSE_MULT))

def _field(x: np.ndarray, keep: np.ndarray, win: int, fallback: float,
           den: Optional[np.ndarray] = None,
           cden: Optional[np.ndarray] = None) -> np.ndarray:
    if den is None or cden is None:
        den, cden = _mask_weights(keep, win)
    masked = np.where(keep, x, 0.0).astype(np.float32)
    num = _uniform_filter(masked, win)
    cnum = _uniform_filter(masked, win * BG_COARSE_MULT)
    fine_ok = den > 0.06
    coarse_ok = cden > 0.02
    out = np.full(x.shape, float(fallback), dtype=np.float32)
    np.divide(cnum, np.maximum(cden, 1e-6), out=out, where=coarse_ok)
    fine = np.divide(num, np.maximum(den, 1e-6), where=fine_ok, out=np.zeros_like(out))
    out = np.where(fine_ok, fine, out)
    return out.astype(np.float32)

def background_od_field(od: np.ndarray, tissue: np.ndarray, win: int) -> tuple[np.ndarray, np.ndarray]:
    h, w = od.shape[:2]
    dab_dir = DAB_OD / np.linalg.norm(DAB_OD)
    proj = (od @ dab_dir).astype(np.float32)

    keep = tissue.copy()
    if not keep.any():
        keep = np.ones((h, w), dtype=bool)
    bg_od = np.empty_like(od, dtype=np.float32)

    for it in range(BG_ITERS + 1):
        if keep.any():
            _kept = od[keep]
            fallbacks = [float(np.median(_kept[:, c])) for c in range(3)]
        else:
            fallbacks = [0.0, 0.0, 0.0]
        den, cden = _mask_weights(keep, win)
        for c in range(3):
            bg_od[..., c] = _field(od[..., c], keep, win, fallbacks[c], den, cden)
        if it == BG_ITERS:
            break
        exc = proj - (bg_od @ dab_dir)
        base = exc[tissue] if tissue.any() else exc.ravel()
        sig = 1.4826 * float(np.median(np.abs(base - np.median(base)))) + 1e-6

        t_proj = proj[tissue] if tissue.any() else proj.ravel()
        if t_proj.size > 200_000:
            t_proj = t_proj[:: t_proj.size // 200_000 + 1]
        med_g = float(np.median(t_proj))
        sig_g = 1.4826 * float(np.median(np.abs(t_proj - med_g))) + 1e-6
        fg = (exc > 3.0 * sig) | (proj > med_g + BG_GLOBAL_FG_K * sig_g)
        if float(fg.mean()) > BG_FG_MAX_FRAC:
            cut = float(np.percentile(exc, 100.0 * (1.0 - BG_FG_MAX_FRAC)))
            fg = exc > cut
        keep = tissue & ~fg
        if float(keep.mean()) < 0.05:
            keep = tissue.copy()
    return bg_od, keep

def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) or 1.0)

def chromogen_fraction(od: np.ndarray,
                       target_od: Optional[np.ndarray] = None,
                       counter_od: Optional[np.ndarray] = None) -> np.ndarray:
    tgt = _unit(np.asarray(target_od, dtype=np.float64) if target_od is not None else DAB_OD)
    ctr = _unit(np.asarray(counter_od, dtype=np.float64) if counter_od is not None else HEMA_OD)
    neu = _unit(NEUTRAL_OD)
    basis = np.stack([ctr, tgt, neu])
    try:
        inv = np.linalg.pinv(basis).astype(np.float32)
    except np.linalg.LinAlgError:
        return np.ones(od.shape[:2], dtype=np.float32)
    a = np.clip(np.clip(od, 0.0, None) @ inv, 0.0, None)
    return (a[..., 1] / (a[..., 1] + a[..., 2] + 1e-6)).astype(np.float32)

def excess_colour(od_exc: np.ndarray, target_od: Optional[np.ndarray] = None
                  ) -> tuple[np.ndarray, np.ndarray]:
    mag = np.linalg.norm(od_exc, axis=2).astype(np.float32)
    safe = np.maximum(mag, 1e-6)
    if target_od is None:
        contrast = od_exc[..., 2] - od_exc[..., 0]
    else:
        d = _unit(np.asarray(target_od, dtype=np.float64))
        axis = d - _unit(NEUTRAL_OD) * float(d @ _unit(NEUTRAL_OD))
        axis = _unit(axis) * np.sqrt(2.0)
        contrast = od_exc @ axis.astype(np.float32)
    return mag, (contrast / safe).astype(np.float32)

def _otsu_log(values: np.ndarray) -> tuple[float, float]:
    v = np.log10(np.maximum(values, 1e-4))
    if v.size < 12:
        return float("nan"), 0.0, 0.0
    hist, edges = np.histogram(v, bins=96)
    centres = 0.5 * (edges[:-1] + edges[1:])
    w = hist.astype(np.float64)
    if _gaussian is not None:
        w = _gaussian(w, HIST_SMOOTH_BINS, mode="nearest")
    total = w.sum()
    if total <= 0:
        return float("nan"), 0.0, 0.0
    p = w / total
    omega = np.cumsum(p)
    mu = np.cumsum(p * centres)
    mu_t = mu[-1]
    denom = omega * (1.0 - omega)
    with np.errstate(invalid="ignore", divide="ignore"):
        sigma_b = np.where(denom > 1e-12, (mu_t * omega - mu) ** 2 / np.maximum(denom, 1e-12), 0.0)
    best = float(sigma_b.max())
    plateau = sigma_b >= best * OTSU_PLATEAU
    k = int(np.argmax(sigma_b))
    centre = float((centres[plateau] * sigma_b[plateau]).sum() / max(sigma_b[plateau].sum(), 1e-12))
    var_total = float(((centres - mu_t) ** 2 * p).sum())
    sep = float(sigma_b[k] / var_total) if var_total > 1e-12 else 0.0

    lo, hi = centres <= centre, centres > centre
    d = 0.0
    if p[lo].sum() > 1e-9 and p[hi].sum() > 1e-9:
        def _stats(m):
            w = p[m] / p[m].sum()
            mu = float((centres[m] * w).sum())
            return mu, float(np.sqrt(max((w * (centres[m] - mu) ** 2).sum(), 1e-12)))
        mu1, s1 = _stats(lo)
        mu2, s2 = _stats(hi)
        d = (mu2 - mu1) / max(s1 + s2, 1e-9)
    return float(10.0 ** centre), sep, float(d)

def _area_weighted(peaks: np.ndarray, areas: np.ndarray, cap: int = 400) -> np.ndarray:
    if peaks.size == 0:
        return peaks
    reps = np.clip((areas // 2).astype(np.int64), 1, cap)
    return np.repeat(peaks, reps)

def _ladder(bar: float, peak_hi: float = 0.0, peak_lo: float = 0.0) -> np.ndarray:
    bar = float(bar)
    hi = (peak_hi * LADDER_END_MARGIN) if peak_hi > 0 else bar * LADDER_STRICT
    lo = (peak_lo / LADDER_END_MARGIN) if peak_lo > 0 else bar * LADDER_LOOSE
    hi = max(hi, bar * 1.02)
    lo = min(lo, bar * 0.98)
    up = np.geomspace(hi, bar, AUTO_LEVEL + 1)
    dn = np.geomspace(bar, lo, N_LEVELS - AUTO_LEVEL)
    return np.concatenate([up[:-1], dn]).astype(np.float32)

def _object_stats(lbl: np.ndarray, n: int, signal: np.ndarray,
                  brown: np.ndarray, seed: np.ndarray,
                  raw_brown: Optional[np.ndarray] = None,
                  raw_warm: Optional[np.ndarray] = None,
                  raw_sat: Optional[np.ndarray] = None,
                  raw_hue: Optional[np.ndarray] = None,
                  chromo_frac: Optional[np.ndarray] = None) -> dict:
    flat = lbl.ravel()
    area = np.bincount(flat, minlength=n + 1).astype(np.int64)
    peak = np.zeros(n + 1, dtype=np.float32)
    np.maximum.at(peak, flat, signal.ravel())
    seeds = np.bincount(flat, weights=seed.ravel().astype(np.float64), minlength=n + 1)

    def _mean(x):
        s = np.bincount(flat, weights=x.ravel().astype(np.float64), minlength=n + 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            return s / np.maximum(area, 1)

    out = {"area": area, "peak": peak, "seeds": seeds,
           "seed_frac": seeds / np.maximum(area, 1), "brown_mean": _mean(brown)}
    out["raw_brown_mean"] = _mean(raw_brown) if raw_brown is not None else np.full(n + 1, -1.0)

    core = signal.ravel() >= 0.5 * peak[flat]
    cn = np.bincount(flat, weights=core.astype(np.float64), minlength=n + 1)

    def _core_mean(x, default=-1.0):
        if x is None:
            return np.full(n + 1, default)
        s = np.bincount(flat, weights=np.where(core, x.ravel().astype(np.float64), 0.0),
                        minlength=n + 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            return s / np.maximum(cn, 1)

    out["raw_brown_core"] = _core_mean(raw_brown)
    out["chromo_frac_core"] = _core_mean(chromo_frac, 1.0)
    out["raw_warm_mean"] = _mean(raw_warm) if raw_warm is not None else np.full(n + 1, -1.0)
    out["raw_sat_mean"] = _mean(raw_sat) if raw_sat is not None else np.full(n + 1, -1.0)

    if raw_hue is not None and raw_sat is not None:
        rad = np.deg2rad(raw_hue.astype(np.float64))
        w = raw_sat.astype(np.float64)
        sx = np.bincount(flat, weights=(np.cos(rad) * w).ravel(), minlength=n + 1)
        sy = np.bincount(flat, weights=(np.sin(rad) * w).ravel(), minlength=n + 1)
        out["hue_mean"] = np.rad2deg(np.arctan2(sy, sx)) % 360.0
    else:
        out["hue_mean"] = np.zeros(n + 1)
    return out

def detect(
    od: np.ndarray,
    tissue: np.ndarray,
    *,
    level: Optional[int] = None,
    min_area_px: Optional[int] = None,
    hue_band_mask: Optional[np.ndarray] = None,
    target_od: Optional[np.ndarray] = None,
    counter_od: Optional[np.ndarray] = None,
    saturation: Optional[np.ndarray] = None,
    debug: Optional[dict] = None,
) -> Detection:
    h, w = od.shape[:2]
    notes: list[str] = []
    frame = h * w
    if min_area_px is None:
        min_area_px = int(max(4, round(frame * MIN_AREA_FRAC)))
    dab_vec = np.asarray(target_od, dtype=np.float64) if target_od is not None else DAB_OD

    total_od = np.clip(od, 0.0, None).sum(axis=2)
    mat_cut = MATERIAL_OD_MIN
    if tissue.any():
        _t = total_od[tissue]
        if _t.size > 200_000:
            _t = _t[:: _t.size // 200_000 + 1]
        mat_cut = float(min(MATERIAL_OD_MIN,
                            MATERIAL_TISSUE_FRAC * float(np.median(_t))))
    material = total_od >= mat_cut
    win = int(max(BG_WIN_MIN, round(max(h, w) * BG_WIN_FRAC)) | 1)
    bg_od, bg_mask = background_od_field(od, tissue & material, win)
    od_exc = (od - bg_od).astype(np.float32)
    rel_scale = max(h, w) / SMOOTH_REF_EDGE
    smooth = SMOOTH_SIGMA * rel_scale
    if smooth > 0.15 and _gaussian is not None:
        od_exc = _gaussian(od_exc, (smooth, smooth, 0), mode="nearest")
    _, brown_fine = excess_colour(od_exc, None if target_od is None else dab_vec)
    brown = brown_fine

    od_pos = np.clip(od, 0.0, None)
    _, raw_brown = excess_colour(od_pos, None if target_od is None else dab_vec)
    raw_mag = np.linalg.norm(od_pos, axis=2) + 1e-6
    raw_bog = ((od_pos[..., 2] - od_pos[..., 1]) / raw_mag).astype(np.float32)
    _tr = np.power(10.0, -od_pos)
    raw_warm = ((_tr[..., 0] - _tr[..., 2]) / (_tr[..., 0] + _tr[..., 2] + 1e-6)).astype(np.float32)

    norm_sat = norm_hue = None

    chromo_frac = chromogen_fraction(od_pos, target_od, counter_od)

    _mag = np.linalg.norm(od_exc, axis=2) + 1e-6
    bog = ((od_exc[..., 2] - od_exc[..., 1]) / _mag).astype(np.float32)
    bog_min = BLUE_OVER_GREEN_MIN
    if target_od is not None:
        u = _unit(np.asarray(target_od, dtype=np.float64))
        bog_min = float(u[2] - u[1]) - 0.25
    csmooth = CHROMA_SMOOTH * rel_scale
    if csmooth > smooth and _gaussian is not None:
        extra = float(np.sqrt(max(csmooth ** 2 - smooth ** 2, 0.0)))
        _, brown_coarse = excess_colour(_gaussian(od_exc, (extra, extra, 0), mode="nearest"),
                                        None if target_od is None else dab_vec)
        brown = np.maximum(brown_fine, brown_coarse)

    dab_dir = _unit(dab_vec).astype(np.float32)
    signal = (od_exc @ dab_dir).astype(np.float32)

    base = signal[bg_mask] if bg_mask.any() else signal.ravel()
    if base.size > 200_000:
        base = base[:: base.size // 200_000 + 1]
    med = float(np.median(base))
    sigma = max(1.4826 * float(np.median(np.abs(base - med))) + 1e-6, 1e-4)

    signal = np.clip(signal - med, 0.0, None).astype(np.float32)
    signal[~(tissue & material)] = 0.0
    evidence = (signal / sigma).astype(np.float32)

    floor = float(max(NOISE_MULT * sigma, ABS_MIN_EXCESS))
    solid = tissue & material

    raw_proj = (od_pos @ dab_dir).astype(np.float32)
    _rp = raw_proj[solid] if solid.any() else raw_proj.ravel()
    if _rp.size > 200_000:
        _rp = _rp[:: _rp.size // 200_000 + 1]
    _rp_med = float(np.median(_rp)) if _rp.size else 0.0
    _rp_sig = 1.4826 * float(np.median(np.abs(_rp - _rp_med))) + 1e-6 if _rp.size else 1.0
    dense_px = raw_proj >= _rp_med + DENSE_SIG_K * _rp_sig

    seed_colour = (brown >= BROWN_SEED) & (bog >= bog_min)
    seed_colour |= dense_px & (raw_brown >= SEED_RAW_BROWN) & (raw_bog >= SEED_RAW_BOG)
    seed_px = solid & seed_colour & (signal >= floor)

    region = solid & (brown >= BROWN_GROW) & (signal >= floor)
    if saturation is not None:
        region &= saturation > NEUTRAL_SAT_MAX
    if hue_band_mask is not None:
        seed_px &= hue_band_mask

    strong = solid & (signal >= max(floor, float(np.percentile(signal[solid], 99.0))
                                    if solid.any() else floor))
    if strong.any() and float(np.median(signal[strong])) >= 2.0 * floor:
        chromogen_share = float((seed_px & strong).sum()) / max(int(strong.sum()), 1)
    else:
        chromogen_share = 1.0

    bar_min = float(max(PEAK_BAR_ABS_MIN, PEAK_BAR_SIG_MIN * sigma))
    lbl = _label(region, connectivity=2)
    n = int(lbl.max())
    ladder_peak_hi = ladder_peak_lo = 0.0
    levels = _ladder(bar_min)
    level_map = np.full((h, w), 255, dtype=np.uint8)
    bar = bar_min
    separability = 0.0
    bar_discard = 0.0
    single_population = False

    if n:
        st = _object_stats(lbl, n, signal, brown, seed_px, raw_brown, raw_warm,
                           norm_sat, norm_hue, chromo_frac)
        chromogen_coloured = st["chromo_frac_core"] >= CHROMO_FRAC_MIN
        eligible = ((st["area"] >= min_area_px) & (st["seeds"] >= OBJ_SEED_MIN_PX)
                    & (st["seed_frac"] >= OBJ_SEED_FRAC) & chromogen_coloured)
        eligible[0] = False

        voters = eligible & (st["area"] >= max(min_area_px, int(frame * BAR_MIN_AREA_FRAC)))
        if int(voters.sum()) < 12:
            voters = eligible
        peaks = _area_weighted(st["peak"][voters], st["area"][voters])

        _obj_dense = st["peak"] >= UNMISTAKABLE_SIG_K * sigma
        unmistakable = (eligible & _obj_dense
                        & (st["raw_brown_mean"] >= UNMISTAKABLE_BROWN))
        bar_ceiling = float("inf")
        if int(unmistakable.sum()) >= UNMISTAKABLE_MIN_N:
            bar_ceiling = float(np.percentile(
                _area_weighted(st["peak"][unmistakable], st["area"][unmistakable]),
                UNMISTAKABLE_PCT))
        if peaks.size >= 12:
            t, separability, discrim = _otsu_log(peaks)
            lo_p, hi_p = np.percentile(peaks, [10, 90])
            one_population = float(hi_p / max(lo_p, 1e-6)) < PEAK_MIN_SPREAD
            if np.isfinite(t):
                bar_discard = float((peaks < t).mean())
            if one_population:
                t = float("nan")
                single_population = True
                notes.append(
                    "The staining here is of an even density — the structures found do "
                    "not separate into stronger and weaker groups — so everything "
                    "chromogen-coloured above the noise is counted.")
            if np.isfinite(t) and separability >= PEAK_BIMODAL_MIN:
                span = max(PEAK_DIFFUSE_HI - PEAK_DIFFUSE_LO, 1e-6)
                trust = float(np.clip((separability - PEAK_DIFFUSE_LO) / span, 0.0, 1.0))
                if trust < 1.0:
                    q = float(np.percentile(peaks, BAR_DIFFUSE_PCT))
                    t = float(np.exp(trust * np.log(max(t, 1e-4))
                                     + (1.0 - trust) * np.log(max(q, 1e-4))))
                bar = float(np.clip(t, bar_min, PEAK_BAR_MAX))
                if trust < 0.5:
                    notes.append(
                        "Staining on this slide is diffuse rather than focal — the "
                        "stained material does not form a population of structures "
                        "clearly separate from the background, so where the boundary "
                        "falls is a judgement rather than a measurement. Treat the "
                        "percentage as a relative reading, compare only within a "
                        "staining batch, and use the sensitivity control to set the "
                        "boundary where you want it.")
            else:
                bar = bar_min
                notes.append("Staining is diffuse rather than focal on this slide — "
                             "no separate population of stained structures was found, "
                             "so only clearly absorbing material is counted.")

            if bar_ceiling < bar:
                _before = bar
                bar = float(max(bar_ceiling, bar_min))
                if bar <= _before * BAR_NOTE_RATIO:
                    notes.append(
                        "Some clearly stained structures fell below the boundary the "
                        "object population implied, so the boundary was lowered to "
                        "include them.")

        _elig_peaks = st["peak"][eligible]
        if _elig_peaks.size:
            ladder_peak_hi = float(_elig_peaks.max())
            ladder_peak_lo = float(_elig_peaks.min())
        levels = _ladder(bar, ladder_peak_hi, ladder_peak_lo)

        _img_px = float(h * w)
        _lo = EXTENT_FINE_AREA_FRAC * _img_px
        _hi = max(EXTENT_BULK_AREA_FRAC * _img_px, _lo + 1.0)
        _t = np.clip((st["area"].astype(np.float64) - _lo) / (_hi - _lo), 0.0, 1.0)
        _frac = EXTENT_PEAK_FRAC_FINE + (EXTENT_PEAK_FRAC - EXTENT_PEAK_FRAC_FINE) * _t
        extent = np.maximum(st["peak"] * _frac, floor * EXTENT_FLOOR_MULT)
        flat_lbl = lbl.ravel()
        inside = signal.ravel() >= extent[flat_lbl]

        desc = levels.astype(np.float64)
        obj_level = np.full(n + 1, LEVEL_NEVER, dtype=np.int32)
        idx = np.searchsorted(-desc, -st["peak"][eligible], side="left")
        obj_level[np.flatnonzero(eligible)] = np.clip(idx, 0, N_LEVELS - 1)
        obj_level[~eligible] = LEVEL_CANDIDATE
        obj_level[0] = LEVEL_NEVER

        lv = obj_level[flat_lbl]
        lv[~inside] = LEVEL_NEVER
        lv[flat_lbl == 0] = LEVEL_NEVER
        level_map = lv.astype(np.uint8).reshape(h, w)

        if debug is not None:
            debug.update({
                "labels": lbl, "region": region, "solid": solid, "signal": signal,
                "floor": floor, "inside": inside.reshape(h, w),
                "area_ok": st["area"] >= min_area_px,
                "seeds_ok": (st["seeds"] >= OBJ_SEED_MIN_PX) & (st["seed_frac"] >= OBJ_SEED_FRAC),
                "colour_ok": chromogen_coloured,
                "eligible": eligible, "stats": st, "bar_ceiling": bar_ceiling,
            })

    sel = int(np.clip(AUTO_LEVEL if level is None else level, 0, N_LEVELS - 1))
    positive = level_map <= sel

    _hist = np.bincount(level_map.ravel(), minlength=256)
    areas = [int(v) for v in np.cumsum(_hist[:N_LEVELS])]
    olbl = _label(positive, connectivity=2)
    obj_areas = np.bincount(olbl.ravel())[1:] if olbl.max() else np.array([], dtype=np.int64)

    if not positive.any():
        notes.append("No chromogen-specific staining was found above this slide's own background.")

    return Detection(
        positive=positive,
        level_map=level_map,
        levels=[round(float(x), 4) for x in levels],
        level=sel,
        auto_level=AUTO_LEVEL,
        areas=areas,
        evidence=evidence,
        excess=signal,
        brownness=brown,
        background_od=bg_od,
        sigma=float(sigma),
        bar=float(bar),
        ladder_hi=float(levels[0] / max(bar, 1e-9)),
        ladder_lo=float(levels[-1] / max(bar, 1e-9)),
        floor=float(floor),
        separability=float(separability),
        bar_discard=float(bar_discard),
        single_population=bool(single_population),
        chromogen_share=float(chromogen_share),
        objects=int(obj_areas.size),
        object_areas=[int(a) for a in obj_areas],
        notes=notes,
    )
