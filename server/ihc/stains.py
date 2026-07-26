"""
ImageSL — stain / chromogen registry.

**ImageSL currently ships DAB-only** (see `ENABLED_KEYS` below). The rest of the
registry stays defined, sourced and ready — it is switched off, not deleted, so
adding the next stain is a one-line change rather than a rewrite.

The user picks the **stain colour / method** (DAB, AEC, Masson trichrome, …), NOT
the antibody: the antibody (CK19, Ki-67, …) only decides *where* the signal is —
the colour on the slide is set by the chromogen, and that colour is what the pixel
engine actually measures. So each entry carries the two things the color-
deconvolution engine needs: the target stain's optical-density vector and the
counterstain's vector.

Vectors:
  * IHC chromogens and classic special stains use the canonical Ruifrok & Johnston
    color-deconvolution vectors (as encoded in scikit-image / the ImageJ
    "Colour Deconvolution" plugin).  https://qupath.readthedocs.io (Separating
    stains) · G. Landini, Colour Deconvolution 2.
  * A few colour-defined stains (Prussian blue, Congo red, …) derive their OD
    vector from a representative pure-stain colour.
  * "Black" silver/argyrophil stains (GMS, reticulin, Verhoeff) carry no hue, so
    they are flagged `black` and detected by darkness-in-tissue instead of hue.

Sources consulted: Roche/Abcam/AAT Bioquest chromogen guides; Leica "Routine and
Special Staining"; Dako Guide to Special Stains; Ruifrok & Johnston 2001.
"""

from __future__ import annotations
import math
import numpy as np


def _od(rgb) -> list[float]:
    """Representative pure-stain RGB (0..255) → normalised optical-density vector."""
    a = np.asarray(rgb, dtype=np.float64)
    od = -np.log10((a + 1.0) / 256.0)
    n = np.linalg.norm(od) or 1.0
    return (od / n).tolist()


def _disp_hex(od) -> str:
    """Display colour of a stain from its OD vector."""
    v = np.asarray(od, dtype=np.float64)
    v = v / (np.linalg.norm(v) or 1.0)
    rgb = np.clip(255.0 * np.power(10.0, -v), 0, 255).astype(int)
    return "#%02x%02x%02x" % tuple(rgb)


def _hue(hex_or_od) -> float:
    if isinstance(hex_or_od, str):
        r = int(hex_or_od[1:3], 16) / 255; g = int(hex_or_od[3:5], 16) / 255; b = int(hex_or_od[5:7], 16) / 255
    else:
        v = np.asarray(hex_or_od); v = v / (np.linalg.norm(v) or 1.0)
        r, g, b = np.power(10.0, -v)
    mx, mn = max(r, g, b), min(r, g, b); d = mx - mn
    if d < 1e-6: return 0.0
    if mx == r: h = ((g - b) / d) % 6
    elif mx == g: h = (b - r) / d + 2
    else: h = (r - g) / d + 4
    return (h * 60) % 360


# --- canonical Ruifrok & Johnston OD vectors (scikit-image / ImageJ) ---------- #
HEMA        = [0.650, 0.704, 0.286]     # haematoxylin (blue nuclei)
DAB         = [0.268, 0.570, 0.776]     # DAB (brown)
AEC         = [0.2743, 0.6796, 0.6803]  # AEC (red)
EOSIN       = [0.070, 0.990, 0.110]     # eosin (pink)
FAST_RED    = [0.2139, 0.8511, 0.4779]  # Fast Red (AP, red)
FAST_BLUE   = [0.7489, 0.6062, 0.2673]  # Fast Blue / BCIP-NBT (AP, blue-purple)
METHYL_GRN  = [0.980, 0.144, 0.133]     # methyl green counterstain
TRI_COLLAGEN= [0.7995, 0.5914, 0.1053]  # Masson methyl-blue (collagen)
TRI_MUSCLE  = [0.1000, 0.7374, 0.6680]  # Masson ponceau-fuchsin (muscle/cytoplasm)
PAS_MAGENTA = [0.1754, 0.9722, 0.1546]  # PAS (magenta)
ALCIAN      = [0.8746, 0.4577, 0.1583]  # Alcian blue
GIEMSA_BLUE = [0.8348, 0.5136, 0.1963]  # Giemsa methylene blue
GIEMSA_EOS  = [0.0928, 0.9541, 0.2831]  # Giemsa eosin
FEULGEN     = [0.9471, 0.2537, 0.1965]  # Feulgen (magenta)
LIGHT_GREEN = [0.4642, 0.8301, 0.3083]  # light green counter
AZAN_BLUE   = [0.8530, 0.5087, 0.1127]  # AZAN anilin blue (collagen)
AZAN_AZO    = [0.0929, 0.8662, 0.4910]  # AZAN azocarmine (nuclei/muscle)


