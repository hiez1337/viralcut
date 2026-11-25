"""Лимит процессора и выбор видеокарты."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from viralcut import cpu_limit, gpu_pref, resources


class CpuLimitSettingsTest(unittest.TestCase):
    def test_limit_clamped(self):
        self.assertEqual(cpu_limit._clamp(10), 25)
        self.assertEqual(cpu_limit._clamp(500), 100)
        self.assertEqual(cpu_limit._clamp("40"), 40)
        self.assertEqual(cpu_limit._clamp("мусор"), 100)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"VIRALCUT_CPU_LIMIT": "30"}):
            self.assertEqual(cpu_limit.limit(), 30)

    def test_cores_for(self):
        total = resources.cpu_count()
        self.assertEqual(cpu_limit.cores_for(100), total)
        self.assertGreaterEqual(cpu_limit.cores_for(25), 1)
        self.assertLessEqual(cpu_limit.cores_for(50), total)

    def test_apply_reports_target(self):
        with mock.patch.object(cpu_limit, "limit", return_value=60):
            applied = cpu_limit.apply()
        cpu_limit.stop()
        if sys.platform == "win32":
            self.assertIn(applied, (60, None))
        else:
            self.assertEqual(applied, 60)

    def test_status_shape(self):
        data = cpu_limit.status()
        for key in ("limit", "applied", "mechanism"):
            self.assertIn(key, data)


class ResourcesWithLimitTest(unittest.TestCase):
    def test_cpu_share_scales_threads(self):
        from viralcut import settings

        with mock.patch.object(settings, "get", return_value={"cpu_limit": 50, "power": 100}):
            with mock.patch.object(resources, "_level", return_value=1.0):
                self.assertLessEqual(resources.cpu_budget(), max(1, resources.cpu_count()))
        with mock.patch.object(settings, "get", return_value={"cpu_limit": 100, "power": 100}):
            with mock.patch.object(resources, "_level", return_value=1.0):
                self.assertEqual(resources.cpu_budget(), resources.cpu_count())

    def test_share_helper(self):
        with mock.patch.dict(os.environ, {"VIRALCUT_CPU_LIMIT": "40"}):
            self.assertAlmostEqual(resources.cpu_share(), 0.4)


class GpuPreferenceTest(unittest.TestCase):
    def test_preference_from_settings(self):
        from viralcut import settings

        with mock.patch.object(settings, "get", return_value={"gpu_preference": "discrete"}):
            self.assertEqual(gpu_pref.preference(), "discrete")

    def test_invalid_preference_falls_back(self):
        from viralcut import settings

        with mock.patch.object(settings, "get", return_value={"gpu_preference": "что-то"}):
            self.assertEqual(gpu_pref.preference(), "auto")

    def test_encoder_order_integrated(self):
        base = ["h264_nvenc", "h264_amf", "h264_qsv"]
        with mock.patch.object(gpu_pref, "preference", return_value="integrated"):
            self.assertEqual(gpu_pref.encoder_order(base)[0], "h264_qsv")

    def test_encoder_order_discrete(self):
        base = ["h264_qsv", "h264_nvenc", "h264_amf"]
        with mock.patch.object(gpu_pref, "preference", return_value="discrete"):
            self.assertEqual(gpu_pref.encoder_order(base)[0], "h264_nvenc")

    def test_apply_auto_is_noop(self):
        info = gpu_pref.apply("auto")
        self.assertFalse(info["applied"])
        self.assertEqual(info["preference"], "auto")


if __name__ == "__main__":
    unittest.main()
