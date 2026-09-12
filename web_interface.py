"""Local browser control panel for the Limelight training recorder.

The server intentionally binds to 127.0.0.1 by default.  The Limelight stream
is read by the existing FFmpeg worker; this module only provides a small local
HTTP API and a dependency-free browser page for starting and stopping it.
"""

from __future__ import annotations

import argparse
import json
import math
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from limelight_recorder import (
    DEFAULT_STATE_FILE,
    DEFAULT_STOP_FILE,
    DEFAULT_STREAM_URL,
    RecorderError,
    StreamInfo,
    OVERLAY_STREAM_PORT,
    RAW_STREAM_PORT,
    STREAM_PORT,
    candidate_hosts,
    discover_stream,
    process_is_running,
    read_json,
    start_recording,
    stop_recording,
    write_json,
    validate_output_mode,
)


SETTINGS_FILE = Path(__file__).resolve().parent / ".limelight_web_settings.json"
LIMELIGHT_API_PORT = 5807
LIMELIGHT_WEB_PORT = 5801

PIPELINE_TYPES = {
    "pipe_color": "Color/Retroreflective",
    "pipe_python": "Python SnapScript",
    "pipe_pythonpro": "Python SnapScriptPro",
    "pipe_fiducial": "AprilTags",
    "pipe_classifier": "Neural Classifier",
    "pipe_detector": "Neural Detector",
    "pipe_barcode": "Barcodes",
    "pipe_viewfinder": "Viewfinder",
    "pipe_focus": "Focus",
}

RESOLUTIONS = {
    0: "640x480 90fps",
    1: "320x240 90fps",
    2: "960x720 40fps",
    3: "1280x960 40fps",
    4: "320x240 90fps - 2x Hardware Zoom",
    5: "320x240 40fps - 3x Hardware Zoom",
    6: "2592x1944 (5MP) 10fps",
}

ORIENTATIONS = {
    0: "Normal",
    1: "Upside-Down",
    2: "Clockwise 90",
    3: "Counter-Clockwise 90",
    4: "Mirror Horizontal",
    5: "Mirror Vertical",
}

FLICKER_MODES = {0: "None", 1: "50hz", 2: "60hz"}
FEED_TYPES = {"raw", "overlay"}


def validate_feed_type(value: Any) -> str:
    """Validate the dashboard's camera-feed choice."""

    feed_type = str(value or "raw").strip().lower()
    if feed_type not in FEED_TYPES:
        raise RecorderError("Choose either the raw camera feed or the processed overlay feed.")
    return feed_type


def _display_host(hostname: str) -> str:
    return f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname


def limelight_api_bases(source_url: str) -> list[str]:
    """Return the Limelight JSON-service URLs for a stream URL."""

    value = source_url.strip()
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    if not parsed.hostname:
        raise RecorderError(f"Invalid Limelight stream URL: {source_url}")

    hosts = [parsed.hostname]
    # limelight.local is normally enough, but the USB gadget address is useful
    # when Windows name resolution is slow or unavailable.
    if parsed.hostname.lower() in {"limelight.local", "limelight"}:
        hosts.extend(["172.28.0.1", "172.26.0.1"])
    elif parsed.hostname.startswith("172."):
        hosts.extend(["limelight.local", "172.28.0.1"])

    bases: list[str] = []
    for host in hosts:
        base = f"http://{_display_host(host)}:{LIMELIGHT_API_PORT}"
        if base not in bases:
            bases.append(base)
    return bases


