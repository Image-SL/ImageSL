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
├── installer.iss   # Inno Setup recipe — wraps the Windows build into an installer
└── requirements.txt
.github/workflows/build-desktop.yml   # CI: builds Win + macOS, smoke-tests, releases
```

## What ships

| Platform | Asset | What it is |
| --- | --- | --- |
| Windows | `ImageSL-Setup-Windows.exe` | Inno Setup installer — per-user, no admin prompt, Start Menu entry + uninstaller |
| macOS | `ImageSL-macOS.dmg` | disk image containing `ImageSL.app` and an Applications shortcut — drag to install |

## Distribution — served by us, not by GitHub

**The repository is private, so GitHub release links cannot be used.** A
`releases/latest/download/...` URL only resolves for accounts that can see the
repo; to everyone else it is a 404 *HTML page*, which navigates the visitor away
instead of downloading. That silently breaks every download button on a public
site, and it was doing exactly that.

The site serves the installers itself:

| Route | What it does |
| --- | --- |
| `GET /api/downloads` | reports the current version and which platforms have a build |
| `GET,HEAD /download/windows` | sends `ImageSL-Setup-Windows.exe` as an attachment |
| `GET,HEAD /download/macos` | sends `ImageSL-macOS.dmg` as an attachment |

Put the built artefacts in **`IMAGESL_DOWNLOAD_DIR`** (default `<repo>/downloads/`)
under exactly the filenames in the table above. That directory is gitignored —
an 82 MB installer does not belong in a repository — so it must be populated as
part of deployment.

The landing page asks `/api/downloads` before enabling its buttons, so a platform
you have not built yet greys out and says "coming soon" instead of handing the
visitor a broken link. The desktop app's update check asks the same endpoint.

> **These names must agree, or downloads break:** `_DOWNLOADS` in
> `server/app.py`, `OutputBaseFilename` in `installer.iss`, and the `asset:`
> matrix in the workflow.

If the repository is ever made public, GitHub releases become a viable host
again and the CI workflow already publishes them — but the site's own routes
work either way, so there is no reason to go back.

The Windows installer is deliberately **per-user** (`PrivilegesRequired=lowest`,
installing under `%LOCALAPPDATA%\Programs\ImageSL`). An unsigned installer that
demands administrator rights is precisely the prompt users are taught to refuse;
a per-user install needs no elevation at all.

On macOS the spec emits a real `.app` via `BUNDLE`. Without it the dmg would hold
a bare Unix executable — double-clicking that opens Terminal rather than the app.

## Build locally

From the repo root, in a clean virtualenv:

```bash
pip install -r desktop/requirements.txt
pyinstaller --noconfirm desktop/ImageSL.spec
```

Smoke-test the result without opening a window (starts the engine, checks it
answers, exits):

```bash
dist/ImageSL/ImageSL --selftest              # macOS / Linux
dist/ImageSL/ImageSL.exe --selftest          # Windows
dist/ImageSL.app/Contents/MacOS/ImageSL --selftest   # the bundled macOS app
```

To produce the Windows installer locally you also need
[Inno Setup 6](https://jrsoftware.org/isdl.php); from the repo root:

```bash
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=2.0.0 /DMyAppVersionNum=2.0.0 desktop\installer.iss
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

**What CI does do today on macOS: an ad-hoc signature** (`codesign --sign -`).
That is not a Developer ID and does not clear Gatekeeper — a first run still
needs right-click → Open. What it does prevent is Apple Silicon refusing an
entirely unsigned bundle with "ImageSL is damaged and can't be opened", which is
the difference between an awkward first launch and an impossible one.

First-run instructions worth putting in the release notes:

- **Windows:** SmartScreen shows "Windows protected your PC" → *More info* →
  *Run anyway*.
- **macOS:** right-click `ImageSL.app` → *Open* → *Open*. Double-clicking a
  quarantined unsigned app just refuses.

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
