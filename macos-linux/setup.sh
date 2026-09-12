#!/usr/bin/env bash
# Limelight Training Recorder setup for macOS and Linux.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FFMPEG_DIR="$PROJECT_DIR/tools/ffmpeg/bin"
FFMPEG_BIN="$FFMPEG_DIR/ffmpeg"

echo "Limelight Training Recorder setup"
echo "Project: $PROJECT_DIR"

find_python() {
    local candidate
    for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON="$(find_python || true)"
if [[ -z "$PYTHON" ]] && [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    echo "Python 3.10+ was not found. Installing Python with Homebrew..."
    brew install python
    PYTHON="$(find_python || true)"
fi
if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python 3.10 or newer is required." >&2
    echo "  macOS:         brew install python" >&2
    echo "  Debian/Ubuntu: sudo apt install python3" >&2
    echo "  Fedora:        sudo dnf install python3" >&2
    exit 1
fi
echo "Found $("$PYTHON" --version 2>&1)"

if command -v ffmpeg >/dev/null 2>&1; then
    echo "FFmpeg was found on PATH: $(command -v ffmpeg)"
elif [[ -x "$FFMPEG_BIN" ]]; then
    echo "FFmpeg is already installed locally."
elif [[ "$(uname -s)" == "Darwin" ]]; then
    if command -v brew >/dev/null 2>&1; then
        echo "FFmpeg was not found. Installing with Homebrew..."
        brew install ffmpeg
    else
        echo "ERROR: FFmpeg was not found and Homebrew is not installed." >&2
        echo "Install Homebrew from https://brew.sh and run this script again," >&2
        echo "or install FFmpeg manually and make sure 'ffmpeg' is on PATH." >&2
        exit 1
    fi
else
    case "$(uname -m)" in
        x86_64 | amd64) FFMPEG_ARCH="amd64" ;;
        aarch64 | arm64) FFMPEG_ARCH="arm64" ;;
        armv7l | armhf) FFMPEG_ARCH="armhf" ;;
        *)
            echo "ERROR: No static FFmpeg build is available for this architecture." >&2
            echo "Install FFmpeg with your package manager and make sure it is on PATH." >&2
            exit 1
            ;;
    esac
    URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FFMPEG_ARCH}-static.tar.xz"
    echo "FFmpeg was not found. Downloading the static Linux build ($FFMPEG_ARCH)..."
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$URL" -o "$TMP_DIR/ffmpeg.tar.xz"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$TMP_DIR/ffmpeg.tar.xz" "$URL"
    else
        echo "ERROR: curl or wget is required to download FFmpeg." >&2
        exit 1
    fi
    tar -xJf "$TMP_DIR/ffmpeg.tar.xz" -C "$TMP_DIR"
    mkdir -p "$FFMPEG_DIR"
    find "$TMP_DIR" -type f -name ffmpeg -exec cp {} "$FFMPEG_BIN" \;
    find "$TMP_DIR" -type f -name ffprobe -exec cp {} "$FFMPEG_DIR/ffprobe" \;
    chmod +x "$FFMPEG_BIN" "$FFMPEG_DIR/ffprobe"
    echo "Installed project-local FFmpeg at $FFMPEG_BIN"
fi

echo
echo "Setup complete. Start recording with:"
echo "  $PYTHON limelight_recorder.py start --fps 3"
echo "Or open the browser interface with:"
echo "  $PYTHON limelight_recorder.py web --open-browser"
