"""Действия помощника: он не только объясняет, но и делает.

Помощник возвращает список команд, а этот модуль их проверяет и исполняет.
Ничего опасного сюда не попадает: только белый список, никаких удалений,
никаких ключей. Всё, что меняет состояние, выполняется на сервере — поэтому
работает и без открытого интерфейса.

Команды:
  * `set_settings` — мощность, лимит процессора, выбор видеокарты;
  * `start_cut` — запустить нарезку по ссылке или уже загруженному проекту
    (с ИИ-режимом, количеством клипов и форматом);
  * `advice` — совет ИИ по проекту;
  * `open_settings`, `open_project`, `set_ai_mode` — подсказки интерфейсу.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import db, jobs, pipeline, resources

# Что снимаем с модели: строгая схема, лишнее отбрасываем.
ACTIONS_SPEC = """Доступные действия (только из этого списка):
{"type":"set_settings","power":0-100,"cpu_limit":25-100,"gpu_preference":"auto|integrated|discrete","encoder":"auto|gpu|cpu"}
{"type":"set_ai_mode","enabled":true|false,"prompt":"текст задания"}
{"type":"start_cut","url":"ссылка","project_id":"id","count":1-50,"min_dur":3-120,"max_dur":5-180,
 "aspect":"9:16|1:1|16:9|4:5","mode":"auto|blur|crop","subtitles":true|false,
 "accent":"yellow|pink|cyan|lime|green|red|orange|lavender",
 "sub_size":40-240,"sub_font":"montserrat|lato|play|ptsans|oswald|unbounded|caveat|golos|onest|marmelad|stalinist|underdog",
 "sub_position":"bottom|middle|top","cut_pauses":true|false,"language":"auto|ru|en",
 "ai_mode":true|false,"ai_prompt":"текст"}
{"type":"rerender_clip","clip_id":"id или clip_number","clip_number":1-50,"mode":"auto|blur|crop","aspect":"9:16|1:1|16:9|4:5",
 "accent":"...","sub_size":40-240,"sub_font":"...","sub_position":"...","cut_pauses":true|false,
 "sub_speed":"calm|normal|fast","text":"полный исправленный текст субтитров клипа"}
{"type":"get_clip","clip_number":1-50}
Перед правкой текста вызови get_clip. Для «первого клипа» используй clip_number=1.
Если правка не указана, спроси какую.
{"type":"update_project","project_id":"id","aspect":"...","mode":"...","subtitles":true|false,
 "accent":"...","sub_size":40-240,"sub_font":"...","cut_pauses":true|false}
{"type":"list_projects"}
{"type":"get_project","project_id":"id"}
{"type":"advice","project_id":"id"}
{"type":"open_settings"}
{"type":"open_project","project_id":"id"}
Если пользователь просит что-то сделать — выполни и коротко подтверди результат.
Если действие не нужно — не добавляй его. В конце ответа добавь ОДИН блок:
```json
{"actions":[{"type":"...", ...}]}
```"""

_ALLOWED = {"set_settings", "set_ai_mode", "start_cut", "rerender_clip", "update_project",
            "list_projects", "get_project", "advice", "open_settings", "open_project", "get_clip"}
_ACCENTS = {"yellow", "pink", "cyan", "lime", "green", "red", "orange", "lavender"}
_FONTS = {"montserrat", "lato", "play", "ptsans", "oswald", "unbounded", "caveat",
          "golos", "onest", "marmelad", "stalinist", "underdog", "marckscript"}
_ASPECTS = {"9:16", "1:1", "16:9", "4:5"}
_MODES = {"auto", "blur", "crop"}
_POSITIONS = {"bottom", "middle", "top"}
_UI_ONLY = {"open_settings", "open_project", "set_ai_mode"}

# Локальный разбор без модели: цифры и числительные в разных падежах.
_WORD_PATTERNS = [
    (r"\bдевяност\w*", 90),
    (r"\bвос[ье]м[иь]десят\w*", 80),
    (r"\bсем[иь]десят\w*", 70),
    (r"\bшест[иь]десят\w*", 60),
    (r"\bпят[иь]десят\w*", 50),
    (r"\bсорок\w*", 40),
    (r"\bтридцат\w*", 30),
    (r"\bдвадцат\w*", 20),
    (r"\bпятнадцат\w*", 15),
    (r"\bдесят\w*", 10),
    (r"\bсто\b", 100),
]


def _number(text: str) -> Optional[int]:
    match = re.search(r"(\d{1,3})", text)
    if match:
        return int(match.group(1))
    for pattern, value in _WORD_PATTERNS:
        if re.search(pattern, text):
            return value
    return None


def _style_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    """Общие поля оформления: и для нарезки, и для пересборки, и для проекта."""
    out: Dict[str, Any] = {}
    if str(item.get("aspect") or "") in _ASPECTS:
        out["aspect"] = item["aspect"]
    if str(item.get("mode") or "") in _MODES:
        out["mode"] = item["mode"]
    if str(item.get("accent") or "") in _ACCENTS:
        out["accent"] = item["accent"]
    if str(item.get("sub_font") or "") in _FONTS:
        out["sub_font"] = item["sub_font"]
    if str(item.get("sub_position") or "") in _POSITIONS:
        out["sub_position"] = item["sub_position"]
    if isinstance(item.get("sub_size"), (int, float)):
        out["sub_size"] = max(40, min(240, int(item["sub_size"])))
    if "subtitles" in item:
        out["subtitles"] = bool(item["subtitles"])
    if "cut_pauses" in item:
        out["cut_pauses"] = bool(item["cut_pauses"])
    if str(item.get("language") or "") in ("auto", "ru", "en"):
        out["language"] = item["language"]
    return out


def validate(raw: Any) -> List[Dict[str, Any]]:
    """Оставляет только известные команды с допустимыми значениями."""
    if not isinstance(raw, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip()
        if kind not in _ALLOWED:
            continue
        action: Dict[str, Any] = {"type": kind}
        if kind == "set_settings":
            if isinstance(item.get("power"), (int, float)):
                action["power"] = max(0, min(100, int(item["power"])))
            if isinstance(item.get("cpu_limit"), (int, float)):
                action["cpu_limit"] = max(25, min(100, int(item["cpu_limit"])))
            pref = str(item.get("gpu_preference") or "")
            if pref in ("auto", "integrated", "discrete"):
                action["gpu_preference"] = pref
            if str(item.get("encoder") or "") in ("auto", "gpu", "cpu"):
                action["encoder"] = item["encoder"]
            if len(action) > 1:
                result.append(action)
        elif kind == "set_ai_mode":
            action["enabled"] = bool(item.get("enabled", True))
            action["prompt"] = str(item.get("prompt") or "")[:600]
            result.append(action)
        elif kind == "start_cut":
            for key in ("url", "project_id", "ai_prompt"):
                value = str(item.get(key) or "").strip()
                if value:
                    action[key] = value[:600]
            for key, low, high in (("count", 1, 50), ("min_dur", 3, 120), ("max_dur", 5, 180)):
                if isinstance(item.get(key), (int, float)):
                    action[key] = max(low, min(high, int(item[key])))
            action.update(_style_fields(item))
            if "ai_mode" in item:
                action["ai_mode"] = bool(item["ai_mode"])
            # без ссылки и id действие применяется к открытому проекту —
            # решение принимает execute(), а не валидатор
            result.append(action)
        elif kind in ("rerender_clip", "get_clip"):
            cid = str(item.get("clip_id") or "").strip()
            if cid:
                action["clip_id"] = cid
            number = item.get("clip_number")
            if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= 50:
                action["clip_number"] = number
            if not (cid or action.get("clip_number")):
                continue
            if kind == "rerender_clip":
                action.update(_style_fields(item))
                if item.get("sub_speed") in ("calm", "normal", "fast"):
                    action["sub_speed"] = item["sub_speed"]
                if isinstance(item.get("text"), str) and item["text"].strip():
                    action["text"] = item["text"].strip()[:5000]
            result.append(action)
        elif kind == "update_project":
            pid = str(item.get("project_id") or "").strip()
            if pid:
                action["project_id"] = pid
                action.update(_style_fields(item))
                if len(action) > 2:
                    result.append(action)
        elif kind == "list_projects":
            result.append(action)
        elif kind == "get_project":
            pid = str(item.get("project_id") or "").strip()
            if pid:
                action["project_id"] = pid
                result.append(action)
        elif kind == "advice":
            pid = str(item.get("project_id") or "").strip()
            if pid:
                action["project_id"] = pid
                result.append(action)
        elif kind == "open_project":
            pid = str(item.get("project_id") or "").strip()
            if pid:
                action["project_id"] = pid
                result.append(action)
        elif kind == "open_settings":
            result.append(action)
    return result


def parse_from_text(text: str) -> List[Dict[str, Any]]:
    """Вытаскивает JSON-блок с командами из ответа модели."""
    cleaned = (text or "").strip()
    start = cleaned.rfind("{")
    while start != -1:
        fragment = cleaned[start:]
        end = fragment.find("}")
        while end != -1:
            candidate = fragment[: end + 1]
            try:
                import json

                data = json.loads(candidate)
            except ValueError:
                end = fragment.find("}", end + 1)
                continue
            if isinstance(data, dict) and isinstance(data.get("actions"), list):
                return validate(data["actions"])
            end = fragment.find("}", end + 1)
        start = cleaned.rfind("{", 0, start)
    return []


def strip_actions(text: str) -> str:
    """Убирает JSON-блок с командами из текста ответа."""
    cleaned = text or ""
    match = re.search(r"```(?:json)?\s*\{\s*\"actions\".*?\}\s*```", cleaned, re.S)
    if match:
        cleaned = cleaned[: match.start()] + cleaned[match.end():]
    else:
        start = cleaned.rfind("{\"actions\"")
        if start != -1:
            cleaned = cleaned[:start]
    return cleaned.strip()


_CLIP_ORDINALS = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5,
    "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10,
}
_CLIP_NUMBER = r"(?:№\s*)?(\d{1,2})|(" + "|".join(_CLIP_ORDINALS) + r")[а-яё]+"
_CLIP_LABEL = r"клип(?:а|е|у|ом)?"
_CLIP_EDIT_FIELDS = ("mode", "aspect", "accent", "sub_size", "sub_font", "sub_position",
                     "cut_pauses", "subtitles", "sub_speed", "text")


def _clip_number(text: str) -> Optional[int]:
    for pattern in (rf"\b(?:{_CLIP_NUMBER})\s+{_CLIP_LABEL}\b",
                    rf"\b{_CLIP_LABEL}\s+(?:{_CLIP_NUMBER})\b"):
        match = re.search(pattern, text.lower().replace("ё", "е"))
        if match:
            digits, ordinal = match.groups()
            return int(digits) if digits else _CLIP_ORDINALS[ordinal]
    return None


def parse_undo(text: str) -> Optional[Dict[str, Any]]:
    clean = text.lower().strip()
    command = re.match(r"^(?:отмени(?:ть)?|откати(?:ть)?|undo)\b", clean)
    if not command:
        return None
    tail = clean[command.end():].strip(" .!?")
    if tail and not re.search(r"правк|изменен|клип|последн|^это$|^last$", tail):
        return None
    action: Dict[str, Any] = {"type": "undo_clip"}
    number = _clip_number(clean)
    if number is not None:
        action["clip_number"] = number
    return action


def parse_local(question: str) -> List[Dict[str, Any]]:
    """Разбирает просьбу без модели: частые формулировки на русском."""
    text = (question or "").lower()
    if not text.strip():
        return []
    undo = parse_undo(text)
    if undo:
        return [undo]
    if re.search(r"^(?:покажи|выведи|назови) .*?(?:таймкод|клип)|^список клипов", text):
        return [{"type": "list_current_clips"}]
    number = _clip_number(text)
    if number is not None and re.search(r"поправ|исправ|измени|пересобер|сделай|убери", text):
        edit: Dict[str, Any] = {"type": "rerender_clip", "clip_number": number}
        for stem, accent in (("ж[её]лт", "yellow"), ("розов", "pink"),
                             ("зел[её]н", "green"), ("красн", "red")):
            if re.search(stem, text):
                edit["accent"] = accent
                break
        aspect = re.search(r"(9:16|1:1|16:9|4:5)", text)
        if aspect:
            edit["aspect"] = aspect[1]
        if re.search(r"убери пауз|вырежи пауз", text):
            edit["cut_pauses"] = True
        if re.search(r"замедл|спокойнее|медленнее", text) and "субтитр" in text:
            edit["sub_speed"] = "calm"
        return [edit]
    actions: List[Dict[str, Any]] = []
    settings_action: Dict[str, Any] = {"type": "set_settings"}

    number = _number(text)
    mentions_cpu = bool(re.search(r"(процессор|cpu|ядер|процент|%)", text))
    mentions_limit = bool(re.search(r"(лимит|потолок|не более|огранич)", text))
    if mentions_limit and number and (mentions_cpu or "процент" in text):
        settings_action["cpu_limit"] = max(25, min(100, number))
    if "мощност" in text and number:
        settings_action["power"] = max(0, min(100, number))
    if "дискретн" in text:
        settings_action["gpu_preference"] = "discrete"
    elif "встроенн" in text and "карт" in text:
        settings_action["gpu_preference"] = "integrated"
    elif "авто" in text and "карт" in text:
        settings_action["gpu_preference"] = "auto"
    if len(settings_action) > 1:
        actions.append(settings_action)

    if re.search(r"(включ|вруб).*(ии|ии-режим|иишк)", text):
        prompt = ""
        match = re.search(r"задани[ея][:\s]+(.+)", question, re.I)
        if match:
            prompt = match.group(1).strip()[:600]
        actions.append({"type": "set_ai_mode", "enabled": True, "prompt": prompt})

    cut = re.search(r"(нарежь|нарез|сделай .*клип|порежь|собери клип)", text)
    if cut:
        action: Dict[str, Any] = {"type": "start_cut"}
        if re.search(r"(ии|ии-режим|иишк|модел)", text):
            action["ai_mode"] = True
        count = re.search(r"(\d{1,2})\s*(клип|нарез|штук)", text)
        if count:
            action["count"] = max(1, min(50, int(count.group(1))))
        aspect = re.search(r"(9:16|1:1|16:9|4:5)", text)
        if aspect:
            action["aspect"] = aspect.group(1)
        pid = re.search(r"([0-9a-f]{12})", text)
        if pid:
            action["project_id"] = pid.group(1)
        link = re.search(r"https?://\S+", question or "")
        if link:
            action["url"] = link.group(0).rstrip(".,)")
        actions.append(action)

    if re.search(r"(открой|покажи).*(настройк)", text):
        actions.append({"type": "open_settings"})
    return actions


def execute(action: Dict[str, Any],
            context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Выполняет одну команду. Возвращает результат для интерфейса."""
    kind = action.get("type")
    if kind == "set_settings":
        from . import ai as ai_module  # noqa: F401 — ключи не трогаем
        from . import cpu_limit, gpu_pref, settings

        patch = {key: action[key] for key in ("power", "cpu_limit", "gpu_preference")
                 if key in action}
        settings.update(patch)
        resources.invalidate()
        cpu_limit.apply()
        gpu_pref.apply()
        summary = resources.summary()
        return {"ok": True, "type": kind, "applied": patch,
                "summary": {key: summary.get(key) for key in
                            ("power", "cpu_limit", "render_workers", "ffmpeg_threads")},
                "message": "Настройки применены: " + ", ".join(
                    f"{key}={value}" for key, value in patch.items())}

    if kind == "set_ai_mode":
        # режим живёт в интерфейсе, но запоминаем задание в настройках проекта
        return {"ok": True, "type": kind, "ui": {"ai_mode": bool(action.get("enabled")),
                                                 "prompt": action.get("prompt") or ""},
                "message": ("ИИ-режим включён" if action.get("enabled")
                            else "ИИ-режим выключен")}

    if kind == "start_cut":
        return _start_cut(action, context or {})

    if kind == "list_current_clips":
        clips = ((context or {}).get("project") or {}).get("clip_index") or []
        def timestamp(seconds):
            minutes, seconds = divmod(int(seconds), 60)
            return f"{minutes:02d}:{seconds:02d}"
        lines = [f"№{clip['number']} · {timestamp(clip['start'])}–{timestamp(clip['end'])} · {clip['title']}"
                 for clip in clips]
        return {"ok": bool(lines), "type": kind,
                "message": "\n".join(lines) if lines else "В открытом проекте пока нет клипов."}

    if kind in ("rerender_clip", "undo_clip", "get_clip"):
        project_id = ((context or {}).get("project") or {}).get("id")
        number = action.get("clip_number")
        cid = action.get("clip_id")
        try:
            if number is not None:
                if not project_id:
                    return {"ok": False, "type": kind, "message": "Сначала откройте проект."}
                cid = next((clip["id"] for clip in db.list_clips(project_id)
                            if clip["idx"] + 1 == number), None)
                if not cid:
                    return {"ok": False, "type": kind, "message": f"Клип №{number} не найден."}
            clip = db.get_clip(cid) if cid else None
        except Exception:  # noqa: BLE001
            return {"ok": False, "type": kind, "message": "База проектов сейчас недоступна."}
        if cid and (not clip or (project_id and clip["project_id"] != project_id)):
            return {"ok": False, "type": kind, "message": "Клип не найден в открытом проекте."}
        if kind == "undo_clip":
            if not project_id:
                return {"ok": False, "type": kind, "message": "Сначала откройте проект."}
            ok, message = jobs.undo_agent_edit(project_id, cid)
            return {"ok": ok, "type": kind, "message": message}
        if not clip:
            return {"ok": False, "type": kind, "message": "Клип не найден."}
        if kind == "get_clip":
            text = " ".join(str(word.get("word") or "") for word in clip.get("words") or [])
            if len(text) > 5000:
                return {"ok": False, "type": kind, "message": "Субтитры слишком длинные для правки одной командой."}
            return {"ok": True, "type": kind, "clip_id": clip["id"],
                    "message": f"Клип №{clip['idx'] + 1}, {clip['start']}–{clip['end']} с.",
                    "transcript": text}
        patch = {key: action[key] for key in _CLIP_EDIT_FIELDS if key in action}
        if not patch:
            return {"ok": False, "type": kind,
                    "message": f"Что поменять в клипе №{clip['idx'] + 1}? Например, цвет или темп субтитров."}
        if clip["status"] != "ready" or not jobs.submit_rerender(clip["id"], patch, agent_edit=True):
            return {"ok": False, "type": kind, "message": "Дождитесь готовности клипа."}
        return {"ok": True, "type": kind, "clip_id": clip["id"],
                "message": f"Правка клипа №{clip['idx'] + 1} поставлена в очередь."}

    if kind == "update_project":
        from . import pipeline as pipeline_module

        try:
            project = db.get_project(action.get("project_id") or "")
        except Exception:  # noqa: BLE001
            return {"ok": False, "type": kind, "message": "База проектов сейчас недоступна."}
        if not project:
            return {"ok": False, "type": kind, "message": "Проект не найден."}
        patch = {key: action[key] for key in
                 ("mode", "aspect", "accent", "sub_size", "sub_font", "sub_position",
                  "subtitles", "cut_pauses") if key in action}
        if not patch:
            return {"ok": False, "type": kind, "message": "Не указано, что поменять."}
        merged = pipeline_module.normalize_options({**(project.get("options") or {}), **patch})
        db.update_project(project["id"], options=merged)
        return {"ok": True, "type": kind, "project_id": project["id"],
                "message": "Настройки проекта обновлены: " + ", ".join(
                    f"{k}={v}" for k, v in patch.items())}

    if kind == "list_projects":
        try:
            items = db.list_projects(limit=10)
        except Exception:  # noqa: BLE001 — база недоступна: не роняем помощника
            return {"ok": False, "type": kind, "message": "База проектов сейчас недоступна."}
        lines = [f"{p['id']} · {p.get('status')} · {(p.get('title') or '')[:40]}"
                 for p in items]
        return {"ok": True, "type": kind, "projects": items,
                "message": "Проекты: " + "; ".join(lines) if lines else "Проектов пока нет."}

    if kind == "get_project":
        try:
            project = db.get_project(action.get("project_id") or "")
            clips = db.list_clips(project["id"]) if project else []
        except Exception:  # noqa: BLE001
            return {"ok": False, "type": kind, "message": "База проектов сейчас недоступна."}
        if not project:
            return {"ok": False, "type": kind, "message": "Проект не найден."}
        return {"ok": True, "type": kind, "project": project, "clips": clips,
                "message": (f"Статус {project.get('status')}, клипов {len(clips)}, "
                            f"язык {project.get('language') or '—'}")}

    if kind == "advice":
        from . import ai as ai_module

        try:
            project = db.get_project(action.get("project_id") or "")
        except Exception:  # noqa: BLE001
            return {"ok": False, "type": kind, "message": "База проектов сейчас недоступна."}
        if not project:
            return {"ok": False, "type": kind,
                    "message": "Проект не найден — сначала запустите нарезку."}
        clips = db.list_clips(project["id"])
        clip = max(clips, key=lambda item: float(item.get("score") or 0.0)) if clips else None
        payload = {"title": project.get("title"), "language": project.get("language"),
                   "duration": project.get("duration"), "clips": clips}
        try:
            result = ai_module.advice(payload, clip)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "type": kind, "message": str(exc)[:300]}
        return {"ok": True, "type": kind, "advice": result.get("advice"),
                "model": result.get("model"), "message": "Совет готов"}

    if kind == "open_settings":
        return {"ok": True, "type": kind, "ui": {"open_settings": True},
                "message": "Открываю настройки"}

    if kind == "open_project":
        pid = action.get("project_id") or ""
        try:
            exists = bool(db.get_project(pid))
        except Exception:  # noqa: BLE001
            exists = False
        if not exists:
            return {"ok": False, "type": kind, "message": "Проект не найден"}
        return {"ok": True, "type": kind, "ui": {"open_project": pid},
                "message": "Открываю проект"}

    return {"ok": False, "type": str(kind), "message": "Неизвестное действие"}


