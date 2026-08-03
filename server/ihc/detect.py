"""
ImageSL — area / object based chromogen detection.

This module replaces the old "one global intensity threshold" decision. Nothing
here asks *"is this pixel darker than 0.43?"*. It asks, in order:

  1. **What extra absorbance does this pixel carry over its own neighbourhood?**
     A large-window, foreground-excluded background field is fitted to the
     optical density itself, so a diffuse tan wash, a lamp gradient, a warm
     scanner tint or genuinely dark tissue is subtracted away *locally*. Every
     later decision is made on that excess, never on the raw density — which is
     what makes the engine scale-consistent: a heavily stained slide is not
     penalised by its own high background, and a pale one is not flooded.

  2. **What colour is the excess?** The excess OD vector is compared against
     reference directions (DAB, haematoxylin, red/RBC, neutral debris). DAB has
     a distinctive blue-over-red absorbance signature that survives at any
     density, unlike HSV hue, which becomes meaningless as a pixel approaches
     black — the exact reason the previous build dropped the darkest, most
     unambiguous stain and kept its halo.

  3. **Which connected AREAS does that evidence form?** Colour-specific *seeds*
     are grown through contiguous excess-carrying pixels (hysteresis), and the
     grown component — the object — is what gets accepted or rejected, by its
     own area, peak evidence, colour purity and how much of it is genuinely
     chromogen-directional. A stained structure is therefore measured whole,
     including the black-saturated core that carries no usable hue on its own,
     while an isolated grey fold or a dust speck with no chromogen-coloured seed
     is rejected however dark it is.

  4. **Where is that area stable?** Sensitivity is not a magic constant. The
     detector is run over a ladder of evidence levels and the operating point is
     chosen by MSER-style stability: the level at which the detected area
     changes least for a change in sensitivity. Real structures persist across
     many levels; noise and background wash explode.

The ladder also produces the `level map`: for every pixel, the *first* level at
which it becomes positive (255 = never). One 8-bit image therefore encodes the
full family of results, so the browser can move the sensitivity slider — and
apply per-region sensitivity offsets from the manual tools — live, with no
server round-trip, and still reproduce the server's object-based decision
exactly.
"""

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
except Exception:  # pragma: no cover - scipy ships with scikit-image
    _HAVE_SCIPY = False
    _gaussian = None
    _sobel = None


# --------------------------------------------------------------------------- #
# Reference absorbance directions (unit OD vectors)
# --------------------------------------------------------------------------- #
# Used to answer "what colour is the EXCESS absorbance here", which is a very
# different question from "what colour is this pixel" and is the one that stays
# answerable on dark pixels.

DAB_OD     = np.array([0.270, 0.570, 0.780])   # brown  — blue >> red
HEMA_OD    = np.array([0.650, 0.700, 0.290])   # blue   — red > blue
RED_OD     = np.array([0.070, 0.990, 0.110])   # eosin / RBC / red chromogen
NEUTRAL_OD = np.array([0.577, 0.577, 0.577])   # ink, dust, folds, shadow


# --------------------------------------------------------------------------- #
# Tuning — every constant is dimensionless (a ratio, or a multiple of this
# slide's own noise), so nothing here encodes an absolute brightness.
# --------------------------------------------------------------------------- #

# How large a neighbourhood counts as "local" for the background. This is the
# scale that separates staining from tone: anything varying more slowly than
# this window is absorbed into the background and stops being signal, anything
# more compact survives as excess. Tissue tone — the centrilobular gradient of a
# diffusely stained liver, a warm scanner cast, a thickness gradient — varies
# over hundreds of pixels; stained structures are a handful across. A window
# around a twentieth of the frame sits between the two, and on the validation
# set it is the difference between a diffusely stained section reading 5.3% of
# its area as positive and reading 1.0%, with every focal slide unmoved.
#
# It is only safe to be this tight because a structure WIDER than the window is
# protected by the absolute foreground test below; without that, a large plaque
# would be absorbed into its own background.
BG_WIN_FRAC      = 0.06   # background-field window as a fraction of the long edge
BG_WIN_MIN       = 33     # ... never smaller than this (px)
BG_COARSE_MULT   = 4      # second, coarser scale used where the fine one is starved
BG_ITERS         = 2      # foreground-exclusion refinement passes
BG_FG_MAX_FRAC   = 0.55   # never exclude more than this share of tissue as foreground
BG_GLOBAL_FG_K   = 6.0    # ... and treat material this many robust sigmas above the
                          # slide's own tissue level as foreground wherever it is,
                          # so a stained region wider than the window cannot end up
                          # inside its own background estimate

# What counts as MATERIAL for the background estimate: absorbance above this
# (summed over channels, against the slide's own white point) means light passed
# through something.
#
# Lumina, vessel spaces, tears and the holes inside a section transmit almost
# everything. They sit *inside* the tissue mask — lumina are conventionally part
# of the section — but they are not material, and averaging them into a
# neighbourhood's background pulls that background down. Every real pixel beside
# a hole then looks like it carries excess absorbance, and the detector rings
# every lumen with false positives while genuine staining in the solid interior,
# measured against an honest background, falls below the same bar. Both halves of
# that were visible on the validation slides: detections were 8.7x more likely
# within a few pixels of a hole than in the interior of the same section.
MATERIAL_OD_MIN  = 0.15
# ... but never more than this share of the section's OWN absorbance, because
# "absorbs less than 0.15" is a statement about a hole only on a slide that
# absorbs appreciably more than 0.15 to begin with.
#
# A thin, weakly counterstained or brightly scanned section can absorb barely
# more than the constant in total. Every pixel of it then reads as a hole: the
# background field is fitted on the 3% of the frame that survives — which is the
# stained material itself — and the slide's noise estimate comes back ten times
# too large (0.10 against the 0.02-0.08 real sections show). The floor and the
# detection bar are both multiples of that noise, so they land far above the
# staining and the section reports 0.000% with 40 obvious structures on it.
#
# Scaling the cut by the tissue's own median absorbance fixes that without
# touching any normal slide: an ordinary section's median runs 0.6-1.0, so this
# term is 0.21-0.35 and the constant above still binds. It only ever engages
# where the constant would otherwise swallow the section.
MATERIAL_TISSUE_FRAC = 0.35

# Colour specificity of the EXCESS absorbance. `brownness` = (od_B − od_R)/‖od‖:
# +0.51 for pure DAB, −0.36 for haematoxylin, +0.03 for eosin/red, 0.00 for
# neutral debris. A dark, channel-crushed DAB core drifts toward 0, which is
# exactly why it is allowed in by CONNECTIVITY (growth) instead of by colour.
BROWN_SEED       = 0.22   # a seed must be unmistakably DAB-directional
BROWN_GROW       = -0.05  # growth only stops at clearly non-brown excess (blue nuclei)
# Growth is permissive about the excess DIRECTION, because a dense core's
# direction collapses toward neutral and it must still be joined to the object it
# belongs to. That permissiveness has to be paid for somewhere: a neutral blob
# TOUCHING a stained structure would otherwise be swallowed into it and counted.
#
# What settles it is ACHROMATICITY, measured on the raw pixels: ink, dust and
# folds carry no colour at all (saturation 0.00-0.08 measured), while chromogen
# — even a dense, channel-crushed core — carries plenty (0.19-0.49 measured).
# This has to be an absolute property of the material, not a comparison with the
# surroundings: against a blue counterstain a grey blob really is "warmer than
# its neighbours", so every background-relative colour measure calls it stain.
NEUTRAL_SAT_MAX  = 0.12

# Which chromogen is it? `brownness` says "absorbs blue more than red", which is
# true of DAB *and* of the red chromogens (AEC, Fast Red) — both look reddish.
# What separates them is the GREEN channel: DAB absorbs blue hardest (+0.21 on
# this axis), a red chromogen absorbs green hardest (−0.88), haematoxylin −0.41.
# Measured on real DAB the axis runs −0.04 to +0.19, so the bar sits far from
# real staining and far from every other chromogen.
#
# This replaces an HSV hue window, which could not do the job: dense chromogen
# shifts red as it darkens, so genuine DAB lands at 8° against a window opening
# at 10° and is thrown away — the parametric suite caught exactly that. A
# channel-ORDER test does not care how dark the pixel is.
BLUE_OVER_GREEN_MIN = -0.15
CHROMOGEN_SHARE_MIN = 0.35   # below this, the slide's chromogen is not this one
OBJ_BROWN_MEAN   = 0.14   # the object must be brown ON AVERAGE — what separates dark
                          # stain (brown rim, crushed core) from dark debris (neutral
                          # throughout). A mean is used rather than "what share of the
                          # pixels clear a colour bar", because a share is a step
                          # function of every pixel it counts: JPEG chroma subsampling
                          # nudges pixels across that bar and whole structures then
                          # appear or vanish, while the mean barely moves.

