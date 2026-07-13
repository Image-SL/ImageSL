# Build the ImageSL Windows client (thin shell) into a single signed-ready .exe.
#
#   powershell -ExecutionPolicy Bypass -File scripts/build_client.ps1
#
# Prereqs: Python 3.10+ on PATH. The script creates an isolated venv, so it
# won't touch your system Python. Output: client/dist/ImageSL.exe
#
# NOTE: this only *builds* the exe. To avoid SmartScreen warnings you must
# then CODE-SIGN it — see docs/SECURITY.md. Building alone does not fix
# reputation warnings; nothing can, except signing + reputation over time.

$ErrorActionPreference = "Stop"
$root   = Resolve-Path "$PSScriptRoot\.."
$client = Join-Path $root "client"

Write-Host "== ImageSL client build ==" -ForegroundColor Magenta

# 1. Regenerate the icon from the logo.
& "$PSScriptRoot\make_ico.ps1" | Out-Host

# 2. Isolated build venv.
$venv = Join-Path $client ".buildvenv"
if (-not (Test-Path $venv)) { python -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --upgrade pip | Out-Host
& $py -m pip install -r (Join-Path $client "requirements.txt") | Out-Host

# 3. PyInstaller. --onefile keeps distribution simple; --windowed hides the
#    console; a clean, unpacked, unobfuscated build minimizes AV false alarms.
Push-Location $client
try {
  & $py -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name "ImageSL" `
    --icon "ImageSL.ico" `
    --manifest "app.manifest" `
    --version-file "version_info.txt" `
    --add-data "config.json;." `
    "imagesl_client.py" | Out-Host
}
finally { Pop-Location }

$exe = Join-Path $client "dist\ImageSL.exe"
if (Test-Path $exe) {
  Write-Host "`nBuilt: $exe" -ForegroundColor Green
  Write-Host "Next: code-sign it (docs/SECURITY.md), then copy to server/dist/ so /download/windows serves it." -ForegroundColor Yellow
} else {
  throw "Build failed — ImageSL.exe not produced."
}
