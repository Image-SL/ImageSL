#!/usr/bin/env bash
#
# Publish the macOS disk image so the site's "Download for macOS" button goes
# live. Run from the repo root, on a machine that can reach AWS.
#
# This does by hand what .github/workflows/build-desktop.yml's `publish` job
# does on a tag. Use it when you have a build sitting in downloads/ and do not
# want to cut a release for it - which is exactly how the Windows installer got
# onto S3 (its sidecar on the bucket is in PowerShell's Get-FileHash casing,
# not the workflow's).
#
# The order below matters and is not cosmetic:
#   1. the immutable v/<version>/ copy first, so a provenance copy exists even
#      if a later step fails and nothing user-facing has moved yet;
#   2. then latest/, the key the app redirects to;
#   3. the digest LAST, because a digest visible before its bytes describes a
#      file nobody can fetch. /api/downloads republishes it and the desktop
#      updater refuses to install anything that does not match - so publishing
#      the image without the sidecar quietly disables that check rather than
#      failing loudly.
set -euo pipefail

BUCKET="${IMAGESL_DOWNLOAD_BUCKET:-imagesl-downloads-581586866061}"
NAME="ImageSL-macOS.dmg"
SRC="${1:-downloads/${NAME}}"
VERSION="$(tr -d ' \t\r\n' < version.txt)"
CTYPE="application/x-apple-diskimage"

[ -f "$SRC" ] || { echo "no build at $SRC - build it first (see desktop/BUILD.md)"; exit 1; }

SUM="$(shasum -a 256 "$SRC" | cut -d' ' -f1)"
BYTES="$(stat -f%z "$SRC" 2>/dev/null || stat -c%s "$SRC")"
echo "publishing $NAME  version=$VERSION  bytes=$BYTES  sha256=$SUM"
printf '%s  %s\n' "$SUM" "$NAME" > "${SRC}.sha256"

# Content-Disposition rides on the OBJECT: /download/macos may hand the request
# off, so we do not always get to set a header on the bytes ourselves.
put() {
  aws s3 cp "$1" "s3://${BUCKET}/$2" \
    --content-type "$3" --cache-control "$4" \
    ${5:+--content-disposition "$5"}
}
DISP="attachment; filename=\"${NAME}\""
IMMUTABLE="public, max-age=31536000, immutable"
# Short max-age on latest/: this key is overwritten every release, so a long one
# would leave caches handing out a previous 126MB image after a new one shipped.
SHORT="public, max-age=300"

put "$SRC"            "v/${VERSION}/${NAME}"        "$CTYPE"                    "$IMMUTABLE" "$DISP"
put "$SRC"            "latest/${NAME}"              "$CTYPE"                    "$SHORT"     "$DISP"
put "${SRC}.sha256"   "v/${VERSION}/${NAME}.sha256" "text/plain; charset=utf-8" "$IMMUTABLE" ""
put "${SRC}.sha256"   "latest/${NAME}.sha256"       "text/plain; charset=utf-8" "$SHORT"     ""

echo
echo "uploaded. the site caches a failed probe for 60s, so give it a minute:"
echo "  curl -s https://imagesl.com/api/downloads"
