#!/usr/bin/env bash
# Build "Audio Ripper.app" — a standalone macOS app (no Python/ffmpeg needed
# on the machine that runs it).
#
# Requirements on the BUILD Mac:
#   * Python 3.11–3.13 from python.org (includes a working Tk).
#     Homebrew Python also works if you `brew install python-tk`.
#
# Usage:   ./packaging/build_mac.sh
# Result:  dist/Audio Ripper.app  and  dist/Audio-Ripper-macOS-<arch>.zip
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
DENO_VERSION="${DENO_VERSION:-v2.5.6}"
PYTHON="${PYTHON:-}"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This script must be run on macOS." >&2
  exit 1
fi

# --- pick a Python that has a working Tk 8.6+
if [[ -z "$PYTHON" ]]; then
  for c in python3.13 python3.12 python3.11 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys, tkinter; assert sys.version_info >= (3, 10); assert tkinter.TkVersion >= 8.6' 2>/dev/null; then
      PYTHON="$(command -v "$c")"; break
    fi
  done
fi
if [[ -z "$PYTHON" ]]; then
  echo "No Python 3.10+ with Tk 8.6 found." >&2
  echo "Install Python from https://www.python.org/downloads/macos/ and re-run." >&2
  exit 1
fi
echo "Using $PYTHON ($("$PYTHON" -c 'import sys,tkinter;print(sys.version.split()[0], "Tk", tkinter.TkVersion)'))"

ARCH="$(uname -m)"   # arm64 (Apple silicon) or x86_64 (Intel)

# --- virtualenv with dependencies
"$PYTHON" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt "pyinstaller>=6.10"

# --- JavaScript runtime (deno) — yt-dlp needs one for YouTube downloads
mkdir -p packaging/vendor
if [[ ! -x packaging/vendor/deno ]]; then
  case "$ARCH" in
    arm64)  DENO_ZIP="deno-aarch64-apple-darwin.zip" ;;
    x86_64) DENO_ZIP="deno-x86_64-apple-darwin.zip" ;;
    *) echo "Unknown architecture $ARCH" >&2; exit 1 ;;
  esac
  echo "Downloading deno $DENO_VERSION ($DENO_ZIP)…"
  curl -fL --retry 3 -o "packaging/vendor/$DENO_ZIP" \
    "https://github.com/denoland/deno/releases/download/$DENO_VERSION/$DENO_ZIP"
  (cd packaging/vendor && unzip -o -q "$DENO_ZIP" && rm "$DENO_ZIP" && chmod +x deno)
fi
packaging/vendor/deno --version | head -1

# --- build the .app
rm -rf build "dist/Audio Ripper" "dist/Audio Ripper.app"
pyinstaller --noconfirm --clean --distpath dist --workpath build packaging/AudioRipper.spec

APP="dist/Audio Ripper.app"
# Ad-hoc sign the whole bundle so Apple silicon will run it.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP" && echo "Signature OK"

# Quick self-test: bundled ffmpeg works.
FF="$(find "$APP" -name 'ffmpeg-*' -type f | head -1)"
"$FF" -version | head -1

# Drag-and-drop library (tkdnd) must be inside the app.
if ! find "$APP" -path '*tkdnd*' -name '*.dylib' | grep -q .; then
  echo "ERROR: tkdnd (drag and drop) was not bundled." >&2
  exit 1
fi
echo "Drag-and-drop support bundled"

ZIP="dist/Audio-Ripper-macOS-$ARCH.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

echo
echo "Built:  $ROOT/$APP"
echo "Zip:    $ROOT/$ZIP"
echo "Drag 'Audio Ripper.app' into /Applications to install."
