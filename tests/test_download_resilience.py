"""Настройки ИИ не теряют ключ, запасной движок и проверка ссылок."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from viralcut import ai, downloader, settings, ytdlp_bin


class SettingsKeepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "settings.json"
        self.patch = mock.patch.object(settings, "SETTINGS_FILE", self.tmp)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_saving_keeps_custom_base(self):
        settings.update({"ai_provider": "custom", "ai_base": "http://127.0.0.1:11434/v1",
                         "ai_model": "qwen2.5", "ai_key": "sk-test-1"})
        settings.update({"ai_provider": "custom", "power": 60})
        data = settings.get()
        self.assertEqual(data["ai_base"], "http://127.0.0.1:11434/v1")
        self.assertEqual(data["ai_model"], "qwen2.5")
        self.assertEqual(data["ai_key"], "sk-test-1")
        self.assertTrue(ai.configured())

    def test_switching_provider_resets_base(self):
        settings.update({"ai_provider": "custom", "ai_base": "http://127.0.0.1:9/v1",
                         "ai_model": "x"})
        settings.update({"ai_provider": "deepseek"})
        self.assertEqual(settings.get()["ai_base"], "")
        self.assertEqual(settings.get()["ai_model"], "")

    def test_key_survives_provider_switch(self):
        settings.update({"ai_key": "sk-test-1", "ai_provider": "custom"})
        settings.update({"ai_provider": "polza"})
        self.assertEqual(settings.get()["ai_key"], "sk-test-1")

    def test_local_provider_configured_without_key(self):
        settings.update({"ai_provider": "local", "ai_key": ""})
        self.assertTrue(ai.configured())

    def test_agent_mode_off_by_default(self):
        self.assertFalse(settings.DEFAULTS["agent_mode"])

    def test_public_hides_key(self):
        settings.update({"ai_key": "sk-secret-123456"})
        public = settings.public(settings.get())
        self.assertEqual(public["ai_key"], "")
        self.assertTrue(public["ai_key_set"])


class UploadLimitTest(unittest.TestCase):
    def test_default_limit_is_large(self):
        from viralcut import main

        # 64 ГБ по умолчанию: свои файлы не должны упираться в 4 ГБ
        self.assertGreaterEqual(main.MAX_UPLOAD_BYTES, 64 * 1024 ** 3)


class BinaryEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(ytdlp_bin, "BIN_DIR", self.tmp)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_disabled_by_env(self):
        with mock.patch.dict(os.environ, {"VIRALCUT_YTDLP_BIN": "0"}):
            self.assertFalse(ytdlp_bin.enabled())
            self.assertIsNone(ytdlp_bin.ensure())

    def test_format_selector_respects_height(self):
        selector = ytdlp_bin._fmt_for(720)
        self.assertIn("height<=720", selector)

    def test_status_shape(self):
        status = ytdlp_bin.status()
        for key in ("enabled", "path", "present", "version"):
            self.assertIn(key, status)

    def test_ensure_downloads_and_marks_executable(self):
        payload = b"x" * (1024 * 1024 + 10)

        class Response:
            def read(self, n=-1):
                return payload if not hasattr(self, "_sent") else (setattr(self, "_sent", 1) or b"")
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        with mock.patch.object(ytdlp_bin.urllib.request, "urlopen", return_value=Response()), \
             mock.patch.dict(os.environ, {"VIRALCUT_YTDLP_BIN": "1"}):
            path = ytdlp_bin.ensure(force=True)
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        if os.name != "nt":
            self.assertTrue(os.access(path, os.X_OK))


class InspectTest(unittest.TestCase):
    def test_inspect_reports_clients(self):
        class FakeYDL:
            def __init__(self, opts):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download=False):
                return {"title": "Тест", "formats": [
                    {"format_id": "137", "height": 1080, "vcodec": "avc1", "ext": "mp4"},
                    {"format_id": "140", "acodec": "aac", "ext": "m4a", "vcodec": "none"},
                ]}

        with mock.patch("yt_dlp.YoutubeDL", FakeYDL):
            result = downloader.inspect_url("https://youtube.com/watch?v=x")
        self.assertIn("clients", result)
        self.assertTrue(result["clients"][0]["ok"])
        self.assertEqual(result["clients"][0]["height"], 1080)


if __name__ == "__main__":
    unittest.main()
