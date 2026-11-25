"""Тесты лимитов мощности: ползунок должен реально менять поведение."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from viralcut import resources


class PowerMappingTest(unittest.TestCase):
    def test_low_power_means_one_render_and_one_thread(self):
        with mock.patch.object(resources, "power", return_value=0):
            self.assertEqual(resources.render_workers(), 1)
            self.assertEqual(resources.ffmpeg_threads(), 1)
            self.assertGreaterEqual(resources.nice_level(), 10)
            self.assertGreaterEqual(resources.whisper_threads(), 1)

    def test_max_power_uses_more_workers_and_threads(self):
        with mock.patch.object(resources, "power", return_value=100):
            workers = resources.render_workers()
            threads = resources.ffmpeg_threads()
            self.assertGreaterEqual(workers, 1)
            self.assertLessEqual(workers, resources.max_render_workers())
            self.assertGreaterEqual(threads, 1)
            # на максимуме приоритет не понижаем
            self.assertEqual(resources.nice_level(), 0)

    def test_workers_grow_with_power(self):
        seen = []
        for level in (0, 25, 50, 75, 100):
            with mock.patch.object(resources, "power", return_value=level):
                seen.append(resources.render_workers())
        self.assertEqual(seen, sorted(seen), f"мощность должна расти монотонно: {seen}")

    def test_summary_contains_power_and_limits(self):
        with mock.patch.object(resources, "power", return_value=50):
            data = resources.summary()
        for key in ("power", "render_workers", "ffmpeg_threads", "whisper_threads", "cpu"):
            self.assertIn(key, data)
        self.assertEqual(data["power"], 50)

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"VIRALCUT_RENDER_WORKERS": "6"}):
            self.assertEqual(resources.render_workers(), 6)


class SettingsPowerTest(unittest.TestCase):
    def test_power_is_saved_and_clamped(self):
        from viralcut import settings

        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(settings, "SETTINGS_FILE", tmp / "settings.json"):
            settings.update({"power": 70})
            self.assertEqual(settings.get()["power"], 70)
            settings.update({"power": 500})
            self.assertEqual(settings.get()["power"], 100)
            settings.update({"power": -20})
            self.assertEqual(settings.get()["power"], 0)

    def test_invalidate_forces_reread(self):
        from viralcut import settings

        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(settings, "SETTINGS_FILE", tmp / "settings.json"):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("VIRALCUT_POWER", None)
                resources.invalidate()
                settings.update({"power": 10})
                self.assertEqual(resources.power(), 10)
                settings.update({"power": 90})
                resources.invalidate()
                self.assertEqual(resources.power(), 90)


class GateTest(unittest.TestCase):
    def test_gate_limits_concurrency_and_releases(self):
        limit = {"value": 1}
        gate = resources.ConcurrencyGate(lambda: limit["value"])
        with gate:
            stats = gate.stats()
            self.assertEqual(stats["active"], 1)
            self.assertEqual(stats["limit"], 1)
        self.assertEqual(gate.stats()["active"], 0)

    def test_gate_follows_new_limit(self):
        limit = {"value": 3}
        gate = resources.ConcurrencyGate(lambda: limit["value"])
        self.assertEqual(gate.limit(), 3)
        limit["value"] = 1
        self.assertEqual(gate.limit(), 1)


if __name__ == "__main__":
    unittest.main()
