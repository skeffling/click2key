#!/usr/bin/env bash
# Generate macOS .icns + a 64px title-bar PNG from logo.png.
# Requires macOS (uses `sips` and `iconutil`).
set -euo pipefail

SRC="logo.png"
if [[ ! -f "$SRC" ]]; then
    echo "$SRC not found. Save your logo as $SRC at the repo root." >&2
    exit 1
fi

ICONSET="assets/icon.iconset"
mkdir -p "$ICONSET"

# Apple's expected sizes (1x + @2x retina).
for s in 16 32 128 256 512; do
    sips -z "$s" "$s" "$SRC" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    sips -z "$((s*2))" "$((s*2))" "$SRC" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done

iconutil -c icns "$ICONSET" -o assets/icon.icns

# Small PNGs for the in-window title bar. The white variant is used in
# dark mode via CTkImage(dark_image=...).
sips -z 28 28 "$SRC" --out assets/title_icon.png >/dev/null
if [[ -f "logo-white.png" ]]; then
    sips -z 28 28 logo-white.png --out assets/title_icon_dark.png >/dev/null
    echo "Built assets/icon.icns, assets/title_icon.png, assets/title_icon_dark.png"
else
    echo "Built assets/icon.icns and assets/title_icon.png (no logo-white.png — title icon won't switch in dark mode)"
fi
