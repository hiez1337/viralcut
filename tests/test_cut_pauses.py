"""Вырезание пауз: план склейки доходит до рендера, слова и кроп едут вместе."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from viralcut import analyze, config, db, jobs, pipeline, reframe, render, subtitles, transcribe, utils


def make_paused_source(path: Path) -> Path:
    """9 секунд с речью-тоном и двумя паузами: 2–4 с и 6–7.5 с."""
    graph = ("sine=frequency=440:duration=2[a0];"
             "anullsrc=r=16000:cl=mono,atrim=duration=2[a1];"
             "sine=frequency=440:duration=2[a2];"
             "anullsrc=r=16000:cl=mono,atrim=duration=1.5[a3];"
             "sine=frequency=440:duration=1.5[a4];"
             "[a0][a1][a2][a3][a4]concat=n=5:v=0:a=1[a]")
    subprocess.run(
        [config.ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=640x360:rate=30:duration=9",
         "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


class CutSpansTest(unittest.TestCase):
    def test_single_span_is_not_cut(self):
        self.assertEqual(render._cut_spans([{"start": 1.0, "end": 9.0}], 1.0, 9.0), [])

    def test_merges_tiny_gaps_and_drops_slivers(self):
        spans = [
            {"start": 0.0, "end": 5.0},
            {"start": 5.01, "end": 8.0},   # шов меньше 0.03 с — склеиваем
            {"start": 8.5, "end": 8.52},   # огрызок 0.02 с — выбрасываем
            {"start": 8.6, "end": 12.0},
        ]
        clean = render._cut_spans(spans, 0.0, 12.0)
        self.assertEqual(clean, [{"start": 0.0, "end": 8.0}, {"start": 8.6, "end": 12.0}])

    def test_rejects_plan_that_eats_most_of_clip(self):
        spans = [{"start": 0.0, "end": 1.0}, {"start": 11.0, "end": 12.0}]
        self.assertEqual(render._cut_spans(spans, 0.0, 12.0), [])

    def test_word_intersecting_two_spans_is_emitted_once(self):
        spans = [{"start": 0.0, "end": 2.0, "output": 0.0},
                 {"start": 4.0, "end": 5.0, "output": 2.0}]
        words = [{"word": "раз", "start": 1.8, "end": 4.3},
                 {"word": "два", "start": 4.8, "end": 5.3}]
        self.assertEqual(analyze.remap_words(words, spans), [
            {"word": "раз", "start": 1.8, "end": 2.3},
            {"word": "два", "start": 2.8, "end": 3.0},
        ])


class PauseSpansTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vc-pause-"))
        self.src = make_paused_source(self.tmp / "paused.mp4")

    def test_pipeline_plan_removes_both_pauses(self):
        spans = pipeline._pause_spans(self.src, 0.0, 9.0)
        self.assertGreaterEqual(len(spans), 3, f"паузы не найдены: {spans}")
        total = sum(s["end"] - s["start"] for s in spans)
        self.assertLess(total, 8.0, "вырезанные паузы должны уменьшить длительность")

    def test_render_cuts_pauses_and_keeps_duration(self):
        spans = pipeline._pause_spans(self.src, 0.0, 9.0)
        out = self.tmp / "cut.mp4"
        result = render.render_clip(self.src, 0.0, 9.0, out, aspect="9:16",
                                    mode="blur", spans=spans)
        info = render.probe(out)
        self.assertAlmostEqual(info["duration"], result["duration"], delta=0.6)
        self.assertLess(info["duration"], 8.5, "клип должен стать короче исходных 9 с")
        self.assertGreater(info["duration"], 4.5, "нельзя выбросить речь вместе с паузами")

    def test_subtitles_follow_cut_timeline(self):
        spans = pipeline._pause_spans(self.src, 0.0, 9.0)
        words = [{"word": "до", "start": 0.5, "end": 1.0},
                 {"word": "в_паузе", "start": 2.5, "end": 3.0},
                 {"word": "после", "start": 4.5, "end": 5.0}]
        moved = analyze.remap_words(words, spans)
        self.assertEqual([w["word"] for w in moved], ["до", "после"])
        out_len = spans[-1]["output"] + (spans[-1]["end"] - spans[-1]["start"])
        ass = subtitles.build_ass(moved, 0.0, out_len, speed="calm")
        self.assertNotIn("в_паузе", ass.upper())


class RemapCropPathTest(unittest.TestCase):
    def test_points_inside_pauses_are_dropped(self):
        path = {"times": [0.0, 1.0, 2.0, 3.0, 4.0],
                "x": [10.0, 20.0, 30.0, 40.0, 50.0],
                "y": [1.0, 2.0, 3.0, 4.0, 5.0],
                "crop_w": 100, "crop_h": 100, "source": [200, 200]}
        spans = [{"start": 0.0, "end": 1.5}, {"start": 3.5, "end": 4.0}]
        out = reframe.remap_crop_path(path, spans, 0.0)
        self.assertIsNotNone(out)
        self.assertEqual(out["times"], [0.0, 1.0, 2.0])
        self.assertEqual(out["x"], [10.0, 20.0, 50.0])
        self.assertAlmostEqual(out["times"][-1], 2.0, places=2)

    def test_too_few_points_falls_back(self):
        path = {"times": [2.0], "x": [10.0], "y": [1.0],
                "crop_w": 100, "crop_h": 100, "source": [200, 200]}
        self.assertIsNone(reframe.remap_crop_path(path, [{"start": 0.0, "end": 1.0}], 0.0))


class RenderOneIntegrationTest(unittest.TestCase):
    """_render_one обязан передать spans в рендер и пересчитать субтитры."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vc-render-one-"))
        self.source = self.tmp / "source.mp4"
        self.source.write_bytes(b"x")  # содержимое не читается: всё замокано

    def test_spans_and_remapped_subtitles_reach_renderer(self):
        curve = [-30.0] * 40
        for i in range(8, 16):
            curve[i] = -55.0  # 2 секунды тишины в окне 0–10 с
        clip = {
            "id": "c1", "idx": 0, "start": 0.0, "end": 10.0,
            "words": [{"word": "раз", "start": 0.5, "end": 0.9},
                      {"word": "пауза", "start": 2.5, "end": 3.0},
                      {"word": "два", "start": 5.0, "end": 5.4}],
            "hook": "", "title": "Тест",
        }
        settings = {"subtitles": True, "mode": "crop", "aspect": "9:16",
                    "cut_pauses": True, "uppercase": False, "show_hook": False,
                    "sub_size": 118, "sub_font": "montserrat",
                    "sub_position": "bottom", "sub_offset": 0, "sub_speed": "calm"}
        captured = {}

        def fake_render(source, start, end, out_path, **kwargs):
            captured.update(kwargs)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"0" * 20_000)
            return {"path": out_path, "duration": 8.0, "width": 1080, "height": 1920}

        with mock.patch.object(analyze, "cached_energy_curve", return_value=(curve, -30.0)), \
             mock.patch.object(render, "render_clip", side_effect=fake_render), \
             mock.patch.object(render, "make_thumb", return_value=None), \
             mock.patch.object(render, "make_preview", return_value=None), \
             mock.patch.object(pipeline.db, "update_clip") as update:
            pipeline._render_one(self.source, self.tmp, clip, settings, None, {})

        spans = captured.get("spans")
        self.assertTrue(spans and len(spans) > 1, f"рендер без spans: {captured}")
        ass_text = (self.tmp / "ass" / "c1.ass").read_text(encoding="utf-8")
        self.assertIn("РАЗ", ass_text.upper())
        self.assertNotIn("ПАУЗА", ass_text.upper())
        update.assert_called_once()
        saved_settings = update.call_args.kwargs["settings"]
        self.assertEqual(saved_settings["rendered_spans"], spans)
        srt = pipeline.clip_srt({**clip, "settings": saved_settings})
        second_word = next(w for w in analyze.remap_words(clip["words"], spans) if w["word"] == "два")
        self.assertIn("00:00:0" + f"{second_word['end']:.3f}".replace(".", ","), srt)
        self.assertNotIn("пауза", srt)

    def test_rejected_plan_leaves_the_original_subtitle_timeline(self):
        with mock.patch.object(analyze, "cached_energy_curve", return_value=([-30.0], -30.0)), \
             mock.patch.object(analyze, "cut_plan", return_value=[
                 {"start": 0.0, "end": 1.0, "output": 0.0},
                 {"start": 9.0, "end": 10.0, "output": 1.0},
             ]):
            spans = pipeline._pause_spans(self.source, 0.0, 10.0)
        self.assertEqual(spans, [])
        words = [{"word": "конец", "start": 9.5, "end": 9.85}]
        self.assertEqual(pipeline._subtitle_timeline({"words": words, "start": 0.0, "end": 10.0}, spans),
                         (words, 0.0, 10.0))


