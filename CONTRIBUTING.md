# Contributing to ImageSL

Thanks for considering it. This is research software that produces numbers
people put in papers, so the bar for changes that affect measurement is higher
than for most projects.

## Before you start

* For anything that changes a **measured result**, open an issue first. Include
  what the current behaviour is, what you think it should be, and why. A change
  that shifts measurements is not a bug fix unless it can be shown to be one.
* For bugs, fixes, docs, and tooling, go straight to a pull request.

## Ground rules for measurement code

`server/ihc/` decides what counts as positive staining. Changes there need:

1. **A stated reason.** What is wrong, on what material, and how do you know.
2. **Evidence.** `scripts/backtest.py` and `scripts/synthetic_matrix.py` exist
   for this. Show before and after on the same inputs.
3. **Agreement between the browser and the server.** The live overlay in
   `server/web/app.js` reproduces the server's decision so the on-screen
   percentage matches the exported one. If you change the rule on one side,
   change it on the other, or the number a user sees stops matching the number
   they export.

## Development

```
pip install -r server/requirements.txt
python -m uvicorn app:app --app-dir server --reload --port 8000
```

Set `IMAGESL_WEB_ANALYZER=1` to enable the analyzer API on a local server; it
is disabled by default because the hosted site is a download page only.

For the desktop app, see `desktop/BUILD.md`.

## Pull requests

* One concern per pull request.
* Say what you verified, and how. "Tests pass" is less useful than the specific
  thing you checked.
* If you change the update mechanism, the installer, or anything under
  `.github/workflows/`, say explicitly what you did to test it. That pipeline
  has failed silently before.

## What we are unlikely to accept

* Reformatting or style-only changes across many files
* New runtime dependencies without a clear reason
* Changes that make the analyzer's output depend on the machine it runs on
