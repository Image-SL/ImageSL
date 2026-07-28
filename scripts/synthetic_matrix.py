"""
ImageSL — parametric generalisation suite.

The point of this file is to stop the engine being right about a handful of
slides and wrong in general. Every scene here is generated from a grid of
conditions, and in each one the truth is known exactly, so recall and precision
are measured rather than eyeballed:

  * how big the stained structures are          (2 px punctum → 120 px plaque)
  * how strongly they are stained               (barely visible → saturated)
  * how dark and how brown the tissue is        (pale section → heavily counterstained)
  * whether the section carries a TONE          (a slow gradient of real chromogen)
  * how much of the frame is holes / lumina
  * whether neutral debris is present           (ink, dust, folds)
  * how noisy, how blurred, what resolution

Each condition exists because it is a way an engine can be accidentally right:
a detector tuned on medium puncta in pale tissue can fail every one of these and
still look perfect on the slides it was tuned against.

    python scripts/synthetic_matrix.py [--full] [--json out.json]

Exit status is non-zero if any condition fails.
"""

from __future__ import annotations

import argparse
import io
import itertools
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

from ihc import engine  # noqa: E402

H, W = 768, 1024

# ---- what "correct" means, per condition ---------------------------------- #
RECALL_MIN     = 0.70   # of the truly stained area, this much must be found
PRECISION_MIN  = 0.70   # of what is called positive, this much must be true stain
DEBRIS_MAX     = 0.10   # of a neutral blob's area, at most this may be counted
TONE_MAX_PCT   = 1.20   # a slide whose only chromogen is a slow tone reads ≤ this %
BLANK_MAX_PCT  = 0.20   # unstained tissue reads ≤ this %

# Realisations of each condition. Fixed and explicit, so a run is reproducible
# and two runs can be compared; several of them, so a condition whose answer
# depends on where the structures happened to land is caught rather than sampled.
SEEDS = (1, 2, 3, 4, 5)
# Two statistically identical scenes may not read more than this far apart.
INSTABILITY_MAX = 0.35    # relative spread across realisations
INSTABILITY_ABS_PP = 0.05  # ... ignoring spreads too small to matter in absolute terms

# Conditions marked `floor=True` sit below the physical detection floor — see
# the long note in `judge()`. They are still run, and their recall is still
# printed, because a large move in it between builds is worth seeing; they are
# just not failed on it.


def _rgb_from_od(od: np.ndarray) -> np.ndarray:
    return np.clip(255.0 * np.power(10.0, -od), 0, 255)


