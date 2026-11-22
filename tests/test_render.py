"""Тесты рендера клипа: обычный путь, откат с аппаратного энкодера, прогресс."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from viralcut import config, render


def make_source(path: Path, seconds: float = 2.0) -> Path:
    """Синтетический исходник: цветной кадр + тон, чтобы не зависеть от стока."""
    subprocess.run(
        [config.ffmpeg(), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vc-render-"))
        self.src = make_source(self.tmp / "src.mp4")

    def _render(self, name: str, **kwargs) -> Path:
        out = self.tmp / name
        render.render_clip(self.src, 0.0, 2.0, out, aspect="9:16", mode="blur", **kwargs)
        return out

    def test_renders_vertical_clip(self):
        out = self._render("plain.mp4")
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 20_000)
        info = render.probe(out)
        self.assertEqual((info["width"], info["height"]), (1080, 1920))

    def test_falls_back_when_hardware_encoder_is_broken(self):
        # «энкодер есть в списке ffmpeg, но железо его не поддерживает»
        render._HW_CACHE.clear()
        render._HW_CACHE["value"] = "h264_nvenc"
        try:
            out = self._render("fallback.mp4")
        finally:
            render._HW_CACHE.clear()
        self.assertTrue(out.exists(), "после отказа железа должен сработать libx264")
        info = render.probe(out)
        self.assertEqual((info["width"], info["height"]), (1080, 1920))

    def test_progress_reaches_one(self):
        seen = []
        out = self.tmp / "progress.mp4"
        render.render_clip(self.src, 0.0, 2.0, out, aspect="9:16", mode="blur",
                           on_progress=seen.append)
        self.assertTrue(seen)
        self.assertEqual(seen[-1], 1.0)
        self.assertTrue(all(0.0 <= p <= 1.0 for p in seen))

    def test_video_args_never_empty(self):
        for encoder in (None, "h264_videotoolbox", "h264_nvenc", "h264_qsv"):
            args = render._video_args(encoder, 21, "veryfast")
            self.assertIn("-c:v", args)
            self.assertGreaterEqual(len(args), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class LongClipRenderTest(unittest.TestCase):
    """Длинный клип: выражение кадрирования не должно превышать лимит ffmpeg."""

    def test_long_clip_with_crop_renders(self):
        import tempfile
        from viralcut import reframe

        tmp = Path(tempfile.mkdtemp(prefix="vc-long-"))
        src = make_source(tmp / "src.mp4", seconds=36.0)
        crop = reframe.build_crop_path(src, 0.0, 36.0, aspect="9:16")
        self.assertIsNotNone(crop)
        expr_x, expr_y = reframe.crop_expressions(crop)
        self.assertLess(len(expr_x), 2000, "слишком длинное x-выражение: ffmpeg упадёт")
        out = tmp / "long.mp4"
        render.render_clip(src, 0.0, 36.0, out, aspect="9:16", mode="auto", crop_path=crop)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 100_000)
