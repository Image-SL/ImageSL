# ImageSL desktop app — build & release

The desktop app is the **whole ImageSL engine bundled into one program**. It
starts the real FastAPI backend on a private `127.0.0.1` port and opens a native
window on the analyzer. It runs **fully offline** — a slide never leaves the
machine; only a best-effort check for a newer release touches the network.

```
desktop/
├── launcher.py     # starts the embedded server, opens the window, checks for updates
├── updater.py      # asks the site's /api/downloads what is published (never raises, offline-safe)
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

There are two ways to supply the file, and a hosted deploy needs one of them —
the installer is gitignored, so pushing code alone gives you the routes and no
build, and the buttons stay greyed out.

**1. From disk.** Put the artefacts in **`IMAGESL_DOWNLOAD_DIR`** (default
`<repo>/downloads/`) under exactly the filenames above. This is what a local run
uses. On a hosted deploy it means a persistent volume, since the file cannot
come from git.

**2. From object storage (recommended when hosted).** Set

| Variable | Effect |
| --- | --- |
| `IMAGESL_DOWNLOAD_URL_WINDOWS` | `/download/windows` 302s here instead of serving bytes |
| `IMAGESL_DOWNLOAD_URL_MACOS` | same for the macOS disk image |

and upload the installer to S3, R2, or any static host. This keeps ~86 MB per
download off the application's own bandwidth, and needs no volume. The redirect
is deliberately **302, not 301** — where a file is hosted is an operational
detail that must stay changeable, and a permanent redirect would be cached by
browsers and outlive the decision.

An external URL takes precedence over the local file when both are set.

The landing page asks `/api/downloads` before enabling its buttons, so a platform
you have not built yet greys out and says "coming soon" instead of handing the
visitor a broken link. The desktop app's update check asks the same endpoint.

**A configured URL is not proof of a file.** `/api/downloads` sends a cached HEAD
to the external URL before reporting a platform available, because a typo, a
failed upload, a bucket policy change or a deleted object otherwise leaves the
variable set, the button lit, and the visitor holding an S3 error page — which is
the exact 404 this whole arrangement exists to prevent. `/download/<platform>`
deliberately redirects *regardless* of that probe: object storage is the
authority on its own contents, and a stale negative must never block a link that
works.

## Publishing to S3 — the live setup

`imagesl.online` serves its installers from S3 and the app 302s to them. Two
repository variables drive it (Settings → Secrets and variables → Actions →
**Variables**, not Secrets — these are not sensitive):

| Variable | Used by | Meaning |
| --- | --- | --- |
| `IMAGESL_DOWNLOAD_BUCKET` | both workflows | the bucket name, e.g. `imagesl-downloads` |
| `IMAGESL_DOWNLOAD_BASE_URL` | `deploy.yml` | optional; overrides the derived S3 URL so a CDN can be put in front later |

With neither set, everything still works — `build-desktop.yml` skips its publish
step with a warning and `deploy.yml` leaves the URLs empty, so the buttons read
"coming soon". That is the honest state, and strictly better than a live button
pointing at nothing.

### One-time bucket setup

Needs the AWS CLI (`winget install Amazon.AWSCLI`) and credentials for account
`581586866061`.

```bash
aws s3api create-bucket --bucket imagesl-downloads --region us-east-2 \
  --create-bucket-configuration LocationConstraint=us-east-2
```

The objects must be anonymously readable — visitors are redirected straight to
them — so allow public reads on the download prefixes only:

```bash
aws s3api put-public-access-block --bucket imagesl-downloads \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false"

