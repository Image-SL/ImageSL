# ImageSL — Security & Distribution (the honest version)

## 1. Source-code protection — solved by architecture

**Goal:** users who download the `.exe` can't get at the core logic.

**How ImageSL achieves it:** the desktop client is a *thin shell*. It opens a
native window to the hosted web app and passes a license key — that's the whole
program. Every algorithm (`server/ihc/engine.py`), the AI integration, and the
`ANTHROPIC_API_KEY` live only on the Railway backend. There is nothing
proprietary compiled into the download, so there is nothing to decompile.

This is strictly stronger than any client-side obfuscation, which can always be
reversed given enough effort. Keep it that way:

- Never bundle the `server/` code, model logic, or API keys into the client.
- Keep secrets in Railway environment variables, never in `client/config.json`
  (which ships inside the exe and is readable).
- Gate the API with `IMAGESL_ACCESS_TOKENS` so a leaked backend URL alone can't
  be used to hammer your Claude quota.

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

- Slides are processed in memory and cached briefly (default 15 min) only to
  power the render sliders; they are not persisted to disk by the backend.
- Only a downsampled thumbnail is sent to the Claude API for vision reasoning,
  and only when the user leaves "Use AI" enabled.
- Add a privacy notice appropriate to your users (e.g. HIPAA considerations if
  slides are patient-linked) before production clinical use.
