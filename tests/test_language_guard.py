"""Язык субтитров: резервные субтитры и проверка письменности."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from viralcut import downloader, transcribe


class SubtitleChoiceTest(unittest.TestCase):
    def test_russian_preferred_over_english(self):
        files = [Path("/tmp/subs.en.vtt"), Path("/tmp/subs.ru.vtt")]
        self.assertTrue(downloader.pick_subtitle_file(files, "ru").endswith("subs.ru.vtt"))

    def test_english_when_asked(self):
        files = [Path("/tmp/subs.en.vtt"), Path("/tmp/subs.ru.vtt")]
        self.assertTrue(downloader.pick_subtitle_file(files, "en").endswith("subs.en.vtt"))

    def test_refuses_other_languages(self):
        self.assertIsNone(downloader.pick_subtitle_file([Path("subs.en.vtt")], "ru"))
        self.assertIsNone(downloader.pick_subtitle_file([Path("subs.ru.vtt")], "en"))

    def test_accepts_regional_language(self):
        self.assertEqual(downloader.pick_subtitle_file([Path("subs.en-US.vtt")], "en"),
                         "subs.en-US.vtt")

    def test_empty_list(self):
        self.assertIsNone(downloader.pick_subtitle_file([], "ru"))


class ScriptGuardTest(unittest.TestCase):
    def test_expected_script(self):
        self.assertEqual(transcribe._expected_script("ru"), "cyr")
        self.assertEqual(transcribe._expected_script("uk"), "cyr")
        self.assertEqual(transcribe._expected_script("en"), "lat")
        self.assertEqual(transcribe._expected_script("de"), "")

    def test_counts(self):
        self.assertEqual(transcribe._script_counts("Привет, мир!"), (9, 0))
        self.assertEqual(transcribe._script_counts("Hello"), (0, 5))

    def test_mismatch_triggers_russian_retry(self):
        """Ожидали русский (проба), а текст вышел английским — перераспознаём."""
        english_text = [
            {"text": "Look, we had a task to make the product in one week.", "start": 0.0, "end": 2.0},
            {"text": "The team built a prototype and checked it with users.", "start": 2.0, "end": 5.0},
        ]
        russian = [
            {"text": "Смотрите, у нас была задача сделать продукт за одну неделю.", "start": 0.0, "end": 2.0},
            {"text": "Команда собрала прототип и проверила его на пользователях.", "start": 2.0, "end": 5.0},
        ]
        with mock.patch.object(transcribe, "_transcribe_span",
                               return_value=(russian, {"language": "ru"})):
            fixed, language = transcribe._fix_language_mismatch(
                None, Path("/tmp/audio.wav"), Path("/tmp"), english_text, "en", None,
                probe="ru")
        self.assertEqual(language, "ru")
        self.assertTrue(fixed[0]["text"].startswith("Смотрите"))

    def test_cyrillic_text_with_english_probe_is_retranscribed(self):
        russian = [
            {"text": "Смотрите, у нас была задача сделать продукт за одну неделю.", "start": 0.0, "end": 2.0},
            {"text": "Команда собрала прототип и проверила его на пользователях.", "start": 2.0, "end": 5.0},
        ]
        english = [
            {"text": "Look, we had a task to make the product in one week.", "start": 0.0, "end": 2.0},
            {"text": "The team built a prototype and checked it with users.", "start": 2.0, "end": 5.0},
        ]
        with mock.patch.object(transcribe, "_transcribe_span",
                               return_value=(english, {"language": "en"})):
            fixed, language = transcribe._fix_language_mismatch(
                None, Path("/tmp/audio.wav"), Path("/tmp"), russian, "ru", None,
                probe="en")
        self.assertEqual(language, "en")
        self.assertIn("Look", fixed[0]["text"])

    def test_good_match_left_alone(self):
        russian = [
            {"text": "Смотрите, у нас была задача сделать продукт за одну неделю.", "start": 0.0, "end": 2.0},
            {"text": "Команда собрала прототип и проверила его на пользователях.", "start": 2.0, "end": 5.0},
        ]
        with mock.patch.object(transcribe, "_transcribe_span") as retry:
            fixed, language = transcribe._fix_language_mismatch(
                None, Path("/tmp/audio.wav"), Path("/tmp"), russian, "ru", None)
        retry.assert_not_called()
        self.assertEqual(language, "ru")
        self.assertEqual(len(fixed), 2)

    def test_short_text_not_retried(self):
        short = [{"text": "Thank you.", "start": 0.0, "end": 1.0}]
        with mock.patch.object(transcribe, "_transcribe_span") as retry:
            fixed, language = transcribe._fix_language_mismatch(
                None, Path("/tmp/audio.wav"), Path("/tmp"), short, "en", None)
        retry.assert_not_called()
        self.assertEqual(language, "en")


if __name__ == "__main__":
    unittest.main()
