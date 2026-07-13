# Build a multi-resolution Windows .ico (PNG-compressed entries) from a PNG.
# Usage: powershell -File scripts/make_ico.ps1 -Source <png> -Out <ico>
param(
  [string]$Source = "$PSScriptRoot\..\client\logo-source.png",
  [string]$Out    = "$PSScriptRoot\..\client\ImageSL.ico"
)

Add-Type -AssemblyName System.Drawing

$sizes = 16, 24, 32, 48, 64, 128, 256
$src = [System.Drawing.Image]::FromFile((Resolve-Path $Source))

# Render each size to PNG bytes.
$entries = @()
foreach ($s in $sizes) {
  $bmp = New-Object System.Drawing.Bitmap $s, $s
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
  $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
  $g.Clear([System.Drawing.Color]::Transparent)
  $g.DrawImage($src, 0, 0, $s, $s)
  $g.Dispose()
  $ms = New-Object System.IO.MemoryStream
  $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
  $entries += , @{ Size = $s; Bytes = $ms.ToArray() }
}
$src.Dispose()

# Assemble the ICO container.
$fs = [System.IO.File]::Open((New-Item -ItemType File -Force -Path $Out).FullName,
      [System.IO.FileMode]::Create)
$bw = New-Object System.IO.BinaryWriter $fs

# ICONDIR header
$bw.Write([UInt16]0)              # reserved
$bw.Write([UInt16]1)              # type = icon
$bw.Write([UInt16]$entries.Count) # image count

$offset = 6 + (16 * $entries.Count)  # header + directory entries
foreach ($e in $entries) {
  $dim = if ($e.Size -ge 256) { 0 } else { $e.Size }   # 0 means 256
  $bw.Write([Byte]$dim)            # width
  $bw.Write([Byte]$dim)            # height
  $bw.Write([Byte]0)               # palette
  $bw.Write([Byte]0)               # reserved
  $bw.Write([UInt16]1)             # color planes
  $bw.Write([UInt16]32)            # bpp
  $bw.Write([UInt32]$e.Bytes.Length)
  $bw.Write([UInt32]$offset)
  $offset += $e.Bytes.Length
}
foreach ($e in $entries) { $bw.Write($e.Bytes) }

$bw.Flush(); $bw.Close(); $fs.Close()
Write-Host "Wrote $Out ($($entries.Count) sizes)"
