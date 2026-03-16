#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"

# Code signing identity — set this to your own Apple Developer identity
# for persistent permissions across rebuilds, or use "-" for ad-hoc signing.
# Example: "Apple Development: you@example.com (TEAMID)"
SIGNING_IDENTITY="${SCRIBER_SIGNING_IDENTITY:--}"

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

echo "==> Code signing..."
# macOS adds provenance xattrs that block codesigning.
# Copy to /tmp (outside APFS provenance tracking), sign there, copy back.
rm -rf /tmp/Scriber.app
ditto --norsrc dist/Scriber.app /tmp/Scriber.app
xattr -cr /tmp/Scriber.app 2>/dev/null || true
# Recreate symlinks without xattrs
find /tmp/Scriber.app -type l | while read link; do
    target=$(readlink "$link")
    rm "$link"
    ln -s "$target" "$link"
done
codesign --deep --force --sign "$SIGNING_IDENTITY" /tmp/Scriber.app
# Copy signed app back
rm -rf dist/Scriber.app
cp -R /tmp/Scriber.app dist/Scriber.app
rm -rf /tmp/Scriber.app
if [ "$SIGNING_IDENTITY" = "-" ]; then
    echo "    Signed: ad-hoc (permissions may reset on rebuild)"
    echo "    Tip: Set SCRIBER_SIGNING_IDENTITY to your Apple Developer identity"
    echo "         for persistent permissions across rebuilds."
else
    echo "    Signed: $(codesign -dv dist/Scriber.app 2>&1 | grep 'Authority=' || echo "$SIGNING_IDENTITY")"
fi

echo ""
echo "==> Build complete!"
echo "    App bundle: dist/Scriber.app"
echo ""
echo "    To install, copy to /Applications:"
echo "      cp -r dist/Scriber.app /Applications/"
echo ""
echo "    On first launch, macOS will ask for:"
echo "      - Microphone access"
echo "      - Accessibility access (for paste)"
echo "    Grant both in System Settings > Privacy & Security."
