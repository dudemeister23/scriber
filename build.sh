#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
ENTITLEMENTS="$SCRIPT_DIR/Scriber.entitlements"

# Code signing identity — uses your Apple Developer cert.
# Hardened runtime + entitlements lets macOS persist TCC permissions
# (microphone, accessibility) across rebuilds by TeamIdentifier.
SIGNING_IDENTITY="${SCRIBER_SIGNING_IDENTITY:-Apple Development: fabianbresan@me.com (6Y2BMGZB7T)}"

echo "==> Creating virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "==> Installing dependencies..."
pip install -q -r requirements.txt
pip install -q -r requirements-build.txt

echo "==> Generating app icon..."
python3 assets/generate_icons.py

echo "==> Cleaning previous build..."
rm -rf build dist

echo "==> Building .app bundle..."
python3 setup.py py2app --no-strip 2>&1

echo "==> Code signing (hardened runtime + entitlements)..."
# macOS adds provenance xattrs that block codesigning.
# Copy to /tmp (outside APFS provenance tracking), sign there, copy back.
APP=/tmp/Scriber.app
rm -rf "$APP"
ditto --norsrc dist/Scriber.app "$APP"
xattr -cr "$APP" 2>/dev/null || true
# Recreate symlinks without xattrs
find "$APP" -type l | while read link; do
    target=$(readlink "$link")
    rm "$link"
    ln -s "$target" "$link"
done

# Sign all nested binaries/dylibs first (inside-out), then the app bundle.
# --options runtime = hardened runtime — lets macOS persist TCC grants
# by TeamIdentifier rather than CDHash.
echo "    Signing nested frameworks and libraries..."
find "$APP/Contents/Frameworks" -type f -name "*.dylib" \
    -exec codesign --force --sign "$SIGNING_IDENTITY" --options runtime \
    --timestamp '{}' ';'

if [ -d "$APP/Contents/Frameworks/Python.framework" ]; then
    codesign --force --sign "$SIGNING_IDENTITY" --options runtime \
        --timestamp \
        "$APP/Contents/Frameworks/Python.framework"
fi

echo "    Signing extension modules..."
find "$APP/Contents/Resources/lib" -type f -name "*.so" \
    -exec codesign --force --sign "$SIGNING_IDENTITY" --options runtime \
    --timestamp '{}' ';'

echo "    Signing main app bundle..."
codesign --force --sign "$SIGNING_IDENTITY" --options runtime \
    --entitlements "$ENTITLEMENTS" --timestamp \
    "$APP/Contents/MacOS/Scriber"

codesign --force --sign "$SIGNING_IDENTITY" --options runtime \
    --entitlements "$ENTITLEMENTS" --timestamp "$APP"

echo "    Verifying signature..."
if codesign --verify --deep --strict "$APP" 2>&1; then
    echo "    ✓ Signature valid (hardened runtime, deep strict)"
else
    echo "    ⚠ Strict verification has warnings (may still work)"
fi

# Copy signed app back
rm -rf dist/Scriber.app
cp -R "$APP" dist/Scriber.app
rm -rf "$APP"

echo ""
echo "==> Build complete!"
codesign -dv dist/Scriber.app 2>&1 | grep -E "flags=|Authority=|TeamIdentifier=" | sed 's/^/    /'
echo ""
echo "    App bundle: dist/Scriber.app"
echo ""
echo "    To install:"
echo "      rm -rf /Applications/Scriber.app"
echo "      cp -R dist/Scriber.app /Applications/"
echo ""
echo "    Permissions (Microphone, Accessibility) should persist across rebuilds"
echo "    as long as you sign with the same developer identity + team ID."
