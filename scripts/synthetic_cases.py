"""
ImageSL — synthetic cases with a known answer.

The slide backtest checks self-consistency on real sections, where nobody has
drawn the truth. These are the complementary half: small constructed scenes
where the correct answer is known exactly, each one built around a failure that
actually happened.

    python scripts/synthetic_cases.py

Exit status is non-zero if any case fails.
"""

from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

from ihc import engine  # noqa: E402

TAN = (205, 178, 168)
BROWN = (96, 58, 30)
GREY = (108, 108, 111)
H, W = 768, 1024


def _tissue(seed: int = 0) -> Image.Image:
    """Textured, absorbing tissue — the thing every case is drawn on."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    tex = 8 * np.sin(xx / 5) * np.cos(yy / 6) + rng.normal(0, 6, (H, W))
    base = np.zeros((H, W, 3), np.float32)
    for c, v in enumerate(TAN):
        base[..., c] = v + tex
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))


def _run(im: Image.Image):
    rgb = np.asarray(im)
    return engine.analyze(rgb, (W, H))


def case_large_plaque():
    """A stained region much wider than the background window must be measured
    WHOLE. It once vanished into its own background, leaving a thin rim: the
    background was fitted over everything first, so the plaque's interior never
    looked like an excess and was never excluded from the fit."""
    im = _tissue()
    d = ImageDraw.Draw(im)
    d.ellipse([300, 250, 560, 470], fill=BROWN)
    yy, xx = np.mgrid[0:H, 0:W]
    plaque = ((xx - 430) / 130.0) ** 2 + ((yy - 360) / 110.0) ** 2 <= 1.0
    _, maps = _run(im)
    covered = float(maps["positive"][plaque].mean())
    return covered >= 0.90, f"plaque covered {covered*100:.1f}% (need >=90%)"


def case_even_density():
    """Structures stained to one even density are one population, not two. A
    split criterion applied to it lands somewhere arbitrary inside the cloud and
    throws away half the stain for a third-decimal difference."""
    im = _tissue(1)
    d = ImageDraw.Draw(im)
    rng = np.random.default_rng(1)
    spots = []
    for _ in range(60):
        x, y = int(rng.integers(60, W - 60)), int(rng.integers(60, H - 60))
        d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=BROWN)
        spots.append((x, y))
    yy, xx = np.mgrid[0:H, 0:W]
    truth = np.zeros((H, W), bool)
    for x, y in spots:
        truth |= ((xx - x) ** 2 + (yy - y) ** 2) <= 16
    _, maps = _run(im)
    covered = float(maps["positive"][truth].mean())
    return covered >= 0.80, f"evenly stained spots covered {covered*100:.1f}% (need >=80%)"


def case_grey_debris():
    """Neutral material — ink, dust, a fold — is not chromogen however dark it
    is, and must not be counted."""
    im = _tissue(2)
    d = ImageDraw.Draw(im)
    d.ellipse([180, 180, 260, 240], fill=GREY)
    d.ellipse([600, 500, 700, 560], fill=(70, 70, 72))
    rng = np.random.default_rng(3)
    for _ in range(30):
        x, y = int(rng.integers(60, W - 60)), int(rng.integers(60, H - 60))
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=BROWN)
    yy, xx = np.mgrid[0:H, 0:W]
    debris = (((xx - 220) / 40.0) ** 2 + ((yy - 210) / 30.0) ** 2 <= 1.0) | \
             (((xx - 650) / 50.0) ** 2 + ((yy - 530) / 30.0) ** 2 <= 1.0)
    _, maps = _run(im)
    leaked = float(maps["positive"][debris].mean())
    return leaked <= 0.05, f"grey debris counted {leaked*100:.1f}% (need <=5%)"


def case_blank_slide():
    """Unstained tissue must read ~zero. Nothing in the engine may manufacture a
    population out of texture."""
    im = _tissue(4)
    res, _ = _run(im)
    return res.positive_percent <= 0.15, f"blank tissue read {res.positive_percent:.3f}% (need <=0.15%)"


def case_tone_vs_structures():
    """A slow tonal gradient across the section is not a structure. A section
    that is simply darker toward one side carries more chromogen, but there is
    nothing countable in it, and painting it green reads as picking up
    background."""
    im = _tissue(5)
    arr = np.asarray(im).astype(np.float32)
    yy, xx = np.mgrid[0:H, 0:W]
    ramp = 46.0 * (0.5 + 0.5 * np.sin(xx / 210.0) * np.cos(yy / 190.0))
    arr[..., 0] -= ramp * 0.55
    arr[..., 1] -= ramp * 0.85
    arr[..., 2] -= ramp * 1.00          # warmer as it darkens: a DAB-like tone
    res, _ = _run(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)))
    # The bar is not zero. This ramp swings ~18% of the transmitted light over
    # roughly 200 px — as steep as the centrilobular zonation of a real
    # diffusely stained liver — and where it turns fastest it is genuinely
    # indistinguishable from a broad, soft structure. A residual around one
    # percent is the honest limit of separating tone from structure by spatial
    # scale; the same scene read 5.3% before that separation existed, and the
    # result carries a note telling the reader the staining is a tone.
    return res.positive_percent <= 1.5, f"tonal gradient read {res.positive_percent:.3f}% (need <=1.5%)"


CASES = [
    ("large plaque measured whole", case_large_plaque),
    ("evenly stained spots kept", case_even_density),
    ("grey debris rejected", case_grey_debris),
    ("blank tissue reads zero", case_blank_slide),
    ("tonal gradient is not structure", case_tone_vs_structures),
]


def main() -> int:
    bad = 0
    for name, fn in CASES:
        ok, detail = fn()
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {name:34s} {detail}")
    print(f"\n{len(CASES)} cases — {bad} failing")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