def _stain(key, name, category, target_od, counter_od, counter_name, desc,
           enzyme="", black=False, min_px=6):
    tod = list(target_od)
    swatch = _disp_hex(tod)
    return {
        "key": key, "name": name, "category": category,
        "target_od": tod, "counter_od": list(counter_od),
        "counter_name": counter_name, "target_hue": round(_hue(tod), 1),
        "swatch": swatch, "description": desc, "enzyme": enzyme,
        "black": black, "min_px": min_px,
    }


IHC = "IHC chromogen"
SPECIAL = "Special / histochemical stain"

STAINS: list[dict] = [
    # ---------------- IHC chromogens ---------------- #
    _stain("dab", "DAB (brown)", IHC, DAB, HEMA, "Haematoxylin",
           "3,3'-diaminobenzidine — HRP. Brown, permanent, alcohol-insoluble; the most common IHC chromogen.", "HRP"),
    _stain("aec", "AEC (red)", IHC, AEC, HEMA, "Haematoxylin",
           "3-amino-9-ethylcarbazole — HRP. Red, water-soluble (aqueous mount); a red alternative to DAB.", "HRP"),
    _stain("fast-red", "Fast Red (red)", IHC, FAST_RED, HEMA, "Haematoxylin",
           "Fast Red / New Fuchsin — alkaline phosphatase. Bright red; common for double-labels or red signal.", "AP"),
    _stain("permanent-red", "Permanent Red (red)", IHC, _od((188, 32, 52)), HEMA, "Haematoxylin",
           "Permanent Red / Warp Red — alkaline phosphatase. Red, permanent and alcohol-resistant.", "AP"),
    _stain("fast-blue", "Fast Blue (blue)", IHC, FAST_BLUE, _od((205, 92, 140)), "Nuclear Fast Red",
           "Fast Blue — alkaline phosphatase. Blue precipitate for multiplex work.", "AP"),
    _stain("bcip-nbt", "BCIP/NBT (blue-purple)", IHC, _od((60, 55, 120)), _od((205, 92, 140)), "Nuclear Fast Red",
           "BCIP/NBT — alkaline phosphatase. Blue-to-purple insoluble precipitate.", "AP"),
    _stain("vina-green", "Vina Green (green)", IHC, _od((40, 120, 80)), _od((205, 92, 140)), "Nuclear Fast Red",
           "Vina Green — HRP. Green stain, used to contrast brown/red in multiplex IHC.", "HRP"),
    _stain("tmb", "TMB (blue)", IHC, _od((40, 80, 170)), HEMA, "Haematoxylin",
           "3,3',5,5'-tetramethylbenzidine — HRP. Blue; mostly research / blotting.", "HRP"),
    _stain("dab-black", "DAB-Nickel / black (black)", IHC, _od((28, 28, 30)), HEMA, "Haematoxylin",
           "Nickel-enhanced DAB — HRP. Dense black; detected by darkness (no hue).", "HRP", black=True, min_px=5),
    _stain("deep-space-black", "Deep Space Black (black)", IHC, _od((25, 25, 28)), _od((205, 92, 140)), "Nuclear Fast Red",
           "Deep Space Black — HRP. Dark grey-to-black; detected by darkness.", "HRP", black=True, min_px=5),

    # ---------------- Special / histochemical stains ---------------- #
    _stain("he", "H&E (routine)", SPECIAL, HEMA, EOSIN, "Eosin",
           "Haematoxylin (blue-purple nuclei) + Eosin (pink cytoplasm/collagen). Measures haematoxylin (nuclear) area by default.", min_px=4),
    _stain("he-eosin", "H&E — eosin area", SPECIAL, EOSIN, HEMA, "Haematoxylin",
           "H&E measuring the eosinophilic (pink cytoplasm / collagen) fraction.", min_px=6),
    _stain("masson-trichrome", "Masson's Trichrome (collagen)", SPECIAL, TRI_COLLAGEN, TRI_MUSCLE, "Muscle (red)",
           "Collagen blue, muscle red, fibrin pink. Measures blue collagen — the standard fibrosis metric.", min_px=6),
    _stain("pas", "PAS (magenta)", SPECIAL, PAS_MAGENTA, HEMA, "Haematoxylin",
           "Periodic acid–Schiff. Magenta for glycogen, glycoprotein, basement membranes and fungal walls.", min_px=6),
    _stain("alcian-blue", "Alcian Blue (mucin)", SPECIAL, ALCIAN, _od((205, 92, 140)), "Nuclear Fast Red",
           "Alcian blue. Blue for acid mucopolysaccharides / acidic mucins.", min_px=6),
    _stain("prussian-blue", "Prussian Blue (iron)", SPECIAL, _od((22, 46, 140)), _od((205, 92, 140)), "Nuclear Fast Red",
           "Perls' Prussian blue. Blue ferric-iron deposits (haemosiderin).", min_px=5),
    _stain("congo-red", "Congo Red (amyloid)", SPECIAL, _od((198, 54, 78)), HEMA, "Haematoxylin",
           "Congo red. Salmon-red amyloid in brightfield (apple-green under polarised light).", min_px=6),
    _stain("picrosirius-red", "Picrosirius Red (collagen)", SPECIAL, _od((172, 26, 40)), _od((240, 220, 60)), "Picric yellow",
           "Picrosirius red. Red collagen fibres on a yellow background; a fibrosis metric.", min_px=6),
    _stain("giemsa", "Giemsa (blue)", SPECIAL, GIEMSA_BLUE, GIEMSA_EOS, "Eosin",
           "Giemsa / methylene blue. Blue-purple nuclei, chromatin and organisms (e.g. H. pylori).", min_px=5),
    _stain("toluidine-blue", "Toluidine Blue", SPECIAL, _od((70, 40, 140)), _od((240, 240, 240)), "Background",
           "Toluidine blue. Blue-violet; mast-cell granules and cartilage metachromasia.", min_px=5),
    _stain("feulgen", "Feulgen (magenta)", SPECIAL, FEULGEN, LIGHT_GREEN, "Light Green",
           "Feulgen reaction. Magenta DNA in nuclei; light-green counterstain.", min_px=5),
    _stain("azan", "AZAN / Mallory (collagen)", SPECIAL, AZAN_BLUE, AZAN_AZO, "Azocarmine",
           "AZAN trichrome. Blue collagen, red nuclei/muscle.", min_px=6),
    _stain("ziehl-neelsen", "Ziehl-Neelsen / AFB (red)", SPECIAL, _od((190, 30, 90)), _od((90, 150, 210)), "Methylene blue",
           "Acid-fast bacilli. Red bacilli on a blue background (carbol fuchsin).", min_px=3),

    # ---------------- Black / silver stains (darkness-detected) ---------------- #
    _stain("gms", "GMS / Grocott (black)", SPECIAL, _od((26, 26, 28)), _od((90, 150, 120)), "Light green",
           "Grocott methenamine silver. Black fungal walls / Pneumocystis on a pale green background.", black=True, min_px=4),
    _stain("reticulin", "Reticulin (silver, black)", SPECIAL, _od((28, 28, 30)), _od((210, 200, 120)), "Background",
           "Reticulin silver impregnation. Black reticulin fibre networks.", black=True, min_px=3),
    _stain("verhoeff", "Verhoeff-Van Gieson (elastic, black)", SPECIAL, _od((26, 26, 28)), _od((190, 60, 60)), "Van Gieson (red)",
           "Verhoeff elastic. Black elastic fibres; Van Gieson red/yellow background.", black=True, min_px=3),
]

