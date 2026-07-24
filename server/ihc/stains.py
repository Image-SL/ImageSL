"""
ImageSL — IHC antibody / stain registry (CPT 88342 panel + common chromogens).

This is metadata, not detection: the pixel engine measures the *chromogen* colour
directly (DAB brown, red, etc.), so it works no matter which antibody was used.
The registry supplies, for each marker:

    * a display name,
    * the expected subcellular compartment (nuclear / cytoplasmic / membranous /
      …) — used to adapt morphology (minimum object size) so tiny nuclear dots
      and thin membranes aren't dropped as noise,
    * the usual chromogen ("DAB" for almost all; a few are commonly red),

so the UI can offer a searchable "select your stain" mode and the engine can
label + tune per marker. "Auto" needs none of this — it detects the chromogen and
labels generically.

Compartment codes:  N nuclear · C cytoplasmic · M membranous ·
                    CM cytoplasmic-membranous · NC nuclear-cytoplasmic ·
                    S secreted/granular (treated like cytoplasmic for morphology)
"""

from __future__ import annotations
import re

# (display name, compartment, [chromogen override]) -----------------------------
_RAW: list[tuple] = [
    ("Adrenocorticotropic hormone (ACTH)", "S"),
    ("ALK (D5F3)", "C"),
    ("Alpha-1-antitrypsin / A1AT", "C"),
    ("Alpha-1-fetoprotein / AFP", "C"),
    ("Alpha-Synuclein", "C"),
    ("Amyloid A component", "C"),
    ("Amyloid precursor protein", "C"),
    ("Annexin A10", "C"),
    ("Arginase-1", "NC"),
    ("ATRX", "N"),
    ("Beta-amyloid", "C"),
    ("B72.3 (tumor-associated glycoprotein)", "CM"),
    ("BAF47 (INI-1)", "N"),
    ("BCL2 oncoprotein", "C"),
    ("BCL6 protein", "N"),
    ("Ber-EP4", "M"),
    ("Beta-catenin", "M"),
    ("BK virus", "N"),
    ("BRAF", "C"),
    ("Brachyury (notochord)", "N"),
    ("CAIX", "M"),
    ("C-KIT / CD117", "CM"),
    ("C4d complement", "M"),
    ("CA-125", "CM"),
    ("Calcitonin", "C"),
    ("H-Caldesmon", "C"),
    ("Calponin", "C"),
    ("Calretinin", "NC"),
    ("Caspase-3", "C"),
    ("CD10 (CALLA)", "M"),
    ("CD123", "M"),
    ("CD138 (syndecan)", "M"),
    ("CD1a", "M"),
    ("CD15 (LEU-M-1)", "M"),
    ("CD163", "M"),
    ("CD20", "M"),
    ("CD21", "M"),
    ("CD23", "M"),
    ("CD279 (PD-1)", "M"),
    ("CD3 (T-cell)", "M"),
    ("CD30", "M"),
    ("CD31", "M"),
    ("CD34 (endothelium)", "M"),
    ("CD34 (stem cell)", "M"),
    ("CD4 (T-helper)", "M"),
    ("CD43", "M"),
    ("CD44", "M"),
    ("CD45 (LCA)", "M"),
    ("CD5 (T-cell)", "M"),
    ("CD56 (NCAM)", "M"),
    ("CD57 (LEU-7)", "M"),
    ("CD61", "M"),
    ("CD68 (macrophage, KP1)", "C"),
    ("CD7 (T-cell)", "M"),
    ("CD70", "M"),
    ("CD71 (erythroid)", "M"),
    ("CD79a (B-cell)", "M"),
    ("CD8 (cytotoxic)", "M"),
    ("CDX2", "N"),
    ("CEA (monoclonal)", "CM"),
    ("CEA (polyclonal)", "CM"),
    ("Chromogranin", "C"),
    ("Chymotrypsin", "C"),
    ("C-MYC", "N"),
    ("Collagen IV", "C"),
    ("CXCL13", "C"),
    ("Cyclin-D1", "N"),
    ("Cytokeratin 19 (CK19)", "CM"),
    ("Cytokeratin 20 (CK20)", "C"),
    ("Cytokeratin 34βE12 (HMW)", "C"),
    ("Cytokeratin 5/6", "C"),
    ("Cytokeratin 7 (CK7)", "C"),
    ("Cytokeratin AE1/AE3", "C"),
    ("Cytokeratin CAM 5.2", "C"),
    ("Cytomegalovirus / CMV", "NC"),
    ("D2-40 (lymphatic)", "M"),
    ("Desmin", "C"),
    ("DOG-1 protein", "M"),
    ("Dysferlin", "M"),
    ("Dystrophin 1", "M"),
    ("Dystrophin 2", "M"),
    ("Dystrophin 3", "M"),
    ("E-Cadherin", "M"),
    ("Epithelial membrane antigen (EMA)", "M"),
    ("Epstein-Barr virus LMP", "CM"),
    ("ERG", "N"),
    ("Ewing's sarcoma / CD99", "M"),
    ("Factor XIIIa", "C"),
    ("Factor VIII", "C"),
    ("Follicle stimulating hormone (FSH)", "C"),
    ("G34W", "N"),
    ("Gastrin", "C"),
    ("GATA-3", "N"),
    ("GCDFP", "C"),
    ("Glial fibrillary acidic protein (GFAP)", "C"),
    ("Glucagon", "C"),
    ("Glucose transporter 1 (GLUT-1)", "M"),
    ("Glutamine synthetase", "C"),
    ("Glycophorin A", "M"),
    ("Glypican-3", "C"),
    ("Growth hormone (GH)", "C"),
    ("H3K27M", "N"),
    ("H3K27me3", "N"),
    ("HBME-1 (mesothelial)", "M"),
    ("Helicobacter pylori", "C"),
    ("Hemoglobin", "C"),
    ("Hepatitis B core antigen (HBcAg)", "NC"),
    ("Hepatitis B surface antigen (HBsAg)", "C"),
    ("HepPar-1 (hepatocyte)", "C"),
    ("Herpes virus I & II", "NC"),
    ("Herpes virus type 8, LNA", "N"),
    ("HMB-45 (melanoma)", "C"),
    ("Human chorionic gonadotropin (HCG)", "C"),
    ("IDH1", "C"),
    ("IgA", "C"),
    ("IgG", "C"),
    ("IgG4", "C"),
    ("IgM", "C"),
    ("IMP3 (KOC)", "C"),
    ("Inhibin", "C"),
    ("INSM1", "N"),
    ("Insulin", "C"),
    ("JC virus", "N"),
    ("Kappa light chain", "C"),
    ("Ki-67 / MIB1", "N"),
    ("Lambda light chain", "C"),
    ("LC3B", "C"),
    ("Luteinizing hormone (LH)", "C"),
    ("Lysozyme (muramidase)", "C"),
    ("Mammaglobin", "C"),
    ("MDM2 protein", "N"),
    ("Melan-A", "C"),
    ("Microphthalmia transcription factor (MITF)", "N"),
    ("MOC-31", "M"),
    ("MUC4", "CM"),
    ("MUM-1 protein", "N"),
    ("Muscle actin (HHF35)", "C"),
    ("Myeloperoxidase (MPO)", "C"),
    ("Myogenin", "N"),
    ("Myoglobin", "C"),
    ("Myosin", "C"),
    ("Napsin-A", "C"),
    ("NKX3.1", "N"),
    ("Neurofilament (non-p) NF-NP", "C"),
    ("Neurofilament / NF", "C"),
    ("Neuron specific enolase (NSE)", "C"),
    ("Neuronal nuclei (NeuN)", "NC"),
    ("OCT-2", "N"),
    ("OCT-4 (germ cell)", "N"),
    ("p16", "NC"),
    ("p40 protein", "N"),
    ("p504S (AMACR)", "C"),
    ("p53 protein", "N"),
    ("p63 protein", "N"),
    ("Parathyroid hormone (PTH)", "C"),
    ("Parvovirus B19", "N"),
    ("PAX5", "N"),
    ("PAX-8 protein", "N"),
    ("Perforin", "C"),
    ("PGP 9.5", "C"),
    ("PLAP", "CM"),
    ("Phosphohistone-H3 (PHH3)", "N"),
    ("Prolactin", "C"),
    ("Prostatic acid phosphatase (PSAP)", "C"),
    ("Prostatic specific antigen (PSA)", "C"),
    ("Renal cell carcinoma antigen (RCC)", "M"),
    ("S100", "NC"),
    ("SALL4", "N"),
    ("SATB2", "N"),
    ("Sarcoglycan A", "M"),
    ("Sarcoglycan B", "M"),
    ("Sarcoglycan D", "M"),
    ("Sarcoglycan G", "M"),
    ("SDHB", "C"),
    ("Smooth muscle actin (SMA)", "C"),
    ("Smooth muscle myosin heavy chain", "C"),
    ("Somatostatin", "C"),
    ("SOX10", "N"),
    ("SOX11", "N"),
    ("Spectrin 1", "M"),
    ("SSTR2A", "M"),
    ("STAT6", "N"),
    ("Surfactant protein B", "C"),
    ("Synaptophysin", "C"),
    ("T-cell receptor beta (TCR beta F1)", "M"),
    ("TAU protein", "C"),
    ("Terminal deoxytransferase (TdT)", "N"),
    ("TFE3", "N"),
    ("Thyroglobulin", "C"),
    ("Thyroid stimulating hormone (TSH)", "C"),
    ("Toxoplasma gondii", "C"),
    ("Treponema pallidum", "C"),
    ("Trypsin", "C"),
    ("Tryptase (mast cell)", "C"),
    ("TTF1", "N"),
    ("Ubiquitin", "C"),
    ("Uroplakin 2", "CM"),
    ("Uroplakin 3", "M"),
    ("Villin", "CM"),
    ("Vimentin", "C"),
    ("Wilms tumor protein (WT-1)", "NC"),
]