aws s3api put-bucket-policy --bucket imagesl-downloads --policy '{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicReadInstallers",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": ["arn:aws:s3:::imagesl-downloads/latest/*",
                 "arn:aws:s3:::imagesl-downloads/v/*"]
  }]
}'
```

`BlockPublicAcls` stays **true**: the policy grants the read, so nothing needs a
public ACL, and leaving ACLs blocked means a stray `--acl public-read` cannot
widen access by accident.

### Let CI write to it

The OIDC role `imagesl-github-deploy` currently has ECR and Lightsail
permissions. It needs S3 writes as well:

```bash
aws iam put-role-policy --role-name imagesl-github-deploy \
  --policy-name imagesl-downloads-write --policy-document '{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:PutObjectAcl", "s3:AbortMultipartUpload"],
    "Resource": "arn:aws:s3:::imagesl-downloads/*"
  }]
}'
```

**Check the role's trust policy before relying on this.** It has only ever been
assumed by `deploy.yml`; if its condition names a specific workflow rather than
the repository, `build-desktop.yml` cannot assume it and the publish step fails
at the credentials stage:

```bash
aws iam get-role --role-name imagesl-github-deploy --query 'Role.AssumeRolePolicyDocument'
```

The `token.actions.githubusercontent.com:sub` condition should be
`repo:solvergent/ImageSL:*` (any ref), not a single-workflow or single-ref match.

### Bootstrap an installer you already have

To make Windows downloads work immediately, without waiting for a CI run:

```bash
aws s3 cp downloads/ImageSL-Setup-Windows.exe s3://imagesl-downloads/latest/ImageSL-Setup-Windows.exe --content-type application/octet-stream --content-disposition 'attachment; filename="ImageSL-Setup-Windows.exe"' --cache-control 'public, max-age=300'
```

`Content-Disposition` is set on the **object** because the app answers with a
302 — it hands off the response and never gets to set a header on the bytes.
Without it a browser may render the file rather than save it.

Then set `IMAGESL_DOWNLOAD_BUCKET` and push (or re-run the deploy workflow) so
the container picks up the URLs. Verify:

```bash
curl -s https://imagesl.online/api/downloads
```

> **These names must agree, or downloads break:** `_DOWNLOADS` in
> `server/app.py`, `OutputBaseFilename` in `installer.iss`, and the `asset:`
> matrix in the workflow.

The CI workflow still attaches the assets to a GitHub Release on a tag, but only
as an archive for people who can see the repository. Nothing user-facing depends
on it, and it must not be reintroduced as the download link.

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
runners, smoke-tests each frozen binary, uploads them to S3 (which is what makes
the site's buttons live), and on a tag also attaches them to a GitHub Release:

```bash
# cut a release
git tag v2.0.1
git push origin v2.0.1
```

A manual run (Actions → "Build desktop apps" → Run workflow) builds,
smoke-tests, and still publishes to S3 — under a `dev-<sha>` archive key plus the
same `latest/` keys — but does not cut a Release. The version baked into a manual
build is `0.0.0-dev`, so use a tag for anything a user will install.

**Note the macOS runner is Apple Silicon**, so the `.app` it produces is arm64.
Intel Macs need a `universal2` build or a separate x86_64 leg; this is not wired
up, and the landing page's "macOS 12 or later" claim does not currently
distinguish the two.

Each platform is smoke-tested as the artifact that actually ships: Windows runs
`--selftest` on the frozen exe before packaging, and macOS runs it on the
**signed `.app`**, then mounts the finished dmg and verifies the bundle inside it.
An earlier version tested `dist/ImageSL/ImageSL`, which on macOS is the plain
COLLECT tree that PyInstaller emits *alongside* the bundle — so the thing in the
dmg was never run at all.

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

On launch the app asks the site's own `/api/downloads` what is published. If that
version is newer than the running one **and** there is genuinely a build for this
platform, a dismissible **Download** banner appears in the window.

It used to ask `github.com/<repo>/releases/latest`, which cannot work here: the
repository is private, so an anonymous request gets a 404 and the check reported
"no update" forever. The site is also the better authority — it answers the
question actually being asked, which is "can this user install something newer",
not "does a git tag exist".

It does **not** silently replace itself — a quantification tool should not swap
its analysis code mid-session without the user's consent.

## Version

`version.txt` at the repo root is the source of truth. CI writes it from the git
tag (`v2.0.1` → `2.0.1`). The running app reads it back for the update check and
the window's About text.
