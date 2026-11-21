"""Tests for custom YouTube quality selection, section download, auto-update toggle, and audit fixes."""
from __future__ import annotations

import concurrent.futures
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from viralcut import (
    custom_cleanup,
    custom_debug,
    custom_downloader,
    custom_fonts,
    custom_updater,
    db,
    downloader,
    errors,
    jobs,
    main,
    pipeline,
    render,
    transcribe,
    updater,
    utils,
)


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

    def test_inspect_model_cache_survives_disappearing_incomplete_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp)
            blobs = models_dir / "models--Systran--faster-whisper-large-v3" / "blobs"
            blobs.mkdir(parents=True)
            inc = blobs / "abc.incomplete"
            inc.write_bytes(b"1234")
            with mock.patch.object(custom_debug, "MODELS_DIR", models_dir), \
                 mock.patch.object(custom_debug, "WHISPER_LOCAL_DIR", ""), \
                 mock.patch.object(custom_debug, "_safe_mtime", side_effect=[0.0]), \
                 mock.patch.object(custom_debug, "_live_file_size", return_value=0):
                res = custom_debug._inspect_model_cache("large-v3")
            self.assertTrue(res["downloading"])

    def test_idle_whisper_resolves_concrete_device(self):
        with mock.patch.object(custom_debug, "_WHISPER", {
            "model": "large-v3", "device": "auto", "compute_type": "int8", "status": "idle", "fallback_reason": None
        }), mock.patch.object(custom_debug, "_resolved_idle_whisper", return_value=("cuda", "float16")):
            snap = custom_debug.snapshot()
        self.assertEqual(snap["whisper"]["device"], "cuda")
        self.assertEqual(snap["whisper"]["compute_type"], "float16")


class CustomUpdaterTest(unittest.TestCase):
    def test_disabled_by_default_and_toggles_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "update.json"
            with mock.patch.object(updater, "STATE_FILE", state_file), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("VIRALCUT_UPDATE_CHECK", None)
                self.assertFalse(custom_updater.DEFAULT_UPDATE_CHECK)
                self.assertFalse(updater.enabled())
                self.assertFalse(updater.payload()["enabled"])
                self.assertFalse(updater.payload()["available"])

                with mock.patch.object(updater, "check") as mocked_check:
                    updater.maybe_check_async()
                    mocked_check.assert_not_called()

                res_on = updater.set_enabled(True)
                self.assertTrue(res_on["enabled"])
                self.assertTrue(updater.enabled())

                res_off = updater.set_enabled(False)
                self.assertFalse(res_off["enabled"])
                self.assertFalse(updater.enabled())

    def test_env_override_disables_persisted_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "update.json"
            with mock.patch.object(updater, "STATE_FILE", state_file):
                updater.set_enabled(True)
                with mock.patch.dict(os.environ, {"VIRALCUT_UPDATE_CHECK": "0"}):
                    self.assertFalse(updater.enabled())


class DbConnectTest(unittest.TestCase):
    def test_connect_closes_sqlite_connection_after_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            with mock.patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with db.connect() as conn:
                    conn.execute("SELECT 1").fetchone()
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")


class RenderFallbackTest(unittest.TestCase):
    def test_nvenc_fallback_preserves_output_tail_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"0" * 4096)
            calls = []

            def fake_run_proc(cmd, duration, out_path, encoder_label, source_res, target_res, on_progress):
                calls.append((encoder_label, list(cmd)))
                if len(calls) == 1:
                    return 1, ["nvenc init error"], False, ""
                out_path.write_bytes(b"x" * 16384)
                return 0, [], False, ""

            with mock.patch.object(render, "probe", return_value={"duration": 5.0, "width": 1920, "height": 1080, "fps": 30.0, "has_audio": False}), \
                 mock.patch.object(render, "hw_encoder", return_value="h264_nvenc"), \
                 mock.patch.object(render, "ffmpeg", return_value="ffmpeg"), \
                 mock.patch.object(render, "_run_render_proc", side_effect=fake_run_proc):
                res = render.render_clip(src, 0.0, 5.0, out)

            self.assertEqual(res["path"], out)
            self.assertEqual([c[0] for c in calls], ["h264_nvenc", "libx264"])
            fallback_cmd = calls[1][1]
            self.assertIn("libx264", fallback_cmd)
            self.assertEqual(fallback_cmd[-6:], ["-r", "30", "-movflags", "+faststart", "-shortest", str(out)])

    def test_run_render_proc_drains_large_stdout_without_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            script = "import sys; sys.stdout.write('x=1\\n'*500 + 'out_time_us=2500000\\n'); sys.stdout.flush()"
            rc, tail, killed, _ = render._run_render_proc(
                [sys.executable, "-c", script],
                5.0,
                out,
                "libx264",
                "1920x1080",
                "1080x1920",
                None,
            )
            self.assertEqual(rc, 0)
            self.assertFalse(killed)
            self.assertEqual(tail, [])


