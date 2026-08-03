# ImageSL desktop app — build & release

The desktop app is the **whole ImageSL engine bundled into one program**. It
starts the real FastAPI backend on a private `127.0.0.1` port and opens a native
window on the analyzer. It runs **fully offline** — a slide never leaves the
machine; only a best-effort check for a newer release touches the network.

```
desktop/
├── launcher.py     # starts the embedded server, opens the window, checks for updates
├── updater.py      # asks GitHub for the latest release (never raises, offline-safe)
├── ImageSL.spec    # PyInstaller recipe — bundles launcher + the entire server/ tree
└── requirements.txt
.github/workflows/build-desktop.yml   # CI: builds Win + macOS, smoke-tests, releases
```

## What ships

| Platform | Asset | What it is |
| --- | --- | --- |
| Windows | `ImageSL-Windows.exe` | one portable executable — download and double-click |
| macOS | `ImageSL-macOS.dmg` | drag-to-Applications disk image (Apple Silicon + Intel) |

The landing page's Download buttons point at
`github.com/<repo>/releases/latest/download/<asset>`, which always redirects to
the newest release — so they never need editing.

## Build locally

From the repo root, in a clean virtualenv:

```bash
pip install -r desktop/requirements.txt
pyinstaller --noconfirm desktop/ImageSL.spec
```

Smoke-test the result without opening a window (starts the engine, checks it
answers, exits):

```bash
dist/ImageSL/ImageSL --selftest        # onedir
# or, onefile:
dist/ImageSL.exe --selftest
```

> **A macOS `.app`/`.dmg` cannot be built on Windows** — PyInstaller does not
> cross-compile. Build the Mac version on a Mac, or let CI do it (below).

## Release via CI (recommended)

`.github/workflows/build-desktop.yml` builds **both** platforms on GitHub's own
runners, smoke-tests each frozen binary, and publishes the assets to a Release:

```bash
# cut a release
git tag v2.0.1
git push origin v2.0.1
```

A manual run (Actions → "Build desktop apps" → Run workflow) builds and
smoke-tests without publishing.

## Code signing — the ONLY legitimate fix for antivirus / SmartScreen warnings

An unsigned executable downloaded from the internet triggers Windows SmartScreen
and can be flagged by antivirus heuristics. **The fix is a real signature, not
obfuscation or packing** — packing a binary to evade detection is what malware
does, degrades trust, and would sink a scientific tool. The spec deliberately
disables UPX for the same reason.

To sign, you need certificates (these cost money and require identity
verification — there is no free shortcut that actually silences the warnings):

- **Windows:** an Authenticode code-signing certificate (OV ~$100–300/yr, or an
  EV cert which clears SmartScreen immediately). Export it as a `.pfx`, base64 it,
  and add as repo secrets `WINDOWS_PFX_BASE64` / `WINDOWS_PFX_PASSWORD`.
- **macOS:** an Apple Developer ID Application certificate ($99/yr Developer
  Program) plus **notarization** (Apple scans and staples the app). Add the
  Developer ID, an app-specific password, and your Team ID as secrets.

The signing/notarization steps are written but commented out in the workflow —
uncomment them once the secrets exist. Until then the app still builds and runs;
users just see the standard "unknown publisher" prompt.

## Auto-update

On launch the app asks `github.com/<repo>/releases/latest` for the newest
version. If it is newer than the running one, a dismissible **Download** banner
appears in the window. It does **not** silently replace itself — a quantification
tool should not swap its analysis code mid-session without the user's consent.
Cutting a new release (tag → CI) is therefore the whole update mechanism.

## Version

`version.txt` at the repo root is the source of truth. CI writes it from the git
tag (`v2.0.1` → `2.0.1`). The running app reads it back for the update check and
the window's About text.
