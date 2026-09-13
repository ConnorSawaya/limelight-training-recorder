import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import limelight_recorder as recorder


class MjpegHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler API name
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        self.wfile.write(b"--frame\r\n")
        self.wfile.flush()

    def log_message(self, _format, *_args):
        return


class RecorderTests(unittest.TestCase):
    def test_candidate_hosts_include_current_and_legacy_windows_addresses(self):
        hosts = recorder.candidate_hosts(0)
        self.assertEqual(hosts[:2], ["172.28.0.1", "172.26.0.1"])
        self.assertEqual(hosts[-2:], ["limelight.local", "limelight"])
        self.assertIn("172.26.0.1", hosts)
        self.assertIn("172.28.0.1", hosts)
        self.assertIn("172.29.0.1", hosts)

    def test_stream_url_for_host_accepts_port(self):
        self.assertEqual(recorder.stream_url_for_host("127.0.0.1:9999"), "http://127.0.0.1:9999")
        self.assertEqual(recorder.stream_url_for_host("http://camera.local"), "http://camera.local:5802")

    def test_probe_and_discover_local_mjpeg_stream(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), MjpegHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            self.assertTrue(recorder.probe_stream_url(url, timeout=1))
            found = recorder.discover_stream(host=f"127.0.0.1:{server.server_port}", timeout=1)
            self.assertEqual(found.url, url)
            self.assertEqual(found.host, "127.0.0.1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_create_session_has_frames_and_metadata_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session = recorder.create_session(temp_dir)
            self.assertTrue(session.root.is_dir())
            self.assertTrue(session.frames.is_dir())
            self.assertEqual(session.video.name, "recording.mp4")
            self.assertEqual(session.metadata.name, "metadata.json")

    def test_sessions_are_numbered_when_started_in_the_same_second(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            recorder, "local_timestamp", return_value="20260912_153000"
        ):
            first = recorder.create_session(temp_dir, "limelight_raw_data")
            second = recorder.create_session(temp_dir, "limelight_raw_data")
            self.assertEqual(first.root.name, "limelight_raw_data_20260912_153000_01")
            self.assertEqual(second.root.name, "limelight_raw_data_20260912_153000_02")
            self.assertEqual(recorder.recording_number(first), 1)
            self.assertEqual(recorder.recording_number(second), 2)

    def test_session_prefix_matches_recorded_feed(self):
        self.assertEqual(
            recorder.session_prefix_for_stream("http://172.28.0.1:5802"),
            "limelight_raw_data",
        )
        self.assertEqual(
            recorder.session_prefix_for_stream("http://172.28.0.1:5800"),
            "limelight_overlay_data",
        )
        self.assertEqual(recorder.feed_type_for_stream("http://172.28.0.1:5800"), "overlay")
        self.assertEqual(recorder.feed_type_for_stream("http://172.28.0.1:5802"), "raw")

    def test_ffmpeg_command_writes_video_and_sampled_jpgs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session = recorder.create_session(temp_dir)
            command = recorder.ffmpeg_command(Path("ffmpeg.exe"), "http://limelight.local:5800", session, 3)
            self.assertIn(str(session.video), command)
            self.assertIn(str(session.frames / "frame_%06d.jpg"), command)
            self.assertIn("fps=3", command)
            self.assertEqual(command.count("-vf"), 2)
            self.assertEqual(command.count("-map"), 2)

    def test_ffmpeg_command_can_preserve_every_incoming_frame(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session = recorder.create_session(temp_dir)
            command = recorder.ffmpeg_command(
                Path("ffmpeg.exe"),
                "http://limelight.local:5800/",
                session,
                None,
                "images_zip",
            )
            self.assertNotIn("-vf", command)
            self.assertIn("-fps_mode", command)
            self.assertNotIn(str(session.video), command)
            self.assertEqual(command.count("-map"), 1)
            self.assertIn("-use_wallclock_as_timestamps", command)

    def test_ffmpeg_command_uses_camera_rate_before_mjpeg_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session = recorder.create_session(temp_dir)
            command = recorder.ffmpeg_command(
                Path("ffmpeg.exe"),
                "http://172.28.0.1:5802/",
                session,
                None,
                "mp4",
                90,
            )
            input_index = command.index("-i")
            self.assertEqual(command[input_index - 2 : input_index], ["-r", "90"])
            self.assertNotIn("-use_wallclock_as_timestamps", command)

    def test_output_mode_validation(self):
        self.assertEqual(recorder.validate_output_mode("both_zip"), "both_zip")
        with self.assertRaises(recorder.RecorderError):
            recorder.validate_output_mode("avi")

    def test_fps_validation_and_formatting(self):
        self.assertEqual(recorder.format_fps("3"), 3.0)
        self.assertEqual(recorder.fps_text(1.5), "1.5")
        with self.assertRaises(Exception):
            recorder.format_fps("0")

    def test_error_message_is_clear_when_stream_is_missing(self):
        with self.assertRaises(recorder.RecorderError) as raised:
            recorder.discover_stream(host="127.0.0.1:1", timeout=0.05)
        message = str(raised.exception)
        self.assertIn("No Limelight camera stream was detected", message)
        self.assertIn("data-capable USB-C cable", message)


if __name__ == "__main__":
    unittest.main()
