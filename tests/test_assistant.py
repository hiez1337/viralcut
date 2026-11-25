"""Встроенный помощник: приветствие, локальные ответы и путь через модель."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from viralcut import assistant, settings


def _ctx(configured: bool = False, model: str = "deepseek-chat",
         provider: str = "deepseek") -> dict:
    return {
        "version": "1.5.16",
        "platform": "darwin",
        "ai": {"configured": configured, "model": model, "provider": provider},
        "limits": {"power": 50, "cpu_limit": 100, "render_workers": 2},
        "gpu": "Apple M1",
        "hw": "h264_videotoolbox",
        "model_cached": True,
    }


class WelcomeTest(unittest.TestCase):
    def test_without_key_mentions_settings(self):
        text = assistant.welcome(_ctx(configured=False))
        self.assertIn("Ключ ИИ пока не добавлен", text)
        self.assertIn("Настройках → ИИ-помощник", text)

    def test_with_key_mentions_model(self):
        text = assistant.welcome(_ctx(configured=True, model="deepseek-chat"))
        self.assertIn("deepseek-chat", text)

    def test_missing_model_warns(self):
        context = _ctx()
        context["model_cached"] = False
        self.assertIn("Модель распознавания", assistant.welcome(context))

    def test_suggestions_are_questions(self):
        items = assistant.suggestions()
        self.assertTrue(3 <= len(items) <= 8)
        self.assertTrue(all(item.strip() for item in items))


class LocalAnswerTest(unittest.TestCase):
    CASES = [
        ("как нарезать видео", "start"),
        ("с чего начать", "start"),
        ("как включить ИИ-режим", "ai_mode"),
        ("куда вставить ключ deepseek", "ai_key"),
        ("почему тормозит компьютер", "limits"),
        ("выходит за кадр субтитры", "subtitles"),
        ("плагины youtube hd", "plugins"),
        ("не открывается на windows 10", "requirements"),
        ("вырезать паузы", "pauses"),
        ("прямой эфир twitch", "streams"),
    ]

    def test_keyword_matching(self):
        for question, expected in self.CASES:
            with self.subTest(question=question):
                found = assistant.local_answer(question)
                self.assertIsNotNone(found, question)
                self.assertEqual(found["section"], expected)

    def test_unknown_question_falls_back(self):
        found = assistant.local_answer("расскажи про квантовую физику")
        self.assertIsNone(found)
        answer = assistant.fallback_answer("расскажи про квантовую физику")
        self.assertEqual(answer["source"], "local")
        self.assertIn("Могу рассказать", answer["answer"])

    def test_knowledge_contains_key_sections(self):
        text = assistant.knowledge_text()
        for title in ("ИИ-режим", "Плагины и YouTube", "Мощность и лимиты", "Требования к системе"):
            self.assertIn(title, text)


class AskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "settings.json"
        self.patch = mock.patch.object(settings, "SETTINGS_FILE", self.tmp)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_without_key_answers_locally(self):
        settings.update({"ai_key": ""})
        result = assistant.ask("как нарезать видео", _ctx(configured=False))
        self.assertEqual(result["source"], "local")
        self.assertIn("С чего начать", result["answer"])
        self.assertIn("встроенным правилам", result.get("hint", ""))

    def test_with_key_calls_model(self):
        settings.update({"ai_key": "sk-test-1", "ai_provider": "custom",
                         "ai_base": "http://127.0.0.1:9", "ai_model": "test"})
        with mock.patch("viralcut.ai.chat", return_value={"text": "Готово", "model": "test"}):
            result = assistant.ask("как нарезать видео", _ctx(configured=True))
        self.assertEqual(result["source"], "ai")
        self.assertEqual(result["answer"], "Готово")

    def test_model_error_falls_back(self):
        from viralcut import ai

        settings.update({"ai_key": "sk-test-1", "ai_provider": "custom",
                         "ai_base": "http://127.0.0.1:9", "ai_model": "test"})
        with mock.patch("viralcut.ai.chat", side_effect=ai.AIError("нет сети")):
            result = assistant.ask("плагины youtube", _ctx(configured=True))
        self.assertEqual(result["source"], "local")
        self.assertIn("Модель недоступна", result.get("hint", ""))

    def test_empty_question(self):
        result = assistant.ask("   ", _ctx())
        self.assertEqual(result["source"], "local")

    def test_prompt_includes_state(self):
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return {"text": "ок", "model": "test"}

        settings.update({"ai_key": "sk-test-1", "ai_provider": "custom",
                         "ai_base": "http://127.0.0.1:9", "ai_model": "test"})
        with mock.patch("viralcut.ai.chat", side_effect=fake_chat):
            assistant.ask("привет", _ctx(configured=True))
        system = captured["messages"][0]["content"]
        self.assertIn("База знаний", system)
        self.assertIn("Состояние", system)
        self.assertIn("deepseek-chat", system)


if __name__ == "__main__":
    unittest.main()
