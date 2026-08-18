# Getting ImageSL up and running

Three ways to run it. No API keys, no accounts, no configuration — the analysis
engine is self-contained numpy and scikit-image.

---

## A. Just use it

<https://imagesl.com> — drop a slide on the page. Nothing to install.

Or install the desktop app from the same page, which bundles the whole engine and
runs it on a private loopback port, fully offline. A slide never leaves the
machine.

---

## B. Run it locally from source

Needs Python 3.12.

```bash
pip install -r server/requirements.txt
uvicorn --app-dir server app:app --port 8000
```

Open <http://localhost:8000>.

To exercise the desktop download buttons too, put `ImageSL-Setup-Windows.exe`
and/or `ImageSL-macOS.dmg` in `downloads/` at the repo root first.

---

## C. Deploy your own

Short version: the `Dockerfile` is the whole
deployment, and pushing to `main` ships it.

---

## Using it

1. **Upload** one slide or a whole folder of `.tif` / `.png` / `.jpg`. Each slide
   gets three panels — Original, Overlay, Stain only — plus the numbers: positive
   area as a percentage of tissue, positive pixels, how many stained structures
   were found, and tissue pixels.

2. **Sensitivity** — the slider directly under the slide, so the image stays in
   view while you drag. It is not a brightness cut: it scales the bar a stained
   structure's own peak has to clear, from 4× stricter to 4× more permissive than
   the operating point the slide chose for itself. The readout is the multiplier
   applied (`×0.76`), which means the same thing on every slide, and **Auto**
   returns to the centre. Everything re-measures live in the browser, with no
   server round-trip.

3. **Regions** — for the cases automation should not decide alone. Pick a tool,
   then drag a rectangle or trace a freehand shape:

   | Tool | Use it for |
   | --- | --- |
   | **Focus** | measure only inside what you draw — one cortical layer, one TMA core, one half of a section |
   | **Ignore** | a fold, pen mark, bubble or torn edge |

   A region says *where* to measure and nothing else. Both tools move the
   positive area and the tissue it is measured against together, so the reported
   percentage is always "positive area ÷ tissue actually measured".

   **This means ignoring an area can move the percentage either way, and both
   directions are correct** — cutting out lightly stained tissue makes the
   remainder more stained on average, so the figure rises. The card spells out
   what the number is measured over whenever a region is active.

   There is deliberately no *More here* / *Less here* tool. A per-region
   operating point made the headline percentage a mixture of several different
   decision rules — not a quantity you can compare between slides or write into a
   method — and the same structure counted or not depending on which side of a
   hand-drawn line it fell.

4. **Export** — every download carries exactly the sensitivity and the regions on
   screen, so a saved TIFF or a batch ZIP always matches what you were looking
   at. The CSV records the operating point, the structure counts, whether the
   staining was focal or diffuse, and both `tissue_pixels` and
   `tissue_pixels_before_regions`, so the scope of any published figure is
   unambiguous.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Analysis expired" when moving sliders | The analysis was idle past `IMAGESL_CACHE_TTL` (default 8 hours). The page sends a heartbeat, so this only happens after a genuinely long gap — click **Analyze** again. |
| Upload rejected as too large | Raise `IMAGESL_MAX_UPLOAD_MB` (default 256). |
| 401 on every API call | `IMAGESL_ACCESS_TOKENS` is set; enter one of those keys in the app's *Access key* box. |
| Download button greyed out, "coming soon" | No installer is published for that platform yet. `curl /api/downloads` to see what the server thinks exists; see [../desktop/BUILD.md](../desktop/BUILD.md). |
| Windows SmartScreen / macOS "unidentified developer" on install | Expected — the installers are not yet code signed. See [SECURITY.md](SECURITY.md). |
| An export names missing slides | The archive carries a `MISSING_SLIDES.txt` listing them by name; re-analyze those and export again. |
