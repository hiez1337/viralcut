"""Мониторинг нагрузки не должен запускать процессы (иначе на Windows мигают окна)."""
from __future__ import annotations

import ctypes
import subprocess
import unittest
from ctypes import wintypes
from unittest import mock

from viralcut import system_stats as ss

ENGINE = (r"\GPU Engine(pid_{pid}_luid_0x00000000_{luid}_phys_{phys}"
          r"_eng_{eng}_engtype_{kind})\Utilization Percentage")


def engine_path(pid=1, luid="0x0000A1B2", phys=0, eng=0, kind="3D"):
    return ENGINE.format(pid=pid, luid=luid, phys=phys, eng=eng, kind=kind)


class WindowsNoSpawnTest(unittest.TestCase):
    def setUp(self):
        ss._GPU_CACHE.update({"value": None, "ts": 0.0})
        ss._GPU_CACHE.pop("legacy", None)
        ss._MEM_CACHE.update({"value": None, "ts": 0.0})
        ss._LAST_CPU.clear()
        ss._LAST_VALUE[0] = 0.0
        ss._LAST_TS[0] = 0.0
        ss._GPU_FAIL_TS[0] = 0.0
        ss._GPU_SOURCE[0] = ""

    def test_snapshot_does_not_spawn_processes(self):
        spawned = []

        def boom(*args, **kwargs):
            spawned.append(args)
            raise AssertionError("не должен запускаться процесс")

        with mock.patch.object(ss, "_platform", return_value="win32"), \
             mock.patch.object(ss, "_cpu_windows", return_value=(10, 20)), \
             mock.patch.object(ss, "_gpu_windows_pdh", return_value={"percent": 7, "name": "GPU"}), \
             mock.patch.object(ss.subprocess, "run", side_effect=boom), \
             mock.patch.object(ss.subprocess, "Popen", side_effect=boom):
            data = ss.snapshot()

        self.assertEqual(spawned, [], "на Windows мониторинг не должен ничего запускать")
        self.assertEqual(data["gpu"]["percent"], 7)
        self.assertIsInstance(data["ram"], dict)

    def test_gpu_fallback_is_rate_limited(self):
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "нет nvidia-smi")

        with mock.patch.object(ss, "_platform", return_value="win32"), \
             mock.patch.object(ss, "_cpu_windows", return_value=(10, 20)), \
             mock.patch.object(ss, "_gpu_windows_pdh", return_value={"percent": None, "name": ""}), \
             mock.patch.object(ss.subprocess, "run", side_effect=fake_run):
            for _ in range(6):
                ss.gpu(force=True)

        self.assertLessEqual(len(calls), 1,
                             "запасной nvidia-smi должен вызываться не чаще раза в 30 с")

    def test_macos_path_used_for_darwin(self):
        ss._GPU_CACHE.update({"value": None, "ts": 0.0})
        with mock.patch.object(ss, "_platform", return_value="darwin"), \
             mock.patch.object(ss, "_gpu_macos",
                               return_value={"percent": 42, "name": "Apple M1"}) as gpu_mock:
            data = ss.gpu(force=True)
        gpu_mock.assert_called_once()
        self.assertEqual(data["percent"], 42)


