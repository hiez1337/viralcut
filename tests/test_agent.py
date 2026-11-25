"""Агент: шаги, лимит шагов, локальный режим и определение локального сервера."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from viralcut import agent, ai, settings


def _ctx(configured: bool = True) -> dict:
    return {
        "version": "1.5.18",
        "platform": "darwin",
        "ai": {"configured": configured, "model": "test", "provider": "custom"},
        "limits": {"power": 50, "cpu_limit": 100, "render_workers": 2},
    }


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "settings.json"
        self.patch = mock.patch.object(settings, "SETTINGS_FILE", self.tmp)
        self.patch.start()
        settings.update({"ai_key": "sk-test-1", "ai_provider": "custom",
                         "ai_base": "http://127.0.0.1:9", "ai_model": "test",
                         "agent_mode": True})

    def tearDown(self):
        self.patch.stop()

    def test_without_model_runs_local_actions(self):
        with mock.patch("viralcut.assistant_actions.execute",
                        return_value={"ok": True, "type": "set_settings",
                                      "message": "Настройки применены"}) as execute:
            result = agent.run("поставь лимит 40 процентов", _ctx(configured=False))
        self.assertEqual(result["source"], "local")
        self.assertTrue(execute.called)
        self.assertIn("Готово", result["answer"])

    def test_multi_step_then_final(self):
        replies = [
            {"text": 'Делаю.\n```json\n{"actions":[{"type":"set_settings","power":70}]}\n```',
             "model": "test"},
            {"text": "Готово, мощность 70%.", "model": "test"},
        ]
        calls = {"n": 0}

        def fake_chat(messages, **kwargs):
            reply = replies[min(calls["n"], len(replies) - 1)]
            calls["n"] += 1
            return reply

        with mock.patch("viralcut.ai.chat", side_effect=fake_chat), \
             mock.patch("viralcut.assistant_actions.execute",
                        return_value={"ok": True, "type": "set_settings",
                                      "message": "Настройки применены"}):
            result = agent.run("поставь мощность 70", _ctx())
        self.assertEqual(result["source"], "ai")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][0]["actions"][0]["type"], "set_settings")
        self.assertEqual(result["answer"], "Готово, мощность 70%.")

    def test_step_limit(self):
        reply = {"text": '```json\n{"actions":[{"type":"set_settings","power":10}]}\n```',
                 "model": "test"}
        with mock.patch("viralcut.ai.chat", return_value=reply), \
             mock.patch("viralcut.assistant_actions.execute",
                        return_value={"ok": True, "type": "set_settings", "message": "ок"}):
            result = agent.run("поставь мощность 10", _ctx())
        self.assertLessEqual(len(result["steps"]), agent.MAX_STEPS)
        self.assertIn("шагов", result["answer"].lower() + " шагов")

    def test_model_failure_after_step_keeps_result(self):
        replies = [{"text": '```json\n{"actions":[{"type":"set_settings","power":20}]}\n```',
                    "model": "test"}]
        calls = {"n": 0}

        def fake_chat(messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return replies[0]
            raise RuntimeError("сеть пропала")

        with mock.patch("viralcut.ai.chat", side_effect=fake_chat), \
             mock.patch("viralcut.assistant_actions.execute",
                        return_value={"ok": True, "type": "set_settings", "message": "ок"}):
            result = agent.run("поставь мощность 20", _ctx())
        self.assertIn("недоступна", result["answer"])
        self.assertEqual(len(result["steps"]), 1)

    def test_model_failure_without_steps_falls_back_locally(self):
        with mock.patch("viralcut.ai.chat", side_effect=RuntimeError("нет сети")), \
             mock.patch("viralcut.assistant_actions.execute",
                        return_value={"ok": True, "type": "set_settings", "message": "ок"}):
            result = agent.run("поставь лимит 40 процентов", _ctx())
        self.assertEqual(result["source"], "local")
        self.assertIn("правилам", result.get("hint", ""))

    def test_empty_task(self):
        result = agent.run("   ", _ctx())
        self.assertEqual(result["steps"], [])
        self.assertIn("задание", result["answer"].lower())


class LocalServerTest(unittest.TestCase):
    def _response(self, payload: dict):
        class Response:
            def read(self):
                return json.dumps(payload).encode()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        return Response()

    def test_detects_ollama(self):
        payload = {"data": [{"id": "qwen2.5:7b"}, {"id": "llama3.1:8b"}]}
        with mock.patch.object(ai.urllib.request, "urlopen", return_value=self._response(payload)):
            found = ai.local_server()
        self.assertTrue(found["available"])
        self.assertEqual(found["base"], "http://127.0.0.1:11434/v1")
        self.assertIn("qwen2.5:7b", found["models"])

    def test_no_server(self):
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=OSError("нет")):
            found = ai.local_server()
        self.assertFalse(found["available"])

    def test_local_provider_needs_no_key(self):
        tmp = Path(tempfile.mkdtemp()) / "settings.json"
        with mock.patch.object(settings, "SETTINGS_FILE", tmp):
            settings.update({"ai_provider": "local", "ai_key": ""})
            self.assertTrue(ai.configured())
            self.assertEqual(ai.status()["base"], "http://127.0.0.1:11434/v1")


if __name__ == "__main__":
    unittest.main()