# --------------------------------------------------------------------------- #
# WHICH STAINS ARE LIVE
# --------------------------------------------------------------------------- #
# Only these keys are offered in "Select stain" and accepted by the API. To bring
# another one online as it is validated:
#
#   1. add its key here                              → it appears in the picker
#   2. if it is an IHC chromogen that auto-detect
#      should also be able to find on its own, add
#      the matching family to engine.ENABLED_FAMILIES
#
# Everything above this line stays exactly as it is. A request naming a stain
# that is not enabled falls back to auto-detect rather than failing.
ENABLED_KEYS: set[str] = {"dab"}

_BY_KEY = {s["key"]: s for s in STAINS}
# common aliases so search / URLs are forgiving
_ALIASES = {"diaminobenzidine": "dab", "trichrome": "masson-trichrome",
            "masson": "masson-trichrome", "h&e": "he", "hande": "he",
            "hematoxylin-eosin": "he", "periodic-acid-schiff": "pas",
            "perls": "prussian-blue", "iron": "prussian-blue", "afb": "ziehl-neelsen",
            "acid-fast": "ziehl-neelsen", "grocott": "gms", "silver": "gms"}


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def is_enabled(key: str | None) -> bool:
    """Is this stain currently shipped? (see ENABLED_KEYS)"""
    if not key:
        return False
    k = _slug(key)
    return (k in ENABLED_KEYS) or (_ALIASES.get(k, "") in ENABLED_KEYS)


def lookup(key: str | None) -> dict | None:
    """Resolve a stain key — ENABLED stains only. A disabled or unknown key
    returns None, which the caller reads as "use auto-detect"."""
    if not key:
        return None
    k = _slug(key)
    k = k if k in _BY_KEY else _ALIASES.get(k, "")
    if k not in ENABLED_KEYS:
        return None
    return _BY_KEY.get(k)


def enabled_stains() -> list[dict]:
    """The live stain entries, in listing order."""
    order = {IHC: 0, SPECIAL: 1}
    live = [s for s in STAINS if s["key"] in ENABLED_KEYS]
    return sorted(live, key=lambda s: (order.get(s["category"], 9), s["name"].lower()))


def as_list() -> list[dict]:
    """UI listing (category then name). Strips the heavy OD vectors for transport.
    Only ENABLED stains are listed — the picker can never offer one the engine is
    not shipping yet."""
    return [{k: s[k] for k in ("key", "name", "category", "swatch", "description", "enzyme")}
            for s in enabled_stains()]