def build_scene(*, obj_radius, obj_count, stain_od, tissue_od, tone, lumina,
                debris, noise, blur, seed=0):
    """Compose a scene in ABSORBANCE, which is how staining actually works:
    tissue and chromogen add, they do not paint over one another."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]

    # Tissue: a haematoxylin base carrying real structure — lobular variation,
    # cell-sized texture and discrete nuclei. It has to look like a section, not
    # a flat fill: a flat fill is correctly refused by the engine's own
    # "is this even a slide?" gate, and every number measured on one would be
    # meaningless.
    hema = np.array([0.650, 0.700, 0.290])
    lobular = 0.22 * np.sin(xx / 130.0 + 0.7) * np.cos(yy / 155.0)
    cells = 0.20 * np.sin(xx / 6.5) * np.cos(yy / 7.5) + 0.12 * np.sin((xx + yy) / 4.5)
    tex = lobular + cells + rng.normal(0, max(noise, 1e-4), (H, W))
    od = (tissue_od * np.clip(1.0 + tex, 0.15, None))[..., None] * hema[None, None, :]

    # nuclei: small, distinctly haematoxylin, the way a counterstain really looks
    nuc = np.zeros((H, W), bool)
    for _ in range(900):
        x = int(rng.integers(3, W - 3)); y = int(rng.integers(3, H - 3))
        r = int(rng.integers(2, 4))
        nuc |= (xx - x) ** 2 + (yy - y) ** 2 <= r * r
    od[nuc] += 0.45 * hema[None, :]

    # a slow tonal gradient of REAL chromogen — more stain, but not a structure
    dab = np.array([0.270, 0.570, 0.780])
    if tone > 0:
        ramp = 0.5 + 0.5 * np.sin(xx / 190.0) * np.cos(yy / 220.0)
        od += (tone * ramp)[..., None] * dab[None, None, :]

    truth = np.zeros((H, W), bool)
    spots = []
    for _ in range(obj_count):
        r = obj_radius
        x = int(rng.integers(r + 8, W - r - 8))
        y = int(rng.integers(r + 8, H - r - 8))
        spots.append((x, y, r))
    for x, y, r in spots:
        m = (xx - x) ** 2 + (yy - y) ** 2 <= r * r
        od[m] += stain_od * dab[None, :]
        truth |= m

    # lumina / holes: material simply absent
    holes = np.zeros((H, W), bool)
    for _ in range(lumina):
        x = int(rng.integers(30, W - 30)); y = int(rng.integers(30, H - 30))
        rx, ry = int(rng.integers(8, 26)), int(rng.integers(6, 20))
        holes |= ((xx - x) / rx) ** 2 + ((yy - y) / ry) ** 2 <= 1.0
    holes &= ~truth
    od[holes] = 0.0

    # neutral debris: dark, no colour at all
    deb = np.zeros((H, W), bool)
    if debris:
        for _ in range(debris):
            x = int(rng.integers(40, W - 40)); y = int(rng.integers(40, H - 40))
            rx, ry = int(rng.integers(10, 30)), int(rng.integers(8, 22))
            deb |= ((xx - x) / rx) ** 2 + ((yy - y) / ry) ** 2 <= 1.0
        deb &= ~truth
        od[deb] = np.array([0.85, 0.85, 0.85])

    rgb = _rgb_from_od(od).astype(np.uint8)
    if blur > 0:
        rgb = np.asarray(Image.fromarray(rgb).filter(ImageFilter.GaussianBlur(blur)))
    return rgb, truth, deb


def score(rgb, truth, debris):
    res, maps = engine.analyze(rgb, (rgb.shape[1], rgb.shape[0]))
    pos = maps["positive"]
    t = int(truth.sum())
    recall = float((pos & truth).sum()) / t if t else 1.0
    p = int(pos.sum())
    precision = float((pos & truth).sum()) / p if p else 1.0
    leak = float((pos & debris).sum()) / int(debris.sum()) if debris.any() else 0.0
    return {
        "positive_pct": res.positive_percent,
        "recall": recall,
        "precision": precision,
        "debris_leak": leak,
        "objects": res.objects,
    }


# --------------------------------------------------------------------------- #
# The grid
# --------------------------------------------------------------------------- #

def conditions(full: bool):
    """Each entry: (name, scene kwargs, what must hold)."""
    out = []

    # 1. object SIZE, from a 2px punctum to a 120px plaque
    for r in ([2, 3, 5, 9, 18, 40, 60] if full else [2, 5, 18, 60]):
        out.append((f"size r={r}",
                    dict(obj_radius=r, obj_count=max(6, int(900 / (r * r))), stain_od=0.55,
                         tissue_od=0.30, tone=0.0, lumina=6, debris=0, noise=0.02, blur=0.4),
                    dict()))

    # 2. stain STRENGTH, from barely visible to saturated
    for s in ([0.12, 0.20, 0.35, 0.55, 0.9, 1.4] if full else [0.12, 0.35, 0.9]):
        out.append((f"strength od={s}",
                    dict(obj_radius=5, obj_count=40, stain_od=s, tissue_od=0.30,
                         tone=0.0, lumina=6, debris=0, noise=0.02, blur=0.4),
                    dict(floor=s <= 0.20)))

    # 3. TISSUE darkness — the background the stain has to beat
    for t in ([0.10, 0.30, 0.55, 0.80] if full else [0.10, 0.55, 0.80]):
        out.append((f"tissue od={t}",
                    dict(obj_radius=5, obj_count=40, stain_od=0.55, tissue_od=t,
                         tone=0.0, lumina=6, debris=0, noise=0.02, blur=0.4),
                    # A very pale section is NOT a floor case: the stain on it is
                    # perfectly visible, it is the section that is faint. It used
                    # to read 0% because an absolute "is this material?" cut
                    # classified 97% of such a section as empty space — fixed in
                    # detect.MATERIAL_TISSUE_FRAC — and is now enforced in full.
                    dict()))

    # 4. TONE — a slow gradient of genuine chromogen, with and without structures
    for tone in ([0.10, 0.25, 0.45] if full else [0.25, 0.45]):
        out.append((f"tone {tone} + structures",
                    dict(obj_radius=5, obj_count=40, stain_od=0.55, tissue_od=0.30,
                         tone=tone, lumina=6, debris=0, noise=0.02, blur=0.4),
                    dict()))
        out.append((f"tone {tone} alone",
                    dict(obj_radius=5, obj_count=0, stain_od=0.0, tissue_od=0.30,
                         tone=tone, lumina=6, debris=0, noise=0.02, blur=0.4),
                    dict(tone_only=True)))

    # 5. LUMINA — holes that must not create a halo of false positives
    for lum in ([0, 10, 40, 90] if full else [0, 40, 90]):
        out.append((f"lumina n={lum}",
                    dict(obj_radius=5, obj_count=40, stain_od=0.55, tissue_od=0.30,
                         tone=0.0, lumina=lum, debris=0, noise=0.02, blur=0.4),
                    dict()))

    # 6. NEUTRAL DEBRIS — dark, colourless, must never be counted
    for deb in ([2, 6, 14] if full else [6, 14]):
        out.append((f"debris n={deb}",
                    dict(obj_radius=5, obj_count=40, stain_od=0.55, tissue_od=0.30,
                         tone=0.0, lumina=6, debris=deb, noise=0.02, blur=0.4),
                    dict()))

    # 7. NOISE and BLUR — scanner quality
    for nz in ([0.01, 0.04, 0.08] if full else [0.01, 0.08]):
        out.append((f"noise {nz}",
                    dict(obj_radius=5, obj_count=40, stain_od=0.55, tissue_od=0.30,
                         tone=0.0, lumina=6, debris=0, noise=nz, blur=0.4),
                    dict()))
    for bl in ([0.0, 1.0, 2.0] if full else [0.0, 2.0]):
        out.append((f"blur {bl}",
                    dict(obj_radius=6, obj_count=40, stain_od=0.55, tissue_od=0.30,
                         tone=0.0, lumina=6, debris=0, noise=0.02, blur=bl),
                    dict()))

    # 8. BLANK — no chromogen anywhere
    out.append(("blank tissue",
                dict(obj_radius=5, obj_count=0, stain_od=0.0, tissue_od=0.30,
                     tone=0.0, lumina=8, debris=0, noise=0.03, blur=0.4),
                dict(blank=True)))
    out.append(("blank tissue, debris only",
                dict(obj_radius=5, obj_count=0, stain_od=0.0, tissue_od=0.30,
                     tone=0.0, lumina=8, debris=8, noise=0.03, blur=0.4),
                dict(blank=True)))
    return out


def judge(name, r, rule):
    fails = []
    if rule.get("blank"):
        if r["positive_pct"] > BLANK_MAX_PCT:
            fails.append(f"blank read {r['positive_pct']:.3f}% (max {BLANK_MAX_PCT})")
    elif rule.get("tone_only"):
        if r["positive_pct"] > TONE_MAX_PCT:
            fails.append(f"tone alone read {r['positive_pct']:.3f}% (max {TONE_MAX_PCT})")
    elif rule.get("floor"):
        # BELOW THE DETECTION FLOOR.
        #
        # Here the staining adds so little absorbance that the strongest
        # excursions of the tissue's own texture out-peak it. Measured on the
        # 0.12 OD condition: the true structures peak at ~0.15 excess while the
        # eligible background blobs reach 0.24-0.35, so *any* unsupervised rule
        # that separates two groups puts the boundary above the real stain.
        # Capping the operating point to force recall was tried and rejected —
        # the cap has to bite at the 75th percentile of the population to help
        # here, and at that setting it also binds on six of the 46 real
        # sections, every one of them a pale slide, where it drops the bar into
        # the parenchyma's own texture and paints it positive.
        #
        # So recall is not required at the floor and neither is stability:
        # there is no stable answer to be had, and demanding one would buy it
        # with false positives on real slides. What IS required is that the
        # engine does not invent staining — precision must hold — and it must
        # say the boundary is a judgement, which it does in `notes`.
        if r["precision"] < PRECISION_MIN:
            fails.append(f"precision {r['precision']*100:.1f}% (min {PRECISION_MIN*100:.0f}%)")
    else:
        if r["recall"] < RECALL_MIN:
            fails.append(f"recall {r['recall']*100:.1f}% (min {RECALL_MIN*100:.0f}%)")
        if r["precision"] < PRECISION_MIN:
            fails.append(f"precision {r['precision']*100:.1f}% (min {PRECISION_MIN*100:.0f}%)")
        if r.get("instability", 0.0) > INSTABILITY_MAX:
            fails.append(f"unstable across realisations: {r['pct_min']:.3f}%-{r['pct_max']:.3f}% "
                         f"({r['instability']*100:.0f}% spread, max {INSTABILITY_MAX*100:.0f}%)")
    if r["debris_leak"] > DEBRIS_MAX:
        fails.append(f"debris leak {r['debris_leak']*100:.1f}% (max {DEBRIS_MAX*100:.0f}%)")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="the dense grid")
    ap.add_argument("--json", default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    rows, bad = [], 0
    for name, kw, rule in conditions(args.full):
        if args.only and args.only not in name:
            continue
        # Every condition is run over SEVERAL realisations, and judged on the
        # worst of them.
        #
        # One realisation per condition is not enough, and the seed must not be
        # `hash(name)`: Python randomises string hashing per process, so this
        # suite silently generated different scenes on every run and its verdict
        # was not reproducible. Worse, it hid the defect that mattered most —
        # across realisations of one nominally identical condition (same object
        # count, same stain density, differing only in where the structures
        # landed) recall ranged from 13% to 98%. A single sample reported
        # whichever of those it happened to draw, so the instability read as
        # noise in the suite rather than as the bug it was.
        rr = []
        for seed in SEEDS:
            rgb, truth, deb = build_scene(seed=seed, **kw)
            rr.append(score(rgb, truth, deb))
        r = {
            "positive_pct": float(np.median([x["positive_pct"] for x in rr])),
            "recall": min(x["recall"] for x in rr),
            "precision": min(x["precision"] for x in rr),
            "debris_leak": max(x["debris_leak"] for x in rr),
            "objects": int(np.median([x["objects"] for x in rr])),
            "pct_min": min(x["positive_pct"] for x in rr),
            "pct_max": max(x["positive_pct"] for x in rr),
        }
        # A measurement instrument must give the same answer for the same
        # specimen. Realisations of one condition are statistically identical,
        # so a large spread between them is a defect in its own right, whatever
        # the recall happens to be.
        spread = r["pct_max"] - r["pct_min"]
        rel = spread / max(r["pct_max"], 1e-9)
        r["instability"] = rel if spread >= INSTABILITY_ABS_PP else 0.0
        fails = judge(name, r, rule)
        bad += bool(fails)
        rows.append({"case": name, **r, "fails": fails})
        flag = "FAIL " + "; ".join(fails) if fails else "ok"
        print(f"{name:26s} pos={r['positive_pct']:6.3f}% ({r['pct_min']:.3f}-{r['pct_max']:.3f}) "
              f"recall={r['recall']*100:5.1f}% "
              f"prec={r['precision']*100:5.1f}% debris={r['debris_leak']*100:4.1f}% "
              f"obj={r['objects']:5d}  {flag}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, indent=1)
    print(f"\n{len(rows)} conditions - {bad} failing")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
