#!/usr/bin/env python3
"""Record a Limelight 3A USB network stream and sample training frames.

The recorder intentionally uses only the Python standard library.  FFmpeg is
the only runtime dependency and is resolved from the project-local tools folder
or from PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.error import URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "training_data"
DEFAULT_STATE_FILE = PROJECT_DIR / ".limelight_recorder_state.json"
DEFAULT_STOP_FILE = PROJECT_DIR / ".limelight_recorder_stop"
OVERLAY_STREAM_PORT = 5800
RAW_STREAM_PORT = 5802
# The raw endpoint is the default so training data is captured before the
# Limelight pipeline draws its targeting/diagnostic overlay.
STREAM_PORT = RAW_STREAM_PORT
WEB_PORT = 5801
# Limelight's USB-C connection appears to Windows as a network adapter.  Use
# that camera IP as the primary intake; the web dashboard can provide a local
# proxy fallback when the USB address is unavailable.
DEFAULT_STREAM_URL = f"http://172.28.0.1:{RAW_STREAM_PORT}/"
USER_AGENT = "LimelightTrainingRecorder/1.0"


class RecorderError(RuntimeError):
    """A user-facing recorder error."""


@dataclass(frozen=True)
class StreamInfo:
    """A reachable Limelight stream endpoint."""

    url: str
    host: str
    source: str


@dataclass(frozen=True)
class SessionPaths:
    """Files created for one recording session."""

    root: Path
    frames: Path
    frames_zip: Path
    video: Path
    metadata: Path
    ffmpeg_log: Path


def local_timestamp() -> str:
    """Return a filesystem-safe local timestamp."""

    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def format_fps(value: str | float) -> float:
    """Validate and normalize a frame sampling rate."""

    try:
        fps = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("FPS must be a number greater than 0") from exc
    if not 0 < fps <= 120:
        raise argparse.ArgumentTypeError("FPS must be greater than 0 and no more than 120")
    return fps


def fps_text(fps: float) -> str:
    """Format FPS for FFmpeg without locale-specific decimal separators."""

    return f"{fps:.6f}".rstrip("0").rstrip(".")


OUTPUT_MODES = {"both", "mp4", "images_zip", "both_zip"}


def validate_output_mode(value: str) -> str:
    """Validate the selected recording outputs."""

    mode = str(value or "both").strip().lower()
    if mode not in OUTPUT_MODES:
        allowed = ", ".join(sorted(OUTPUT_MODES))
        raise RecorderError(f"Output mode must be one of: {allowed}.")
    return mode


def normalize_host(host: str) -> tuple[str, Optional[int]]:
    """Accept a hostname, IP, or URL and return hostname plus optional port."""

    value = host.strip()
    if not value:
        raise RecorderError("The Limelight host cannot be empty.")
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    if not parsed.hostname:
        raise RecorderError(f"Invalid Limelight host: {host}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RecorderError(f"Invalid port in Limelight host: {host}") from exc
    return parsed.hostname, port


def stream_url_for_host(host: str, port: int = STREAM_PORT) -> str:
    """Build the Limelight MJPEG URL from a host or host:port value."""

    hostname, explicit_port = normalize_host(host)
    actual_port = explicit_port or port
    # Brackets are required if a user supplies an IPv6 host.
    display_host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    return f"http://{display_host}:{actual_port}"


def candidate_hosts(usb_index: int = 0) -> list[str]:
    """Return likely Limelight USB hostnames/IPs for Windows and legacy images."""

    if usb_index < 0:
        raise RecorderError("USB index must be zero or greater.")

    # Limelight documentation and the Limelight libraries have used multiple
    # USB gadget subnets across OS generations.  Trying all known values makes
    # this work with both current and older Limelight 3A images.
    hosts: list[str] = []
    for subnet in ("172.28", "172.26", "172.29", "172.27"):
        hosts.append(f"{subnet}.{usb_index}.1")
    hosts.extend(["limelight.local", "limelight"])
    return hosts


def _probe_stream(url: str, timeout: float = 1.5) -> bool:
    """Check that an HTTP endpoint looks like a live MJPEG stream."""

    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status < 200 or response.status >= 300:
                return False
            content_type = response.headers.get("Content-Type", "").lower()
            if any(
                marker in content_type
                for marker in ("multipart", "mjpeg", "jpeg", "image/")
            ):
                return True
            first_bytes = response.read(128)
            # Some firmware versions omit a useful Content-Type header.  A
            # JPEG start marker or multipart boundary is sufficient evidence.
            return b"\xff\xd8" in first_bytes or first_bytes.startswith(b"--")
    except (OSError, URLError, socket.timeout, TimeoutError):
        return False


def probe_stream_url(url: str, timeout: float = 1.5) -> bool:
    """Public wrapper used by the tests and discovery code."""

    return _probe_stream(url, timeout=timeout)


def discover_stream(
    host: Optional[str] = None,
    usb_index: int = 0,
    stream_url: Optional[str] = None,
    timeout: float = 1.5,
) -> StreamInfo:
    """Find a reachable Limelight stream, using parallel short probes."""

    if stream_url:
        normalized_url = stream_url.strip()
        if not normalized_url:
            raise RecorderError("The stream URL cannot be empty.")
        if not normalized_url.startswith(("http://", "https://")):
            normalized_url = f"http://{normalized_url}"
        if probe_stream_url(normalized_url, timeout=timeout):
            parsed = urlparse(normalized_url)
            return StreamInfo(normalized_url, parsed.hostname or normalized_url, "manual URL")
        raise RecorderError(
            f"The configured stream URL did not respond as an MJPEG stream: {normalized_url}"
        )

    hosts = [host] if host else candidate_hosts(usb_index)
    urls = [(candidate, stream_url_for_host(candidate)) for candidate in hosts]

    # Prefer the USB gadget addresses over hostnames.  This makes a successful
    # recording use the camera's direct USB-network feed even when a hostname
    # happens to resolve through another network interface.
    direct_urls = [(candidate, url) for candidate, url in urls if candidate.startswith("172.")]
    for candidate, url in direct_urls:
        if probe_stream_url(url, timeout=timeout):
            parsed = urlparse(url)
            return StreamInfo(url, parsed.hostname or candidate, "direct USB network")

    urls = [(candidate, url) for candidate, url in urls if not candidate.startswith("172.")]

    # Probing in parallel keeps a disconnected USB adapter from causing a long
    # serial wait while still using conservative per-host timeouts.
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        futures = {
            pool.submit(probe_stream_url, url, timeout): (candidate, url)
            for candidate, url in urls
        }
        for future in as_completed(futures):
            candidate, url = futures[future]
            try:
                if future.result():
                    parsed = urlparse(url)
                    return StreamInfo(url, parsed.hostname or candidate, "automatic discovery")
            except Exception:
                # A bad DNS response or malformed optional candidate should not
                # prevent the remaining known endpoints from being checked.
                continue

    attempted = ", ".join(url for _, url in urls)
    raise RecorderError(
        "No Limelight camera stream was detected.\n"
        "Make sure the Limelight 3A is powered on, fully booted, and connected "
        "with a data-capable USB-C cable (not flash mode). Wait up to 20 seconds "
        "after plugging it in, then try again.\n"
        f"Tried: {attempted}\n"
        "If the Limelight has a custom/static IP, use --host <IP> or open its "
        "web page at port 5801 to verify the address."
    )


def resolve_ffmpeg(explicit: Optional[str] = None) -> Path:
    """Resolve FFmpeg from an explicit path, project tools, or PATH."""

    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(PROJECT_DIR / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
    candidates.append(PROJECT_DIR / "tools" / "ffmpeg" / "bin" / "ffmpeg")
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(Path(which))

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    raise RecorderError(
        "FFmpeg was not found. Run setup_windows.bat once, or install FFmpeg and "
        "put ffmpeg.exe on PATH."
    )


def _session_prefix(prefix: str) -> str:
    """Keep generated session names readable and safe on every platform."""

    value = re.sub(r"[^A-Za-z0-9_-]+", "_", str(prefix or "limelight_data")).strip("_-")
    return value or "limelight_data"


def create_session(output_dir: str | Path, prefix: str = "limelight_raw_data") -> SessionPaths:
    """Create a numbered, timestamped training-data session directory."""

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = local_timestamp()
    safe_prefix = _session_prefix(prefix)
    recording_number = 1
    while True:
        session_root = root / f"{safe_prefix}_{timestamp}_{recording_number:02d}"
        try:
            session_root.mkdir()
            break
        except FileExistsError:
            recording_number += 1
    frames = session_root / "frames"
    frames.mkdir(parents=True)
    return SessionPaths(
        root=session_root,
        frames=frames,
        frames_zip=session_root / "frames.zip",
        video=session_root / "recording.mp4",
        metadata=session_root / "metadata.json",
        ffmpeg_log=session_root / "ffmpeg.log",
    )


def session_prefix_for_stream(stream_url: str) -> str:
    """Choose a clear output prefix for raw versus overlay recordings."""

    try:
        port = urlparse(stream_url).port
    except ValueError:
        port = None
    if port == RAW_STREAM_PORT:
        return "limelight_raw_data"
    if port == OVERLAY_STREAM_PORT:
        return "limelight_overlay_data"
    return "limelight_data"


def feed_type_for_stream(stream_url: str) -> str:
    """Return the user-facing feed type for a recorded stream URL."""

    return "overlay" if session_prefix_for_stream(stream_url) == "limelight_overlay_data" else "raw"


def recording_number(session: SessionPaths) -> int:
    """Read the sequential recording number from a generated session name."""

    match = re.search(r"_(\d+)$", session.root.name)
    return int(match.group(1)) if match else 1


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def ensure_not_running(state_file: Path) -> None:
    state = read_json(state_file) if state_file.exists() else {}
    pid = int(state.get("pid", 0) or 0)
    if pid and process_is_running(pid):
        raise RecorderError(
            f"A recording is already running (PID {pid}). Use the stop command first."
        )
    if state_file.exists():
        state_file.unlink(missing_ok=True)


def ffmpeg_command(
    ffmpeg: Path,
    stream_url: str,
    session: SessionPaths,
    fps: Optional[float],
    output_mode: str = "both",
) -> list[str]:
    """Build one FFmpeg command producing the selected recording outputs."""

    output_mode = validate_output_mode(output_mode)

    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rw_timeout",
        "5000000",
        "-i",
        stream_url,
    ]
    if output_mode in {"both", "both_zip", "mp4"}:
        command.extend(
            [
                "-map",
                "0:v:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
            ]
        )
        if fps is None:
            # Preserve the camera's incoming frame cadence instead of asking
            # FFmpeg to duplicate/drop frames to a guessed constant rate.
            command.extend(["-fps_mode", "passthrough"])
        else:
            command.extend(["-vf", f"fps={fps_text(fps)}"])
        command.append(str(session.video))
    if output_mode in {"both", "both_zip", "images_zip"}:
        command.extend(["-map", "0:v:0"])
        if fps is None:
            command.extend(["-fps_mode", "passthrough"])
        else:
            command.extend(["-vf", f"fps={fps_text(fps)}"])
        command.extend(
            [
                "-q:v",
                "2",
                str(session.frames / "frame_%06d.jpg"),
            ]
        )
    return command


def zip_frame_images(session: SessionPaths) -> int:
    """Bundle the saved JPG frames into a portable ZIP archive."""

    frame_paths = sorted(session.frames.glob("*.jpg"))
    with zipfile.ZipFile(session.frames_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for frame_path in frame_paths:
            archive.write(frame_path, arcname=frame_path.name)
    return len(frame_paths)


def request_ffmpeg_stop(process: subprocess.Popen[bytes]) -> None:
    """Ask FFmpeg to finalize its outputs, then use a hard stop if needed."""

    if process.poll() is not None:
        return
    try:
        if process.stdin:
            process.stdin.write(b"q\n")
            process.stdin.flush()
    except (OSError, ValueError):
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
        except (OSError, ValueError):
            process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run_session(
    stream: StreamInfo,
    ffmpeg: Path,
    output_dir: str | Path,
    fps: Optional[float],
    output_mode: str = "both",
    state_file: Path = DEFAULT_STATE_FILE,
    stop_file: Path = DEFAULT_STOP_FILE,
) -> int:
    """Run FFmpeg until stopped, then finalize metadata and cleanup state."""

    output_mode = validate_output_mode(output_mode)
    session_prefix = session_prefix_for_stream(stream.url)
    session = create_session(output_dir, session_prefix)
    command = ffmpeg_command(ffmpeg, stream.url, session, fps, output_mode)
    session_number = recording_number(session)
    feed_type = feed_type_for_stream(stream.url)
    metadata: dict[str, Any] = {
        "session_name": session.root.name,
        "recording_number": session_number,
        "feed_type": feed_type,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "stream_url": stream.url,
        "stream_host": stream.host,
        "discovery": stream.source,
        "frame_rate_fps": fps,
        "capture_mode": "maximum available" if fps is None else "sampled",
        "output_mode": output_mode,
        "video_file": session.video.name if output_mode in {"both", "both_zip", "mp4"} else None,
        "frames_directory": session.frames.name,
        "frames_zip": session.frames_zip.name if output_mode in {"both_zip", "images_zip"} else None,
        "ffmpeg_log": session.ffmpeg_log.name,
        "status": "recording",
    }
    write_json(session.metadata, metadata)

    state = read_json(state_file)
    state.update(
        {
            "pid": os.getpid(),
            "session_dir": str(session.root),
            "session_name": session.root.name,
            "recording_number": session_number,
            "feed_type": feed_type,
            "stream_url": stream.url,
            "fps": fps,
            "capture_mode": "maximum available" if fps is None else "sampled",
            "output_mode": output_mode,
            "status": "recording",
        }
    )
    write_json(state_file, state)
    stop_file.unlink(missing_ok=True)

    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    stopped_by_request = False
    return_code = 1
    print(f"Recording Limelight stream: {stream.url}")
    print(f"Saving session to: {session.root}")
    if fps is None:
        print("Saving every incoming camera frame (maximum available FPS)")
    else:
        print(f"Sampling JPG frames at {fps_text(fps)} FPS")
    print(f"Output mode: {output_mode}")
    print("Stop with stop_recorder.bat, or press Ctrl+C in foreground mode.")

    with session.ffmpeg_log.open("ab") as log_file:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
        except OSError as exc:
            error_message = f"Could not start FFmpeg: {exc}"
            metadata.update(
                {
                    "ended_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "status": "error",
                    "error": error_message,
                    "frames_saved": 0,
                    "video_saved": False,
                }
            )
            write_json(session.metadata, metadata)
            state_file.unlink(missing_ok=True)
            stop_file.unlink(missing_ok=True)
            raise RecorderError(error_message) from exc
        try:
            while True:
                if stop_file.exists():
                    stopped_by_request = True
                    request_ffmpeg_stop(process)
                    break
                return_code = process.poll()
                if return_code is not None:
                    break
                time.sleep(0.25)
        except KeyboardInterrupt:
            stopped_by_request = True
            request_ffmpeg_stop(process)
        finally:
            if process.poll() is None:
                request_ffmpeg_stop(process)

    frame_count = sum(1 for _ in session.frames.glob("*.jpg"))
    zipped_count = 0
    if output_mode in {"both_zip", "images_zip"}:
        zipped_count = zip_frame_images(session)
    metadata.update(
        {
            "ended_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "stopped" if stopped_by_request else ("complete" if return_code == 0 else "error"),
            "ffmpeg_exit_code": process.returncode,
            "frames_saved": frame_count,
            "frames_zipped": zipped_count,
            "video_saved": session.video.exists(),
            "zip_saved": session.frames_zip.exists(),
        }
    )
    write_json(session.metadata, metadata)

    current_state = read_json(state_file)
    if int(current_state.get("pid", 0) or 0) == os.getpid():
        state_file.unlink(missing_ok=True)
    stop_file.unlink(missing_ok=True)

    if not stopped_by_request and process.returncode not in (0, None):
        raise RecorderError(
            f"FFmpeg stopped with exit code {process.returncode}. "
            f"See {session.ffmpeg_log} for details."
        )
    print(f"Saved {frame_count} JPG frame(s) to: {session.frames}")
    if session.frames_zip.exists():
        print(f"Saved JPG ZIP: {session.frames_zip}")
    if session.video.exists():
        print(f"Saved video: {session.video}")
    return 0


def start_recording(args: argparse.Namespace) -> int:
    """Preflight and start a foreground or background recording."""

    state_file = Path(args.state_file).expanduser().resolve()
    stop_file = Path(args.stop_file).expanduser().resolve()
    ensure_not_running(state_file)
    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    fps = None if args.max_fps else args.fps
    output_mode = validate_output_mode(args.output_mode)
    stream = discover_stream(
        host=args.host,
        usb_index=args.usb_index,
        stream_url=args.stream_url,
        timeout=args.timeout,
    )
    stop_file.unlink(missing_ok=True)

    if args.foreground:
        return run_session(stream, ffmpeg, args.output_dir, fps, output_mode, state_file, stop_file)

    state = {
        "pid": 0,
        "status": "starting",
        "stream_url": stream.url,
        "feed_type": feed_type_for_stream(stream.url),
        "fps": fps,
        "capture_mode": "maximum available" if fps is None else "sampled",
        "output_mode": output_mode,
    }
    write_json(state_file, state)
    child_command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--stream-url",
        stream.url,
    ]
    if fps is None:
        child_command.extend(["--max-fps"])
    else:
        child_command.extend(["--fps", fps_text(fps)])
    child_command.extend(
        [
            "--output-dir",
            str(Path(args.output_dir).expanduser().resolve()),
            "--ffmpeg",
            str(ffmpeg),
            "--state-file",
            str(state_file),
            "--stop-file",
            str(stop_file),
            "--output-mode",
            output_mode,
        ]
    )
    creation_flags = 0
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
        popen_kwargs["creationflags"] = creation_flags
    else:
        popen_kwargs["start_new_session"] = True

    try:
        child = subprocess.Popen(child_command, **popen_kwargs)
    except OSError as exc:
        state_file.unlink(missing_ok=True)
        raise RecorderError(f"Could not start the background recorder: {exc}") from exc
    state["pid"] = child.pid
    write_json(state_file, state)
    print(f"Recording started in the background (PID {child.pid}).")
    print(f"Stream: {stream.url}")
    print(f"Stop with: {Path(__file__).resolve().parent / 'stop_recorder.bat'}")
    return 0


def stop_recording(state_file: Path = DEFAULT_STATE_FILE, stop_file: Path = DEFAULT_STOP_FILE) -> int:
    """Request a running recorder to stop and wait for cleanup."""

    state = read_json(state_file) if state_file.exists() else {}
    pid = int(state.get("pid", 0) or 0)
    if not pid or not process_is_running(pid):
        state_file.unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)
        print("No active Limelight recording was found.")
        return 0

    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.touch()
    print(f"Stopping recording (PID {pid})...")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and process_is_running(pid):
        time.sleep(0.25)

    if process_is_running(pid):
        # The recorder normally notices the stop file quickly.  This fallback
        # prevents a hung FFmpeg/network process from leaving a stale state.
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        time.sleep(1)
        state_file.unlink(missing_ok=True)
        print("The recorder did not exit cleanly; its process was terminated.")
    else:
        print("Recording stopped and files finalized.")
    stop_file.unlink(missing_ok=True)
    return 0


def show_status(state_file: Path = DEFAULT_STATE_FILE) -> int:
    """Print a small machine-readable/human-readable status."""

    if not state_file.exists():
        print("No active Limelight recording.")
        return 0
    state = read_json(state_file)
    pid = int(state.get("pid", 0) or 0)
    if pid and process_is_running(pid):
        print(f"Recording active (PID {pid})")
        print(json.dumps(state, indent=2))
    else:
        print("No active Limelight recording (stale state cleaned up).")
        state_file.unlink(missing_ok=True)
    return 0


def add_recording_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fps", type=format_fps, default=3.0, help="Recording frame rate; default: 3")
    parser.add_argument(
        "--max-fps",
        action="store_true",
        help="Save every incoming camera frame instead of sampling at --fps",
    )
    parser.add_argument(
        "--output-mode",
        choices=sorted(OUTPUT_MODES),
        default="both",
        help="Save both MP4 and JPGs, MP4 only, or JPGs with an optional ZIP",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Training-data root folder")
    parser.add_argument("--ffmpeg", help="Path to ffmpeg.exe; setup installs a local copy automatically")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help=argparse.SUPPRESS)
    parser.add_argument("--stop-file", default=str(DEFAULT_STOP_FILE), help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record Limelight 3A USB camera training data.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Detect the Limelight and start recording")
    add_recording_options(start)
    start.add_argument("--host", help="Manual Limelight host/IP override")
    start.add_argument(
        "--stream-url",
        help=(
            "Exact MJPEG URL to use, for example "
            f"{DEFAULT_STREAM_URL}; skips automatic discovery"
        ),
    )
    start.add_argument("--usb-index", type=int, default=0, help="USB camera index; default: 0")
    start.add_argument("--timeout", type=float, default=1.5, help=argparse.SUPPRESS)
    start.add_argument("--foreground", action="store_true", help="Keep the recorder in this console")

    run = subparsers.add_parser("run", help="internal recorder worker")
    add_recording_options(run)
    run.add_argument("--stream-url", required=True, help=argparse.SUPPRESS)

    stop = subparsers.add_parser("stop", help="Gracefully stop the active recording")
    stop.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help=argparse.SUPPRESS)
    stop.add_argument("--stop-file", default=str(DEFAULT_STOP_FILE), help=argparse.SUPPRESS)

    status = subparsers.add_parser("status", help="Show whether a recording is active")
    status.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help=argparse.SUPPRESS)

    web = subparsers.add_parser("web", help="Run the local browser control interface")
    web.add_argument("--bind", default="127.0.0.1", help="Local interface address; default: 127.0.0.1")
    web.add_argument("--port", type=int, default=8080, help="Local interface port; default: 8080")
    web.add_argument(
        "--stream-url",
        default=DEFAULT_STREAM_URL,
        help=f"Default Limelight MJPEG URL; default: {DEFAULT_STREAM_URL}",
    )
    web.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Training-data root folder")
    web.add_argument("--ffmpeg", help="Path to ffmpeg.exe; setup installs a local copy automatically")
    web.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help=argparse.SUPPRESS)
    web.add_argument("--stop-file", default=str(DEFAULT_STOP_FILE), help=argparse.SUPPRESS)
    web.add_argument(
        "--output-mode",
        choices=sorted(OUTPUT_MODES),
        default="both",
        help="Default output mode for the browser interface",
    )
    web.add_argument("--open-browser", action="store_true", help="Open the control page automatically")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "start":
            return start_recording(args)
        if args.command == "run":
            parsed = urlparse(args.stream_url)
            stream = StreamInfo(
                args.stream_url,
                parsed.hostname or args.stream_url,
                "automatic discovery",
            )
            return run_session(
                stream,
                resolve_ffmpeg(args.ffmpeg),
                args.output_dir,
                None if args.max_fps else args.fps,
                args.output_mode,
                Path(args.state_file).expanduser().resolve(),
                Path(args.stop_file).expanduser().resolve(),
            )
        if args.command == "stop":
            return stop_recording(Path(args.state_file).expanduser().resolve(), Path(args.stop_file).expanduser().resolve())
        if args.command == "status":
            return show_status(Path(args.state_file).expanduser().resolve())
        if args.command == "web":
            # Import only for the web command so the recorder remains usable
            # as a small standard-library CLI tool.
            from web_interface import run_server

            return run_server(args)
    except RecorderError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