# --------------------------------------------------------------------------- #
# Reading colour on DENSE chromogen
# --------------------------------------------------------------------------- #
# The readings above are all taken on the EXCESS absorbance — the pixel minus its
# own local background — which is the sensitive way to see dilute staining. On
# the densest chromogen it fails, and it fails for a specific, unavoidable
# reason: the local background under a stained structure is itself tinted by the
# same chromogen, so subtracting it removes precisely the signature being tested
# for. A near-black bile duct on a tan section measures excess-brown +0.09
# against a +0.14 bar and is thrown away — the darkest, least ambiguous staining
# on the slide, the material a pathologist points at first.
#
# Measured over 90 407 candidate objects across the 46-slide validation set: of
# the objects whose peak excess is 12+ times the slide's own texture — material
# nobody would call anything but stain — 8.8% were rejected as "not chromogen
# coloured", with median excess-brown +0.091 and median excess-warmth +0.020.
#
# The fix is to ask the colour question of the material's OWN absorbance
# instead. That reading does not degrade with density, because it is a direction
# in absorbance space rather than a difference of two similar quantities:
#
#     raw brownness   (od_B − od_R)/‖od‖   DAB +0.51 · haematoxylin −0.36
#                                          red chromogen +0.04 · neutral 0.00
#     raw blue/green  (od_B − od_G)/‖od‖   DAB +0.21 · haematoxylin −0.41
#                                          red chromogen −0.88 · neutral 0.00
#
# On the same 630 wrongly-rejected objects those read +0.222 and +0.103 median
# (5th percentile +0.138 and +0.058) — nothing like neutral debris, which sits
# at 0.00 on both. The bars below sit between the two with margin on each side,
# and stay clear of every other chromogen rather than merely of grey.
# These bars are deliberately STRICT, and much stricter than the excess-colour
# ones, because an object only reaches this test after the excess reading has
# already declined it. Passing on the raw reading alone is therefore an override,
# and an override needs strong evidence rather than the benefit of the doubt.
#
# Set loosely (0.12 / 0.06) they admitted intraluminal granular debris — the
# olive-grey casts of pigment and cells that sit inside vessel lumina — which is
# dense, so it passes every density test, and sits near enough to neutral that a
# permissive colour bar cannot see it. Every one of the largest objects the raw
# path admitted at those bars was debris; not one was a duct.
#
# Measured over 24 such objects confirmed by eye against 79 unmistakable DAB
# ductules on the same sections:
#
#                    luminal debris         DAB ductules
#     raw brownness  0.129 - 0.206          0.343 - 0.416
#     raw warmth     0.087 - 0.150          0.263 - 0.325
#     saturation     0.160 - 0.259          0.412 - 0.485
#
# The three bars sit in the gap on each reading, and an object must clear ALL of
# them. Saturation is included because it is the reading on which the two are
# furthest apart, and because it asks the one question the others do not: does
# this material carry colour at all, or is it merely dark?
# ... and the conjunction described above is NOT what is used any more.
#
# The reasoning above is sound and the conclusion was wrong, for a reason worth
# recording. Two of the three readings it ANDs together are computed from
# TRANSMITTED light — raw warmth is (T_R − T_B)/(T_R + T_B) and saturation is
# (T_max − T_min)/T_max — so both collapse toward zero on near-black material.
# That is exactly the material this override exists to rescue. Requiring all
# three therefore reinstated the failure it was written to fix: measured across
# 154 slides, the colour gate was refusing a mean 8.3% of the unmistakable
# chromogen on the worst slides and up to 32% on one (OK3029).
#
# Dropping the two transmittance terms and judging on the absorbance direction
# alone cut the median miss from 2.91% to 0.44% and the worst from 17.7% to 7.5%
# with no movement in the non-chromogen share of positives. That direction has
# since been replaced in turn by a proper decomposition, because a direction
# still describes the total absorbance and so is dragged around by the
# counterstain — see CHROMO_FRAC_MIN, which is what the object gate actually
# uses now.
#
# These three are no longer gated on. They are still computed and reported per
# object, because they are what the earlier analyses were written against and a
# future one will want to compare.
OBJ_RAW_BROWN    = 0.22   # reported only — object mean od (B−R)
OBJ_RAW_WARM     = 0.20   # reported only — raw warmth (R−B)/(R+B)
OBJ_RAW_SAT      = 0.30   # reported only — raw saturation

# --------------------------------------------------------------------------- #
# Intraluminal debris
# --------------------------------------------------------------------------- #
# Granular casts of pigment, bile and cells sitting inside a vessel lumen. They
# are the hardest false positive on these sections and they defeat every test
# built so far, for a specific reason: they are DENSE, so every absorbance and
# prominence test passes them, and they sit against a pale lumen, so their excess
# over the local background is large and reads faintly brown. A gate on any one
# reading either lets them through or takes real staining with it.
#
# What they are not is *coloured like DAB*. Two readings say so together, and
# only together:
#
#                     luminal debris    DAB ductules   all detected objects
#     saturation      0.16 - 0.26       0.41 - 0.49    1st pct 0.220
#     hue (deg)       28.8 - 70.7       15.7 - 28.9    median 21.1, 95th 34.8
#
# So an object is refused when it is BOTH washed-out AND olive rather than
# brown. Either alone would be wrong: genuinely faint stain is pale but stays
# brown, and a dense chromogen core drifts in hue but stays saturated. Requiring
# both costs 4.7% of the detected area across the 46 sections and removes every
# one of the debris bodies confirmed by eye.
#
# Hue is taken as a saturation-weighted circular mean, so the near-neutral pixels
# inside a granule cloud — whose hue is arbitrary — do not vote on the answer.
#
# The two conditions are ANDed, and that structure matters. Adding them into one
# graded score was tried and is much worse: a pale but unmistakably brown
# structure (saturation 0.24, hue 22 deg) then has its low saturation counted
# against it even though its hue says exactly what it is, and the suite showed
# the cost immediately — up to 44% of the obvious stain on a section discarded.
# Under an AND, hue alone acquits it, which is the right answer.
#
# The trade is that an object near either bar can flip under a re-encode. That
# is bounded and acceptable here — the conditions are near-orthogonal, so an
# object has to sit on BOTH boundaries at once to be genuinely marginal — and it
# is measured by the compression check in the backtest rather than assumed.
#
# Both readings are taken on the white-point-normalised, chroma-smoothed image
# (`norm_sat` / `norm_hue` below), NOT on raw HSV. Raw HSV is not a property of
# the material: it moves with the lamp's colour temperature and with JPEG chroma
# subsampling. Measured across 2 200 objects, a threshold on raw hue moved by up
# to 9.3 deg under a warm lamp or a re-encode, against a population boundary
# with barely a degree of margin — so objects flipped wholesale and two sections
# moved 65-88%. The normalised reading halves that drift, and unlike the raw one
# it means the same thing on two scanners.
#
# The bars are then set CONSERVATIVELY rather than at the point of best
# separation. The two populations overlap — there is no cut that removes every
# debris body and keeps every faint structure — so the choice is which error to
# make. Refusing only material that is unambiguously washed-out AND olive
# removes the large luminal casts that prompted this while leaving borderline
# objects counted, which is the right way round: an over-count is visible and
# can be excluded with an Ignore region, whereas silently deleting faint
# staining is not visible at all.
DEBRIS_SAT_MAX   = 0.22
DEBRIS_HUE_MIN   = 30.0

