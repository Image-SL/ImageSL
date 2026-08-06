# ImageSL — Security & Distribution (the honest version)

## 1. Desktop architecture — the engine ships with the app

> **This section was rewritten.** It previously described the desktop client as
> a *thin shell* that opened a window onto the hosted backend and passed a
> license key, and instructed the reader to "never bundle the `server/` code
> into the client". That has not been true since the offline app landed: the
> desktop build bundles the entire `server/` tree and runs it locally. The old
> advice now describes the opposite of the shipping design, so it is corrected
> here rather than left to mislead.

**How the desktop app works today:** `desktop/launcher.py` starts the real
FastAPI application on a random loopback port and opens a native window onto it.
The whole engine — `server/ihc/engine.py`, `detect.py`, the stain registry — is
inside the download. See [desktop/BUILD.md](../desktop/BUILD.md).

**What that trades away, deliberately:** the algorithms are no longer hidden.
Anyone sufficiently motivated can extract the bundled Python from the
distribution. That is an accepted consequence of the goal that replaced
obfuscation — a slide never leaves the machine, which matters far more to the
people using this than source secrecy does, and the source is public anyway.

What still holds:

- **Never ship secrets in the desktop build.** There is no API key, no license
  key and no account in it, and none should be added — anything bundled is
  readable. The app authenticates to nothing because it talks to nothing.
- The launcher clears `IMAGESL_ACCESS_TOKENS` for the embedded server: the
  socket is bound to `127.0.0.1` on an ephemeral port, so the only caller is the
  window in front of the user.
- For the **hosted** deployment, secrets stay in the host's environment
  variables, and `IMAGESL_ACCESS_TOKENS` still gates `/api/*` so a leaked
  backend URL alone cannot be used to hammer it.

## 2. Windows Defender / SmartScreen — what's actually true

**You asked for the exe to "not get flagged." Here is the honest engineering
reality, because it changes what you should do:**

SmartScreen and Defender do **not** decide based on how the binary is compiled.
They decide based on:

1. **Code signing** — is the exe signed by a certificate tied to a verified
   identity?
2. **Reputation** — has that signed identity's software been downloaded and run
   enough, without incident, to be trusted?

An **unsigned** executable from a brand-new publisher will show the blue
"Windows protected your PC" SmartScreen prompt **no matter what you do to the
build.** There is no compiler flag, packer setting, or trick that removes it —
and anything marketed as "AV evasion" is both ineffective against reputation
systems and a red flag in itself. ImageSL will not ship that.

### What genuinely reduces / removes the warning

| Action | Effect |
| --- | --- |
| **Buy an OV code-signing certificate** (~$150–400/yr, e.g. DigiCert, Sectigo, SSL.com) and sign the exe | Removes the "unknown publisher" label; SmartScreen warning fades as reputation builds over days/weeks of downloads. |
| **Buy an EV code-signing certificate** (~$250–600/yr, hardware token / cloud HSM) and sign | Grants **immediate** SmartScreen reputation for many users — the fastest path to a clean first-run. |
| Build clean: no UPX packing, no obfuscation, real version metadata, `asInvoker` manifest | Reduces heuristic *false positives* in third-party AV. ImageSL already does all of this. |
| Submit the signed exe to Microsoft via the Defender portal if a false positive occurs | Clears specific false detections. |

### How to sign (once you have a certificate)

```powershell
# EV certs live on a hardware token / cloud HSM; OV certs you import as a .pfx.
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
  /n "Your Company Name" client\dist\ImageSL.exe

# verify
signtool verify /pa /v client\dist\ImageSL.exe
```

Timestamping (`/tr`) keeps the signature valid after the cert expires. After
signing, copy the exe to `server/dist/ImageSL.exe` and redeploy so
`/download/windows` serves the signed build.

### What ImageSL does on its side to help

- `--onefile --windowed`, **no UPX**, **no obfuscation** — the traits AV
  heuristics flag are exactly the ones we avoid.
- Full `version_info.txt` metadata (company, product, version) and a real icon.
- `asInvoker` manifest — requests no admin elevation, which trustworthy apps
  don't need.
- A tiny codebase (a WebView shell), so there's little for a scanner to trip on.

**Bottom line:** build clean (done) → **sign with an OV/EV cert (your step, it
costs money and requires identity verification)** → distribute. That is the only
real path, and it's the industry-standard one.

## 3. Handling user data

- Slides are processed in memory and cached only to power the render sliders and
  the exports; they are not persisted to disk by the backend. An idle analysis is
  dropped after `IMAGESL_CACHE_TTL` (default 8 hours).
- **No part of a slide is sent to any third party.** The engine is numpy and
  scikit-image running in this process; there is no model API call anywhere in
  the codebase, and no network egress on the analysis path at all.
- In the desktop app the analysis never leaves the machine in the first place —
  the engine is bundled and served on a private loopback port.
- Add a privacy notice appropriate to your users (e.g. HIPAA considerations if
  slides are patient-linked) before production clinical use.
