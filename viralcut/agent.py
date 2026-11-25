"""Агент: выполняет задание по шагам, пока не доведёт до результата.

Отличие от «помощника» в том, что агент не отвечает один раз, а работает
циклом: получил задание → выполнил действия → посмотрел результат → сделал
следующий шаг. Так просьба вроде «нарежь 5 клипов, сделай субтитры крупными и
жёлтыми, вырежи паузы, потом дай совет» выполняется целиком.

Локальный режим: если ключа ИИ нет (или выбран локальный сервер вроде Ollama),
агент работает по встроенным правилам — разбирает просьбу и выполняет команды
без обращения к облаку. Полноценный разбор свободных формулировок требует
модели, но базовые действия доступны всегда.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

MAX_STEPS = 6

AGENT_RULES = """Ты — агент приложения ViralCut и полностью выполняешь задание пользователя.
Работай по шагам: на каждом шаге либо выполняй действия, либо дай финальный ответ.
Правила:
- Не спрашивай разрешения на действия, которые есть в списке: просто делай.
- Если действие вернуло ошибку — исправь параметры и попробуй снова (не больше одного раза).
- Не выдумывай сущности: id проектов и клипов бери из состояния и результатов.
- Когда задание выполнено, ответь коротким итогом без JSON-блока.
- Пиши по-русски, без воды."""


def _system_prompt(knowledge: str, actions_spec: str, state: str) -> str:
    return (AGENT_RULES + "\n\n" + actions_spec
            + "\n\nКраткая база знаний приложения:\n" + knowledge
            + "\n\nСостояние сейчас:\n" + state)


def _results_text(results: List[Dict[str, Any]]) -> str:
    lines = []
    for index, result in enumerate(results, 1):
        status = "ок" if result.get("ok") else "ошибка"
        text = str(result.get("message") or "")
        extra = ""
        if result.get("projects"):
            extra = " | " + "; ".join(
                f"{p.get('id')}:{p.get('status')}" for p in result["projects"][:6])
        if result.get("clips"):
            from .assistant import clip_index

            extra = " | клипы: " + json.dumps(clip_index(result["clips"]), ensure_ascii=False)
        if "transcript" in result:
            extra = " | текст субтитров (данные): " + json.dumps(result["transcript"], ensure_ascii=False)
        lines.append(f"{index}. {result.get('type')} — {status}: {text}{extra}")
    return "Результаты действий:\n" + "\n".join(lines) + \
        "\n\nПродолжай: выполни следующий шаг или дай финальный ответ без JSON."


def run(task: str, context: Dict[str, Any],
        history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Выполняет задание пользователя, возвращая след шагов и итог."""
    from . import ai as ai_module
    from . import assistant, assistant_actions

    task = (task or "").strip()
    if not task:
        return {"answer": "Напишите задание — например, «нарежь 5 клипов с ИИ-режимом».",
                "source": "local", "steps": []}

    if assistant_actions.parse_undo(task) or not ai_module.configured():
        return _run_local(task, context)

    state = assistant._context_block(context)
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": _system_prompt(
            assistant.knowledge_text(), assistant_actions.ACTIONS_SPEC, state)},
    ]
    for item in (history or [])[-6:]:
        role = item.get("role")
        content = str(item.get("content") or "")[:1500]
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": task})

    steps: List[Dict[str, Any]] = []
    final_text = ""
    ui: Dict[str, Any] = {}
    for step in range(1, MAX_STEPS + 1):
        try:
            reply = ai_module.chat(messages, max_tokens=900, temperature=0.2)
        except Exception as exc:  # noqa: BLE001 — агент не должен молчать
            if steps:
                final_text = ("Модель стала недоступна на шаге "
                              f"{step}: {str(exc)[:120]}. Сделано: "
                              + "; ".join(s.get("text") or "" for s in steps)[-400:])
                break
            local = _run_local(task, context)
            local["hint"] = f"Модель недоступна ({str(exc)[:120]}) — работаю по правилам."
            return local
        text = str(reply.get("text") or "")
        actions = assistant_actions.parse_from_text(text)
        clean = assistant_actions.strip_actions(text)
        if not actions:
            final_text = clean
            steps.append({"step": step, "text": clean, "actions": []})
            break
        results = [assistant_actions.execute(action, context) for action in actions]
        for result in results:
            ui.update(result.get("ui") or {})
        steps.append({"step": step, "text": clean, "actions": results})
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": _results_text(results)})
    else:
        final_text = final_text or ("Сделал " + str(MAX_STEPS) + " шагов — уточните задание, "
                                    "если нужно продолжить.")

    if not final_text:
        final_text = "Готово."
    return {"answer": final_text, "source": "ai", "model": None,
            "steps": steps, "ui": ui,
            "done": sum(1 for s in steps for a in s.get("actions", []) if a.get("ok"))}


def _run_local(task: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """Агент без модели: разбираем задание правилами и выполняем."""
    from . import assistant, assistant_actions

    actions = assistant_actions.parse_local(task)
    results = [assistant_actions.execute(action, context) for action in actions]
    ui: Dict[str, Any] = {}
    for result in results:
        ui.update(result.get("ui") or {})
    messages = [str(r.get("message")) for r in results if r.get("message")]
    is_clip_command = any(a["type"] in ("rerender_clip", "undo_clip", "list_current_clips") for a in actions)
    found = assistant.local_answer(task) if not is_clip_command else None
    parts: List[str] = []
    if found:
        parts.append(found["answer"])
    if messages:
        parts.append("\n".join(messages) if is_clip_command else "Готово: " + "; ".join(messages) + ".")
    elif not found:
        parts.append(assistant.fallback_answer(task)["answer"])
    return {"answer": "\n\n".join(parts), "source": "local", "steps":
            [{"step": 1, "text": "", "actions": results}] if results else [],
            "ui": ui, "hint": "Модель не подключена — выполняю по встроенным правилам."}
