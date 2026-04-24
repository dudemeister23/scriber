#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv-pyinstaller"
ENTITLEMENTS="$SCRIPT_DIR/Scriber.entitlements"
BUILD_BACKUP_DIR="$SCRIPT_DIR/.build-backups"
SIGNING_IDENTITY="${SCRIBER_SIGNING_IDENTITY:-}"
TEST_APP="${SCRIBER_TEST_APP:-$HOME/Scriber-PyInstaller.app}"

if [ -z "$SIGNING_IDENTITY" ]; then
    SIGNING_IDENTITY="$(
        security find-identity -v -p codesigning |
            awk -F '"' '/Apple Development/ { print $2; exit }'
    )"
fi

if [ -z "$SIGNING_IDENTITY" ]; then
    echo "ERROR: No Apple Development signing identity found." >&2
    echo "Set SCRIBER_SIGNING_IDENTITY to the certificate name and rerun." >&2
    exit 1
fi

echo "==> Creating virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "==> Installing dependencies..."
pip install -q -r requirements-pyinstaller.txt

echo "==> Generating app icon..."
python3 assets/generate_icons.py
xattr -cr "$SCRIPT_DIR/assets/Scriber.icns" "$SCRIPT_DIR/scriber/icons" 2>/dev/null || true

echo "==> Preserving previous PyInstaller artifacts..."
if [ -d build ] || [ -d dist ]; then
    BACKUP_STAMP="$(date +%Y%m%d-%H%M%S)"
    BACKUP_PATH="$BUILD_BACKUP_DIR/pyinstaller-$BACKUP_STAMP"
    mkdir -p "$BACKUP_PATH"
    if [ -d build ]; then
        mv build "$BACKUP_PATH/build"
        echo "    Moved build -> $BACKUP_PATH/build"
    fi
    if [ -d dist ]; then
        mv dist "$BACKUP_PATH/dist"
        echo "    Moved dist -> $BACKUP_PATH/dist"
    fi
else
    echo "    No previous build artifacts found."
fi

echo "==> Building .app bundle with PyInstaller..."
pyinstaller --clean --noconfirm Scriber.spec

echo "==> Code signing (hardened runtime + entitlements)..."
APP="$SCRIPT_DIR/dist/Scriber.app"
SIGNED_APP="/tmp/Scriber-pyinstaller.app"
rm -rf "$SIGNED_APP"
ditto --norsrc --noextattr "$APP" "$SIGNED_APP"
xattr -cr "$SIGNED_APP" 2>/dev/null || true

echo "    Signing nested libraries and extension modules..."
find "$SIGNED_APP" \( -name "*.dylib" -o -name "*.so" \) -type f -print0 |
    while IFS= read -r -d '' file; do
        codesign --force --sign "$SIGNING_IDENTITY" --options runtime --timestamp "$file"
    done

if [ -d "$SIGNED_APP/Contents/Frameworks/Python.framework" ]; then
    echo "    Signing Python framework..."
    codesign --force --sign "$SIGNING_IDENTITY" --options runtime \
        --timestamp "$SIGNED_APP/Contents/Frameworks/Python.framework"
fi

echo "    Signing main executable..."
xattr -cr "$SIGNED_APP" 2>/dev/null || true
codesign --force --sign "$SIGNING_IDENTITY" --options runtime \
    --entitlements "$ENTITLEMENTS" --timestamp \
    "$SIGNED_APP/Contents/MacOS/Scriber"

echo "    Signing app bundle..."
codesign --force --sign "$SIGNING_IDENTITY" --options runtime \
    --entitlements "$ENTITLEMENTS" --timestamp "$SIGNED_APP"

echo "    Verifying signature..."
if codesign --verify --deep --strict "$SIGNED_APP" 2>&1; then
    echo "    Signature valid (hardened runtime, deep strict)"
else
    echo "    Strict verification has warnings (app may still work)"
fi

rm -rf "$APP"
ditto --norsrc --noextattr "$SIGNED_APP" "$APP"
rm -rf "$TEST_APP"
ditto --norsrc --noextattr "$SIGNED_APP" "$TEST_APP"
xattr -cr "$APP" "$TEST_APP" 2>/dev/null || true
rm -rf "$SIGNED_APP"

echo ""
echo "==> PyInstaller build complete!"
codesign -dv "$APP" 2>&1 | grep -E "flags=|Authority=|TeamIdentifier=" | sed 's/^/    /'
echo ""
echo "    App bundle: dist/Scriber.app"
echo "    Test copy:  $TEST_APP"
