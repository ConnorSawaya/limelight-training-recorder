# Limelight Training Recorder

Record the Limelight 3A camera stream over its USB-C connection and save training data as MP4 and/or JPG frames on Windows, macOS, and Linux.

No `pip install` needed — the recorder is pure Python standard library. FFmpeg is the only runtime dependency, and setup installs a project-local copy automatically.

## One-line setup

Paste one command into a terminal. No git required — it downloads the project, checks Python 3.10+, and installs FFmpeg if missing.

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/ConnorSawaya/limelight-training-recorder/main/install/install.ps1 | iex
```

**macOS / Linux** (Terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/ConnorSawaya/limelight-training-recorder/main/install/install.sh | bash
```

Or, if you prefer to clone it yourself:

```text
git clone https://github.com/ConnorSawaya/limelight-training-recorder.git
```

Then run `windows\setup_windows.bat` on Windows or `macos-linux/setup.sh` on macOS/Linux.

## Quick setup

1. Install **Python 3.10+** if it is not already installed ([Windows](https://www.python.org/downloads/windows/), `brew install python` on macOS). Windows setup can install Python via `winget`.
2. Connect the Limelight 3A's USB-C communication port directly to the computer with a data-capable USB-C cable. **Do not hold the blue configuration button** while plugging in — that puts the camera into flash mode.
3. Wait ~20 seconds for the Limelight to boot and for the USB network connection to appear.
4. Run setup once: double-click **`windows\setup_windows.bat`** on Windows, or run **`macos-linux/setup.sh`** on macOS/Linux.

## Repository layout

```text
limelight_recorder.py    Core recorder CLI (Windows, macOS, Linux)
web_interface.py         Local browser control panel
install/                 One-line installers (install.ps1, install.sh)
windows/                 Windows setup and batch launchers
macos-linux/setup.sh     macOS/Linux setup
tests/                   Standard-library unit tests
tools/                   Project-local FFmpeg (created by setup, git-ignored)
training_data/           Recorded sessions (created when recording, git-ignored)
```

## Record

The `*.bat` launchers are Windows-only. On macOS/Linux use `python3` instead of `py -3`.

| Action | Windows (double-click) | Command line |
| --- | --- | --- |
| Start (background, 3 FPS) | `windows\start_recorder.bat` | `py -3 limelight_recorder.py start --fps 3` (macOS/Linux: `python3 …`) |
| Stop and finalize | `windows\stop_recorder.bat` | `py -3 limelight_recorder.py stop` |
| Check status | `windows\status_recorder.bat` | `py -3 limelight_recorder.py status` |
| Watch in console | `windows\run_foreground.bat` | `py -3 limelight_recorder.py start --foreground` |

## Browser interface

On Windows, double-click **`windows\start_web.bat`** (after setup). On macOS/Linux run:

```text
python3 limelight_recorder.py web --open-browser
```

The control panel opens at:

```text
http://127.0.0.1:8080/
```

The page shows the live MJPEG feed and reads and writes Limelight camera settings (resolution/FPS, exposure, gain, orientation, flicker correction, white balance) directly on the device. The default recording feed is the Limelight's raw MJPEG endpoint on port `5802`, which is intended to provide the camera image before the normal pipeline overlay. Use the **Camera feed to record** menu to choose the processed overlay stream on port `5800` instead.

The server binds to `127.0.0.1`, so it is only reachable from this computer. If port 8080 is taken, use another:

```text
py -3 limelight_recorder.py web --port 8090 --open-browser
```

(On macOS/Linux: `python3 limelight_recorder.py web --port 8090 --open-browser`.)

## Recording options

```text
py -3 limelight_recorder.py start --max-fps                     # every incoming frame
py -3 limelight_recorder.py start --fps 10                      # sample at 10 FPS
py -3 limelight_recorder.py start --output-mode mp4             # MP4 only
py -3 limelight_recorder.py start --output-mode images_zip      # JPGs + frames.zip
py -3 limelight_recorder.py start --host 172.26.0.1 --fps 3     # manual camera IP
```

(On macOS/Linux use `python3 limelight_recorder.py …`.)

* `--fps` accepts any value greater than 0 up to 120. The camera stream itself sets the hard FPS ceiling.
* `--max-fps` saves every frame the camera delivers.
* Output modes: `both` (default), `mp4`, `images_zip`, `both_zip`.
* ZIP output is created as `frames.zip` when recording stops; the individual JPGs remain in the session's `frames` folder.

## Output

Each run creates a timestamped folder under `training_data`:

```text
training_data/
  session_20260912_143015/
    recording.mp4
    frames/
      frame_000001.jpg
      frame_000002.jpg
    frames.zip             (when a ZIP output mode is selected)
    metadata.json
    ffmpeg.log
```

`metadata.json` records the stream URL, capture mode, output mode, timestamps, and output counts.

## Camera detection

The recorder probes the direct USB-network addresses on port `5802`, preferring `172.28.0.1`, and then the Limelight hostnames. It verifies the endpoint is an MJPEG stream before starting FFmpeg. The dashboard can use its local MJPEG proxy as a backup only when the direct USB feed cannot be reached.

Verify the connection in a browser:

```text
http://limelight.local:5801
http://172.26.0.1:5801
http://172.28.0.1:5801
```

The Limelight's raw camera stream is on port `5802`; the normal processed/overlay stream is on port `5800`, and its web interface is on port `5801`. For a custom/static address, pass `--host <IP>` or use `--stream-url <URL>`.

## Troubleshooting

* **No Limelight camera stream was detected** — make sure the camera is powered, fully booted, and connected with a USB-C data cable. Try the browser URLs above, then use `--host` if the web interface works at another address.
* **Camera shows as a flash/storage device** — unplug it, do not hold the configuration button, and reconnect.
* **FFmpeg was not found** — run setup again (`windows\setup_windows.bat` or `macos-linux/setup.sh`), or install FFmpeg and add it to PATH.
* **A recording is already running** — use `windows\stop_recorder.bat` or `windows\status_recorder.bat`.
* **Debugging** — inspect `ffmpeg.log` inside the newest session folder.

## Tests

```text
py -3 -m unittest discover -s tests -v
```

On macOS/Linux: `python3 -m unittest discover -s tests -v`.