class CustomFontsSyncTest(unittest.TestCase):
    def test_sync_skips_unchanged_copies_and_is_thread_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            fonts_dir = Path(tmp) / "bundled"
            custom_dir = Path(tmp) / "custom"
            runtime_dir = Path(tmp) / "runtime"
            fonts_dir.mkdir()
            custom_dir.mkdir()
            (fonts_dir / "Sample.ttf").write_bytes(b"TTFDATA" * 100)

            with mock.patch.object(custom_fonts, "FONTS_DIR", fonts_dir), \
                 mock.patch.object(custom_fonts, "CUSTOM_DIR", custom_dir), \
                 mock.patch.object(custom_fonts, "RUNTIME_DIR", runtime_dir), \
                 mock.patch.object(custom_fonts, "META_PATH", custom_dir / "fonts.json"), \
                 mock.patch.object(Path, "symlink_to", side_effect=OSError("no symlink priv")):
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(lambda _: custom_fonts.sync(), range(4)))
                self.assertTrue(all(r == runtime_dir for r in results))
                self.assertTrue((runtime_dir / "Sample.ttf").is_file())

                with mock.patch.object(shutil, "copy2") as mocked_copy:
                    custom_fonts.sync()
                    mocked_copy.assert_not_called()


class AuditFixesTest(unittest.TestCase):
    def test_download_section_smart_propagates_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(downloader, "_pot_enabled", return_value=False), \
                 mock.patch.object(downloader, "_youtube_info", side_effect=errors.Cancelled()):
                with self.assertRaises(errors.Cancelled):
                    custom_downloader.download_section_smart(
                        "https://youtu.be/9-2Gz9343hQ", Path(tmp), 0.0, 5.0
                    )

    def test_clip_rerender_validation_and_eco_mode_support(self):
        clip_ok = {
            "id": "c1", "project_id": "p1", "idx": 0, "start": 10.0, "end": 25.0,
            "words": [], "settings": {"segments": True}, "hook": "h", "title": "t", "caption": "cap",
        }
        proj_ok = {"id": "p1", "url": "https://youtu.be/abc", "source": None, "duration": 120.0, "meta": {}}
        with mock.patch.object(main.db, "get_clip", return_value=clip_ok), \
             mock.patch.object(main.db, "get_project", return_value=proj_ok), \
             mock.patch.object(jobs, "submit_rerender") as mocked_submit:
            with self.assertRaises(HTTPException) as ctx:
                main.clip_rerender("c1", main.ClipEdit(start=10.0, end=11.0))
            self.assertEqual(ctx.exception.status_code, 400)
            mocked_submit.assert_not_called()

            res = main.clip_rerender("c1", main.ClipEdit(mode="auto"))
            self.assertTrue(res["ok"])
            mocked_submit.assert_called_once()

        with mock.patch.object(pipeline.db, "get_clip", return_value=clip_ok), \
             mock.patch.object(pipeline.db, "get_project", return_value=proj_ok), \
             mock.patch.object(pipeline.db, "update_clip"), \
             mock.patch.object(custom_downloader, "rerender_section_clip") as mocked_eco:
            pipeline.rerender_clip("c1", {"mode": "auto"})
            mocked_eco.assert_called_once()

    def test_transcribe_cleans_audio16k_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            media = workdir / "audio.webm"
            media.write_bytes(b"0" * 2048)
            wav = workdir / "audio16k.wav"

            def make_wav(_src, dst, sample_rate=16000):
                dst.write_bytes(b"RIFF" + b"0" * 100)
                return dst

            with mock.patch.object(utils, "probe", return_value={"duration": 12.0, "has_audio": True, "width": 0, "height": 0}), \
                 mock.patch.object(utils, "extract_asr_audio", side_effect=make_wav), \
                 mock.patch.object(transcribe, "_get_model", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    transcribe.transcribe(media, workdir)
            self.assertFalse(wav.exists())


class CustomCleanupTest(unittest.TestCase):
    def test_storage_summary_and_scoped_cleanup_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            projects_dir = data_dir / "projects"
            models_dir = data_dir / "models"
            db_path = data_dir / "viralcut.db"
            projects_dir.mkdir()
            (models_dir / "models--Systran--faster-whisper-large-v3").mkdir(parents=True)
            (models_dir / "models--Systran--faster-whisper-large-v3" / "model.bin").write_bytes(b"M" * (2 * 1024 * 1024))

            with mock.patch.object(db, "DB_PATH", db_path), \
                 mock.patch.object(custom_cleanup, "DATA_DIR", data_dir), \
                 mock.patch.object(custom_cleanup, "PROJECTS_DIR", projects_dir), \
                 mock.patch.object(custom_cleanup, "MODELS_DIR", models_dir), \
                 mock.patch.object(jobs, "active_project_ids", return_value=[]):
                db.init_db()
                custom_cleanup.invalidate_cache()

                # 1. done eco project: temp has audio16k.wav, zip, preview.mp4, audio.webm; clips stay
                p_done = db.create_project("https://youtu.be/done1", {}, title="DoneEco")
                p_done_dir = projects_dir / p_done["id"]
                (p_done_dir / "clips").mkdir(parents=True)
                (p_done_dir / "clips" / "clip_01.mp4").write_bytes(b"C" * (1024 * 1024))
                (p_done_dir / "audio16k.wav").write_bytes(b"W" * (512 * 1024))
                (p_done_dir / "preview.mp4").write_bytes(b"P" * (256 * 1024))
                (p_done_dir / "audio.webm").write_bytes(b"A" * (256 * 1024))
                (p_done_dir / f"viralcut_{p_done['id']}.zip").write_bytes(b"Z" * (512 * 1024))
                db.update_project(p_done["id"], status="done", source=str(p_done_dir / "audio.webm"), meta={"segments_mode": True})

                # 2. cancelled project with heavy source.mp4 and stale rendering clip
                p_canc = db.create_project("https://youtu.be/canc1", {}, title="Cancelled")
                p_canc_dir = projects_dir / p_canc["id"]
                p_canc_dir.mkdir(parents=True)
                (p_canc_dir / "source.mp4").write_bytes(b"S" * (2 * 1024 * 1024))
                (p_canc_dir / "source.mp4.part").write_bytes(b"P" * (128 * 1024))
                db.add_clip(p_canc["id"], 0, 0.0, 10.0, 0.5, "c", "h", "cap", [], {}, status="rendering")
                db.update_project(p_canc["id"], status="cancelled", source=str(p_canc_dir / "source.mp4"))

                # 3. review project: source.mp4 must NEVER be touched by temp or sources cleanup
                p_rev = db.create_project("https://youtu.be/rev1", {}, title="Review")
                p_rev_dir = projects_dir / p_rev["id"]
                p_rev_dir.mkdir(parents=True)
                (p_rev_dir / "source.mp4").write_bytes(b"R" * (1024 * 1024))
                db.update_project(p_rev["id"], status="review", source=str(p_rev_dir / "source.mp4"))

                # 4. running project: must be completely skipped even if it has temp files
                p_run = db.create_project("https://youtu.be/run1", {}, title="Running")
                p_run_dir = projects_dir / p_run["id"]
                p_run_dir.mkdir(parents=True)
                (p_run_dir / "audio16k.wav").write_bytes(b"X" * (512 * 1024))
                db.update_project(p_run["id"], status="running")

                # 5. orphan folder not in DB
                orphan_dir = projects_dir / "orphan_deadbeef"
                orphan_dir.mkdir(parents=True)
                (orphan_dir / "junk.tmp").write_bytes(b"O" * (512 * 1024))

                summary = custom_cleanup.storage_summary(force=True)
                self.assertEqual(summary["active_skipped"], 1)
                self.assertGreater(summary["temp_mb"], 1.5)
                self.assertAlmostEqual(summary["sources_mb"], 2.0, delta=0.1)
                self.assertAlmostEqual(summary["models_mb"], 2.0, delta=0.1)

                res_temp = main.debug_cleanup(main.CleanupIn(scope="temp"))
                self.assertTrue(res_temp["ok"])
                self.assertGreater(res_temp["freed_mb"], 1.5)
                self.assertFalse((p_done_dir / "audio16k.wav").exists())
                self.assertFalse((p_done_dir / "preview.mp4").exists())
                self.assertFalse((p_done_dir / "audio.webm").exists())
                self.assertFalse((p_done_dir / f"viralcut_{p_done['id']}.zip").exists())
                self.assertFalse((p_canc_dir / "source.mp4.part").exists())
                self.assertFalse(orphan_dir.exists())
                self.assertTrue((p_done_dir / "clips" / "clip_01.mp4").exists())
                self.assertTrue((p_canc_dir / "source.mp4").exists())
                self.assertTrue((p_rev_dir / "source.mp4").exists())
                self.assertTrue((p_run_dir / "audio16k.wav").exists())

                res_src = main.debug_cleanup(main.CleanupIn(scope="sources"))
                self.assertTrue(res_src["ok"])
                self.assertAlmostEqual(res_src["freed_mb"], 2.0, delta=0.1)
                self.assertFalse((p_canc_dir / "source.mp4").exists())
                self.assertIsNone(db.get_project(p_canc["id"])["source"])
                self.assertTrue((p_rev_dir / "source.mp4").exists())
                self.assertTrue((p_done_dir / "clips" / "clip_01.mp4").exists())
                self.assertTrue((models_dir / "models--Systran--faster-whisper-large-v3" / "model.bin").exists())

                with self.assertRaises(HTTPException) as ctx:
                    main.debug_cleanup(main.CleanupIn(scope="invalid"))
                self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