_COMPARTMENT_NAME = {
    "N": "Nuclear", "C": "Cytoplasmic", "M": "Membranous",
    "CM": "Cytoplasmic / membranous", "NC": "Nuclear / cytoplasmic",
    "S": "Cytoplasmic (granular)",
}

# Minimum connected-component size (px @ ~1024-edge) per compartment: nuclear
# markers stain small dots, membranes are thin — allow smaller objects there so
# genuine signal isn't pruned; diffuse cytoplasmic stains keep a firmer floor.
_COMPARTMENT_MIN_PX = {"N": 4, "M": 4, "CM": 6, "NC": 5, "C": 8, "S": 8}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s


def _category(name: str, comp: str) -> str:
    n = name.lower()
    if re.match(r"^cd\d|^cd-", n) or "lca" in n or "leu-" in n:
        return "CD / lymphoid markers"
    if "cytokeratin" in n or n.startswith("ck") or "cam 5.2" in n:
        return "Cytokeratins"
    if any(k in n for k in ("virus", "cmv", "ebv", "hbsag", "hbcag", "herpes",
                            "pylori", "toxoplasma", "treponema", "parvovirus", "bk ", "jc ")):
        return "Infectious agents"
    if any(k in n for k in ("hormone", "acth", "insulin", "glucagon", "gastrin",
                            "calcitonin", "prolactin", "tsh", "fsh", "lh)", "somatostatin",
                            "chromogranin", "synaptophysin", "insm1", "hcg")):
        return "Endocrine / neuroendocrine"
    if any(k in n for k in ("actin", "desmin", "myosin", "myogenin", "myoglobin",
                            "caldesmon", "calponin", "dystrophin", "sarcoglycan", "dysferlin")):
        return "Muscle"
    if any(k in n for k in ("melan", "hmb", "sox10", "mitf", "s100")):
        return "Melanocytic / neural"
    if comp == "N":
        return "Nuclear / transcription factors"
    return "General / lineage markers"


STAINS: list[dict] = []
for row in _RAW:
    name, comp = row[0], row[1]
    chromo = row[2] if len(row) > 2 else "DAB"
    STAINS.append({
        "key": _slug(name),
        "name": name,
        "compartment": comp,
        "compartment_name": _COMPARTMENT_NAME.get(comp, "Cytoplasmic"),
        "min_px": _COMPARTMENT_MIN_PX.get(comp, 8),
        "chromogen": chromo,
        "category": _category(name, comp),
    })

_BY_KEY = {s["key"]: s for s in STAINS}


def lookup(key: str | None) -> dict | None:
    if not key:
        return None
    return _BY_KEY.get(key) or _BY_KEY.get(_slug(key))


def as_list() -> list[dict]:
    """Public listing for the UI (sorted by name within category)."""
    return sorted(STAINS, key=lambda s: (s["category"], s["name"].lower()))
