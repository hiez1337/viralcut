"""Tests for custom YouTube quality selection, section download, and live debug telemetry."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from viralcut import custom_debug, custom_downloader, downloader, main, utils


class PickBestFormatTest(unittest.TestCase):
    def test_prefers_1080p_over_360p_muxed(self):
        info = {
            "formats": [
                {"format_id": "18", "height": 360, "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "protocol": "https", "tbr": 111},
                {"format_id": "137", "height": 1080, "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "protocol": "https", "tbr": 261},
                {"format_id": "140", "height": None, "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "protocol": "https", "abr": 128},
            ]
        }
        fmt, height = custom_downloader.pick_best_format(info, 1080)
        self.assertEqual((fmt, height), ("137+140", 1080))

    def test_prefers_1080p_vp9_over_360p_avc1_mp4(self):
        info = {
            "formats": [
                {"format_id": "18", "height": 360, "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "protocol": "https", "tbr": 111},
                {"format_id": "248", "height": 1080, "ext": "webm", "vcodec": "vp9", "acodec": "none", "protocol": "https", "tbr": 215},
                {"format_id": "251", "height": None, "ext": "webm", "vcodec": "none", "acodec": "opus", "protocol": "https", "abr": 160},
            ]
        }
        fmt, height = custom_downloader.pick_best_format(info, 1080)
        self.assertEqual((fmt, height), ("248+251", 1080))

    def test_prefers_https_over_m3u8_at_same_height_and_codec(self):
        info = {
            "formats": [
                {"format_id": "270", "height": 1080, "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "protocol": "m3u8_native", "tbr": 1334},
                {"format_id": "137", "height": 1080, "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "protocol": "https", "tbr": 261},
                {"format_id": "140", "height": None, "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "protocol": "https", "abr": 128},
            ]
        }
        fmt, height = custom_downloader.pick_best_format(info, 1080)
        self.assertEqual((fmt, height), ("137+140", 1080))

    def test_respects_max_height_cap(self):
        info = {
            "formats": [
                {"format_id": "18", "height": 360, "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "protocol": "https"},
                {"format_id": "136", "height": 720, "ext": "mp4", "vcodec": "avc1.64001F", "acodec": "none", "protocol": "https"},
                {"format_id": "137", "height": 1080, "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "protocol": "https"},
                {"format_id": "140", "height": None, "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "protocol": "https", "abr": 128},
            ]
        }
        self.assertEqual(custom_downloader.pick_best_format(info, 720), ("136+140", 720))
        self.assertEqual(custom_downloader.pick_best_format(info, 360), ("18", 360))


class DownloadSectionSmartTest(unittest.TestCase):
    def test_uses_youtube_info_default_client_and_records_meta(self):
        captured_opts = []
        info = {
            "formats": [
                {"format_id": "18", "height": 360, "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "protocol": "https"},
                {"format_id": "137", "height": 1080, "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "protocol": "https"},
                {"format_id": "140", "height": None, "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "protocol": "https", "abr": 128},
            ]
        }

        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts
                captured_opts.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=True):
                dest = Path(self.opts["outtmpl"].replace(".%(ext)s", ".mp4"))
                dest.write_bytes(b"0" * 4096)
                return {
                    "format_id": "137+140",
                    "height": 1080,
                    "width": 1920,
                    "vcodec": "avc1.640028",
                }

        with tempfile.TemporaryDirectory() as tmp:
            meta = {}
            with mock.patch.object(downloader, "_pot_enabled", return_value=False), \
                 mock.patch.object(downloader, "_youtube_info", return_value=(info, {}, "по умолчанию")), \
                 mock.patch("yt_dlp.YoutubeDL", FakeYDL), \
                 mock.patch.object(utils, "probe", return_value={"duration": 3.0, "width": 1920, "height": 1080, "fps": 30.0, "has_audio": True}):
                path = downloader.download_section(
                    "https://youtu.be/9-2Gz9343hQ",
                    Path(tmp),
                    10.0,
                    13.0,
                    max_height=1080,
                    meta_out=meta,
                )
            self.assertTrue(path.exists())
            self.assertEqual(len(captured_opts), 1)
            self.assertNotIn("extractor_args", captured_opts[0])
            self.assertEqual(captured_opts[0]["format"], "137+140")
            self.assertEqual(meta["source_height"], 1080)
            self.assertFalse(meta["fallback"])

    def test_falls_back_to_sd_with_reason_when_hd_fails(self):
        info = {
            "formats": [
                {"format_id": "137", "height": 1080, "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "protocol": "https"},
                {"format_id": "140", "height": None, "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "protocol": "https", "abr": 128},
            ]
        }
        calls = {"n": 0}

        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=True):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("HTTP Error 403: Forbidden")
                dest = Path(self.opts["outtmpl"].replace(".%(ext)s", ".mp4"))
                dest.write_bytes(b"0" * 4096)
                return {"format_id": "18", "height": 360, "width": 640, "vcodec": "avc1.42001E"}

        with tempfile.TemporaryDirectory() as tmp:
            meta = {}
            with mock.patch.object(downloader, "_pot_enabled", return_value=False), \
                 mock.patch.object(downloader, "_youtube_info", return_value=(info, {}, "по умолчанию")), \
                 mock.patch("yt_dlp.YoutubeDL", FakeYDL), \
                 mock.patch.object(utils, "probe", return_value={"duration": 3.0, "width": 640, "height": 360, "fps": 30.0, "has_audio": True}):
                downloader.download_section(
                    "https://youtu.be/9-2Gz9343hQ",
                    Path(tmp),
                    10.0,
                    13.0,
                    max_height=1080,
                    meta_out=meta,
                )
            self.assertTrue(meta["fallback"])
            self.assertTrue(meta["fallback_reason"])
            self.assertEqual(meta["source_height"], 360)


class CustomDebugTest(unittest.TestCase):
    def test_snapshot_and_process_tracking(self):
        pid = os.getpid()
        custom_debug.register_process(pid, "ffmpeg", label="clip_01.mp4", encoder="h264_nvenc", source_res="1920x1080", target_res="1920x1080")
        custom_debug.update_process(pid, progress=0.5)
        try:
            snap = custom_debug.snapshot()
            for key in ("server", "system", "gpu", "whisper", "download", "active_projects", "children"):
                self.assertIn(key, snap)
            self.assertEqual(snap["server"]["pid"], pid)
            self.assertGreater(snap["server"]["ram_mb"], 0)
            self.assertGreaterEqual(snap["system"]["cpu_count"], 1)
            self.assertTrue(any(c["pid"] == pid and c["progress"] == 0.5 for c in snap["children"]))
        finally:
            custom_debug.unregister_process(pid)

        after = custom_debug.snapshot()
        self.assertFalse(any(c["pid"] == pid for c in after["children"]))

    def test_upscale_warning_and_main_endpoint(self):
        custom_debug.record_download(source_height=360, target_height=1080)
        self.assertTrue(main.debug_status()["download"]["upscale_warning"])

        custom_debug.record_download(source_height=1080, target_height=1080, fallback=False, fallback_reason="")
        snap = main.debug_status()
        self.assertFalse(snap["download"]["upscale_warning"])
        self.assertFalse(snap["download"]["fallback"])


if __name__ == "__main__":
    unittest.main()
