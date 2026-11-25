"""Действия помощника: разбор просьб, белый список и выполнение."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from viralcut import assistant_actions as actions
from viralcut import settings


class ParseLocalTest(unittest.TestCase):
    def _types(self, text: str):
        return [item["type"] for item in actions.parse_local(text)]

    def test_cpu_limit_request(self):
        parsed = actions.parse_local("поставь лимит 40 процентов процессора")
        self.assertEqual(parsed[0]["type"], "set_settings")
        self.assertEqual(parsed[0]["cpu_limit"], 40)

    def test_cpu_limit_with_word_number(self):
        parsed = actions.parse_local("ограничь процессор до тридцати процентов")
        self.assertEqual(parsed[0]["cpu_limit"], 30)

    def test_power_request(self):
        parsed = actions.parse_local("поставь мощность 80")
        self.assertEqual(parsed[0]["power"], 80)

    def test_gpu_choice(self):
        self.assertEqual(actions.parse_local("выбери дискретную видеокарту")[0]["gpu_preference"],
                         "discrete")
        self.assertEqual(actions.parse_local("используй встроенную карту")[0]["gpu_preference"],
                         "integrated")

    def test_ai_mode(self):
        parsed = actions.parse_local("включи ии-режим")
        self.assertEqual(parsed[0]["type"], "set_ai_mode")
        self.assertTrue(parsed[0]["enabled"])

    def test_cut_with_count_and_aspect(self):
        parsed = actions.parse_local("нарежь 5 клипов в 9:16")
        cut = [item for item in parsed if item["type"] == "start_cut"][0]
        self.assertEqual(cut["count"], 5)
        self.assertEqual(cut["aspect"], "9:16")

    def test_cut_with_url(self):
        parsed = actions.parse_local("нарежь https://youtu.be/abc123")
        cut = [item for item in parsed if item["type"] == "start_cut"][0]
        self.assertEqual(cut["url"], "https://youtu.be/abc123")

    def test_open_settings(self):
        self.assertIn("open_settings", self._types("открой настройки"))

    def test_clip_number_used_for_edit_and_undo(self):
        self.assertEqual(actions.parse_local("поправь первый клип, сделай субтитры жёлтыми"),
                         [{"type": "rerender_clip", "clip_number": 1, "accent": "yellow"}])
        self.assertEqual(actions.parse_local("отмени правку второго клипа"),
                         [{"type": "undo_clip", "clip_number": 2}])
        self.assertIsNone(actions.parse_undo("отмени загрузку"))

    def test_plain_question_has_no_actions(self):
        self.assertEqual(actions.parse_local("что такое субтитры?"), [])

    def test_empty_question(self):
        self.assertEqual(actions.parse_local("   "), [])


class ValidateTest(unittest.TestCase):
    def test_unknown_action_dropped(self):
        self.assertEqual(actions.validate([{"type": "delete_everything"}]), [])

    def test_not_a_list(self):
        self.assertEqual(actions.validate({"type": "open_settings"}), [])

    def test_values_clamped(self):
        parsed = actions.validate([{"type": "set_settings", "cpu_limit": 500, "power": -5}])
        self.assertEqual(parsed[0]["cpu_limit"], 100)
        self.assertEqual(parsed[0]["power"], 0)

    def test_start_cut_without_target_applies_to_open_project(self):
        # цель может прийти из контекста (открытый проект) — валидатор не режет
        self.assertEqual(actions.validate([{"type": "start_cut"}])[0]["type"], "start_cut")
        parsed = actions.validate([{"type": "start_cut", "url": "https://x"}])
        self.assertEqual(parsed[0]["url"], "https://x")

    def test_bad_aspect_ignored(self):
        parsed = actions.validate([{"type": "start_cut", "url": "https://x", "aspect": "2:1"}])
        self.assertNotIn("aspect", parsed[0])


class ParseFromTextTest(unittest.TestCase):
    def test_extracts_fenced_actions(self):
        text = 'Готово.\n```json\n{"actions":[{"type":"open_settings"}]}\n```'
        self.assertEqual(actions.parse_from_text(text)[0]["type"], "open_settings")

    def test_extracts_inline_actions(self):
        text = 'Сделал {"actions":[{"type":"set_settings","power":60}]}'
        self.assertEqual(actions.parse_from_text(text)[0]["power"], 60)

    def test_no_actions(self):
        self.assertEqual(actions.parse_from_text("просто текст"), [])

    def test_strip_actions_removes_block(self):
        text = 'Готово.\n```json\n{"actions":[{"type":"open_settings"}]}\n```'
        self.assertEqual(actions.strip_actions(text), "Готово.")

    def test_strip_actions_keeps_text_without_block(self):
        self.assertEqual(actions.strip_actions("просто текст"), "просто текст")


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "settings.json"
        self.patch = mock.patch.object(settings, "SETTINGS_FILE", self.tmp)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_set_settings_applies_and_reports(self):
        from viralcut import cpu_limit, resources

        result = actions.execute({"type": "set_settings", "cpu_limit": 40, "power": 70})
        self.assertTrue(result["ok"])
        self.assertEqual(settings.get()["cpu_limit"], 40)
        self.assertEqual(settings.get()["power"], 70)
        self.assertEqual(result["summary"]["cpu_limit"], 40)
        self.assertIn("cpu_limit=40", result["message"])
        resources.invalidate()
        cpu_limit.stop()

    def test_set_ai_mode_returns_ui(self):
        result = actions.execute({"type": "set_ai_mode", "enabled": True, "prompt": "вирусное"})
        self.assertEqual(result["ui"]["ai_mode"], True)
        self.assertEqual(result["ui"]["prompt"], "вирусное")

    def test_open_settings_returns_ui(self):
        self.assertEqual(actions.execute({"type": "open_settings"})["ui"]["open_settings"], True)

    def test_open_project_missing(self):
        with mock.patch.object(actions.db, "get_project", return_value=None):
            self.assertFalse(actions.execute({"type": "open_project", "project_id": "nope"})["ok"])

    def test_open_project_survives_broken_db(self):
        with mock.patch.object(actions.db, "get_project", side_effect=RuntimeError("нет базы")):
            result = actions.execute({"type": "open_project", "project_id": "abc"})
        self.assertFalse(result["ok"])
        self.assertIn("не найден", result["message"])

    def test_start_cut_without_target(self):
        result = actions.execute({"type": "start_cut"}, {})
        self.assertFalse(result["ok"])
        self.assertIn("ссылку", result["message"])

    def test_start_cut_existing_project(self):
        project = {"id": "abc123456789", "status": "review", "options": {"count": 3}}
        with mock.patch.object(actions.db, "get_project", return_value=project), \
             mock.patch.object(actions.db, "update_project") as update, \
             mock.patch.object(actions.jobs, "submit_process") as submit:
            result = actions.execute({"type": "start_cut", "project_id": "abc123456789", "count": 5})
        self.assertTrue(result["ok"])
        update.assert_called_once()
        submit.assert_called_once_with("abc123456789")

    def test_start_cut_new_url(self):
        created = {"id": "new123456789"}
        with mock.patch.object(actions.db, "create_project", return_value=created), \
             mock.patch.object(actions.db, "update_project"), \
             mock.patch.object(actions.jobs, "submit_prepare") as prepare:
            result = actions.execute({"type": "start_cut", "url": "https://youtu.be/x",
                                      "ai_mode": True, "count": 2})
        self.assertTrue(result["ok"])
        self.assertEqual(result["project_id"], "new123456789")
        prepare.assert_called_once_with("new123456789")

    def test_unknown_action(self):
        self.assertFalse(actions.execute({"type": "rm_rf"})["ok"])


if __name__ == "__main__":
    unittest.main()