# --------------------------------------------------------------------------- #
# ... and why that test is no longer what decides it
# --------------------------------------------------------------------------- #
# Everything above describes a saturation-and-hue test on the (normalised,
# chroma-smoothed) TRANSMITTED image, applied per pixel before objects are
# grouped. Measured against a 154-slide corpus it is the single largest source of
# error in the detector, in both directions, and it has to go.
#
# What it actually does, measured. Two populations were built without reference
# to any of the readings being compared — DAB objects identified by connectivity
# to unambiguous excess-brown seeds, and dense objects containing no such seed
# anywhere — and each rule scored on the decision the detector really makes,
# per object:
#
#                                        refuses DAB    refuses seedless
#     mean sat < 0.22 AND hue > 30            6.5%             32.1%
#     mean od (B-R) < 0.18                    0.6%             35.2%
#
# The replacement is better on BOTH axes: a tenth of the damage to real staining
# while refusing more debris. And the hue half of the old test discriminates
# nothing at all — DAB objects sit at a median hue of 23.6 deg and seedless ones
# at 27.9, distributions that almost completely overlap.
#
# The reason is the one this module already states about HSV hue and then went on
# to violate: a ratio taken on transmitted light collapses as a pixel approaches
# black, so both readings degrade exactly on the densest chromogen — the material
# nobody would mistake for anything else. An absorbance DIRECTION does not: it is
# scale-free, so a near-black duct core reads the same brown as its rim.
#
# The second half of the change matters as much as the first. The old test was
# applied PER PIXEL, before connectivity, and a gate that erodes 8% of a
# structure's pixels does not cost 8% — it fragments the structure into pieces
# that then fail the area and seed-fraction gates, and the whole thing is lost.
# Attributed pixel by pixel across the corpus, that mechanism accounted for 81%
# of the unmistakable dark-brown chromogen missed on the worst slide (OK3369),
# 79% on OK3258 and 76% on OK3413 — against a per-pixel kill rate of only 8%.
#
# So the neutral-material test is now a single per-OBJECT veto, below. The
# original objection to judging per object — that a duct running along a vessel
# wall is connected to the cast inside it, so refusing the merged body deletes
# the duct — is still true, but it is now a 0.6% risk rather than a 6.5% one,
# and the Exclude tool removes such a body by hand in one drag.
#
# `chroma_readings` and `looks_like_debris` are kept: they are still reported per
# object as diagnostics, and the regression suite compares against them.
# --------------------------------------------------------------------------- #
# What replaces it: how much of this object's absorbance IS the chromogen
# --------------------------------------------------------------------------- #
# The direction of the total absorbance was the first replacement, and the
# parametric suite immediately found the flaw in it: a direction describes
# EVERYTHING absorbing at that pixel, so a strong counterstain drags a genuinely
# DAB-stained pixel toward haematoxylin. Worked through on the suite's own
# scenes, a spot carrying 0.55 OD of DAB reads
#
#     over 0.10 OD of haematoxylin   od (B-R) = +0.387   ... obviously brown
#     over 0.30 OD                   od (B-R) = +0.212   ... marginal
#     over 0.80 OD                   od (B-R) = -0.007   ... "not brown at all"
#
# so the veto refused real staining on every heavily counterstained section and
# recall on the suite collapsed to near zero for tissue_od >= 0.55. These liver
# sections are barely counterstained, so the corpus never showed it; the suite
# exists for exactly this.
#
# The fix is to stop asking about a direction and decompose the absorbance onto
# the three things that are actually present — counterstain, chromogen, and
# neutral material (ink, pigment, fold, shadow) — and ask what SHARE of it is the
# chromogen, against the neutral part only:
#
#     chromogen fraction = a_chromogen / (a_chromogen + a_neutral)
#
# The counterstain term drops out of the ratio, which is the point: it is not
# what is being judged. Both worked examples above then read 1.000, pure grey
# reads 0.000, and grey with a trace of chromogen in it reads 0.143.
#
# Measured per object over the corpus (2 843 DAB objects against 462 dense
# objects carrying no chromogen seed anywhere), and compared with every rule
# tried before it:
#
#                                        refuses DAB    refuses non-chromogen
#     mean sat < 0.22 AND hue > 30            6.5%             32.1%
#     mean od (B-R) < 0.18                    0.6%             35.2%
#     chromogen fraction < 0.30               0.6%             41.1%
#
# It is the best of the three on both axes at once, it is the only one of the
# three that survives a counterstain, and unlike a transmittance ratio it does
# not degrade on a near-black core. Basis condition number 16.2 — comfortably
# invertible.
# The bar itself is 0.15, not the 0.30 the object measurement above would
# suggest, and the reason is what the two numbers are measuring. That comparison
# scored "dense objects containing no strong chromogen seed" as the negative
# class, and that class is not pure debris — most of it is genuine faint
# staining, which is exactly what a detector must not throw away. Swept against
# the corpus instead, 0.30 leaves six slides missing more than 5% of the obvious
# chromogen (worst 34%), while 0.15 leaves one (worst 11.6%), and the share of
# positives that are neutral does not move (0.58% to 0.61% at the median).
#
# What guards the low end is not this number but the suites that measure debris
# directly: `synthetic_cases` requires grey debris to stay under 5% of the
# measured area, and `synthetic_matrix` reports the debris it plants explicitly.
# Pure neutral material decomposes to a chromogen fraction of 0.000, so it is
# nowhere near this bar.
#
# Raised 0.15 -> 0.25 (2026-07-30). 0.15 was letting through the grey/black
# deposits in liver — pigment and folds that are dense and faintly warm, not
# chromogen. They are not rare noise: on OK3306 they were the LARGEST objects in
# the frame, drawn as solid filled blobs among the fine ductular speckle, which
# is what a reader notices first. Measured on the detector's own object stats,
# they score chromo_frac_core 0.229-0.239 while real ductular structures on the
# same slides sit at p1=0.399, median 0.671 — a gap with nothing in it.
# Swept against the corpus (46 slides, --quick):
#     0.15  0 fail  miss max 2.3%   grey max 8.47%
#     0.20  0 fail  miss max 2.3%   grey max 5.59%   (keeps every OK3306 blob)
#     0.25  0 fail  miss max 3.2%   grey max 5.47%
#     0.30  1 FAIL  miss max 15.4%  grey max 3.85%   (OK3029 loses 15.4%)
# 0.30 reproduces the failure the paragraph above predicted, so the ceiling is
# real. 0.20 is safer on paper but sits just under the deposits' own 0.229 and
# removes none of them, which is the whole point — so 0.25: the first value that
# clears the deposits while no slide newly misses >5% of its obvious chromogen.
CHROMO_FRAC_MIN = 0.25    # below this share, the object is not this chromogen

# --------------------------------------------------------------------------- #
# The detection bar may not discard what is unmistakably chromogen
# --------------------------------------------------------------------------- #
# The bar is chosen by splitting the population of object peaks, which answers
# "where do background bumps end and structures begin". On a slide whose staining
# spans a genuine range — strong ducts and a weaker ductular reaction around them
# — that split can land INSIDE the real population, and the weaker half is
# discarded. Measured across the corpus after the debris fix, this was the whole
# of the remaining false-negative problem: every slide still missing a large share
# of obvious chromogen had a bar many times its own candidate floor (OK3038
# bar 0.381 vs floor 0.025, missing 61%; OK3029 0.223/0.048, missing 35%;
# OK2996 0.314/0.062, missing 23%) and a high separability, so the split was
# trusted completely.
#
# Separability cannot see this. It measures how cleanly the population divides in
# two, not whether both halves are stain — and it is HIGHEST (0.86-0.91 on those
# slides) exactly when a real bimodal stain distribution is cut down the middle.
#
# So the split keeps its say, but not the final say: whatever it proposes, the bar
# is not allowed to sit above material that is unmistakably chromogen by its own
# absorbance — dense, and clearly brown-directional, a reading that owes nothing
# to the local background or to the population statistics that set the bar. The
# clamp is a quantile rather than the minimum so that one marginal object cannot
# drag the operating point down onto a slide's texture.
UNMISTAKABLE_BROWN = 0.28   # object mean od (B-R): DAB objects median 0.325
UNMISTAKABLE_SIG_K = 5.0    # ... and this dense, in the slide's own tissue units
UNMISTAKABLE_PCT   = 10.0   # bar <= this percentile of such objects' peaks
UNMISTAKABLE_MIN_N = 6      # ... only when there are enough of them to be a population
BAR_NOTE_RATIO     = 0.67   # ... and only SAY so when it moved the bar this much
SEED_RAW_BROWN   = 0.14   # a dense pixel may seed on the raw reading alone ...
SEED_RAW_BOG     = 0.04   # ... if its channel ORDER is the chromogen's, not red's
# "Dense" is a statement about this slide, not an absolute brightness: material
# whose absorbance along the chromogen axis sits this many robust deviations
# above the slide's own tissue level. That is the condition under which the
# excess reading stops being trustworthy, so it is the condition under which the
# raw reading is allowed to stand in — never on ordinary tissue tone.
DENSE_SIG_K      = 4.0
OBJ_SEED_FRAC    = 0.06   # ... and the chromogen colour must not be one stray pixel
OBJ_SEED_MIN_PX  = 2      # ... on at least two pixels

# Mild spatial smoothing of the excess before objects are formed. Genuine
# structures are several pixels across; single-pixel spikes are compression
# artefacts and sensor noise. Smoothing first is what keeps a measurement stable
# when the same slide arrives as a JPEG. The radius is expressed against a
# 1024-pixel working edge and rescaled with the image, so the same physical
# amount of blur is applied whatever resolution the slide is analysed at —
# a fixed pixel radius would smooth a downscaled copy twice as hard and shift
# its answer.
#
# 0.80, not 1.0. Eased alongside EXTENT_PEAK_FRAC for the finer overlay: at 1.0
# the pre-object blur fused neighbouring puncta into a single solid patch, so the
# green read as coarse blobs rather than tracing the granular/filamentous stain.
# 0.80 still absorbs single-pixel codec/sensor spikes (structures are several
# pixels across) while letting closely spaced puncta separate into their own
# objects — which is what keeps recall roughly flat despite the tighter isophote.
# 0.80 rather than 0.70: the recovered-puncta count plateaus by 0.80 (5479 px of
# positive on OK2828 vs 5524 at 0.70 — within 1%), so the extra blur costs no
# real recall while keeping more of the slide's own texture out of the object
# population, which is what protects the tonal-gradient / faint-tissue precision.
SMOOTH_SIGMA     = 0.80
SMOOTH_REF_EDGE  = 1024.0
# The COLOUR of the excess is smoothed harder than its magnitude. A chromogen's
# hue is a property of the structure and varies slowly across it, while its
# density varies pixel to pixel — and every image codec in existence exploits
# exactly that by storing chroma at reduced resolution. Reading colour at the
# scale it actually lives at costs nothing and stops a re-encoded copy of the
# same slide from losing structures to chroma subsampling.
CHROMA_SMOOTH    = 2.2

# Candidate region. The floor is the weakest excess absorbance that can belong
# to an object at all. `sigma` here is not sensor noise — it is how much this
# slide's own unstained tissue varies along the chromogen axis, i.e. its
# texture. Scaling the floor by it is what makes a coarse, mottled section and a
# smooth one behave the same.
NOISE_MULT       = 1.6
ABS_MIN_EXCESS   = 0.025

