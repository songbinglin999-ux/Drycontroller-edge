#!/usr/bin/env bash
# Build "DryController USB Maker.app" bundle with icon.
# Run once after pulling the repo, or whenever the icon should regenerate.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR/DryController USB Maker.app"
ICONSET_DIR="$SCRIPT_DIR/icon.iconset"

info()  { echo "  → $*"; }
error() { echo "  ✗ $*" >&2; exit 1; }

info "Building DryController USB Maker.app …"

# ── 1. Generate icon ────────────────────────────────────────────
info "Generating USB icon …"
command -v python3 >/dev/null 2>&1 || error "python3 not found; required to generate icon PNGs in $ICONSET_DIR"
python3 -c '
import os, struct, zlib

def create_png(path: str, size: int = 256) -> None:
    """Generate a simple USB-drive icon PNG."""
    # colour palette
    BG      = (240, 244, 248, 255)  # pale grey
    BODY    = (37,  99,  235, 255)  # blue
    BODY_DK = (29,  78,  216, 255)  # dark blue
    CONN    = (148, 163, 184, 255)  # silver
    CONN2   = (203, 213, 225, 255)  # light silver
    WHITE   = (255, 255, 255, 255)

    pixels = [BG] * (size * size)

    # ── USB stick body (horizontal rectangle, rounded-ish) ──
    # proportions on a 256×256 canvas
    body_l = int(size * 0.15)   # left
    body_r = int(size * 0.85)   # right
    body_t = int(size * 0.30)   # top
    body_b = int(size * 0.68)   # bottom
    conn_l = int(size * 0.85)   # connector left
    conn_r = int(size * 0.92)   # connector right
    conn_t = int(size * 0.38)   # connector top
    conn_b = int(size * 0.60)   # connector bottom
    hole_r = 5                   # corner radius (approx)

    def in_rect(x: int, y: int, l: int, r: int, t: int, b: int) -> bool:
        return l <= x < r and t <= y < b

    def in_rounded_rect(x: int, y: int, l: int, r: int, t: int, b: int, rad: int) -> bool:
        if not (l <= x < r and t <= y < b):
            return False
        # corners
        if x - l < rad and y - t < rad:          # top-left
            return (x - l - rad)**2 + (y - t - rad)**2 <= rad**2
        if r - 1 - x < rad and y - t < rad:      # top-right
            return (r - 1 - x - rad)**2 + (y - t - rad)**2 <= rad**2
        if x - l < rad and b - 1 - y < rad:      # bottom-left
            return (x - l - rad)**2 + (b - 1 - y - rad)**2 <= rad**2
        if r - 1 - x < rad and b - 1 - y < rad:  # bottom-right
            return (r - 1 - x - rad)**2 + (b - 1 - y - rad)**2 <= rad**2
        return True

    for y in range(size):
        for x in range(size):
            idx = y * size + x
            # USB connector
            if in_rounded_rect(x, y, conn_l, conn_r, conn_t, conn_b, 3):
                if x < conn_l + (conn_r - conn_l) // 3:
                    pixels[idx] = CONN2
                else:
                    pixels[idx] = CONN
            # USB body
            elif in_rounded_rect(x, y, body_l, body_r, body_t, body_b, 12):
                # subtle gradient effect
                if y < body_t + (body_b - body_t) // 3:
                    pixels[idx] = BODY
                else:
                    pixels[idx] = BODY_DK
                # USB symbol (simple white rectangle in middle)
                sym_l = int(size * 0.35)
                sym_r = int(size * 0.55)
                sym_t = int(size * 0.42)
                sym_b = int(size * 0.56)
                if in_rect(x, y, sym_l, sym_r, sym_t, sym_b):
                    pixels[idx] = WHITE

    # ── write PNG ──
    def pack_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk  = struct.pack(">I", len(data))
        chunk += chunk_type
        chunk += data
        chunk += struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        return chunk

    def build_rows() -> bytes:
        raw = b""
        for y in range(size):
            raw += b"\x00"  # filter: none
            for x in range(size):
                r, g, b, a = pixels[y * size + x]
                raw += struct.pack("BBBB", r, g, b, a)
        return raw

    ihdr  = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    raw_rows = build_rows()

    png  = b"\x89PNG\r\n\x1a\n"
    png += pack_chunk(b"IHDR", ihdr)
    png += pack_chunk(b"IDAT", zlib.compress(raw_rows))
    png += pack_chunk(b"IEND", b"")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)

create_png("'"$ICONSET_DIR"'/icon_256x256.png", 256)
create_png("'"$ICONSET_DIR"'/icon_512x512.png", 512)

# retinas
import shutil
for base, retina in [("icon_256x256.png", "icon_128x128@2x.png"),
                     ("icon_512x512.png", "icon_256x256@2x.png")]:
    shutil.copy2(f"'"$ICONSET_DIR"'/{base}", f"'"$ICONSET_DIR"'/{retina}")

# smaller sizes via downscale (just copy same for simplicity — macOS will scale)
for name in ["icon_128x128.png", "icon_64x64.png", "icon_32x32.png", "icon_16x16.png"]:
    shutil.copy2("'"$ICONSET_DIR"'/icon_256x256.png", f"'"$ICONSET_DIR"'/{name}")
for name in ["icon_64x64@2x.png", "icon_32x32@2x.png", "icon_16x16@2x.png"]:
    shutil.copy2("'"$ICONSET_DIR"'/icon_256x256.png", f"'"$ICONSET_DIR"'/{name}")

print("Icon PNGs generated.")
'

# ── 2. Convert to ICNS ──────────────────────────────────────────
info "Converting to ICNS …"
command -v iconutil >/dev/null 2>&1 || error "iconutil not found; install the macOS command line tools"
iconutil --convert icns "$ICONSET_DIR" --output "$SCRIPT_DIR/icon.icns" || error "iconutil failed"
rm -rf "$ICONSET_DIR"

# ── 3. Build .app bundle ────────────────────────────────────────
info "Assembling .app bundle …"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# launcher script
cat > "$APP_DIR/Contents/MacOS/launcher" << 'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../../../.." && pwd)"
open -a Terminal "$REPO/tools/os-installer-usb-maker/launch.command"
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/launcher"

# Info.plist
cat > "$APP_DIR/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundleIconFile</key>
    <string>icon</string>
    <key>CFBundleIdentifier</key>
    <string>com.drycontroller.os-installer-usb-maker</string>
    <key>CFBundleName</key>
    <string>DryController USB Maker</string>
    <key>CFBundleDisplayName</key>
    <string>DryController USB Maker</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

# copy icon
cp "$SCRIPT_DIR/icon.icns" "$APP_DIR/Contents/Resources/"

# ── 4. Ad-hoc sign (required for macOS Launch Services to grant file access) ──
info "Signing .app bundle (ad-hoc) …"
command -v codesign >/dev/null 2>&1 || error "codesign not found; install Xcode Command Line Tools"
codesign --force --deep --sign - "$APP_DIR" || error "codesign failed"

# ── 5. Done ─────────────────────────────────────────────────────
info "Done → $APP_DIR"
echo ""
echo "  You can now drag this to the Dock or Desktop:"
echo "    open '$APP_DIR'"
echo ""
echo "  To regenerate the icon later, re-run:"
echo "    bash $0"
