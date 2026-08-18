from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

from ihc import detect, engine

MISS_MAX      = 0.05
MISS_MIN_PX   = 60
FLOOD_MAX     = 0.10
GREY_MAX      = 0.22
TISSUE_MIN    = 0.90
ABS_TOL_PP    = 0.50
LIGHT_TOL     = 0.30
SCALE_TOL     = 0.40
NOISE_TOL     = 0.30

OBVIOUS_OD    = 0.40
OBVIOUS_SIG   = 7.0
OBVIOUS_BROWN = 0.30

RAW_SIG_K       = 6.0
RAW_MIN_BLOB_PX = 12
RAW_CHROMO_MIN  = 0.30

COVER_RAW_OD  = 0.60
COVER_WARM    = 0.15
COVER_MIN_PX  = 400
GREEN = np.array([57, 255, 20], dtype=np.float64)

def analyse(rgb: np.ndarray, level=None):
    res, maps = engine.analyze(rgb, (rgb.shape[1], rgb.shape[0]), level=level)
    return res, maps

def glass_estimate(rgb: np.ndarray) -> float:
    from scipy.ndimage import uniform_filter
    hsv_h, sat, val = engine._rgb_to_hsv(rgb.astype(np.float32) / 255.0)
    white = np.percentile(rgb.reshape(-1, 3), 99.0, axis=0)
    m = uniform_filter(val, 9)
    m2 = uniform_filter(val * val, 9)
    std = np.sqrt(np.clip(m2 - m * m, 0, None))
    bright = val >= (float(np.min(white)) / 255.0 - 0.06)
    return float((bright & (sat < 0.15) & (std < 0.012)).mean())

def positive_fraction(rgb: np.ndarray) -> float:
    res, _ = analyse(rgb)
    return res.positive_percent

def _grey_object_fraction(pos: np.ndarray, brown: np.ndarray, rgb: np.ndarray) -> float:
    if not pos.any():
        return 0.0
    from skimage.measure import label
    f = rgb.astype(np.float32)
    warm = (f[..., 0] - f[..., 2]) / (f[..., 0] + f[..., 2] + 1.0)
    lbl = label(pos, connectivity=2)
    n = int(lbl.max())
    flat = lbl.ravel()
    area = np.bincount(flat, minlength=n + 1)
    bmean = np.bincount(flat, weights=brown.ravel().astype(np.float64), minlength=n + 1) / np.maximum(area, 1)
    wmean = np.bincount(flat, weights=warm.ravel().astype(np.float64), minlength=n + 1) / np.maximum(area, 1)
    bad = (bmean < 0.12) & (wmean < 0.10)
    bad[0] = False
    return float(area[bad].sum()) / float(area[1:].sum() or 1)

def _flood_object_fraction(pos: np.ndarray, sig: np.ndarray, sigma: float) -> float:
    if not pos.any():
        return 0.0
    from skimage.measure import label
    lbl = label(pos, connectivity=2)
    n = int(lbl.max())
    if not n:
        return 0.0
    flat = lbl.ravel()
    area = np.bincount(flat, minlength=n + 1).astype(np.float64)
    peak = np.zeros(n + 1, dtype=np.float32)
    np.maximum.at(peak, flat, sig.ravel())
    bad = peak < max(0.05, 1.2 * sigma)
    bad[0] = False
    return float(area[bad].sum()) / float(area[1:].sum() or 1)

