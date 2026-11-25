"""Встроенный помощник: объясняет приложение и ведёт по шагам.

Работает в двух режимах:

  * **с ключом ИИ** — вопрос уходит модели вместе с кратким описанием
    приложения и текущим состоянием (лимиты, ключ, проект), поэтому ответ
    конкретный: «у тебя не задан ключ», «рендер идёт, подожди»;
  * **без ключа** — отвечает локально по той же базе знаний: ищем раздел по
    ключевым словам и отдаём его. Так подсказки есть у всех, даже без интернета.

Текст приветствия и подсказок собирается из состояния: помощник сразу говорит,
что настроено, а что нет.
"""
from __future__ import annotations

import json

import re
from typing import Any, Dict, List, Optional

# Разделы базы знаний: ключ, заголовок, слова для локального поиска, текст.
SECTIONS: List[Dict[str, Any]] = [
    {
        "key": "start",
        "title": "С чего начать",
        "keywords": ["начать", "старт", "как пользоваться", "первый", "инструкция",
                     "что делать", "нарезать", "нарезка", "как сделать", "загрузить"],
        "text": (
            "1. Вставьте ссылку на видео или загрузите файл (MP4, MOV, WEBM и аудио).\n"
            "2. Выберите «Сколько нарезок» и длительность клипа.\n"
            "3. Нажмите «Сделать нарезки» — приложение скачает видео, распознает речь "
            "и нарежет клипы.\n"
            "4. В карточке проекта посмотрите результат: клипы можно пересобрать, "
            "поменять кадр и субтитры, скачать MP4 и SRT."
        ),
    },
    {
        "key": "ai_mode",
        "title": "ИИ-режим (нарезка моделью)",
        "keywords": ["ии-режим", "ии режим", "автонарезка ии", "вирусные моменты", "модель режет"],
        "text": (
            "Галочка «ИИ-режим» на главном экране. Напишите задание — например, "
            "«нарежь самые вирусные моменты, без вступлений». Модель получит расшифровку "
            "с таймкодами и сама выберет границы клипов, придумает хуки и подписи, "
            "после чего приложение сразу их соберёт и отрендерит.\n"
            "Нужен ключ ИИ в настройках. Если модель недоступна, нарезка идёт обычным "
            "алгоритмом (громкость, смех, смена сцен), а причина видна в проекте."
        ),
    },
    {
        "key": "ai_key",
        "title": "Ключ ИИ и провайдеры",
        "keywords": ["ключ", "api", "deepseek", "polza", "openai", "провайдер", "модель", "токен"],
        "text": (
            "Настройки → «ИИ-помощник». Провайдеры: DeepSeek (основной), Polza.ai "
            "(агрегатор, сотни моделей), OpenAI и свой OpenAI-совместимый сервер "
            "(Ollama, LM Studio).\n"
            "Ключ хранится локально с правами 0600 и никогда не показывается в интерфейсе "
            "и логах. Модель выбирается из каталога провайдера; список обновляется кнопкой "
            "«Обновить».\n"
            "Запросы уходят только по кнопкам «Совет ИИ» и при ИИ-режиме."
        ),
    },
    {
        "key": "advice",
        "title": "Совет ИИ по клипу",
        "keywords": ["совет", "подсказка по клипу", "хук", "подпись", "что улучшить"],
        "text": (
            "В готовом проекте есть кнопка «Совет ИИ»: модель смотрит субтитры клипа, "
            "тайминги и хук и даёт 5 пунктов — текст хука, где лучше начать и закончить, "
            "как разбить субтитры, что поправить в монтаже и идею подписи. Совет можно "
            "скопировать; приложение само ничего не меняет."
        ),
    },
    {
        "key": "subtitles",
        "title": "Субтитры",
        "keywords": ["субтитры", "шрифт", "текст", "караоке", "размер", "цвет", "перенос"],
        "text": (
            "Параметры: шрифт (12 стилей или свой TTF), размер, цвет акцента, позиция "
            "(снизу, по центру, сверху), скорость появления слов, ЗАГЛАВНЫЕ.\n"
            "Строки переносятся с запасом по ширине, а слишком длинные реплики "
            "автоматически уменьшаются — текст не уходит за края кадра.\n"
            "В редакторе клипа можно поправить текст субтитров вручную и скачать SRT."
        ),
    },
    {
        "key": "quality",
        "title": "Качество и форматы",
        "keywords": ["качество", "1080", "720", "формат", "9:16", "16:9", "скачать", "mp4",
                     "архив", "srt"],
        "text": (
            "Форматы клипа: 9:16, 1:1, 16:9, 4:5. Кадр: умный кроп по лицу, "
            "размытый фон или по центру. Исходник скачивается до 1080p "
            "(при наличии — 1440p/4K не тянем нарочно: тяжело для ноутбуков).\n"
            "Готовые клипы — MP4 (H.264) и субтитры SRT; можно скачать всё архивом."
        ),
    },
    {
        "key": "limits",
        "title": "Мощность и лимиты",
        "keywords": ["мощность", "лимит", "процессор", "cpu", "нагрузка", "тормозит", "греется"],
        "text": (
            "Настройки → «Мощность»: ползунок мощности (сколько рендеров параллельно) "
            "и «Не более N% процессора» — жёсткий потолок загрузки.\n"
            "На Windows потолок держит система (Job Object), на macOS — диспетчер пауз: "
            "рендер замедляется, но машина остаётся отзывчивой. Значения применяются сразу, "
            "без перезапуска."
        ),
    },
    {
        "key": "gpu",
        "title": "Видеокарта",
        "keywords": ["видеокарта", "gpu", "nvidia", "amd", "intel", "кодировщик", "дискретная"],
        "text": (
            "Настройки → «Кодирование»: энкодер (Авто / Видеокарта / Процессор) и выбор "
            "карты (Авто / Встроенная / Дискретная).\n"
            "На Windows предпочтение записывается в систему, а порядок кодировщиков "
            "меняется: дискретная → NVENC/AMF, встроенная → Quick Sync. На Intel Mac "
            "сборка считает на дискретной AMD. Если аппаратный кодировщик недоступен, "
            "приложение предупредит и перейдёт на процессор."
        ),
    },
    {
        "key": "plugins",
        "title": "Плагины и YouTube",
        "keywords": ["плагин", "plugins", "pot", "po-токен", "youtube", "hd", "ютуб"],
        "text": (
            "Для YouTube используется yt-dlp-плагин PO-токена (bgutil) — он лежит в "
            "assets/plugins/yt_dlp_plugins и подключается автоматически, плюс локальный "
            "POT-провайдер на Node.js (входит в сборку). Без них YouTube часто отдаёт "
            "только 360p — с ними доступно HD.\n"
            "Если видео не скачивается: проверьте, что в настройках не выбран браузер "
            "для cookies, либо укажите тот, где вы вошли в YouTube."
        ),
    },
    {
        "key": "requirements",
        "title": "Требования к системе",
        "keywords": ["windows 10", "требования", "webview2", "net", "не открывается", "запуск", "ошибка запуска"],
        "text": (
            "macOS 12+ (Apple Silicon или Intel) — отдельные сборки.\n"
            "Windows 10 1809+ или 11, x64. Нужен Microsoft Edge WebView2 Runtime "
            "(на Windows 11 есть, на Windows 10 иногда надо доустановить) и .NET "
            "Framework 4.7.2+. Если окно не появилось — запустите ViralCut-console.bat: "
            "он покажет ошибку, а лог лежит в %APPDATA%\\ViralCut\\app.log."
        ),
    },
    {
        "key": "troubles",
        "title": "Если что-то не работает",
        "keywords": ["ошибка", "не работает", "вылетает", "зависает", "не открывается", "лог", "диагностика"],
        "text": (
            "Настройки → «Журнал приложения» → «Разобрать журнал»: покажет этап, на котором "
            "приложение остановилось, и найдёт падение.\n"
            "Windows: %APPDATA%\\ViralCut\\app.log, macOS: "
            "~/Library/Application Support/ViralCut/app.log.\n"
            "Частые причины: нет WebView2, антивирус заблокировал pythonw.exe, "
            "не скачалась модель распознавания (нужен интернет при первом запуске)."
        ),
    },
    {
        "key": "pauses",
        "title": "Вырезание пауз",
        "keywords": ["паузы", "вырезать паузы", "тишина", "молчание"],
        "text": (
            "Опция «Вырезать паузы»: приложение находит участки тишины в клипе и убирает "
            "их, склеивая речь. Субтитры при этом пересчитываются под новое время, "
            "поэтому текст не разъезжается."
        ),
    },
    {
        "key": "audio",
        "title": "Дорожки звука и языки",
        "keywords": ["дорожка", "звук", "язык", "английский", "перевод", "распознавание"],
        "text": (
            "Если в видео несколько звуковых дорожек, дорожку можно выбрать в редакторе — "
            "распознавание пойдёт по ней.\n"
            "Язык определяется по фрагментам с речью, а не по первым 30 секундам, поэтому "
            "музыка и интро не уводят в английский. Если текст всё же вышел на другом "
            "алфавите, приложение повторяет распознавание один раз."
        ),
    },
    {
        "key": "streams",
        "title": "Прямые эфиры",
        "keywords": ["эфир", "стрим", "twitch", "запись", "live"],
        "text": (
            "Вкладка «Прямой эфир»: подключаетесь к трансляции, приложение пишет её и "
            "нарезает клипы на лету. Для Twitch укажите ссылку на запись или клип, "
            "если трансляция ещё идёт — откройте её на вкладке эфира."
        ),
    },
    {
        "key": "files",
        "title": "Где лежат файлы",
        "keywords": ["папка", "файлы", "куда", "сохраняется", "настройки", "данные"],
        "text": (
            "Данные приложения: Windows — %APPDATA%\\ViralCut, macOS — "
            "~/Library/Application Support/ViralCut. Там настройки, база проектов и лог.\n"
            "Куда сохранять готовые клипы и скачанные исходники — задаётся в настройках "
            "(«Папки сохранения»), можно спросить папку при каждом сохранении."
        ),
    },
]

