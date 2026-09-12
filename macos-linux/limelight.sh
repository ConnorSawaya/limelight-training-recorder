#!/usr/bin/env bash
# Shared macOS/Linux launcher. The small wrapper scripts call this file.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -n "${LIMELIGHT_PYTHON:-}" ]]; then
    PYTHON="$LIMELIGHT_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
else
    echo "ERROR: Python 3.10 or newer was not found. Run macos-linux/setup.sh first." >&2
    exit 1
fi

command_name="${1:-}"
shift || true
case "$command_name" in
    web) set -- web --open-browser "$@" ;;
    start) set -- start "$@" ;;
    foreground) set -- start --foreground "$@" ;;
    stop) set -- stop "$@" ;;
    status) set -- status "$@" ;;
    *)
        echo "Usage: $0 {web|start|foreground|stop|status} [options]" >&2
        exit 2
        ;;
esac

exec "$PYTHON" "$PROJECT_DIR/limelight_recorder.py" "$@"
