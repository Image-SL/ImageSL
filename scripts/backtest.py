"""
ImageSL detection backtest.

Runs the real engine over a folder of slides and applies objective checks that
need no hand-drawn ground truth. Every check is a statement that must hold for
any correct IHC quantifier, and each failure names the slide and the number.

  MISS      Unmistakable chromogen — dense, clearly brown, well above the
            slide's own tissue texture — that the detector did not call
            positive. This is the failure that made the previous build report
            near-zero on obviously stained sections.

  FLOOD     Positive pixels that carry essentially no excess absorbance, i.e.
            background counted as signal.

  GREY      Positive pixels whose excess absorbance is not chromogen-coloured:
            folds, ink, dust, haematoxylin.

  COVER     Reported, not enforced. The share of material that is chromogen by
            the RAW image — absorbing strongly AND clearly warm — that was
            detected. It is a useful second opinion on under-detection, in the
            units a person looking at the slide would use rather than the
            engine's own.

            It is NOT a pass/fail gate, because on a diffusely stained section
            the tissue itself meets that description: the metric cannot tell
            "missed a structure" from "correctly declined to count the tissue's
            own tone", and the two have opposite meanings. Requiring local
            prominence to separate them just turns it back into MISS. Watch it
            for large moves between runs; judge under-detection by MISS.

  TISSUE    A tissue mask that has thrown away material which is plainly not
            bare glass (the collapse that silently changes every denominator).

  LIGHT     Instability against a ±12% illumination change. A measurement that
            moves when the lamp does is not a measurement.

  SCALE     Instability against analysing the same slide at a different
            working resolution.

  NOISE     Instability against mild JPEG compression.

Usage:
    python scripts/backtest.py <folder-of-images> [--montage out_dir] [--jobs N]
"""

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

from ihc import detect, engine  # noqa: E402

# --------------------------------------------------------------------------- #
# Tolerances
# --------------------------------------------------------------------------- #

MISS_MAX      = 0.05    # ≤5% of unmistakable stain may be missed
MISS_MIN_PX   = 60      # ... and a miss under this many pixels is not a finding
FLOOD_MAX     = 0.10    # ≤10% of positives may carry no real excess
GREY_MAX      = 0.22    # ≤22% of positives may be non-chromogen-coloured
TISSUE_MIN    = 0.90    # tissue mask ≥ 90% of the not-obviously-glass area
ABS_TOL_PP    = 0.50    # ... and under this many percentage points is never a failure
LIGHT_TOL     = 0.30    # ≤30% relative change in positive area under ±12% light
SCALE_TOL     = 0.40    # ≤40% relative change at a different working resolution
NOISE_TOL     = 0.30    # ≤30% relative change under JPEG q80

OBVIOUS_OD    = 0.40    # excess OD that is unmistakably chromogen
OBVIOUS_SIG   = 7.0     # ... and this many texture sigmas above background
OBVIOUS_BROWN = 0.30    # ... and this clearly the chromogen's own colour

COVER_RAW_OD  = 0.60    # raw absorbance along the chromogen axis that qualifies
COVER_WARM    = 0.15    # ... together with this much raw warmth, (R−B)/(R+B)
COVER_MIN_PX  = 400     # below this the population is too small to judge
GREEN = np.array([57, 255, 20], dtype=np.float64)


def analyse(rgb: np.ndarray, level=None):
    res, maps = engine.analyze(rgb, (rgb.shape[1], rgb.shape[0]), level=level)
    return res, maps


def glass_estimate(rgb: np.ndarray) -> float:
    """Independent, deliberately crude estimate of the BARE GLASS fraction:
    bright, colourless and smooth. Anything else is material of some kind."""
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
    """Share of the positive AREA sitting in structures that are not
    chromogen-coloured.

    Two independent colour readings, because either one alone is unfair to a
    genuine detection: the absorbance-excess direction (`brown`), which is the
    engine's own measure and goes flat on dense, channel-crushed stain; and the
    raw warmth of the pixels, (R − B)/(R + B), which stays clearly positive for
    dark brown and sits at zero for ink, dust and folds. Only a structure that
    fails BOTH is grey.
    """
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


def check(path: str, montage_dir=None, quick: bool = False) -> dict:
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
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
    obv_px = int(obvious.sum())
    miss = float((obvious & ~pos).sum()) / obv_px if obv_px else 0.0

    pos_px = int(pos.sum())
    flood = float((pos & (sig < max(0.05, 1.2 * sigma))).sum()) / pos_px if pos_px else 0.0
    # Grey is asked OBJECT by object: a dense chromogen core is colour-crushed and
    # would look "grey" pixel-wise even though the structure it belongs to is
    # unmistakably brown. What must not be counted is a whole structure that is
    # neutral — a fold, an ink mark, a dust speck.
    grey = _grey_object_fraction(pos, brown, rgb)

    # Independent of the engine's own excess units: what a person would call
    # obviously stained in the raw image.
    from ihc import detect as _detect
    dab = _detect.DAB_OD / np.linalg.norm(_detect.DAB_OD)
    raw_abs = (maps["od"] @ dab).astype(np.float32)
    fl = rgb.astype(np.float32)
    warm_raw = (fl[..., 0] - fl[..., 2]) / (fl[..., 0] + fl[..., 2] + 1.0)
    # ... and locally prominent, if only slightly. Without that clause the
    # population swallows the tissue tone of a diffusely stained section — which
    # is exactly the material the engine is right not to count — and the check
    # would punish correct behaviour.
    unambiguous = (tissue & (raw_abs >= COVER_RAW_OD) & (warm_raw >= COVER_WARM)
                   & (sig >= max(0.05, 1.5 * sigma)))
    unamb_px = int(unambiguous.sum())
    cover = float((unambiguous & pos).sum()) / unamb_px if unamb_px else 1.0

    glass = glass_estimate(rgb)
    tissue_ratio = (tis_px / rgb[..., 0].size) / max(1e-6, 1.0 - glass)

    base = res.positive_percent

    # A perturbation "moved the answer" only if it moved it BOTH relatively and
    # in absolute terms. On a slide reading 0.4% positive area, a 40% relative
    # swing is 0.16 percentage points — smaller than the difference between two
    # honest human annotations, and reporting it as a failure would just be
    # reporting the small denominator.
    def rel(x):
        r = abs(x - base) / max(base, 0.05)
        return r if abs(x - base) >= ABS_TOL_PP else 0.0

    light = scale = noise = 0.0
    if not quick:
        # Illumination is varied DOWNWARD and by colour temperature. Scaling a
        # bright-field scan up instead would push a background that already sits
        # at 247/255 into clipping, and pale tissue that has been flattened to
        # pure white is not recoverable by any method — that would test the
        # sensor, not the engine.
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
    # A percentage of a handful of pixels is noise, not a finding: the miss has
    # to be a real area before it counts as a failure.
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