# Object detection bar. Objects are split from background by clustering the
# population of OBJECT PEAKS (Otsu in log space) — a decision about structures,
# not about pixels, and the reason the operating point transfers across slides
# with wildly different staining density. The clamps stop a slide with no
# staining at all (a unimodal noise population, where the split is arbitrary)
# from inventing a bar low enough to "detect" its own texture.
# Excess OD below which a blob is not visibly stained at all. Measured
# trade-off: lowering this from 0.09 to 0.06 more than doubles recall on
# very faint staining (0.12 OD applied) while leaving blank tissue at 0.000%,
# tone-only slides at 0.000%, and every one of the 46 real sections unchanged to
# three decimal places — their bars are set by their own object populations,
# far above this floor. Below 0.06 nothing further is gained.
PEAK_BAR_ABS_MIN = 0.06
PEAK_BAR_SIG_MIN = 2.6    # ... and never below this many texture sigmas
PEAK_BAR_MAX     = 1.20
PEAK_BIMODAL_MIN = 0.22   # separability below this ⇒ no stain population at all
# A split is only meaningful if there are two groups to split. Otsu's
# separability is a ratio of variances, so it scores high even on a single cloud
# of near-identical peaks — a section stained to an even density — where the
# split then lands *inside* the cloud and most of the stain is discarded over a
# difference in the third decimal place.
#
# What actually distinguishes the two situations is how WIDE the population is.
# Measured: across 46 real sections the object peaks span p90/p10 = 2.6 to 10.2
# (median 6.0); a synthetic field of identically stained structures spans 1.1 to
# 1.4. The bar below sits in that gap with ~30% margin on both sides.
#
# Two other candidates were measured and rejected, which is worth recording so
# they are not tried again: the class-separation ratio (mu_hi − mu_lo)/(s_hi +
# s_lo) runs 1.4-3.3 on real slides and 2.2-3.9 on the single-population case —
# overlapping, and *backwards*; and the histogram valley depth at the split is
# higher for the single population than for most real slides, because
# area-weighting makes a comb of spikes with deep gaps between them.
#
# The value matters more than it looks, and it is measured, not chosen. Across
# random realisations of ONE nominally identical scene — same object count, same
# stain density, differing only in where the structures landed — the spread ran
# 1.11 to 2.07. At the old bar of 1.80 that straddled the decision, so the same
# nominal specimen read anywhere between 13% and 98% recall (0.058% to 0.436%
# positive area) depending on nothing a user could see or control. A measurement
# instrument may not do that.
#
# Across the 46 real sections, where the population genuinely is background
# bumps plus stained structures, the spread runs 2.78 to 9.61. The two
# distributions do not overlap — 1.11-2.07 against 2.78-9.61 — and the bar sits
# in the gap between them, 11% clear of the single-population maximum and 21%
# clear of the real-slide minimum. No slide in the validation set is near it, so
# raising it from 1.80 changes none of their measurements; it only stops the
# uniform case from landing on the wrong side of a coin flip.
#
# Two alternatives were measured and rejected, recorded so they are not retried:
#
#   * Histogram valley depth at the split. Real slides 0.014-1.085, single
#     populations 0.000-0.338 — overlapping, and BACKWARDS, because area
#     weighting turns the population into a comb of spikes with deep gaps
#     between them that a split can legitimately land in.
#
#   * The share of stained AREA the split discards. Real slides 0.11-0.88,
#     single populations 0.20-0.98 — overlapping just as badly. Capping it was
#     tried at 0.58 and is actively harmful: on a pale section with sparse
#     ductular staining it drops the bar to the noise floor and paints the
#     parenchyma's own texture green, which is the exact false-positive failure
#     the object split exists to prevent. The split is load-bearing; only the
#     decision of WHETHER to trust it needed fixing.
PEAK_MIN_SPREAD  = 2.30
# How much to trust the split. When the object peaks really are two clusters,
# the split is a strong, meaningful statement and is used as-is. When they are a
# continuum, it is not: the position that best separates the population is then
# barely better than its neighbours, and the same slide re-encoded or resized
# can land the split at either end — one reading 0.2% and the other 6%. The
# distribution's own quantiles, by contrast, are almost unchanged between those
# two copies. So the bar is blended between the two by exactly how bimodal the
# population is, and no slide sits on a cliff edge.
HIST_SMOOTH_BINS = 3.0    # blur (in histogram bins) applied before the split
# Below this separability the peaks are one continuous cloud, not two groups.
# A separation criterion applied to a population that does not separate has no
# preferred answer: two nearly-equal splits sit at opposite ends of the
# distribution and the same slide, resized or re-encoded, lands on either — one
# copy reading 0.3% and the other 6%. Where that is the case the split is
# dropped in favour of a fixed quantile of the population, which barely moves
# between those copies, and the user is told the boundary is a judgement.
#
# The handover is gradual rather than a switch at one separability value: a hard
# switch is itself a cliff, and a slide sitting on it flips between the two rules
# for no better reason than being resized.
PEAK_DIFFUSE_LO  = 0.50   # at or below: the split is ignored entirely
PEAK_DIFFUSE_HI  = 0.58   # at or above: the split is used as-is
BAR_DIFFUSE_PCT  = 78.0
# Only well-resolved objects get a VOTE on where the bar goes. A three-pixel
# blob's peak is mostly a statement about the sampling grid, and there are
# thousands of them; letting them decide makes the operating point jump when the
# same slide arrives as a JPEG or at another resolution. They are still detected
# once the bar is set — they just do not choose it.
BAR_MIN_AREA_FRAC = 3.5e-5
# Some slides carry three populations, not two — dense puncta, a moderate zone,
# and background — and a two-way split then has two nearly equally good answers
# it can flip between. Averaging the whole near-optimal band of splits lands
# between them and moves smoothly instead of jumping.
OTSU_PLATEAU     = 0.90

# Object extent. The AREA of an accepted structure is the isophote at half its
# OWN peak absorbance — the classic full-width-half-maximum edge — floored so it
# can never run out into the noise.
#
# Measuring each object against itself rather than against a shared cut is what
# keeps *area* from being contaminated by *intensity*: two structures of the
# same physical size measure the same area whether one is twice as dark as the
# other. It also decouples area from the detection bar, so moving the
# sensitivity changes WHICH structures are counted without silently resizing
# every structure already counted — which is what made the measurement lurch
# when the same slide arrived compressed or at another resolution.
# 0.22, not 0.34. Measuring each object at a fraction of its OWN peak is right
# for all the reasons above, but the fraction was set too high, and on a dense
# structure that is a systematic under-measurement: at 0.34 a duct whose peak is
# 1.2 OD is only counted down to 0.41, so its genuinely stained periphery falls
# outside its own footprint. Attributed exactly across the corpus, this single
# constant was the largest remaining source of missed chromogen — 14.7% of it on
# OK3450, 11.6% on OK3445, 10.4% on OK3487.
#
# Swept with everything else held fixed, 0.34 -> 0.22 cut the median miss from
# 2.91% to 0.64% and took the slides missing more than 5% from 17 to 3, while the
# non-chromogen share of positives fell slightly (0.72% -> 0.56%). Going further
# to 0.15 buys almost nothing more (median 0.33%, same worst case, same count) and
# costs a great deal of edge.
#
# 0.26 rather than 0.22 is where it settled, decided on the parametric suite
# rather than the corpus: the two score the same on real slides (one section over
# 5% missed either way, median 0.22% against 0.06%), but 0.22 measures each
# structure far enough past its own edge to fail the suite's precision bar
# against a hard-edged synthetic disc, and 0.26 does not. 0.26 also cuts the
# share of positive area that is only marginally darker than its surroundings
# from 24% to 19%.
#
# The suite's smallest case (a 2 px radius disc, ~12 px of truth) still fails on
# precision at 61%, and did at 71% before any of this — an isophote cannot
# measure an object at the sampling scale without overshooting it. That is a
# limit of the method on objects a few pixels across, not something to tune the
# constant to at the cost of real recall.
# The fraction is SIZE-AWARE, because the two failures it has to avoid live at
# opposite ends of the size axis and pull the constant in opposite directions:
#
#   * On a SMALL structure the point-spread halo is a large share of its
#     footprint, so a low fraction paints the green out past the visibly stained
#     core into clean tissue and fuses neighbouring puncta into one blob. The
#     overlay IS the measurement here (screen == CSV), so that halo is also
#     counted. Small structures want a TIGHT (high) fraction.
#   * On a LARGE, dense structure the fraction is a straight recall knob: its
#     interior excess is depressed by its own local background (a plaque wider
#     than the background window partly subtracts itself), so a high fraction's
#     isophote sits inside the true edge and sheds the outer annulus — and area
#     goes as r^2, so shedding 15% of the radius loses ~30% of the area. Large
#     structures want a LOOSE (low) fraction.
#
# A single constant cannot serve both: at 0.50 the fine overlay is right but a
# 60 px synthetic plaque loses half its area (recall 52%, suite min 70%); at 0.26
# the plaque is measured whole but every punctum blooms. So the fraction ramps
# from FINE on small objects to the recall-preserving bulk value on large ones,
# by object area. Small/medium discs (r<=18) already pass the suite at the FINE
# fraction; only the largest need the loose one, so the ramp sits above them.
#
# 0.26 (bulk) is the long-validated value — see the sweep notes above; it is what
# keeps real ducts and plaques measured to their edge. 0.50 (fine) is where a
# punctum stops blooming without dropping medium puncta.
EXTENT_PEAK_FRAC      = 0.26   # bulk / large structures — recall-preserving
EXTENT_PEAK_FRAC_FINE = 0.34   # small structures — tighter than bulk so the
                               #   overlay traces fine stain instead of blooming
                               #   into its halo, but NOT tighter than the
                               #   real-slide MISS gate allows.
                               #
                               #   Capped by backtest.py, not by the synthetic
                               #   suite. On real sections the MISS check (dense,
                               #   unmistakable chromogen that was not called
                               #   positive; MISS_MAX 5%) rises steeply as this is
                               #   tightened, because a high isophote sheds the
                               #   brown PERIPHERY of dense structures — measured
                               #   on OK2828/OK2998: miss 2.9/2.4% at 0.34, 4.9/
                               #   4.6% at 0.38, 8.7/7.8% at 0.42, 13.4/11.7% at
                               #   0.46. 0.34 keeps a safe margin under 5% across
                               #   the unseen corpus while still de-blooming the
                               #   fine overlay; most of the visual gain is from
                               #   the SMOOTH_SIGMA drop, which costs no MISS.