def _raw_obvious_stain(od: np.ndarray, tissue: np.ndarray) -> np.ndarray:
    from skimage.measure import label
    od_pos = np.clip(od, 0.0, None)
    dab = detect.DAB_OD / np.linalg.norm(detect.DAB_OD)
    proj = (od_pos @ dab).astype(np.float32)
    frac = detect.chromogen_fraction(od_pos)
    t = proj[tissue] if tissue.any() else proj.ravel()
    if t.size > 300_000:
        t = t[:: t.size // 300_000 + 1]
    med = float(np.median(t))
    sig = 1.4826 * float(np.median(np.abs(t - med))) + 1e-6
    m = tissue & (proj >= med + RAW_SIG_K * sig) & (frac >= RAW_CHROMO_MIN)
    if not m.any():
        return m
    lbl = label(m, connectivity=2)
    if not lbl.max():
        return m
    areas = np.bincount(lbl.ravel())
    keep = np.flatnonzero(areas >= RAW_MIN_BLOB_PX)
    return np.isin(lbl, keep[keep > 0])

def _drop_debris_structures(mask: np.ndarray, od: np.ndarray) -> np.ndarray:
    if not mask.any():
        return mask
    from skimage.measure import label
    lbl = label(mask, connectivity=2)
    n = int(lbl.max())
    if not n:
        return mask
    frac = detect.chromogen_fraction(np.clip(od, 0.0, None))
    flat = lbl.ravel()
    area = np.bincount(flat, minlength=n + 1).astype(np.float64)
    fmean = np.bincount(flat, weights=frac.ravel().astype(np.float64),
                        minlength=n + 1) / np.maximum(area, 1)
    bad = fmean < 0.10
    bad[0] = False
    return mask & ~bad[lbl]

def check(path: str, montage_dir=None, quick: bool = False) -> dict:
    with open(path, "rb") as fh:
        rgb, _source_size = engine.load_rgb(fh.read())
    t0 = time.time()
    res, maps = analyse(rgb)
    secs = time.time() - t0

    pos = maps["positive"]
    tissue = maps["tissue_mask"]
    sig = maps["excess"]
    brown = maps["brownness"]
    sigma = float(maps["sigma"])
    tis_px = int(tissue.sum())

    obvious = (tissue & (sig >= max(OBVIOUS_OD, OBVIOUS_SIG * sigma))
               & (brown >= OBVIOUS_BROWN))
    obvious = obvious | _raw_obvious_stain(maps["od"], tissue)
    obvious = _drop_debris_structures(obvious, maps["od"])
    obv_px = int(obvious.sum())
    miss = float((obvious & ~pos).sum()) / obv_px if obv_px else 0.0

    pos_px = int(pos.sum())
    flood = _flood_object_fraction(pos, sig, sigma)
    grey = _grey_object_fraction(pos, brown, rgb)

    from ihc import detect as _detect
    dab = _detect.DAB_OD / np.linalg.norm(_detect.DAB_OD)
    raw_abs = (maps["od"] @ dab).astype(np.float32)
    fl = rgb.astype(np.float32)
    warm_raw = (fl[..., 0] - fl[..., 2]) / (fl[..., 0] + fl[..., 2] + 1.0)
    unambiguous = (tissue & (raw_abs >= COVER_RAW_OD) & (warm_raw >= COVER_WARM)
                   & (sig >= max(0.05, 1.5 * sigma)))
    unamb_px = int(unambiguous.sum())
    cover = float((unambiguous & pos).sum()) / unamb_px if unamb_px else 1.0

    glass = glass_estimate(rgb)
    tissue_ratio = (tis_px / rgb[..., 0].size) / max(1e-6, 1.0 - glass)

    base = res.positive_percent

    def rel(x):
        r = abs(x - base) / max(base, 0.05)
        return r if abs(x - base) >= ABS_TOL_PP else 0.0

    light = scale = noise = 0.0
    if not quick:
        f = rgb.astype(np.float32)
        dim = (f * 0.88).astype(np.uint8)
        dimmer = (f * 0.78).astype(np.uint8)
        warm = np.clip(f * np.array([0.95, 0.92, 0.86], np.float32), 0, 255).astype(np.uint8)
        light = max(rel(positive_fraction(dim)),
                    rel(positive_fraction(dimmer)),
                    rel(positive_fraction(warm)))

        small = np.asarray(Image.fromarray(rgb).resize(
            (int(rgb.shape[1] * 0.75), int(rgb.shape[0] * 0.75)), Image.LANCZOS))
        scale = rel(positive_fraction(small))

        import io as _io
        buf = _io.BytesIO()
        Image.fromarray(rgb).save(buf, format="JPEG", quality=80)
        noisy = np.asarray(Image.open(_io.BytesIO(buf.getvalue())).convert("RGB"))
        noise = rel(positive_fraction(noisy))

    fails = []
    if miss > MISS_MAX and (miss * obv_px) >= MISS_MIN_PX:
        fails.append(f"MISS {miss*100:.1f}% of {obv_px}px")
    if flood > FLOOD_MAX:
        fails.append(f"FLOOD {flood*100:.1f}%")
    if grey > GREY_MAX:
        fails.append(f"GREY {grey*100:.1f}%")
    if tissue_ratio < TISSUE_MIN:
        fails.append(f"TISSUE {tissue_ratio*100:.1f}%")
    if light > LIGHT_TOL:
        fails.append(f"LIGHT {light*100:.0f}%")
    if scale > SCALE_TOL:
        fails.append(f"SCALE {scale*100:.0f}%")
    if noise > NOISE_TOL:
        fails.append(f"NOISE {noise*100:.0f}%")

    if montage_dir:
        os.makedirs(montage_dir, exist_ok=True)
        out = rgb.astype(np.float64).copy()
        out[pos] = 0.5 * out[pos] + 0.5 * GREEN
        gap = np.full((rgb.shape[0], 6, 3), 255, np.uint8)
        Image.fromarray(np.hstack([rgb, gap, out.astype(np.uint8)])).save(
            os.path.join(montage_dir, os.path.basename(path)[:-4] + ".png"))

    return {
        "file": os.path.basename(path),
        "positive_pct": round(base, 3),
        "positive_px": pos_px,
        "tissue_pct": round(res.tissue_percent, 2),
        "objects": res.objects,
        "bar": res.detection_bar,
        "sigma": round(sigma, 4),
        "separability": res.separability,
        "miss": round(miss, 4),
        "flood": round(flood, 4),
        "grey": round(grey, 4),
        "cover": round(cover, 4),
        "unambiguous_px": unamb_px,
        "tissue_ratio": round(tissue_ratio, 3),
        "light": round(light, 3),
        "scale": round(scale, 3),
        "noise": round(noise, 3),
        "secs": round(secs, 2),
        "fails": fails,
    }

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--montage", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--only", default=None, help="substring filter")
    ap.add_argument("--quick", action="store_true",
                    help="skip the illumination / resolution / compression perturbations")
    args = ap.parse_args()

    files = sorted(
        f for ext in ("png", "tif", "tiff", "jpg", "jpeg")
        for f in glob.glob(os.path.join(args.folder, f"*.{ext}"))
    )
    if args.only:
        files = [f for f in files if args.only in os.path.basename(f)]
    if not files:
        print(f"no images in {args.folder}")
        return 2

    rows, failed = [], 0
    for f in files:
        r = check(f, args.montage, args.quick)
        rows.append(r)
        flag = "FAIL " + "; ".join(r["fails"]) if r["fails"] else "ok"
        failed += bool(r["fails"])
        print(f"{r['file'][:30]:32s} pos={r['positive_pct']:6.3f}% tis={r['tissue_pct']:6.2f} "
              f"obj={r['objects']:5d} bar={r['bar']:.3f} sig={r['sigma']:.3f} "
              f"miss={r['miss']*100:5.1f}% cover={r['cover']*100:5.1f}% grey={r['grey']*100:4.1f}% "
              f"L/S/N={r['light']*100:3.0f}/{r['scale']*100:3.0f}/{r['noise']*100:3.0f}% {flag}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, indent=1)

    pct = [r["positive_pct"] for r in rows]
    print(f"\n{len(rows)} slides — {failed} failing")
    print(f"positive% : min {min(pct):.3f}  median {sorted(pct)[len(pct)//2]:.3f}  max {max(pct):.3f}")
    for key in ("miss", "flood", "grey", "light", "scale", "noise"):
        vals = [r[key] for r in rows]
        print(f"{key:10s}: median {sorted(vals)[len(vals)//2]*100:5.1f}%  worst {max(vals)*100:6.1f}% "
              f"({max(rows, key=lambda r: r[key])['file'][:24]})")
    cov = [r["cover"] for r in rows]
    worst = min(rows, key=lambda r: r["cover"])
    print(f"{'cover':10s}: median {sorted(cov)[len(cov)//2]*100:5.1f}%  worst {min(cov)*100:6.1f}% ({worst['file'][:24]})")
    return 1 if failed else 0

if __name__ == "__main__":
    raise SystemExit(main())