class EngineSummaryTest(unittest.TestCase):
    """Суммируем процессы на одном движке, затем берём самый занятый."""

    def test_sums_per_engine_without_combining_engines_or_adapters(self):
        summary = ss._summarize_engine_utilization([
            (engine_path(pid=1, luid="0x0AAA"), 30.0),
            (engine_path(pid=2, luid="0x0AAA"), 25.0),
            (engine_path(pid=1, luid="0x0AAA", eng=1, kind="VideoEncode"), 68.0),
            (engine_path(pid=2, luid="0x0AAA", eng=1, kind="VideoEncode"), 12.0),
            (engine_path(pid=1, luid="0x0AAA", eng=2), 31.0),
            (engine_path(pid=1, luid="0x0BBB", kind="Copy"), 70.0),
        ])
        self.assertEqual(summary["percent"], 80)
        self.assertEqual(summary["adapters"], 2)

    def test_caps_at_100(self):
        summary = ss._summarize_engine_utilization([
            (engine_path(pid=1, luid="0x0AAA"), 80.0),
            (engine_path(pid=2, luid="0x0AAA"), 80.0),
        ])
        self.assertEqual(summary["percent"], 100)

    def test_empty_means_no_data(self):
        self.assertIsNone(ss._summarize_engine_utilization([])["percent"])
        self.assertIsNone(ss._summarize_engine_utilization(
            [(engine_path(kind="Copy"), float("nan"))])["percent"])

    def test_zero_is_a_valid_idle_sample(self):
        self.assertEqual(ss._summarize_engine_utilization(
            [(engine_path(kind="VideoEncode"), 0.0)])["percent"], 0)


class VendorTest(unittest.TestCase):
    def test_vendor_from_name(self):
        self.assertEqual(ss._gpu_vendor("NVIDIA GeForce RTX 4070"), "nvidia")
        self.assertEqual(ss._gpu_vendor("AMD Radeon RX 6800 XT"), "amd")
        self.assertEqual(ss._gpu_vendor("Intel(R) UHD Graphics 620"), "intel")
        self.assertEqual(ss._gpu_vendor("Microsoft Basic Display Adapter"), "")


class WindowsFallbackTest(unittest.TestCase):
    """PDH → nvidia-smi → ADL: каждый вендор получает свой источник."""

    def setUp(self):
        ss._GPU_CACHE.update({"value": None, "ts": 0.0})
        ss._GPU_CACHE.pop("legacy", None)
        ss._GPU_FAIL_TS[0] = 0.0
        ss._GPU_SOURCE[0] = ""

    def test_adl_used_when_pdh_and_smi_fail(self):
        with mock.patch.object(ss, "_platform", return_value="win32"), \
             mock.patch.object(ss, "_gpu_windows_pdh", return_value={"percent": None, "name": ""}), \
             mock.patch.object(ss, "_gpu_nvidia", return_value={"percent": None, "name": ""}), \
             mock.patch.object(ss, "_gpu_windows_adl",
                               return_value={"percent": 55, "name": "AMD Radeon", "vendor": "amd"}), \
             mock.patch.object(ss, "_windows_adapters_cached", return_value=[]), \
             mock.patch.object(ss, "_gpu_windows_registry_name", return_value="AMD Radeon RX 6800"):
            data = ss.gpu(force=True)
        self.assertEqual(data["percent"], 55)
        self.assertEqual(data["source"], "adl")
        self.assertEqual(ss._GPU_SOURCE[0], "adl")

    def test_nvidia_smi_before_adl(self):
        with mock.patch.object(ss, "_platform", return_value="win32"), \
             mock.patch.object(ss, "_gpu_windows_pdh", return_value={"percent": None, "name": ""}), \
             mock.patch.object(ss, "_gpu_nvidia",
                               return_value={"percent": 41, "name": "NVIDIA GeForce RTX 3060"}), \
             mock.patch.object(ss, "_gpu_windows_adl") as adl, \
             mock.patch.object(ss, "_windows_adapters_cached", return_value=[]), \
             mock.patch.object(ss, "_gpu_windows_registry_name", return_value="NVIDIA GeForce RTX 3060"):
            data = ss.gpu(force=True)
        adl.assert_not_called()
        self.assertEqual(data["percent"], 41)
        self.assertEqual(data["vendor"], "nvidia")
        self.assertEqual(data["source"], "nvidia-smi")

    def test_pdh_yields_vendor_and_registry_name(self):
        with mock.patch.object(ss, "_platform", return_value="win32"), \
             mock.patch.object(ss, "_gpu_windows_pdh",
                               return_value={"percent": 17, "name": "GPU", "adapters": 1}), \
             mock.patch.object(ss, "_windows_adapters_cached", return_value=[]), \
             mock.patch.object(ss, "_gpu_windows_registry_name",
                               return_value="Intel(R) Iris Xe Graphics"):
            data = ss.gpu(force=True)
        self.assertEqual(data["percent"], 17)
        self.assertEqual(data["vendor"], "intel")
        self.assertEqual(data["name"], "Intel(R) Iris Xe Graphics")
        self.assertEqual(data["source"], "pdh")

    def test_adl_absent_returns_no_data_not_crash(self):
        with mock.patch.object(ss, "_platform", return_value="win32"), \
             mock.patch.object(ss, "_gpu_windows_pdh", return_value={"percent": None, "name": ""}), \
             mock.patch.object(ss, "_gpu_nvidia", return_value={"percent": None, "name": ""}), \
             mock.patch.object(ss, "_gpu_windows_adl", return_value={"percent": None, "name": "", "vendor": "amd"}), \
             mock.patch.object(ss, "_windows_adapters_cached", return_value=[]), \
             mock.patch.object(ss, "_gpu_windows_registry_name", return_value="Microsoft Basic Display Adapter"):
            data = ss.gpu(force=True)
        self.assertIsNone(data["percent"])
        self.assertEqual(data["source"], "none")