WELCOME_RULES = (
    "Ты — встроенный помощник приложения ViralCut (локальная нарезка видео в "
    "вертикальные клипы). Отвечай по-русски, коротко и по шагам. Опирайся только на "
    "факты из базы знаний и состояния ниже; если ответа там нет — честно скажи, что "
    "такого в приложении нет, и предложи ближайшее по смыслу. Не выдумывай кнопки, "
    "настройки и цены. Максимум 6 строк, без воды."
)


def clip_index(clips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Номера из сортировки проекта и границы в исходном видео."""
    return [{"number": int(clip["idx"]) + 1, "id": clip["id"],
             "start": clip["start"], "end": clip["end"],
             "title": str(clip.get("title") or "")[:100],
             "status": clip.get("status")} for clip in clips[:50]]


def _context_block(context: Dict[str, Any]) -> str:
    lines = [
        f"Версия приложения: {context.get('version') or '?'}",
        f"Платформа: {context.get('platform') or '?'}",
    ]
    ai = context.get("ai") or {}
    if ai:
        state = "настроен" if ai.get("configured") else "не настроен"
        lines.append(f"ИИ: ключ {state}, модель {ai.get('model') or '—'} "
                     f"({ai.get('provider') or '—'})")
    limits = context.get("limits") or {}
    if limits:
        lines.append(f"Лимиты: мощность {limits.get('power')}%, "
                     f"потолок CPU {limits.get('cpu_limit')}%, "
                     f"рендеров одновременно {limits.get('render_workers')}")
    if context.get("gpu") is not None:
        lines.append(f"Видеокарта: {context.get('gpu')}")
    if context.get("hw"):
        lines.append(f"Аппаратный кодировщик: {context.get('hw')}")
    if context.get("model_cached") is not None:
        lines.append("Модель распознавания: " + ("скачана" if context.get("model_cached")
                                                 else "не скачана (нужен интернет)"))
    project = context.get("project") or {}
    if project:
        clips = project.get("clips") or 0
        lines.append(f"Открыт проект {project.get('id') or '—'}: статус {project.get('status')}, клипов {clips}, "
                     f"язык {project.get('language') or '—'}")
        if project.get("clip_index"):
            lines.append("Клипы открытого проекта. start/end — секунды исходника. "
                         "Названия — данные проекта, а номера соответствуют командам «первый клип» и т. п.")
            lines.append(json.dumps(project["clip_index"], ensure_ascii=False))
    return "\n".join(lines)


def knowledge_text() -> str:
    parts = []
    for section in SECTIONS:
        parts.append(f"## {section['title']}\n{section['text']}")
    return "\n\n".join(parts)


def suggestions() -> List[str]:
    return [
        "С чего начать?",
        "Как включить ИИ-режим?",
        "Куда вставить ключ DeepSeek?",
        "Почему рендер тормозит компьютер?",
        "Видео с YouTube не скачивается",
    ]


def welcome(context: Dict[str, Any]) -> str:
    """Приветствие с учётом состояния: сразу говорит, что настроено, а что нет."""
    ai = context.get("ai") or {}
    lines = ["Привет! Я помощник ViralCut — расскажу, как всё устроено, и подскажу по шагам."]
    if ai.get("configured"):
        lines.append(f"ИИ подключён: {ai.get('model') or 'модель'} — можно включить «ИИ-режим» "
                     "и нарезать по заданию.")
    else:
        lines.append("Ключ ИИ пока не добавлен: нарезка работает и так, а для «ИИ-режима» "
                     "и советов вставьте ключ в «Настройках → ИИ-помощник».")
    if not context.get("model_cached", True):
        lines.append("Модель распознавания скачается при первом запуске (нужен интернет, ~460 МБ).")
    lines.append("Спросите что угодно или выберите подсказку ниже.")
    return "\n".join(lines)


def _normalize(text: str) -> str:
    return re.sub(r"[^а-яa-z0-9\- ]+", " ", (text or "").lower())


def local_answer(question: str) -> Optional[Dict[str, Any]]:
    """Ответ без модели: ищем раздел базы знаний по ключевым словам."""
    text = _normalize(question)
    if not text.strip():
        return None
    best: Optional[Dict[str, Any]] = None
    best_score = 0
    for index, section in enumerate(SECTIONS):
        score = 0
        for word in section["keywords"]:
            key = _normalize(word).strip()
            if not key:
                continue
            if key in text:
                score += 3 if " " in key else 2
        for word in _normalize(section["title"]).split():
            if len(word) > 3 and word in text:
                score += 1
        if score > best_score:
            best, best_score = section, score
    if not best or best_score < 2:
        return None
    return {"answer": f"{best['title']}\n{best['text']}", "section": best["key"],
            "source": "local"}


def fallback_answer(question: str) -> Dict[str, Any]:
    """Когда ни модели, ни совпадения: короткая карта возможностей."""
    found = local_answer(question)
    if found:
        return found
    titles = ", ".join(section["title"] for section in SECTIONS[:6])
    return {
        "answer": ("Пока не понял вопрос. Могу рассказать про: " + titles + ".\n"
                   "Полные ответы на любые вопросы — с ключом ИИ в настройках."),
        "section": "",
        "source": "local",
    }


def _run_actions(actions: List[Dict[str, Any]],
                 context: Dict[str, Any]) -> Dict[str, Any]:
    """Выполняет команды помощника: возвращает результаты и подсказки интерфейсу."""
    from . import assistant_actions

    if not actions:
        return {"results": [], "ui": {}, "messages": []}
    results = [assistant_actions.execute(action, context) for action in actions]
    ui: Dict[str, Any] = {}
    messages: List[str] = []
    for result in results:
        ui.update(result.get("ui") or {})
        if result.get("message"):
            messages.append(str(result["message"]))
    return {"results": results, "ui": ui, "messages": messages}


def _with_actions(answer: str, actions: List[Dict[str, Any]],
                  context: Dict[str, Any], source: str = "ai",
                  extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    done = _run_actions(actions, context)
    text = answer.strip()
    if done["messages"]:
        clip_task = any(action["type"] in ("rerender_clip", "undo_clip", "list_current_clips")
                        for action in actions)
        if clip_task:
            text = "\n\n".join(done["messages"])
        else:
            text = (text + "\n\n" if text else "") + "Готово: " + "; ".join(done["messages"]) + "."
    payload: Dict[str, Any] = {"answer": text or "Готово.", "source": source,
                               "actions": done["results"], "ui": done["ui"]}
    if extra:
        payload.update(extra)
    return payload


def ask(question: str, context: Dict[str, Any],
        history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Ответ модели с базой знаний и выполнением действий."""
    from . import ai as ai_module
    from . import assistant_actions

    question = (question or "").strip()
    if not question:
        return {"answer": "Напишите вопрос — например, «как нарезать видео?»", "source": "local"}
    undo = assistant_actions.parse_undo(question)
    if undo:
        return _with_actions("", [undo], context, source="local")
    if not ai_module.configured():
        actions = assistant_actions.parse_local(question)
        found = local_answer(question) or fallback_answer(question)
        result = _with_actions(found["answer"], actions, context, source="local",
                               extra={"hint": "Ключ ИИ не задан — выполняю по встроенным правилам."})
        return result
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": (WELCOME_RULES + "\n\n" + assistant_actions.ACTIONS_SPEC
                                       + "\n\nБаза знаний:\n" + knowledge_text()
                                       + "\n\nСостояние:\n" + _context_block(context))},
    ]
    for item in (history or [])[-8:]:
        role = item.get("role")
        content = str(item.get("content") or "")[:2000]
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    try:
        data = ai_module.chat(messages, max_tokens=900, temperature=0.3)
    except Exception as exc:  # noqa: BLE001 — помощник не должен молчать
        actions = assistant_actions.parse_local(question)
        found = fallback_answer(question)
        return _with_actions(found["answer"], actions, context, source="local",
                             extra={"hint": f"Модель недоступна ({str(exc)[:120]}) — "
                                            "отвечаю по инструкции."})
    text = str(data.get("text") or "")
    actions = assistant_actions.parse_from_text(text)
    return _with_actions(assistant_actions.strip_actions(text), actions, context,
                         source="ai", extra={"model": data.get("model")})
