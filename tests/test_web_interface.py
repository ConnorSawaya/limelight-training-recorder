import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_interface


class WebInterfaceTests(unittest.TestCase):
    def make_app(self, temp_dir: str) -> web_interface.RecorderWebApp:
        temp_path = Path(temp_dir)
        args = argparse.Namespace(
            stream_url="http://172.28.0.1:5802/",
            output_dir=str(temp_path / "training_data"),
            ffmpeg=None,
            port=8080,
            output_mode="both",
            state_file=str(temp_path / "state.json"),
            stop_file=str(temp_path / "stop"),
        )
        with patch.object(web_interface, "SETTINGS_FILE", temp_path / "settings.json"):
            return web_interface.RecorderWebApp(args)

    def test_dashboard_has_no_manual_stream_url_or_output_save_button(self):
        self.assertNotIn('id="stream"', web_interface.PAGE)
        self.assertNotIn("Limelight MJPEG stream URL", web_interface.PAGE)
        self.assertNotIn("Save output settings", web_interface.PAGE)
        self.assertIn("These settings save automatically", web_interface.PAGE)

    def test_feed_choice_builds_the_internal_stream_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self.make_app(temp_dir)
            parsed = app._parse_settings(
                {
                    "feed_type": "overlay",
                    "output_mode": "mp4",
                    "output_dir": temp_dir,
                }
            )
            self.assertEqual(parsed["feed_type"], "overlay")
            self.assertEqual(parsed["stream_url"], "http://172.28.0.1:5800/")

    def test_saved_settings_do_not_expose_the_stream_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self.make_app(temp_dir)
            settings = app._settings_payload()
            self.assertNotIn("stream_url", settings)
            self.assertEqual(settings["feed_type"], "raw")

    def test_camera_setting_ranges_match_limelight_3a_controls(self):
        client = web_interface.LimelightApi([])
        self.assertEqual(client._number({"value": 2}, "value", 2, 3300), 2)
        self.assertEqual(client._number({"value": 40}, "value", 0, 40, integer=True), 40)
        self.assertEqual(client._number({"value": 1}, "value", 1, 45), 1)
        self.assertEqual(client._number({"value": 2500}, "value", 500, 2500), 2500)
        with self.assertRaises(web_interface.RecorderError):
            client._number({"value": 41}, "value", 0, 40, integer=True)


if __name__ == "__main__":
    unittest.main()