class PdhSafetyTest(unittest.TestCase):
    """Без pdh.dll вызов не должен бросать исключение.

    Тесты платформонезависимые: на Windows-раннере pdh.dll реально есть, и
    проверка «нет Windows» там бессмысленна — подменяем саму загрузку DLL.
    """

    def test_pdh_returns_no_data_without_dll(self):
        with mock.patch.object(ss, "_pdh", return_value=None):
            self.assertIsNone(ss._gpu_windows_pdh().get("percent"))

    def test_windows_pdh_reads_video_encode_instances(self):
        pdh = mock.Mock()
        pdh.PdhOpenQueryW.return_value = 0
        pdh.PdhAddEnglishCounterW.return_value = 0
        pdh.PdhCollectQueryData.return_value = 0
        pdh.PdhCloseQuery.return_value = 0

        def format_array(_counter, _format, size, count, array):
            item = ss._PDH_VALUE[0]
            if array is None:
                ctypes.cast(size, ctypes.POINTER(wintypes.DWORD)).contents.value = 3 * ctypes.sizeof(item)
                return ctypes.c_int32(0x800007D2).value
            ctypes.cast(count, ctypes.POINTER(wintypes.DWORD)).contents.value = 3
            for index, (kind, value, status) in enumerate(
                [("3D", 0, 0), ("VideoEncode", 72, 1), ("VideoDecode", 99, 3)]
            ):
                array[index].szName = engine_path(eng=index, kind=kind)
                array[index].FmtValue.doubleValue = value
                array[index].FmtValue.CStatus = status
            return 0

        pdh.PdhGetFormattedCounterArrayW.side_effect = format_array
        with mock.patch.object(ss, "_PDH_DLL", [None]), mock.patch.object(ss, "_PDH_VALUE", [None]), \
             mock.patch.object(ss.ctypes, "WinDLL", return_value=pdh, create=True), \
             mock.patch.object(ss.time, "sleep"):
            self.assertEqual(ss._gpu_windows_pdh()["percent"], 72)
        self.assertEqual(pdh.PdhCollectQueryData.call_count, 2)
        self.assertEqual(pdh.PdhAddEnglishCounterW.call_args.args[1],
                         r"\GPU Engine(*)\Utilization Percentage")
        pdh.PdhCloseQuery.assert_called_once()

    def test_pdh_dll_cached_after_failure(self):
        ss._PDH_DLL[0] = None
        with mock.patch.object(ss.ctypes, "WinDLL", side_effect=OSError("нет pdh.dll"),
                               create=True):
            first = ss._pdh()
            second = ss._pdh()
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertIs(ss._PDH_DLL[0], False)
        ss._PDH_DLL[0] = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
