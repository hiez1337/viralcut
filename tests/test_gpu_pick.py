"""Выбор дискретной видеокарты среди нескольких адаптеров."""
from __future__ import annotations

import unittest

from viralcut import system_stats


class DiscreteGpuPickTest(unittest.TestCase):
    def test_prefers_discrete_over_integrated(self):
        name, discrete = system_stats._pick_gpu([
            "Intel(R) UHD Graphics 630",
            "NVIDIA GeForce RTX 3060 Laptop GPU",
        ])
        self.assertEqual(name, "NVIDIA GeForce RTX 3060 Laptop GPU")
        self.assertTrue(discrete)

    def test_amd_discrete_beats_intel(self):
        name, discrete = system_stats._pick_gpu([
            "Intel(R) Iris Xe Graphics",
            "AMD Radeon RX 6800M",
        ])
        self.assertEqual(name, "AMD Radeon RX 6800M")
        self.assertTrue(discrete)

    def test_single_integrated_is_not_discrete(self):
        name, discrete = system_stats._pick_gpu(["Intel(R) Iris Xe Graphics"])
        self.assertEqual(name, "Intel(R) Iris Xe Graphics")
        self.assertFalse(discrete)

    def test_empty_list(self):
        self.assertEqual(system_stats._pick_gpu([]), ("", False))

    def test_vendor_detection(self):
        self.assertEqual(system_stats._gpu_vendor("NVIDIA GeForce RTX 4070"), "nvidia")
        self.assertEqual(system_stats._gpu_vendor("AMD Radeon RX 7800"), "amd")
        self.assertEqual(system_stats._gpu_vendor("Intel Arc A770"), "intel")
        self.assertEqual(system_stats._gpu_vendor("Apple M1"), "")


if __name__ == "__main__":
    unittest.main()