# Object-area band (as a fraction of the frame) over which the fraction ramps
# FINE -> bulk. Below _LO an object is "small" (full FINE fraction); above _HI it
# is "large" (full bulk fraction); linear in between. _LO ~ r=18 disc, _HI ~ r=34,
# expressed against the frame so it is resolution-independent like MIN_AREA_FRAC.
EXTENT_FINE_AREA_FRAC = 1.3e-3
EXTENT_BULK_AREA_FRAC = 4.6e-3
EXTENT_FLOOR_MULT = 1.0   # ... never below this × the candidate floor

# Object gates (level-independent).
MIN_AREA_FRAC    = 6e-6   # ≈5 px at 1024×768; below this it is sensor noise

# Sensitivity ladder — pure relative scaling of the detection bar around the
# automatic operating point, so the slider means the same thing on every slide.
#
# 201 steps, not 25. At 25 steps each notch multiplied the detection bar by 1.12,
# which on a slide with a few hundred structures turns a large group of them on
# at once — the slider behaved as a series of jumps rather than a dial, and there
# was no position between "too little" and "too much". At 201 steps dragging
# eases structures in and out continuously.
#
# The ceiling is the level map itself: it is 8-bit with 255 reserved for "never
# positive", so up to 255 levels can be carried. Nothing else in the engine or
# the browser needs to change — both apply the map by comparison, and the ladder
# is generated, not enumerated.
N_LEVELS         = 201
AUTO_LEVEL       = 100    # centre of the ladder = the automatic bar

# Two sentinel values above the ladder, carried in the same uint8 level map.
#
# 255 means "no candidate object here at all" — the detector found nothing that
# could be chromogen, at any sensitivity, and no control may turn it positive.
#
# 254 means "this IS a candidate object, and the detector refused it" — refused
# on colour, size or seed count rather than on the sensitivity bar. Automatic
# detection treats it exactly like 255 (every comparison it makes is against a
# level in 0..200, so 254 can never satisfy one), but it is reachable by the
# manual **Include** tool. That is the difference between a hand tool that can
# only turn the sensitivity up locally and one that can actually recover a
# structure the engine judged wrongly — which is what "Include" has to be able
# to do to be worth having.
LEVEL_CANDIDATE  = 254
LEVEL_NEVER      = 255
# Fallback span, used only when the slide has no object population to measure
# against (see `_ladder`): 4x stricter at level 0, 4x more permissive at level 200.
LADDER_STRICT    = 4.0
LADDER_LOOSE     = 0.25
# How far past the slide's own extreme object peaks the two ends of the ladder
# sit. Small, but strictly greater than 1, which is what makes level 0 detect
# NOTHING and the last level detect EVERY object the slide has — see `_ladder`.
LADDER_END_MARGIN = 1.02


@dataclass
class Detection:
    """Everything the caller needs, at every sensitivity, in one object."""
    positive: np.ndarray                 # bool mask at the selected level
    level_map: np.ndarray                # uint8 — first level at which a px is positive
    levels: list                         # detection bar (excess OD) per level
    level: int                           # selected level index
    auto_level: int                      # automatic operating point (ladder centre)
    areas: list                          # positive px per level
    evidence: np.ndarray                 # float32 chromogen excess in noise sigmas
    excess: np.ndarray                   # float32 chromogen excess (OD units)
    brownness: np.ndarray                # float32 colour of that excess
    background_od: np.ndarray            # float32 HxWx3 fitted background OD
    sigma: float                         # robust noise of the excess (OD units)
    bar: float = 0.0                     # automatic detection bar (excess OD)
    # The two ends of the sensitivity ladder, as multiples of `bar`. Reported so
    # the slider's label and the CSV can name the multiplier a level applies
    # WITHOUT re-deriving it from fixed constants — the span is per-slide now
    # (see `_ladder`), so a constant would name the wrong number.
    ladder_hi: float = LADDER_STRICT     # multiplier at level 0 (strictest)
    ladder_lo: float = LADDER_LOOSE      # multiplier at the last level (loosest)
    floor: float = 0.0                   # weakest excess that can belong to an object
    separability: float = 0.0            # how cleanly the object peaks split in two
    bar_discard: float = 0.0             # share of the object area the split would drop
    single_population: bool = False      # the peaks were one cloud, so no split was used
    chromogen_share: float = 1.0         # share of the strong excess that is THIS chromogen
    objects: int = 0                     # accepted objects at the selected level
    object_areas: list = field(default_factory=list)
    notes: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Background field
# --------------------------------------------------------------------------- #

def _masked_mean(x: np.ndarray, m: np.ndarray, win: int) -> tuple[np.ndarray, np.ndarray]:
    """Normalised box mean of `x` over the pixels in `m` (0 elsewhere)."""
    mf = m.astype(np.float32)
    num = _uniform_filter(np.where(m, x, 0.0).astype(np.float32), win)
    den = _uniform_filter(mf, win)
    return num, den


def _mask_weights(keep: np.ndarray, win: int) -> tuple[np.ndarray, np.ndarray]:
    """The two DENOMINATORS a `_field` fit needs — how much of each pixel's fine
    and coarse neighbourhood is usable background.

    Hoisted out of `_field` because they depend only on the mask, not on the
    channel being fitted, and the fit runs once per colour channel. Computing
    them inside meant three identical pairs of box filters per pass over a
    megapixel image — half of all the filtering the background fit did, for
    nothing. Same arrays, same result, half the work."""
    k = keep.astype(np.float32)          # cast once; both scales read the same array
    return (_uniform_filter(k, win),
            _uniform_filter(k, win * BG_COARSE_MULT))


def _field(x: np.ndarray, keep: np.ndarray, win: int, fallback: float,
           den: Optional[np.ndarray] = None,
           cden: Optional[np.ndarray] = None) -> np.ndarray:
    """Smooth `x` over `keep` only, at two scales, so the estimate survives even
    where a whole neighbourhood is foreground.

    `den` / `cden` are the mask denominators from `_mask_weights`; they are
    passed in so the caller can share them across channels."""
    if den is None or cden is None:
        den, cden = _mask_weights(keep, win)
    # Build the masked channel ONCE — the two scales filter the same array, and
    # this ran twice per call, three channels deep, on every refinement pass.
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
    """Fit the slide's own *local* background absorbance, excluding stain.

    Returns (background OD field HxWx3, background pixel mask).

    Why a field and not a number: real slides carry a diffuse chromogen wash, a
    counterstain gradient, uneven illumination and tissue that is genuinely
    denser in places. A single global background makes every one of those look
    like signal in one region and hides signal in another. Fitting the
    background *where there is no stain* and interpolating across the stain is
    what makes the excess comparable everywhere on the slide.
    """
    h, w = od.shape[:2]
    dab_dir = DAB_OD / np.linalg.norm(DAB_OD)
    proj = (od @ dab_dir).astype(np.float32)          # absorbance along the DAB axis

    keep = tissue.copy()
    if not keep.any():
        keep = np.ones((h, w), dtype=bool)
    bg_od = np.empty_like(od, dtype=np.float32)

    for it in range(BG_ITERS + 1):
        # One boolean extraction for all three channels; `od[..., c][keep]` built
        # a fresh megapixel copy per channel, three times per refinement pass.
        if keep.any():
            _kept = od[keep]
            fallbacks = [float(np.median(_kept[:, c])) for c in range(3)]
        else:
            fallbacks = [0.0, 0.0, 0.0]
        den, cden = _mask_weights(keep, win)      # same for all three channels
        for c in range(3):
            bg_od[..., c] = _field(od[..., c], keep, win, fallbacks[c], den, cden)
        if it == BG_ITERS:
            break
        exc = proj - (bg_od @ dab_dir)
        base = exc[tissue] if tissue.any() else exc.ravel()
        sig = 1.4826 * float(np.median(np.abs(base - np.median(base)))) + 1e-6

        # Foreground by LOCAL contrast — plus an absolute test, which is what
        # rescues a stained region larger than the window from erasing itself.
        # Local contrast alone is self-consistent in the worst way there: the
        # first fit, made over everything, already reads high inside such a
        # region, so its interior never looks like an excess, is never excluded,
        # and stays baked into its own background for every later pass. A
        # 200-pixel plaque then measures as background with a thin positive rim.
        # Material far darker than the slide's own tissue as a whole is
        # foreground wherever it sits, however wide it is.
        t_proj = proj[tissue] if tissue.any() else proj.ravel()
        if t_proj.size > 200_000:
            t_proj = t_proj[:: t_proj.size // 200_000 + 1]
        med_g = float(np.median(t_proj))
        sig_g = 1.4826 * float(np.median(np.abs(t_proj - med_g))) + 1e-6
        fg = (exc > 3.0 * sig) | (proj > med_g + BG_GLOBAL_FG_K * sig_g)
        # never let foreground exclusion run away on a densely stained slide
        if float(fg.mean()) > BG_FG_MAX_FRAC:
            cut = float(np.percentile(exc, 100.0 * (1.0 - BG_FG_MAX_FRAC)))
            fg = exc > cut
        keep = tissue & ~fg
        if float(keep.mean()) < 0.05:      # pathological: keep the whole tissue
            keep = tissue.copy()
    return bg_od, keep


# --------------------------------------------------------------------------- #
# Colour specificity of the excess
# --------------------------------------------------------------------------- #

def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) or 1.0)