class SubtitleTimingTest(unittest.TestCase):
    def test_short_phrases_survive_in_all_presets_without_overlapping(self):
        words = [
            {"word": "да.", "start": 0.0, "end": 0.25},
            {"word": "точно.", "start": 0.16, "end": 0.26},
            {"word": "длиннаяреплика.", "start": 0.24, "end": 0.91},
            {"word": "финал.", "start": 1.98, "end": 2.05},
        ]
        for speed in subtitles.SPEEDS:
            with self.subTest(speed=speed):
                ass = subtitles.build_ass(words, 0.0, 2.0, show_hook=False, uppercase=False, speed=speed)
                lines = [row.split(",", 9) for row in ass.splitlines() if row.startswith("Dialogue: 0,")]
                for word in words:
                    self.assertIn(word["word"], ass)
                for line in lines:
                    self.assertLess(line[1], line[2])
                for current, following in zip(lines, lines[1:]):
                    self.assertLessEqual(current[2], following[1])


class AudioTrackTest(unittest.TestCase):
    def test_audio_extraction_and_pause_cache_use_selected_stream(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paused = make_paused_source(root / "paused.mp4")
            source = root / "two-tracks.mp4"
            subprocess.run(
                [config.ffmpeg(), "-v", "error", "-i", str(paused),
                 "-f", "lavfi", "-i", "sine=frequency=880:duration=9",
                 "-map", "0:v", "-map", "0:a:0", "-map", "1:a:0",
                 "-c:v", "copy", "-c:a", "aac", str(source)],
                check=True,
            )
            self.assertEqual(utils.probe(source)["audio_tracks"], 2)
            self.assertTrue(pipeline._pause_spans(source, 0, 9, audio_track=0))
            self.assertEqual(pipeline._pause_spans(source, 0, 9, audio_track=1), [])
            # 2.5–3.5 с: первая дорожка молчит, вторая содержит тон.
            quiet = utils.extract_asr_audio(source, root / "quiet.wav", audio_track=0, window=(2.5, 3.5))
            active = utils.extract_asr_audio(source, root / "active.wav", audio_track=1, window=(2.5, 3.5))
            self.assertLess(abs(utils.read_audio_mono(quiet)).max(), 0.001)
            self.assertGreater(abs(utils.read_audio_mono(active)).max(), 0.03)
            self.assertAlmostEqual(utils.media_duration(active), 1.0, delta=0.03)

    def test_switching_track_recognizes_only_clip_window_and_saves_absolute_words(self):
        clip = {"id": "c1", "project_id": "p1", "start": 30.0, "end": 35.0, "path": "existing.mp4",
                "title": "Тест", "hook": "", "caption": "", "words": [], "settings": {"audio_track": 0}}
        project = {"id": "p1", "source": "source.mp4", "options": {"audio_track": 1}}
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(pipeline, "PROJECTS_DIR", Path(folder)), \
             mock.patch.object(db, "get_clip", return_value=clip), \
             mock.patch.object(db, "get_project", return_value=project), \
             mock.patch.object(utils, "probe", return_value={"audio_tracks": 2}), \
             mock.patch.object(transcribe, "transcribe", return_value={
                 "segments": [{"words": [{"word": "новая", "start": 0.4, "end": 0.7}]}],
             }) as asr, \
             mock.patch.object(pipeline, "_render_one") as render_one:
            pipeline.rerender_clip("c1", {})
        self.assertEqual(asr.call_args.kwargs["audio_track"], 1)
        self.assertEqual(asr.call_args.kwargs["window"], (30.0, 35.0))
        self.assertEqual(render_one.call_args.args[5]["audio_track"], 1)
        self.assertEqual(render_one.call_args.kwargs["clip_updates"]["words"],
                         [{"word": "новая", "start": 30.4, "end": 30.7}])


class RerenderRecoveryTest(unittest.TestCase):
    def test_agent_undo_restores_snapshot_and_respects_later_edits(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(db, "DB_PATH", Path(folder) / "test.db"), \
             mock.patch.object(jobs._clip_pool, "submit", side_effect=lambda work: work()), \
             mock.patch.object(jobs, "_agent_edits", []):
            db.init_db()
            project = db.create_project("local:source", {})
            clip = db.add_clip(project["id"], 0, 0, 5, 1.0, "Тест", "", "",
                               [{"word": "Привет", "start": 0.1, "end": 0.9}],
                               {"accent": "pink"}, status="ready")
            pid, cid = project["id"], clip["id"]

            def render_saved(clip_id, payload, *, restore=None):
                current = db.get_clip(clip_id)
                fields = restore if restore is not None else {"settings": {**current["settings"], **payload}}
                db.update_clip(clip_id, status="ready", **fields)
                return db.get_clip(clip_id)

            with mock.patch.object(pipeline, "rerender_clip", side_effect=render_saved):
                self.assertTrue(jobs.submit_rerender(cid, {"accent": "yellow"}, agent_edit=True))
                self.assertTrue(jobs.submit_rerender(cid, {"accent": "green"}, agent_edit=True))
                self.assertTrue(jobs.undo_agent_edit(pid, cid)[0])
                self.assertEqual(db.get_clip(cid)["settings"]["accent"], "yellow")
                self.assertTrue(jobs.undo_agent_edit(pid)[0])
                self.assertEqual(db.get_clip(cid)["settings"], clip["settings"])
                self.assertEqual(db.get_clip(cid)["words"], clip["words"])
                self.assertTrue(jobs.submit_rerender(cid, {"accent": "yellow"}, agent_edit=True))
                db.update_clip(cid, settings={"accent": "cyan"})
                self.assertFalse(jobs.undo_agent_edit(pid, cid)[0])
                self.assertEqual(db.get_clip(cid)["settings"]["accent"], "cyan")

    def test_failed_job_leaves_old_clip_ready_and_its_settings_intact(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clip.mp4"
            path.write_bytes(b"original clip")
            with mock.patch.object(db, "DB_PATH", Path(folder) / "test.db"), \
                 mock.patch.object(jobs._clip_pool, "submit", side_effect=lambda work: work()), \
                 mock.patch.object(pipeline, "rerender_clip", side_effect=utils.MediaError("encoder failed")):
                db.init_db()
                project = db.create_project("local:source", {})
                clip = db.add_clip(project["id"], 0, 0, 5, 1.0, "Тест", "Первый", "", [], {},
                                   status="ready")
                db.update_clip(clip["id"], path=str(path))
                self.assertTrue(jobs.submit_rerender(clip["id"], {"hook": "Второй"}))
                ready = db.get_clip(clip["id"])
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["hook"], "Первый")
            self.assertEqual(ready["error"], "encoder failed")
            self.assertEqual(path.read_bytes(), b"original clip")


class OptionTest(unittest.TestCase):
    def test_cut_pauses_survives_normalization(self):
        opts = pipeline.normalize_options({"cut_pauses": True})
        self.assertTrue(opts["cut_pauses"])
        self.assertFalse(pipeline.normalize_options({})["cut_pauses"])

    def test_clip_settings_keep_flag(self):
        opts = pipeline.normalize_options({"cut_pauses": True})
        self.assertTrue(pipeline._clip_settings(opts)["cut_pauses"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
