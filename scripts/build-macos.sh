#!/usr/bin/env bash
# Build Sort Memories.app for macOS (no signing, no notarization — v0.1.x).
# Usage : ./scripts/build-macos.sh
# Output :
#   - dist/Sort Memories.app  (46 MB)
#   - dist/SortMemories-macos-vX.Y.Z.zip  (~21 MB, GitHub Release asset)
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
    echo "Creating venv (Python 3.11)…"
    /opt/homebrew/bin/python3.11 -m venv .venv
fi

echo "Installing dependencies…"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet flask pillow pillow-heif rawpy exifread imagehash numpy pywebview appdirs send2trash certifi pyinstaller

echo "Cleaning previous build…"
trash dist build/work 2>/dev/null || true

echo "Running PyInstaller…"
.venv/bin/pyinstaller build/SortMemories.spec \
    --clean --noconfirm \
    --distpath dist \
    --workpath build/work

VERSION=$(.venv/bin/python -c "from sort_memories import __version__; print(__version__)")
ZIP_NAME="SortMemories-macos-v${VERSION}.zip"

echo "Creating release zip: ${ZIP_NAME}…"
ditto -c -k --sequesterRsrc --keepParent "dist/Sort Memories.app" "dist/${ZIP_NAME}"

echo ""
echo "✓ Built dist/Sort Memories.app ($(du -sh "dist/Sort Memories.app" | awk '{print $1}'))"
echo "✓ Zipped dist/${ZIP_NAME} ($(du -sh "dist/${ZIP_NAME}" | awk '{print $1}'))"
echo ""
echo "Next : test the .app, then 'gh release create v${VERSION} dist/${ZIP_NAME}'"