class LimelightApi:
    """Small standard-library client for the Limelight camera service."""

    def __init__(self, bases: list[str], timeout: float = 3.0):
        self.bases = bases
        self.timeout = timeout
        self.base_url = ""

    def _request(self, base: str, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        data = None
        headers = {"Accept": "application/json", "User-Agent": "LimelightTrainingRecorder/1.0"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{base}{path}", headers=headers, data=data, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            raise RecorderError(
                f"Limelight settings request failed ({exc.code}) at {base}{path}"
                + (f": {detail[:240]}" if detail else ".")
            ) from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise RecorderError(f"Could not reach Limelight settings at {base}: {exc}") from exc

        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body.decode("utf-8", errors="replace").strip()

    def _find_working_base(self) -> str:
        errors: list[str] = []
        for base in self.bases:
            try:
                status = self._request(base, "/status")
                if isinstance(status, dict):
                    self.base_url = base
                    return base
                errors.append(f"{base}: invalid status response")
            except RecorderError as exc:
                errors.append(str(exc))
        raise RecorderError(
            "Could not reach the Limelight camera settings service.\n"
            "Make sure the Limelight is powered on, fully booted, and connected over USB-C.\n"
            + "\n".join(errors)
        )

    @staticmethod
    def _int_value(profile: dict[str, Any], key: str, default: int = 0) -> int:
        try:
            return int(profile.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float_value(profile: dict[str, Any], key: str, default: float = 0.0) -> float:
        try:
            value = float(profile.get(key, default))
            return value if math.isfinite(value) else default
        except (TypeError, ValueError):
            return default

    def get_camera_settings(self) -> dict[str, Any]:
        base = self._find_working_base()
        status = self._request(base, "/status")
        pipeline_index = self._int_value(status, "pipelineIndex", 0)
        profile = self._request(base, f"/pipeline-atindex?index={pipeline_index}")
        if not isinstance(profile, dict):
            raise RecorderError("The Limelight returned an invalid pipeline settings response.")
        pipeline_type = str(profile.get("pipeline_type") or "pipe_viewfinder")
        measured_fps = self._float_value(status, "fps", 0.0)
        return {
            "api_base_url": base,
            "device_web_url": base.rsplit(":", 1)[0] + f":{LIMELIGHT_WEB_PORT}/",
            "pipeline_index": pipeline_index,
            "fps": measured_fps,
            "pipeline_type": pipeline_type,
            "pipeline_type_label": PIPELINE_TYPES.get(pipeline_type, pipeline_type),
            "source_image": self._int_value(profile, "image_source"),
            "resolution": self._int_value(profile, "pipeline_res"),
            "orientation": self._int_value(profile, "image_flip"),
            "exposure": self._float_value(profile, "exposure"),
            "black_level_offset": self._int_value(profile, "black_level"),
            "sensor_gain": self._float_value(profile, "lcgain"),
            "flicker_correction": self._int_value(profile, "flicker"),
            "red_balance": self._float_value(profile, "red_balance"),
            "blue_balance": self._float_value(profile, "blue_balance"),
        }

    @staticmethod
    def _number(payload: dict[str, Any], key: str, minimum: float, maximum: float, integer: bool = False) -> int | float:
        try:
            value = float(payload[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise RecorderError(f"Limelight setting '{key}' must be a number.") from exc
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise RecorderError(f"Limelight setting '{key}' must be between {minimum:g} and {maximum:g}.")
        if integer:
            if value != int(value):
                raise RecorderError(f"Limelight setting '{key}' must be a whole number.")
            return int(value)
        return value

    def save_camera_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_camera_settings()
        updates: dict[str, Any] = {}
        if "pipeline_type" in payload:
            pipeline_type = str(payload["pipeline_type"] or "")
            if pipeline_type not in PIPELINE_TYPES:
                raise RecorderError("Choose a valid Limelight pipeline type.")
            updates["pipeline_type"] = pipeline_type
        if "source_image" in payload:
            updates["image_source"] = self._number(payload, "source_image", 0, 1, integer=True)
        if "resolution" in payload:
            updates["pipeline_res"] = self._number(payload, "resolution", 0, max(RESOLUTIONS), integer=True)
        if "orientation" in payload:
            updates["image_flip"] = self._number(payload, "orientation", 0, max(ORIENTATIONS), integer=True)
        if "exposure" in payload:
            updates["exposure"] = self._number(payload, "exposure", 0, 3300)
        if "black_level_offset" in payload:
            updates["black_level"] = self._number(payload, "black_level_offset", 0, 1000, integer=True)
        if "sensor_gain" in payload:
            updates["lcgain"] = self._number(payload, "sensor_gain", 0, 100)
        if "flicker_correction" in payload:
            updates["flicker"] = self._number(payload, "flicker_correction", 0, max(FLICKER_MODES), integer=True)
        if "red_balance" in payload:
            updates["red_balance"] = self._number(payload, "red_balance", 0, 4095)
        if "blue_balance" in payload:
            updates["blue_balance"] = self._number(payload, "blue_balance", 0, 4095)
        if not updates:
            raise RecorderError("No Limelight camera settings were provided.")

        self._request(current["api_base_url"], "/update-pipeline?flush=1", "POST", updates)
        return self.get_camera_settings()


PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Limelight Training Recorder</title>
  <style>
    :root { color-scheme: dark; --bg: #11151b; --panel: #1b222c; --line: #303b49; --text: #edf2f7; --muted: #a8b3c0; --blue: #55a8ff; --green: #35d07f; --red: #ff6d72; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; padding-right: 380px; background: radial-gradient(circle at top right, #20334a, var(--bg) 48%); color: var(--text); font: 15px/1.45 Segoe UI, system-ui, sans-serif; }
    main { width: min(1080px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 48px; }
    h1 { margin: 0 0 6px; font-size: clamp(25px, 4vw, 38px); letter-spacing: -.02em; }
    h2 { margin: 0 0 16px; font-size: 18px; }
    .pipeline { margin: 0 0 18px; padding: 11px 15px; border: 1px solid #31506b; border-radius: 10px; background: #142333; }
    .pipeline-title { border: 1px solid #36cf80; border-radius: 999px; color: #8ff0b8; padding: 4px 9px; font-size: 11px; font-weight: 800; letter-spacing: .07em; }
    .settings-sidebar { position: fixed; inset: 0 0 0 auto; z-index: 10; width: 380px; overflow-y: auto; padding: 22px; background: #171e27; border-left: 1px solid var(--line); box-shadow: -12px 0 36px #0008; transition: width .18s ease, padding .18s ease; }
    .settings-sidebar.collapsed { width: 44px; padding: 12px 8px; overflow: hidden; }
    .settings-sidebar.collapsed .settings-content { display: none; }
    .settings-collapse { width: 28px; height: 32px; padding: 2px; color: var(--text); background: #344252; }
    .settings-sidebar.collapsed .settings-collapse { display: block; margin: 0 auto; }
    .settings-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
    .settings-header h2 { margin: 0; }
    .setting-group { margin-top: 18px; padding-top: 18px; border-top: 1px solid var(--line); }
    .setting-group h3 { margin: 0 0 12px; font-size: 15px; }
    .camera-state { display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 12px; margin: 0 0 10px; }
    .camera-state strong { color: var(--text); text-align: right; overflow-wrap: anywhere; }
    .camera-state.ok strong { color: var(--green); }
    .camera-state.bad strong { color: var(--red); }
    .setting-note { color: var(--muted); font-size: 12px; margin: 9px 0 0; }
    .setting-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .layout { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(290px, .7fr); gap: 18px; align-items: start; }
    .panel { background: color-mix(in srgb, var(--panel) 94%, transparent); border: 1px solid var(--line); border-radius: 14px; padding: 20px; box-shadow: 0 12px 36px #0004; }
    .output-settings { margin-top: 18px; }
    .output-heading { display: flex; align-items: start; justify-content: space-between; gap: 18px; }
    .output-heading h2 { margin-bottom: 0; }
    .save-state { color: var(--green); font-size: 13px; min-height: 20px; text-align: right; }
    .output-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 18px; }
    .preview { padding: 10px; }
    .preview-box { aspect-ratio: 16 / 10; display: grid; place-items: center; overflow: hidden; border-radius: 9px; background: #080a0d; border: 1px solid #26303b; }
    #preview { display: block; width: 100%; height: 100%; object-fit: contain; }
    .empty { color: var(--muted); text-align: center; padding: 30px; }
    label { display: block; color: var(--muted); font-size: 13px; margin: 15px 0 6px; }
    input, select { width: 100%; border: 1px solid #465465; border-radius: 7px; background: #11161d; color: var(--text); padding: 10px 11px; font: inherit; }
    input:focus { outline: 2px solid #55a8ff66; border-color: var(--blue); }
    select:focus { outline: 2px solid #55a8ff66; border-color: var(--blue); }
    .row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }
    button { border: 0; border-radius: 7px; color: #07111b; background: var(--blue); cursor: pointer; padding: 10px 16px; font-weight: 700; font: inherit; }
    button:hover { filter: brightness(1.08); }
    button:disabled { cursor: not-allowed; opacity: .45; }
    button.secondary { color: var(--text); background: #344252; }
    button.stop { color: #230b0d; background: var(--red); }
    button.save { color: var(--text); background: #51657b; }
    .status-line { display: flex; align-items: center; gap: 9px; margin-bottom: 17px; }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: #8995a3; }
    .dot.ok { background: var(--green); box-shadow: 0 0 10px #35d07f88; }
    .dot.bad { background: var(--red); box-shadow: 0 0 10px #ff6d7288; }
    .dot.busy { background: #ffd166; box-shadow: 0 0 10px #ffd16688; }
    .message { white-space: pre-wrap; color: var(--muted); min-height: 44px; }
    .error { color: #ff9c9f; }
    .details { border-top: 1px solid var(--line); margin-top: 18px; padding-top: 15px; color: var(--muted); font-size: 13px; }
    .details div { display: flex; justify-content: space-between; gap: 14px; margin: 7px 0; }
    .details span:last-child { color: var(--text); text-align: right; overflow-wrap: anywhere; }
    .hint { color: var(--muted); font-size: 12px; margin: 8px 0 0; }
    @media (max-width: 780px) { body { padding-right: 0; } .layout, .output-grid { grid-template-columns: 1fr; } .settings-sidebar { width: min(380px, 92vw); } }
  </style>
</head>
<body>
  <aside id="settingsSidebar" class="settings-sidebar" aria-label="Settings">
    <div class="settings-header">
      <h2>Settings</h2>
      <button id="settingsCollapse" class="settings-collapse" aria-expanded="true" title="Collapse settings">&gt;</button>
    </div>
    <div class="settings-content">
      <div class="setting-group">
        <h3>Limelight camera</h3>
        <div id="cameraState" class="camera-state"><span>Connection</span><strong>Loading...</strong></div>
        <div class="camera-state"><span>Camera stream FPS</span><strong id="cameraFps">-</strong></div>
        <div class="camera-state"><span>Active pipeline</span><strong id="pipelineIndex">-</strong></div>
        <label for="pipelineType">Pipeline type</label>
        <select id="pipelineType">
          <option value="pipe_color">Color/Retroreflective</option>
          <option value="pipe_python">Python SnapScript</option>
          <option value="pipe_pythonpro">Python SnapScriptPro</option>
          <option value="pipe_fiducial">AprilTags</option>
          <option value="pipe_classifier">Neural Classifier</option>
          <option value="pipe_detector">Neural Detector</option>
          <option value="pipe_barcode">Barcodes</option>
          <option value="pipe_viewfinder">Viewfinder</option>
          <option value="pipe_focus">Focus</option>
        </select>
        <label for="sourceImage">Source image</label>
        <select id="sourceImage">
          <option value="0">Camera</option>
          <option value="1">Snapshot</option>
        </select>
        <label for="resolution">Resolution / camera FPS</label>
        <select id="resolution">
          <option value="0">640x480 90fps</option>
          <option value="1">320x240 90fps</option>
          <option value="2">960x720 40fps</option>
          <option value="3">1280x960 40fps</option>
          <option value="4">320x240 90fps - 2x Hardware Zoom</option>
          <option value="5">320x240 40fps - 3x Hardware Zoom</option>
          <option value="6">2592x1944 (5MP) 10fps</option>
        </select>
        <label for="orientation">Stream orientation</label>
        <select id="orientation">
          <option value="0">Normal</option>
          <option value="1">Upside-Down</option>
          <option value="2">Clockwise 90</option>
          <option value="3">Counter-Clockwise 90</option>
          <option value="4">Mirror Horizontal</option>
          <option value="5">Mirror Vertical</option>
        </select>
        <label for="exposure">Exposure (.01 ms)</label>
        <input id="exposure" type="number" min="0" max="3300" step="1">
        <label for="blackLevel">Black level offset</label>
        <input id="blackLevel" type="number" min="0" max="1000" step="1">
        <label for="sensorGain">Sensor gain</label>
        <input id="sensorGain" type="number" min="0" max="100" step="0.1">
        <label for="flicker">Flicker correction</label>
        <select id="flicker">
          <option value="0">None</option>
          <option value="1">50hz</option>
          <option value="2">60hz</option>
        </select>
        <label for="redBalance">Red balance</label>
        <input id="redBalance" type="number" min="0" max="4095" step="1">
        <label for="blueBalance">Blue balance</label>
        <input id="blueBalance" type="number" min="0" max="4095" step="1">
        <div class="row setting-actions">
          <button id="refreshCamera" class="secondary">Refresh camera settings</button>
          <button id="saveCamera" class="save">Save to Limelight</button>
        </div>
      </div>
      <div class="setting-group">
        <h3>Recording input</h3>
        <label for="feedType">Camera feed to record</label>
        <select id="feedType">
          <option value="raw" selected>Raw camera feed (before overlay)</option>
          <option value="overlay">Processed feed (with overlay)</option>
        </select>
        <p class="setting-note">The recorder automatically finds the Limelight over USB-C and uses the proxy only as a backup.</p>
      </div>
    </div>
  </aside>
  <main>
    <h1>Limelight Training Recorder</h1>
    <div class="pipeline">
      <span id="pipelineTitle" class="pipeline-title">USB DIRECT</span>
    </div>
    <div class="layout">
      <section class="panel preview">
        <h2>Live camera feed</h2>
        <div class="preview-box">
          <img id="preview" alt="Limelight camera preview" hidden>
          <div id="empty" class="empty">Click Check connection to find the Limelight.</div>
        </div>
      </section>
      <section class="panel">
        <h2>Recording controls</h2>
        <div class="status-line"><span id="dot" class="dot"></span><span id="status">Not recording</span></div>
        <div class="row">
          <button id="check" class="secondary">Check connection</button>
          <button id="start">Start recording</button>
          <button id="stop" class="stop" disabled>Stop recording</button>
        </div>
        <p id="message" class="message" role="status"></p>
        <div class="details">
          <div><span>Current session</span><span id="session">-</span></div>
          <div><span>Recording number</span><span id="recordingNumber">-</span></div>
          <div><span>Intake path</span><span id="intakeMode">USB direct preferred</span></div>
          <div><span>Frames saved</span><span id="frames">-</span></div>
          <div><span>Capture mode</span><span id="captureMode">Limelight camera rate</span></div>
          <div><span>Output root</span><span id="output">training_data</span></div>
        </div>
      </section>
    </div>
    <section class="panel output-settings">
      <div class="output-heading">
        <div>
          <h2>Recording output</h2>
          <p class="setting-note">These settings save automatically and apply to the next recording.</p>
        </div>
        <span id="saveState" class="save-state" role="status"></span>
      </div>
      <div class="output-grid">
        <div>
          <label for="outputMode">Save output as</label>
          <select id="outputMode">
            <option value="both">MP4 + JPG frames</option>
            <option value="mp4">MP4 only</option>
            <option value="images_zip">JPG images + ZIP only</option>
            <option value="both_zip">MP4 + JPG images + ZIP</option>
          </select>
        </div>
        <div>
          <label for="outputDir">Output folder</label>
          <input id="outputDir" type="text" value="training_data" spellcheck="false">
        </div>
      </div>
    </section>
  </main>
  <script>
    const settingsSidebar = document.getElementById('settingsSidebar');
    const settingsCollapse = document.getElementById('settingsCollapse');
    const feedType = document.getElementById('feedType');
    const preview = document.getElementById('preview');
    const empty = document.getElementById('empty');
    const start = document.getElementById('start');
    const stop = document.getElementById('stop');
    const check = document.getElementById('check');
    const refreshCamera = document.getElementById('refreshCamera');
    const saveCamera = document.getElementById('saveCamera');
    const outputMode = document.getElementById('outputMode');
    const outputDir = document.getElementById('outputDir');
    const cameraState = document.getElementById('cameraState');
    const cameraFps = document.getElementById('cameraFps');
    const pipelineIndex = document.getElementById('pipelineIndex');
    const pipelineType = document.getElementById('pipelineType');
    const sourceImage = document.getElementById('sourceImage');
    const resolution = document.getElementById('resolution');
    const orientation = document.getElementById('orientation');
    const exposure = document.getElementById('exposure');
    const blackLevel = document.getElementById('blackLevel');
    const sensorGain = document.getElementById('sensorGain');
    const flicker = document.getElementById('flicker');
    const redBalance = document.getElementById('redBalance');
    const blueBalance = document.getElementById('blueBalance');
    const dot = document.getElementById('dot');
    const status = document.getElementById('status');
    const message = document.getElementById('message');
    const session = document.getElementById('session');
    const recordingNumber = document.getElementById('recordingNumber');
    const frames = document.getElementById('frames');
    const captureMode = document.getElementById('captureMode');
    const output = document.getElementById('output');
    const intakeMode = document.getElementById('intakeMode');
    const pipelineTitle = document.getElementById('pipelineTitle');
    const saveState = document.getElementById('saveState');
    let saveTimer = null;
    let activePreviewUrl = '';

    function setSettingsCollapsed(collapsed) {
      settingsSidebar.classList.toggle('collapsed', collapsed);
      settingsCollapse.setAttribute('aria-expanded', String(!collapsed));
      settingsCollapse.textContent = collapsed ? '<' : '>';
      settingsCollapse.title = collapsed ? 'Expand settings' : 'Collapse settings';
    }

    function setMessage(text, isError = false) {
      message.textContent = text || '';
      message.className = isError ? 'message error' : 'message';
    }
    function updatePreview(url = activePreviewUrl) {
      activePreviewUrl = url || '';
      if (!url) { preview.hidden = true; empty.hidden = false; return; }
      preview.src = url;
      preview.hidden = false;
      empty.hidden = true;
    }
    function updateStatus(data) {
      const active = Boolean(data.recording);
      const starting = data.status === 'starting';
      dot.className = 'dot ' + (active ? 'ok' : starting ? 'busy' : '');
      status.textContent = active ? 'Recording' : starting ? 'Starting...' : 'Not recording';
      start.disabled = active || starting;
      stop.disabled = !active && !starting;
      if (data.recording && data.stream_url) {
        updatePreview(data.stream_url);
      }
      if (data.intake_mode) {
        intakeMode.textContent = data.intake_mode;
        pipelineTitle.textContent = data.intake_mode.toLowerCase().includes('proxy') ? 'PROXY BACKUP' : 'USB DIRECT';
      }
      if (data.capture_mode) captureMode.textContent = data.capture_mode === 'maximum available' ? 'Limelight camera rate' : data.capture_mode;
      if (data.session_dir) session.textContent = data.session_dir;
      if (data.recording_number) recordingNumber.textContent = '#' + data.recording_number;
      if (data.latest_session && data.latest_session.recording_number) {
        recordingNumber.textContent = '#' + data.latest_session.recording_number;
      }
      if (data.latest_session && data.latest_session.frames_saved !== undefined) frames.textContent = data.latest_session.frames_saved;
      if (data.output_dir) output.textContent = data.output_dir;
      if (!active && data.latest_session && data.latest_session.status === 'error' && data.latest_session.error) {
        setMessage(data.latest_session.error, true);
      }
    }
    function collectSettings() {
      return {feed_type: feedType.value, output_mode: outputMode.value, output_dir: outputDir.value.trim()};
    }
    function applySettings(settings) {
      if (settings.feed_type) feedType.value = settings.feed_type;
      if (settings.output_mode) outputMode.value = settings.output_mode;
      if (settings.output_dir) outputDir.value = settings.output_dir;
    }
    async function loadSettings() {
      try { const data = await callApi('/api/settings'); applySettings(data.settings); }
      catch (error) { setMessage('Could not load saved settings: ' + error.message, true); }
    }
    async function saveSettings() {
      saveState.textContent = 'Saving...';
      try {
        const data = await callApi('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(collectSettings())});
        applySettings(data.settings);
        saveState.textContent = 'Saved';
      } catch (error) {
        saveState.textContent = 'Could not save: ' + error.message;
      }
    }
    function scheduleSaveSettings() {
      saveState.textContent = 'Pending...';
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = setTimeout(saveSettings, 500);
    }
    function cameraFields() {
      return {
        pipeline_type: pipelineType.value,
        source_image: sourceImage.value,
        resolution: resolution.value,
        orientation: orientation.value,
        exposure: exposure.value,
        black_level_offset: blackLevel.value,
        sensor_gain: sensorGain.value,
        flicker_correction: flicker.value,
        red_balance: redBalance.value,
        blue_balance: blueBalance.value
      };
    }
    function applyCameraSettings(settings) {
      if (settings.pipeline_type) pipelineType.value = settings.pipeline_type;
      sourceImage.value = String(settings.source_image ?? 0);
      resolution.value = String(settings.resolution ?? 0);
      orientation.value = String(settings.orientation ?? 0);
      exposure.value = settings.exposure ?? '';
      blackLevel.value = settings.black_level_offset ?? '';
      sensorGain.value = settings.sensor_gain ?? '';
      flicker.value = String(settings.flicker_correction ?? 0);
      redBalance.value = settings.red_balance ?? '';
      blueBalance.value = settings.blue_balance ?? '';
      cameraFps.textContent = settings.fps ? Number(settings.fps).toFixed(1) + ' FPS' : '-';
      pipelineIndex.textContent = settings.pipeline_index ?? '-';
      cameraState.className = 'camera-state ok';
      cameraState.querySelector('strong').textContent = 'Connected';
    }
    async function loadCameraSettings(showMessage = false) {
      refreshCamera.disabled = true;
      try {
        const data = await callApi('/api/camera-settings');
        applyCameraSettings(data.settings);
        if (showMessage) setMessage('Limelight camera settings loaded.');
      } catch (error) {
        cameraState.className = 'camera-state bad';
        cameraState.querySelector('strong').textContent = 'Not detected';
        cameraFps.textContent = '-';
        pipelineIndex.textContent = '-';
        if (showMessage) setMessage(error.message, true);
      } finally { refreshCamera.disabled = false; }
    }
    async function saveCameraSettings() {
      saveCamera.disabled = true;
      try {
        const data = await callApi('/api/camera-settings', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(cameraFields())
        });
        applyCameraSettings(data.settings);
        setMessage('Limelight camera settings saved. Recording will use the camera stream rate shown above.');
      } catch (error) { setMessage(error.message, true); }
      finally { saveCamera.disabled = false; }
    }
    async function callApi(path, options = {}) {
      const response = await fetch(path, options);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'The local recorder returned an error.');
      return data;
    }
    async function refresh() {
      try { updateStatus(await callApi('/api/status')); }
      catch (error) { setMessage('Could not reach the local recorder: ' + error.message, true); }
    }
    async function checkConnection() {
      check.disabled = true;
      setMessage('Detecting the Limelight over USB-C ...');
      try {
        const data = await callApi('/api/check?feed_type=' + encodeURIComponent(feedType.value));
        if (data.preview_url) updatePreview(data.preview_url);
        if (data.intake_mode) {
          intakeMode.textContent = data.intake_mode;
          pipelineTitle.textContent = data.intake_mode.toLowerCase().includes('proxy') ? 'PROXY BACKUP' : 'USB DIRECT';
        }
        setMessage('Limelight detected via ' + (data.intake_mode || 'direct feed') + '.');
        dot.className = 'dot ok';
      } catch (error) {
        setMessage(error.message, true);
        dot.className = 'dot bad';
      } finally { check.disabled = false; }
    }
    async function startRecording() {
      start.disabled = true;
      setMessage('Checking the Limelight and starting FFmpeg ...');
      try {
        const data = await callApi('/api/start', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(collectSettings()) });
        setMessage(data.message);
        updateStatus(data.status);
      } catch (error) { setMessage(error.message, true); await refresh(); }
    }
    async function stopRecording() {
      stop.disabled = true;
      setMessage('Stopping and finalizing the MP4 ...');
      try {
        const data = await callApi('/api/stop', { method: 'POST' });
        setMessage(data.message);
        updateStatus(data.status);
      } catch (error) { setMessage(error.message, true); await refresh(); }
    }
    check.addEventListener('click', checkConnection);
    refreshCamera.addEventListener('click', () => loadCameraSettings(true));
    saveCamera.addEventListener('click', saveCameraSettings);
    feedType.addEventListener('change', () => { scheduleSaveSettings(); updatePreview(''); checkConnection(); loadCameraSettings(); });
    outputMode.addEventListener('change', scheduleSaveSettings);
    outputDir.addEventListener('input', scheduleSaveSettings);
    start.addEventListener('click', startRecording);
    stop.addEventListener('click', stopRecording);
    settingsCollapse.addEventListener('click', () => setSettingsCollapsed(!settingsSidebar.classList.contains('collapsed')));
    updatePreview('');
    loadSettings().then(() => { checkConnection(); loadCameraSettings(); });
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>'''


class RecorderWebApp:
    """State and operations shared by the HTTP request handlers."""

    def __init__(self, args: argparse.Namespace):
        saved = read_json(SETTINGS_FILE)
        saved_stream_url = str(saved.get("stream_url") or "").strip()
        # Migrate the old hostname default so the first attempt is now the
        # direct USB gadget address requested by the user.
        if saved_stream_url.rstrip("/") in {
            "http://limelight.local:5800",
            "http://limelight:5800",
            "http://172.28.0.1:5800",
        }:
            saved_stream_url = DEFAULT_STREAM_URL
        self.default_stream_url = saved_stream_url or args.stream_url or DEFAULT_STREAM_URL
        saved_feed_type = str(saved.get("feed_type") or "").strip().lower()
        if saved_feed_type not in FEED_TYPES:
            try:
                saved_feed_type = "overlay" if urlparse(self.default_stream_url).port == OVERLAY_STREAM_PORT else "raw"
            except ValueError:
                saved_feed_type = "raw"
        self.feed_type = saved_feed_type
        saved_output_dir = saved.get("output_dir") or args.output_dir
        self.output_dir = Path(str(saved_output_dir)).expanduser().resolve()
        self.ffmpeg = args.ffmpeg
        self.web_port = int(getattr(args, "port", 8080))
        self.last_intake_mode = "USB direct preferred"
        try:
            self.default_output_mode = validate_output_mode(
                str(saved.get("output_mode") or getattr(args, "output_mode", "both"))
            )
        except RecorderError:
            self.default_output_mode = "both"
        self.state_file = Path(getattr(args, "state_file", DEFAULT_STATE_FILE)).expanduser().resolve()
        self.stop_file = Path(getattr(args, "stop_file", DEFAULT_STOP_FILE)).expanduser().resolve()
        self.operation_lock = threading.Lock()
        self.last_error: str | None = None

    def _settings_payload(self) -> dict[str, Any]:
        return {
            "feed_type": self.feed_type,
            "output_mode": self.default_output_mode,
            "output_dir": str(self.output_dir),
        }

    def _persist_settings(self) -> None:
        write_json(SETTINGS_FILE, self._settings_payload())

    def _parse_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_feed_type = payload.get("feed_type")
        # Accept stream_url from older dashboard clients while keeping it out
        # of the current UI and saved settings format.
        if not requested_feed_type and payload.get("stream_url"):
            try:
                requested_feed_type = (
                    "overlay"
                    if urlparse(str(payload["stream_url"])).port == OVERLAY_STREAM_PORT
                    else "raw"
                )
            except ValueError:
                requested_feed_type = self.feed_type
        feed_type = validate_feed_type(requested_feed_type or self.feed_type)
        source_url = self._stream_url_for_feed(feed_type)

        output_dir_text = str(payload.get("output_dir") or self.output_dir).strip()
        if not output_dir_text:
            raise RecorderError("Enter an output folder.")
        output_dir = Path(output_dir_text).expanduser().resolve()
        if output_dir.exists() and not output_dir.is_dir():
            raise RecorderError(f"The output path is not a folder: {output_dir}")

        return {
            "stream_url": source_url,
            "feed_type": feed_type,
            "output_mode": validate_output_mode(
                str(payload.get("output_mode") or self.default_output_mode)
            ),
            "output_dir": output_dir,
        }

    def _apply_settings(self, parsed: dict[str, Any]) -> None:
        self.feed_type = parsed["feed_type"]
        self.default_output_mode = parsed["output_mode"]
        self.output_dir = parsed["output_dir"]
        self._persist_settings()

    def _stream_url_for_feed(self, feed_type: str) -> str:
        """Build the internal Limelight stream URL from the selected feed."""

        value = self.default_stream_url.strip()
        if "://" not in value:
            value = f"http://{value}"
        parsed = urlparse(value)
        if not parsed.hostname:
            raise RecorderError("The Limelight host could not be determined automatically.")
        port = OVERLAY_STREAM_PORT if feed_type == "overlay" else RAW_STREAM_PORT
        return f"http://{_display_host(parsed.hostname)}:{port}/"

    def _latest_session(self) -> dict[str, Any] | None:
        if not self.output_dir.is_dir():
            return None
        session_prefixes = ("session_", "limelight_raw_data_", "limelight_overlay_data_", "limelight_data_")
        sessions = [
            path
            for path in self.output_dir.iterdir()
            if path.is_dir() and path.name.startswith(session_prefixes)
        ]
        if not sessions:
            return None
        latest = max(sessions, key=lambda path: path.stat().st_mtime)
        metadata = read_json(latest / "metadata.json")
        if not metadata:
            return {"directory": str(latest), "session_name": latest.name}
        return {
            "directory": str(latest),
            "session_name": metadata.get("session_name") or latest.name,
            "recording_number": metadata.get("recording_number"),
            "status": metadata.get("status"),
            "frames_saved": metadata.get("frames_saved", 0),
            "error": metadata.get("error") or (
                f"FFmpeg stopped with exit code {metadata.get('ffmpeg_exit_code')}. "
                f"See {latest / 'ffmpeg.log'} for details."
                if metadata.get("status") == "error"
                else None
            ),
        }

    def status(self) -> dict[str, Any]:
        state = read_json(self.state_file) if self.state_file.exists() else {}
        pid = int(state.get("pid", 0) or 0)
        active = bool(pid and process_is_running(pid))
        status = str(state.get("status", "idle"))
        if pid and not active:
            # A worker that has finished should not leave the UI stuck in
            # "recording". Its metadata remains in the session folder.
            self.state_file.unlink(missing_ok=True)
            state = {}
            status = "idle"
        elif not pid and status == "starting":
            active = True

        return {
            "recording": active,
            "status": status if active else "idle",
            "pid": pid or None,
            "session_dir": state.get("session_dir"),
            "session_name": state.get("session_name"),
            "recording_number": state.get("recording_number"),
            "stream_url": state.get("stream_url") or self.default_stream_url,
            "intake_mode": self.last_intake_mode,
            "feed_type": state.get("feed_type") or self.feed_type,
            "fps": state.get("fps"),
            "output_dir": str(self.output_dir),
            "latest_session": self._latest_session(),
            "last_error": self.last_error,
        }

    def settings(self) -> dict[str, Any]:
        return {"settings": self._settings_payload()}

    def camera_settings(self, source_url: str | None = None) -> dict[str, Any]:
        source_url = (source_url or self.default_stream_url).strip()
        client = LimelightApi(limelight_api_bases(source_url))
        return {"settings": client.get_camera_settings()}

    def save_camera_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.operation_lock:
            if self.status()["recording"]:
                raise RecorderError("Stop the current recording before changing Limelight camera settings.")
            source_url = str(payload.get("stream_url") or self.default_stream_url).strip()
            client = LimelightApi(limelight_api_bases(source_url))
            settings = client.save_camera_settings(payload)
            return {"message": "Limelight camera settings saved.", "settings": settings}

    @staticmethod
    def _direct_stream_candidates(source_url: str) -> list[str]:
        parsed = urlparse(source_url)
        hostname = (parsed.hostname or "").lower()
        if hostname not in {"limelight.local", "limelight"} and not hostname.startswith("172."):
            return [source_url]
        stream_port = parsed.port or STREAM_PORT
        candidates = [
            f"http://{host}:{stream_port}"
            for host in candidate_hosts(0)
            if host.startswith("172.")
        ]
        if source_url not in candidates:
            candidates.append(source_url)
        return candidates

    def _proxy_url(self, source_url: str) -> str:
        return (
            f"http://127.0.0.1:{self.web_port}/proxy/stream?source_url="
            f"{quote(source_url, safe='')}"
        )

    def _select_intake(self, source_url: str) -> tuple[StreamInfo, str]:
        errors: list[str] = []
        for direct_url in self._direct_stream_candidates(source_url):
            try:
                return discover_stream(stream_url=direct_url, timeout=1.5), "USB direct"
            except RecorderError as exc:
                errors.append(str(exc).split("\n", 1)[0])

        # The fallback still reads the Limelight camera stream, but routes it
        # through this local server so FFmpeg has a second intake path.
        stream_port = urlparse(source_url).port or STREAM_PORT
        fallback_url = f"http://limelight.local:{stream_port}/"
        try:
            discover_stream(stream_url=fallback_url, timeout=1.5)
        except RecorderError as exc:
            errors.append(str(exc).split("\n", 1)[0])
            raise RecorderError(
                "The direct USB Limelight feed was not detected, and the proxy backup is unavailable.\n"
                + "\n".join(errors)
            ) from exc
        return StreamInfo(self._proxy_url(fallback_url), "127.0.0.1", "local proxy backup"), "Proxy backup"

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.operation_lock:
            self._apply_settings(self._parse_settings(payload))
            return {"message": "Settings saved.", "settings": self._settings_payload()}

    def check(self, feed_type: str | None = None) -> dict[str, Any]:
        feed_type = validate_feed_type(feed_type or self.feed_type)
        source_url = self._stream_url_for_feed(feed_type)
        info, intake_mode = self._select_intake(source_url)
        return {
            "reachable": True,
            "stream_url": info.url,
            "preview_url": info.url,
            "host": info.host,
            "intake_mode": intake_mode,
            "feed_type": feed_type,
        }

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.operation_lock:
            current = self.status()
            if current["recording"]:
                raise RecorderError("A recording is already running. Stop it before starting another.")

            parsed = self._parse_settings(payload)
            self._apply_settings(parsed)
            source_url = parsed["stream_url"]
            selected_stream, intake_mode = self._select_intake(source_url)
            # The camera's Resolution setting controls the incoming stream FPS.
            # Keep the incoming cadence intact instead of applying a second
            # recorder-side sampling rate.
            fps = None
            output_mode = parsed["output_mode"]

            args = argparse.Namespace(
                host=None,
                stream_url=selected_stream.url,
                usb_index=0,
                timeout=1.5,
                foreground=False,
                fps=fps,
                max_fps=True,
                output_mode=output_mode,
                output_dir=str(parsed["output_dir"]),
                ffmpeg=self.ffmpeg,
                state_file=str(self.state_file),
                stop_file=str(self.stop_file),
            )
            try:
                start_recording(args)
                self.last_error = None
                self.last_intake_mode = intake_mode
            except RecorderError as exc:
                self.last_error = str(exc)
                raise
            return {
                "message": f"Recording started via {intake_mode.lower()} using the Limelight camera stream rate ({output_mode}).",
                "status": self.status(),
            }

    def stop(self) -> dict[str, Any]:
        with self.operation_lock:
            was_recording = self.status()["recording"]
            stop_recording(self.state_file, self.stop_file)
            message = (
                "Recording stopped and files finalized."
                if was_recording
                else "No active Limelight recording was found."
            )
            return {"message": message, "status": self.status()}


class Handler(BaseHTTPRequestHandler):
    """Small JSON API plus the one-page control panel."""

    server: ThreadingHTTPServer

    @property
    def app(self) -> RecorderWebApp:
        return self.server.recorder_app  # type: ignore[attr-defined]

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_page(self) -> None:
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_payload(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RecorderError("Invalid request body.") from exc
        if length > 32_768:
            raise RecorderError("Request body is too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecorderError("Request body must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise RecorderError("Request body must be a JSON object.")
        return value

    def _proxy_stream(self, source_url: str) -> None:
        """Forward a Limelight MJPEG stream for the backup intake path."""

        request = Request(
            source_url,
            headers={
                "Accept": "multipart/x-mixed-replace, image/jpeg, */*",
                "User-Agent": "LimelightTrainingRecorder/1.0 proxy",
            },
        )
        try:
            with urlopen(request, timeout=5) as response:
                content_type = response.headers.get(
                    "Content-Type", "multipart/x-mixed-replace; boundary=frame"
                )
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        return
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, URLError, TimeoutError):
            # FFmpeg and browsers close a probe connection after receiving the
            # headers or a frame.  That is normal for an MJPEG endpoint.
            return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API name
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_page()
            return
        try:
            if parsed.path == "/proxy/stream":
                query = parse_qs(parsed.query)
                source_url = query.get("source_url", [self.app.default_stream_url])[0].strip()
                if not source_url:
                    raise RecorderError("The proxy source URL cannot be empty.")
                self._proxy_stream(source_url)
                return
            if parsed.path == "/api/status":
                self._send_json(200, self.app.status())
                return
            if parsed.path == "/api/settings":
                self._send_json(200, self.app.settings())
                return
            if parsed.path == "/api/camera-settings":
                query = parse_qs(parsed.query)
                source_url = query.get("stream_url", [self.app.default_stream_url])[0]
                self._send_json(200, self.app.camera_settings(source_url))
                return
            if parsed.path == "/api/check":
                query = parse_qs(parsed.query)
                feed_type = query.get("feed_type", [self.app.feed_type])[0]
                self._send_json(200, self.app.check(feed_type))
                return
            self._send_json(404, {"error": "Not found."})
        except RecorderError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # Keep unexpected server errors readable in the UI.
            self._send_json(500, {"error": f"Local recorder error: {exc}"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API name
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/start":
                self._send_json(200, self.app.start(self._read_payload()))
                return
            if parsed.path == "/api/settings":
                self._send_json(200, self.app.save_settings(self._read_payload()))
                return
            if parsed.path == "/api/camera-settings":
                self._send_json(200, self.app.save_camera_settings(self._read_payload()))
                return
            if parsed.path == "/api/stop":
                self._send_json(200, self.app.stop())
                return
            self._send_json(404, {"error": "Not found."})
        except RecorderError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # Keep unexpected server errors readable in the UI.
            self._send_json(500, {"error": f"Local recorder error: {exc}"})

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def run_server(args: argparse.Namespace) -> int:
    """Run the local control panel until Ctrl+C closes it."""

    if not 1 <= args.port <= 65535:
        raise RecorderError("The web port must be between 1 and 65535.")
    app = RecorderWebApp(args)
    try:
        server = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as exc:
        raise RecorderError(
            f"Could not open the local web interface at http://{args.bind}:{args.port}: {exc}"
        ) from exc
    server.daemon_threads = True
    server.recorder_app = app  # type: ignore[attr-defined]
    page_url = f"http://{args.bind}:{args.port}/"
    print(f"Limelight recorder control panel: {page_url}")
    print(f"Default camera intake: {app.default_stream_url}")
    print("Keep this window open while using the browser controls. Press Ctrl+C to exit.")
    if args.open_browser:
        webbrowser.open(page_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWeb interface closed.")
    finally:
        server.server_close()
    return 0