def chromogen_fraction(od: np.ndarray,
                       target_od: Optional[np.ndarray] = None,
                       counter_od: Optional[np.ndarray] = None) -> np.ndarray:
    """What share of this pixel's absorbance is the chromogen rather than neutral
    material — see CHROMO_FRAC_MIN.

    The absorbance is decomposed onto three directions: the counterstain, the
    chromogen, and neutral grey. Returning the chromogen's share of
    (chromogen + neutral) makes the answer independent of how heavily the section
    is counterstained, which is the whole reason for doing it this way, while
    still separating chromogen from pigment, ink and folds.

    Linear in optical density, so it does not weaken on dense material the way
    any ratio of transmitted intensities does.
    """
    tgt = _unit(np.asarray(target_od, dtype=np.float64) if target_od is not None else DAB_OD)
    ctr = _unit(np.asarray(counter_od, dtype=np.float64) if counter_od is not None else HEMA_OD)
    neu = _unit(NEUTRAL_OD)
    basis = np.stack([ctr, tgt, neu])
    try:
        inv = np.linalg.pinv(basis).astype(np.float32)
    except np.linalg.LinAlgError:            # degenerate basis: no verdict
        return np.ones(od.shape[:2], dtype=np.float32)
    a = np.clip(np.clip(od, 0.0, None) @ inv, 0.0, None)
    return (a[..., 1] / (a[..., 1] + a[..., 2] + 1e-6)).astype(np.float32)