def _start_cut(action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Запускает нарезку: по ссылке или по уже загруженному проекту."""
    options: Dict[str, Any] = {}
    for key in ("count", "min_dur", "max_dur", "aspect", "mode", "subtitles",
                "accent", "sub_size", "sub_font", "sub_position", "cut_pauses", "language"):
        if key in action:
            options[key] = action[key]
    if action.get("ai_mode"):
        options["ai_mode"] = True
        options["autostart"] = True
        options["ai_prompt"] = action.get("ai_prompt") or ""
    project_id = str(action.get("project_id") or "").strip()
    url = str(action.get("url") or "").strip()

    if not project_id and not url:
        current = (context.get("project") or {})
        if current.get("id"):
            project_id = str(current["id"])
        else:
            return {"ok": False, "type": "start_cut",
                    "message": "Сначала пришлите ссылку или загрузите видео."}

    if project_id:
        project = db.get_project(project_id)
        if not project:
            return {"ok": False, "type": "start_cut", "message": "Проект не найден."}
        status = str(project.get("status") or "")
        if status in ("queued", "preparing", "processing", "running"):
            return {"ok": True, "type": "start_cut", "project_id": project_id,
                    "message": "Нарезка уже идёт — подождите результат."}
        db.update_project(project_id, options=pipeline.normalize_options(
            {**(project.get("options") or {}), **options}))
        if status in ("new", "", "error", "cancelled"):
            jobs.submit_prepare(project_id)
        else:
            jobs.submit_process(project_id)
        return {"ok": True, "type": "start_cut", "project_id": project_id,
                "message": "Запустил нарезку проекта — клипы появятся сами."}

    normalized = pipeline.normalize_options(options)
    project = db.create_project("pending", normalized, title=url[:120] or "Новый проект")
    db.update_project(project["id"], url=url)
    jobs.submit_prepare(project["id"])
    return {"ok": True, "type": "start_cut", "project_id": project["id"],
            "message": "Принял ссылку — скачиваю и нарезаю."}
