# Limelight Training Recorder

Record the Limelight 3A camera stream over its USB-C connection and save training data as MP4 and/or JPG frames on Windows, macOS, and Linux.

No `pip install` needed — the recorder is pure Python standard library. FFmpeg is the only runtime dependency, and setup installs a project-local copy automatically.

## One-line setup

Paste one command into a terminal. No git required — it downloads the project, checks Python 3.10+, and installs FFmpeg if missing.

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/ConnorSawaya/limelight-training-recorder/main/install.ps1 | iex
```

**macOS / Linux** (Terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/ConnorSawaya/limelight-training-recorder/main/install.sh | bash
```

Or, if you prefer to clone it yourself:

```text
git clone https://github.com/ConnorSawaya/limelight-training-recorder.git
```

Then run `setup_windows.bat` on Windows or `./setup.sh` on macOS/Linux.

## Quick setup

1. Install **Python 3.10+** if it is not already installed ([Windows](https://www.python.org/downloads/windows/), `brew install python` on macOS). Windows setup can install Python via `winget`.
2. Connect the Limelight 3A's USB-C communication port directly to the computer with a data-capable USB-C cable. **Do not hold the blue configuration button** while plugging in — that puts the camera into flash mode.
3. Wait ~20 seconds for the Limelight to boot and for the USB network connection to appear.
4. Run setup once: double-click **`setup_windows.bat`** on Windows, or run **`./setup.sh`** on macOS/Linux.

## Record

The `*.bat` launchers are Windows-only. On macOS/Linux use `python3` instead of `py -3`.

| Action | Windows (double-click) | Command line |
| --- | --- | --- |
| Start (background, 3 FPS) | `start_recorder.bat` | `py -3 limelight_recorder.py start --fps 3` (macOS/Linux: `python3 …`) |
| Stop and finalize | `stop_recorder.bat` | `py -3 limelight_recorder.py stop` |
| Check status | `status_recorder.bat` | `py -3 limelight_recorder.py status` |
| Watch in console | `run_foreground.bat` | `py -3 limelight_recorder.py start --foreground` |

## Browser interface

On Windows, double-click **`start_web.bat`** (after setup). On macOS/Linux run:

```text
python3 limelight_recorder.py web --open-browser
```

The control panel opens at:

```text
http://127.0.0.1:8080/
```

The page shows the live MJPEG feed, checks the camera connection, and lets you pick the recording rate before starting. It also reads and writes Limelight camera settings (resolution, exposure, gain, orientation, flicker correction, white balance) directly on the device.

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

The recorder probes `limelight.local:5800`, then known USB-network addresses (`172.26.0.1`, `172.27.0.1`, `172.28.0.1`, `172.29.0.1`) in parallel, and verifies the endpoint is an MJPEG stream before starting FFmpeg.

Verify the connection in a browser:

```text
http://limelight.local:5801
http://172.26.0.1:5801
http://172.28.0.1:5801
```

The camera's stream is normally the same host on port `5800`; its web interface is on port `5801`. For a custom/static address, pass `--host <IP>` or use `--stream-url <URL>`.

## Troubleshooting

* **No Limelight camera stream was detected** — make sure the camera is powered, fully booted, and connected with a USB-C data cable. Try the browser URLs above, then use `--host` if the web interface works at another address.
* **Camera shows as a flash/storage device** — unplug it, do not hold the configuration button, and reconnect.
* **FFmpeg was not found** — run setup again (`setup_windows.bat` or `./setup.sh`), or install FFmpeg and add it to PATH.
* **A recording is already running** — use `stop_recorder.bat` or `status_recorder.bat`.
* **Debugging** — inspect `ffmpeg.log` inside the newest session folder.

## Tests

```text
py -3 -m unittest discover -s tests -v
```

On macOS/Linux: `python3 -m unittest discover -s tests -v`.