def chroma_readings(od: np.ndarray, rel_scale: float = 1.0
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Saturation and hue of the MATERIAL: white-point-normalised, chroma-smoothed.

    The single definition used both by the detector and by the regression suite,
    so the two cannot end up asking different questions of the same pixels. See
    DEBRIS_SAT_MAX for why neither the normalisation nor the smoothing is
    optional.

    `od` is optical density against the slide's own white point; `rel_scale` is
    the working resolution relative to SMOOTH_REF_EDGE.
    """
    t = np.power(10.0, -np.clip(od, 0.0, None))
    sigma = CHROMA_SMOOTH * rel_scale
    if sigma > 0.15 and _gaussian is not None:
        t = _gaussian(t, (sigma, sigma, 0), mode="nearest")
    mx = t.max(axis=2)
    mn = t.min(axis=2)
    sat = ((mx - mn) / np.maximum(mx, 1e-6)).astype(np.float32)
    r, g, b = t[..., 0], t[..., 1], t[..., 2]
    d = np.maximum(mx - mn, 1e-6)
    h = np.where(mx == r, ((g - b) / d) % 6.0,
                 np.where(mx == g, (b - r) / d + 2.0, (r - g) / d + 4.0))
    return sat, ((h * 60.0) % 360.0).astype(np.float32)


def looks_like_debris(sat, hue):
    """Washed-out AND olive at once — intraluminal granular casts, not chromogen.

    Takes either arrays (pixelwise) or per-object means. Hue is compared as a
    signed circular distance so a chromogen just below 0 deg is not read as
    350 deg away."""
    dhue = (np.asarray(hue) - DEBRIS_HUE_MIN + 180.0) % 360.0 - 180.0
    return (np.asarray(sat) < DEBRIS_SAT_MAX) & (dhue > 0.0)


def excess_colour(od_exc: np.ndarray, target_od: Optional[np.ndarray] = None
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Describe the EXTRA absorbance at each pixel.

    Returns (magnitude, brownness):

      * magnitude — ‖excess OD‖, how much extra light this pixel absorbs.
      * brownness — the chromogen's own absorbance signature, projected out of
                    the excess and normalised: for DAB the blue-over-red split
                    (B − R)/‖e‖, which is +0.51 for pure DAB, −0.36 for
                    haematoxylin, +0.03 for eosin / red blood cells and 0.00 for
                    neutral debris (ink, dust, folds, shadow).

    Why not hue: HSV hue is computed from ratios that collapse as a pixel
    approaches black, so the densest — least ambiguous — chromogen reads as
    almost any colour, and a hue window then drops precisely the pixels a human
    would call obviously positive. A channel-ORDER contrast survives that
    crushing; it merely weakens toward 0, which the object logic handles by
    connectivity rather than by pretending the colour is still measurable.
    """
    mag = np.linalg.norm(od_exc, axis=2).astype(np.float32)
    safe = np.maximum(mag, 1e-6)
    if target_od is None:
        contrast = od_exc[..., 2] - od_exc[..., 0]
    else:
        # Generic chromogen: contrast along the direction that separates this
        # chromogen from neutral absorbance.
        d = _unit(np.asarray(target_od, dtype=np.float64))
        axis = d - _unit(NEUTRAL_OD) * float(d @ _unit(NEUTRAL_OD))
        axis = _unit(axis) * np.sqrt(2.0)     # scale to match the DAB B−R form
        contrast = od_exc @ axis.astype(np.float32)
    return mag, (contrast / safe).astype(np.float32)


# --------------------------------------------------------------------------- #
# The detector
# --------------------------------------------------------------------------- #

def _otsu_log(values: np.ndarray) -> tuple[float, float]:
    """Otsu split of a positive population in log space.

    Returns (threshold, separability). Separability is the between-class share
    of the total variance: high when the population really is two populations
    (background bumps vs stained structures), low when it is one smooth cloud —
    which is how a slide with no staining announces itself.
    """
    v = np.log10(np.maximum(values, 1e-4))
    if v.size < 12:
        return float("nan"), 0.0, 0.0
    hist, edges = np.histogram(v, bins=96)
    centres = 0.5 * (edges[:-1] + edges[1:])
    w = hist.astype(np.float64)
    # Smooth the population before splitting it. Sampling noise puts small extra
    # peaks in the histogram, and each one is a candidate split that a two-class
    # criterion can jump to; the same slide re-encoded or resized then lands on a
    # different one. Blurring at the scale of that noise leaves the real modes
    # exactly where they are and takes the spurious ones away.
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
    # The between-class variance is usually flat near its maximum, so taking the
    # single best bin lets the split hop several bins for a change as small as a
    # JPEG re-encode. Averaging over the near-optimal plateau gives the same
    # answer with none of that jitter. (A soft two-component fit was tried here
    # and is worse: on a population that is a continuum rather than two clean
    # clusters, the fitted boundary wanders far outside the plateau.)
    best = float(sigma_b.max())
    plateau = sigma_b >= best * OTSU_PLATEAU
    k = int(np.argmax(sigma_b))
    centre = float((centres[plateau] * sigma_b[plateau]).sum() / max(sigma_b[plateau].sum(), 1e-12))
    var_total = float(((centres - mu_t) ** 2 * p).sum())
    sep = float(sigma_b[k] / var_total) if var_total > 1e-12 else 0.0

    # Are the two sides actually two groups, or one cloud cut in half?
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


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """Median of `values` weighted by `weights` (here: by the area each object
    contributes, so the answer describes the stained area rather than the count
    of blobs, which is dominated by specks)."""
    if values.size == 0 or weights.sum() <= 0:
        return float("nan")
    order = np.argsort(values)
    w = weights[order].astype(np.float64)
    c = np.cumsum(w) / w.sum()
    return float(values[order][int(np.searchsorted(c, 0.5))])


def _area_weighted(peaks: np.ndarray, areas: np.ndarray, cap: int = 400) -> np.ndarray:
    """Repeat each object's peak in proportion to the area it contributes, so the
    split is decided by where the stained AREA is, not by how many specks exist."""
    if peaks.size == 0:
        return peaks
    reps = np.clip((areas // 2).astype(np.int64), 1, cap)
    return np.repeat(peaks, reps)


def _ladder(bar: float, peak_hi: float = 0.0, peak_lo: float = 0.0) -> np.ndarray:
    """Sensitivity ladder: the detection bar scaled around the automatic one.

    Level AUTO_LEVEL is always exactly the bar the object population itself
    chose, so the automatic measurement is untouched by anything here. Lower
    indices are stricter, higher ones more permissive, and the scaling stays
    geometric — one notch is a fixed *ratio*, so "×0.72" means the same degree
    of conservatism on a faint slide and a dense one alike.

    What the two ENDS are anchored to is the slide's own object population,
    not a fixed multiple of the bar, and that is the whole point of this
    function. A fixed ±4x window is a statement about the bar; whether the
    slider does anything is a statement about where the object PEAKS lie, and
    the two need not overlap at all:

      * If every peak sits above 4x the bar, every object is already on at
        level 0 and stays on to level 200 — the control is inert over its whole
        travel. Measured on the 46-slide validation set, one section (OK3306)
        moved from 3.92% at level 0 to 4.98% at level 200, with 69 of the top
        levels changing literally nothing; four sections detected staining at
        level 0, where the honest answer is an empty overlay.
      * If every peak sits below the bar's 4x reach in the other direction, the
        top of the slider is dead in the same way. Across the 46 sections a
        median of 45 of the 200 steps changed nothing, and up to 137 did not.

    Anchoring instead to `peak_hi` (the strongest object on the slide) and
    `peak_lo` (the weakest) makes the two ends mean what a user reads them as:

        level 0        →  bar above every peak      →  nothing is detected
        level N-1      →  bar below every peak      →  every object is detected

    and spends all 201 steps inside the range where moving the control actually
    changes the answer. `LADDER_END_MARGIN` keeps each end strictly outside the
    population so those two statements hold exactly rather than by a rounding.

    With no object population to measure (a blank or unstained slide) there is
    nothing to anchor to and the fixed span is used.
    """
    bar = float(bar)
    hi = (peak_hi * LADDER_END_MARGIN) if peak_hi > 0 else bar * LADDER_STRICT
    lo = (peak_lo / LADDER_END_MARGIN) if peak_lo > 0 else bar * LADDER_LOOSE
    # The ladder must descend THROUGH the bar: level AUTO_LEVEL is the automatic
    # operating point and nothing above may reorder that, however the slide's
    # peaks happen to sit relative to it.
    hi = max(hi, bar * 1.02)
    lo = min(lo, bar * 0.98)
    up = np.geomspace(hi, bar, AUTO_LEVEL + 1)
    dn = np.geomspace(bar, lo, N_LEVELS - AUTO_LEVEL)
    return np.concatenate([up[:-1], dn]).astype(np.float32)


def _sharpness(signal: np.ndarray, flat_lbl: np.ndarray, inside: np.ndarray,
               peak: np.ndarray, n: int, rel_scale: float) -> np.ndarray:
    """How steeply each object falls away from its own peak.

    The mean gradient of the excess over the object's measured footprint,
    divided by the object's height. Dividing by the height removes staining
    intensity from the answer — a faint punctum and a dense one of the same
    shape score the same — and rescaling by the working resolution removes image
    size, since the same structure spans fewer pixels in a smaller copy and
    would otherwise look steeper.

    Structures have boundaries and land high. A diffusely stained region has no
    boundary and lands low, however much chromogen it carries.
    """
    if _sobel is None:
        return np.full(n + 1, np.inf)
    grad = np.hypot(_sobel(signal, axis=1), _sobel(signal, axis=0)).ravel() / 4.0
    sel = inside & (flat_lbl > 0)
    lab = flat_lbl[sel]
    cnt = np.bincount(lab, minlength=n + 1).astype(np.float64)
    gsum = np.bincount(lab, weights=grad[sel].astype(np.float64), minlength=n + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        gmean = gsum / np.maximum(cnt, 1.0)
        out = (gmean / np.maximum(peak, 1e-6)) * float(rel_scale)
    out[cnt == 0] = 0.0
    return out


def _object_stats(lbl: np.ndarray, n: int, signal: np.ndarray,
                  brown: np.ndarray, seed: np.ndarray,
                  raw_brown: Optional[np.ndarray] = None,
                  raw_warm: Optional[np.ndarray] = None,
                  raw_sat: Optional[np.ndarray] = None,
                  raw_hue: Optional[np.ndarray] = None,
                  chromo_frac: Optional[np.ndarray] = None) -> dict:
    """Per-object measurements, all from bincounts over the label image."""
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

    # The same colour reading, taken over the object's STRONGEST pixels only —
    # those at or above half its own peak.
    #
    # A mean over the whole footprint is not a property of the material: it is a
    # property of the material AND of how far the footprint was allowed to
    # extend. Loosening EXTENT_PEAK_FRAC to stop under-measuring dense structures
    # grew every object, diluted every whole-object mean with pale periphery, and
    # pushed objects back under the colour gates — trading one false negative for
    # another. Measured after that change, the two mean-based colour tests were
    # the whole of the remaining miss: 9.9% and 4.3% of the obvious chromogen on
    # the failing slides.
    #
    # Read on the core instead, the answer depends only on the material, so the
    # two constants stop fighting each other. Half the peak rather than the very
    # darkest pixels, because an 8-bit pixel that has saturated to black has no
    # reliable direction left either — the useful signal is dense but not clipped.
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

    # Hue as a SATURATION-WEIGHTED circular mean. Weighted, because a washed-out
    # pixel's hue is arbitrary and would otherwise vote as loudly as a coloured
    # one; circular, because hue wraps and an ordinary mean of 359 and 1 is 180.
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
    """Area-based chromogen detection.

    `od`        HxWx3 optical density (already white-point corrected).
    `tissue`    bool mask of real tissue — sets what can be measured at all.
    `level`     sensitivity level index; None → the automatic operating point.
    `target_od` overrides the chromogen direction (stain-selection mode).
    `debug`, if given a dict, is filled with the intermediate masks and per-object
    gate verdicts. It exists so a regression run can attribute a miss to the exact
    gate that caused it instead of re-deriving this function by hand — a
    re-derivation drifts from the real code and then explains the wrong thing.

    The decision has three separable parts, and none of them is a global
    brightness cut:

      1. *Where is the background?*  A foreground-excluded OD field, fitted per
         slide and per neighbourhood.
      2. *Which blobs are structures?*  Connected areas of excess absorbance,
         clustered by their peak into "background bump" and "stained object".
      3. *How big is each structure?*  Its own isophote, at a fixed fraction of
         the detection bar, inside its own footprint.
    """
    h, w = od.shape[:2]
    notes: list[str] = []
    frame = h * w
    if min_area_px is None:
        min_area_px = int(max(4, round(frame * MIN_AREA_FRAC)))
    dab_vec = np.asarray(target_od, dtype=np.float64) if target_od is not None else DAB_OD

    # ---- 1. local background and the excess absorbance it leaves ---------- #
    # Only MATERIAL informs the background. Lumina and holes belong to the
    # section but transmit nearly all the light; averaging them into a
    # neighbourhood drags its background down and rings every hole with false
    # positives. They are interpolated across instead.
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
    # NOT clipped per channel: clipping negatives first would turn a pixel that
    # is merely a shade bluer than its background into a "pure brown" excess, and
    # the whole slide's texture noise then reads as maximally chromogen-coloured.
    od_exc = (od - bg_od).astype(np.float32)
    rel_scale = max(h, w) / SMOOTH_REF_EDGE
    smooth = SMOOTH_SIGMA * rel_scale
    if smooth > 0.15 and _gaussian is not None:
        od_exc = _gaussian(od_exc, (smooth, smooth, 0), mode="nearest")
    # Colour is read at TWO scales and the stronger reading wins. The coarse one
    # is what survives chroma subsampling on broad structures; but a structure
    # only two or three pixels across — and much of real staining is — has its
    # colour diluted by the unstained tissue either side of it at that scale,
    # which was dropping thin stained streaks entirely. Reading fine as well and
    # taking the better evidence keeps both.
    _, brown_fine = excess_colour(od_exc, None if target_od is None else dab_vec)
    brown = brown_fine

    # ---- the material's OWN colour, which does not degrade with density ---- #
    # Read on the raw absorbance rather than on the excess. See OBJ_RAW_BROWN:
    # under a dense structure the local background carries the same chromogen, so
    # subtracting it cancels the very signature the excess reading is looking
    # for, and the darkest staining on the slide reads as neutral debris.
    od_pos = np.clip(od, 0.0, None)
    # The same colour question `excess_colour` asks, put to the raw absorbance —
    # so it is rotated onto the chosen chromogen in selection mode exactly as the
    # excess reading is.
    _, raw_brown = excess_colour(od_pos, None if target_od is None else dab_vec)
    raw_mag = np.linalg.norm(od_pos, axis=2) + 1e-6
    raw_bog = ((od_pos[..., 2] - od_pos[..., 1]) / raw_mag).astype(np.float32)
    # Warmth of the raw pixel, (R−B)/(R+B). Neutral material sits at 0.00 however
    # dark it is; dense chromogen stays clearly positive.
    _tr = np.power(10.0, -od_pos)
    raw_warm = ((_tr[..., 0] - _tr[..., 2]) / (_tr[..., 0] + _tr[..., 2] + 1e-6)).astype(np.float32)

    # `chroma_readings` is NOT called here any more. Nothing is gated on
    # saturation or hue since CHROMO_FRAC_MIN replaced the debris test, so the
    # only thing it fed were two per-object numbers no decision reads — at the
    # cost of a chroma-smoothing Gaussian over the whole image on every analysis
    # (0.13 s a slide, ~9% of the measurement). The function stays for the
    # regression suite and for future analysis; it is simply off the hot path.
    norm_sat = norm_hue = None

    # How much of the absorbance here is this chromogen rather than neutral
    # material — the one colour reading the object gate uses. See
    # CHROMO_FRAC_MIN for why it is a decomposition and not a direction.
    chromo_frac = chromogen_fraction(od_pos, target_od, counter_od)

    # Which chromogen this excess belongs to — see BLUE_OVER_GREEN_MIN.
    _mag = np.linalg.norm(od_exc, axis=2) + 1e-6
    bog = ((od_exc[..., 2] - od_exc[..., 1]) / _mag).astype(np.float32)
    bog_min = BLUE_OVER_GREEN_MIN
    if target_od is not None:
        u = _unit(np.asarray(target_od, dtype=np.float64))
        bog_min = float(u[2] - u[1]) - 0.25    # the same margin, around this chromogen
    csmooth = CHROMA_SMOOTH * rel_scale
    if csmooth > smooth and _gaussian is not None:
        extra = float(np.sqrt(max(csmooth ** 2 - smooth ** 2, 0.0)))
        _, brown_coarse = excess_colour(_gaussian(od_exc, (extra, extra, 0), mode="nearest"),
                                        None if target_od is None else dab_vec)
        brown = np.maximum(brown_fine, brown_coarse)

    dab_dir = _unit(dab_vec).astype(np.float32)
    signal = (od_exc @ dab_dir).astype(np.float32)      # excess along the chromogen axis

    base = signal[bg_mask] if bg_mask.any() else signal.ravel()
    if base.size > 200_000:
        base = base[:: base.size // 200_000 + 1]
    med = float(np.median(base))
    sigma = max(1.4826 * float(np.median(np.abs(base - med))) + 1e-6, 1e-4)

    signal = np.clip(signal - med, 0.0, None).astype(np.float32)
    # A pixel that transmits essentially all the light cannot be stained,
    # whichever side of the tissue mask it falls on.
    signal[~(tissue & material)] = 0.0
    evidence = (signal / sigma).astype(np.float32)

    # ---- 2. candidate objects -------------------------------------------- #
    floor = float(max(NOISE_MULT * sigma, ABS_MIN_EXCESS))
    solid = tissue & material

    # Material far darker than this slide's own tissue, measured along the
    # chromogen axis in the slide's own robust units. This is where the
    # excess-colour reading stops being reliable (see OBJ_RAW_BROWN), and the
    # only place the raw reading is allowed to speak for it.
    raw_proj = (od_pos @ dab_dir).astype(np.float32)
    _rp = raw_proj[solid] if solid.any() else raw_proj.ravel()
    if _rp.size > 200_000:
        _rp = _rp[:: _rp.size // 200_000 + 1]
    _rp_med = float(np.median(_rp)) if _rp.size else 0.0
    _rp_sig = 1.4826 * float(np.median(np.abs(_rp - _rp_med))) + 1e-6 if _rp.size else 1.0
    dense_px = raw_proj >= _rp_med + DENSE_SIG_K * _rp_sig

    seed_colour = (brown >= BROWN_SEED) & (bog >= bog_min)
    # A dense pixel may seed on its own absorbance direction instead. Without
    # this a thin, uniformly near-black duct has no seed anywhere — its rim,
    # where the excess reading still works, is under a pixel wide — and the whole
    # structure is discarded for want of two seed pixels.
    seed_colour |= dense_px & (raw_brown >= SEED_RAW_BROWN) & (raw_bog >= SEED_RAW_BOG)
    seed_px = solid & seed_colour & (signal >= floor)

    region = solid & (brown >= BROWN_GROW) & (signal >= floor)
    if saturation is not None:
        region &= saturation > NEUTRAL_SAT_MAX      # never grow into achromatic material
    # Intraluminal granular debris is NOT excluded here. It used to be — per
    # pixel, on a transmittance saturation/hue test, before anything was grouped —
    # and that was the detector's largest error in both directions. It is now a
    # single per-object veto on the object's own absorbance direction
    # (OBJ_NEUTRAL_BROWN), which is where the measurements say the two populations
    # actually separate. See the long note beside that constant.
    if hue_band_mask is not None:
        seed_px &= hue_band_mask

    # Is the chromogen on this slide the one we are measuring? Asked of the
    # strongly absorbing material, by direction rather than by hue.
    strong = solid & (signal >= max(floor, float(np.percentile(signal[solid], 99.0))
                                    if solid.any() else floor))
    # Only worth asking when there IS strongly absorbing material to judge. On a
    # slide whose staining is merely faint, the excess direction of near-noise
    # pixels is meaningless, and answering anyway tells the user their chromogen
    # is the wrong colour when in truth it is simply weak.
    #
    # `strong` is by construction the DENSEST material on the slide, which is
    # exactly where the excess reading collapses — so asking it whether those
    # pixels are chromogen-coloured, on the excess alone, answered "no DAB here"
    # for ten of the 46 validation slides, every one of them plainly DAB. The
    # raw-reading seed path above is what makes this verdict trustworthy.
    if strong.any() and float(np.median(signal[strong])) >= 2.0 * floor:
        chromogen_share = float((seed_px & strong).sum()) / max(int(strong.sum()), 1)
    else:
        chromogen_share = 1.0        # no verdict

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
        # ONE colour criterion, not two OR'd together with a veto on top. What
        # share of the object's core absorbance is this chromogen rather than
        # neutral material — counterstain-corrected, density-stable, and measured
        # to refuse 0.6% of real DAB objects against 41% of dense objects
        # carrying no chromogen at all. It subsumes both the excess-colour test
        # and the neutral veto that preceded it, which is why they are gone: an
        # OR of two readings cannot veto anything, and a veto stacked on an OR is
        # just a third rule to keep in step with the other two.
        chromogen_coloured = st["chromo_frac_core"] >= CHROMO_FRAC_MIN
        eligible = ((st["area"] >= min_area_px) & (st["seeds"] >= OBJ_SEED_MIN_PX)
                    & (st["seed_frac"] >= OBJ_SEED_FRAC) & chromogen_coloured)
        eligible[0] = False

        # The detection bar comes from the population of object peaks, not from
        # any pixel statistic: two clusters ⇒ "bumps" and "structures".
        # Weighted by area: a stained slide's AREA lives in a few strong
        # structures, while its noise lives in thousands of tiny blobs that
        # would otherwise dominate an unweighted population and drag the split
        # down into the texture.
        voters = eligible & (st["area"] >= max(min_area_px, int(frame * BAR_MIN_AREA_FRAC)))
        if int(voters.sum()) < 12:
            voters = eligible
        peaks = _area_weighted(st["peak"][voters], st["area"][voters])

        # Objects nobody would call anything but chromogen, judged on their own
        # absorbance alone — no local background, no population statistics. The
        # ceiling they impose on the bar is applied after the split below; see
        # UNMISTAKABLE_BROWN for why the split cannot be trusted to respect them.
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
            # Reported, not enforced: how much of the stained area this split
            # throws away. It is in the export because it tells a reader how
            # consequential the operating point was on their slide — but it is
            # NOT a gate, because it does not separate the two cases (see
            # PEAK_MIN_SPREAD).
            if np.isfinite(t):
                bar_discard = float((peaks < t).mean())
            if one_population:
                t = float("nan")          # one population: there is nothing to split
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

            # Whatever the split proposed, the operating point may not sit above
            # material that is unmistakably chromogen on its own absorbance.
            # Never below `bar_min` either: this is a ceiling on over-strictness,
            # not a licence to detect a slide's texture.
            if bar_ceiling < bar:
                _before = bar
                bar = float(max(bar_ceiling, bar_min))
                # Only say so when the correction was large enough to matter.
                # It engages to some degree on most slides — 89 of 154 in one
                # export — and a note that appears on the majority of a batch is
                # wallpaper rather than information: it trains the reader to skip
                # the notes column, which is where the genuinely unusual slides
                # announce themselves. A third off the boundary is the point at
                # which the automatic operating point was meaningfully wrong.
                if bar <= _before * BAR_NOTE_RATIO:
                    notes.append(
                        "Some clearly stained structures fell below the boundary the "
                        "object population implied, so the boundary was lowered to "
                        "include them.")

        # The ends of the sensitivity ladder are the extremes of THIS slide's
        # object population, so the control spans exactly the range over which
        # it changes the answer — see `_ladder`. Taken over the eligible objects
        # only: the rest can never be positive at any level, so including them
        # would stretch the ladder across a range where nothing happens.
        _elig_peaks = st["peak"][eligible]
        if _elig_peaks.size:
            ladder_peak_hi = float(_elig_peaks.max())
            ladder_peak_lo = float(_elig_peaks.min())
        levels = _ladder(bar, ladder_peak_hi, ladder_peak_lo)

        # ---- 3. extent, structure test, and the level map ----------------- #
        # Each object's measured footprint is its own isophote, which does not
        # depend on the sensitivity at all. The structure test is applied on that
        # footprint, because that is where a boundary either exists or does not;
        # at the looser candidate footprint a soft gradient and a sharp punctum
        # are not reliably separable.
        # Size-aware isophote fraction: FINE (tight) on small objects so the green
        # traces the stain without blooming into its halo, ramping to the bulk
        # value on large ones so a dense plaque keeps its outer annulus. See the
        # EXTENT_PEAK_FRAC notes for why the two ends pull the constant apart.
        _img_px = float(h * w)
        _lo = EXTENT_FINE_AREA_FRAC * _img_px
        _hi = max(EXTENT_BULK_AREA_FRAC * _img_px, _lo + 1.0)
        _t = np.clip((st["area"].astype(np.float64) - _lo) / (_hi - _lo), 0.0, 1.0)
        _frac = EXTENT_PEAK_FRAC_FINE + (EXTENT_PEAK_FRAC - EXTENT_PEAK_FRAC_FINE) * _t
        extent = np.maximum(st["peak"] * _frac, floor * EXTENT_FLOOR_MULT)
        flat_lbl = lbl.ravel()
        inside = signal.ravel() >= extent[flat_lbl]

        # The ladder descends, so "this object qualifies" is monotone in the
        # level index: one searchsorted gives the first level it appears at.
        desc = levels.astype(np.float64)
        obj_level = np.full(n + 1, LEVEL_NEVER, dtype=np.int32)
        idx = np.searchsorted(-desc, -st["peak"][eligible], side="left")
        obj_level[np.flatnonzero(eligible)] = np.clip(idx, 0, N_LEVELS - 1)
        # A candidate the gates refused is marked as such rather than erased, so
        # the Include tool can recover it by hand. Automatic detection is
        # unaffected: LEVEL_CANDIDATE is above every ladder index.
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

    # Cumulative histogram rather than one full-image pass per level — the same
    # numbers, but O(frame) instead of O(frame x levels), which is what makes a
    # 201-step ladder cost no more than the 25-step one did.
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
