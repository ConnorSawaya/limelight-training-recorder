#!/usr/bin/env bash
# One-line installer for macOS and Linux:
#   curl -fsSL https://raw.githubusercontent.com/ConnorSawaya/limelight-training-recorder/main/install.sh | bash
set -euo pipefail

REPO="ConnorSawaya/limelight-training-recorder"
BRANCH="main"
DEST="${LIMELIGHT_DIR:-$PWD/limelight-training-recorder}"
ARCHIVE_URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"

if [[ -d "$DEST/.git" ]]; then
    echo "Updating the existing checkout in $DEST..."
    git -C "$DEST" pull --ff-only
elif [[ -f "$DEST/limelight_recorder.py" ]]; then
    echo "Using the existing folder $DEST."
else
    echo "Downloading limelight-training-recorder to $DEST..."
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$ARCHIVE_URL" -o "$TMP_DIR/repo.tar.gz"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$TMP_DIR/repo.tar.gz" "$ARCHIVE_URL"
    else
        echo "ERROR: curl or wget is required." >&2
        exit 1
    fi
    tar -xzf "$TMP_DIR/repo.tar.gz" -C "$TMP_DIR"
    mkdir -p "$DEST"
    cp -R "$TMP_DIR/limelight-training-recorder-$BRANCH/." "$DEST/"
fi

exec bash "$DEST/setup.sh"
