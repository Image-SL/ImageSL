<#
.SYNOPSIS
  Publish the macOS disk image to S3 from Windows, so the site's
  "Download for macOS" button goes live.

.DESCRIPTION
  The PowerShell twin of scripts/publish_macos_download.sh. It exists because
  the credentials for this bucket live on the Windows machine - that is how the
  Windows installer got there in the first place - while the .dmg can only be
  built on a Mac. Uploading is just bytes, so the two do not have to be the
  same computer.

  Verify the SHA-256 after copying the image over. A 126 MB file that arrives
  truncated still uploads perfectly happily, and the failure then shows up as a
  disk image that will not mount on someone else's Mac.

  Order is deliberate, and matches the CI publish job:
    1. the immutable v/<version>/ copy, so a provenance copy exists even if a
       later step fails and nothing user-facing has moved yet;
    2. latest/, the key the app actually redirects to;
    3. the digest LAST - a digest visible before its bytes describes a file
       nobody can fetch yet. /api/downloads republishes it and the desktop
       updater refuses to install anything that does not match, so a missing
       sidecar does not fail loudly: it silently turns the check off.

.EXAMPLE
  .\publish_macos_download.ps1 -Source C:\Users\me\Downloads\ImageSL-macOS.dmg
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [string]$Version = "1.0.0",
    [string]$Bucket  = "imagesl-downloads-581586866061",
    # Set this to the digest reported on the build machine to prove the copy
    # across arrived intact. Skipped when empty.
    [string]$ExpectedSha256 = ""
)

$ErrorActionPreference = "Stop"

$Name  = "ImageSL-macOS.dmg"
$CType = "application/x-apple-diskimage"
$Disp  = "attachment; filename=`"$Name`""
$Immutable = "public, max-age=31536000, immutable"
# Short max-age on latest/: that key is overwritten every release, so a long one
# would leave caches handing out the previous image after a new one shipped.
$Short = "public, max-age=300"

if (-not (Test-Path -LiteralPath $Source)) { throw "No file at $Source" }

$bytes = (Get-Item -LiteralPath $Source).Length
$sum   = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash
Write-Host "publishing $Name  version=$Version  bytes=$bytes"
Write-Host "sha256=$sum"

if ($ExpectedSha256 -and ($sum -ne $ExpectedSha256.ToUpper())) {
    throw "SHA-256 mismatch - the copy from the Mac is not intact.`n  expected $($ExpectedSha256.ToUpper())`n  got      $sum"
}

# Two spaces between digest and name, the sha256sum convention the server reads.
$sidecar = Join-Path ([IO.Path]::GetDirectoryName((Resolve-Path -LiteralPath $Source))) "$Name.sha256"
"$sum  $Name" | Set-Content -LiteralPath $sidecar -NoNewline -Encoding ascii
Add-Content -LiteralPath $sidecar -Value "" -Encoding ascii

function Put($file, $key, $type, $cache, $disposition) {
    $args = @("s3", "cp", $file, "s3://$Bucket/$key",
              "--content-type", $type, "--cache-control", $cache)
    if ($disposition) { $args += @("--content-disposition", $disposition) }
    & aws @args
    if ($LASTEXITCODE -ne 0) { throw "upload of $key failed ($LASTEXITCODE)" }
}

Put $Source  "v/$Version/$Name"        $CType                      $Immutable $Disp
Put $Source  "latest/$Name"            $CType                      $Short     $Disp
Put $sidecar "v/$Version/$Name.sha256" "text/plain; charset=utf-8" $Immutable $null
Put $sidecar "latest/$Name.sha256"     "text/plain; charset=utf-8" $Short     $null

Write-Host ""
Write-Host "uploaded. the site caches a failed probe for 60s, so give it a minute:"
Write-Host "  curl.exe -s https://imagesl.com/api/downloads"
