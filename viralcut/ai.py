"""ИИ-помощник: советы по готовым клипам через внешний API.

Работает с любым OpenAI-совместимым API (`/chat/completions`): DeepSeek,
OpenAI, локальные серверы (Ollama/LM Studio/vLLM) — пользователь выбирает
провайдера и вставляет свой ключ. Ключ хранится в `data/settings.json` с правами
0600 и никогда не попадает в ответы API и логи.

Запрос уходит только по кнопке «Совет ИИ»: приложение ничего не отправляет
само, и в подсказке в интерфейсе это написано.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

PROVIDERS: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_url": "https://platform.deepseek.com/api_keys",
        "note": "Основной провайдер: свой ключ DeepSeek, тарификация напрямую.",
    },
    "polza": {
        "label": "Polza.ai",
        "base": "https://polza.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "key_url": "https://polza.ai/dashboard/api-keys",
        "note": "Агрегатор: один ключ на сотни моделей (OpenAI, Claude, Qwen, DeepSeek и др.).",
    },
    "openai": {
        "label": "OpenAI",
        "base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_url": "https://platform.openai.com/api-keys",
    },
    "local": {
        "label": "Локальная модель",
        "base": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5",
        "key_url": "",
        "note": "Ollama или LM Studio на этом компьютере: данные не покидают машину.",
        "keyless": "1",
    },
    "custom": {
        "label": "Свой сервер",
        "base": "",
        "model": "",
        "key_url": "",
    },
}

MAX_TRANSCRIPT_CHARS = 6000

# Если каталог моделей закрыт ключом (так у DeepSeek), показываем известные
# модели своего провайдера — выпадающий список не должен быть пустым.
FALLBACK_MODELS: Dict[str, List[str]] = {
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "polza": ["openai/gpt-4o-mini", "openai/gpt-4o", "deepseek/deepseek-chat"],
    "openai": ["gpt-4o-mini", "gpt-4o"],
    "custom": [],
}
TIMEOUT = 60


class AIError(RuntimeError):
    """Понятная человеку ошибка обращения к ИИ."""


def _env_key() -> str:
    for name in ("VIRALCUT_AI_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def key(conf: Optional[Dict[str, str]] = None) -> str:
    """Возвращает ключ только для API, которому он был назначен."""
    try:
        from . import settings

        data = settings.get()
        current_scope = settings.ai_key_scope(data)
        scope = [conf["provider"], conf["base"]] if conf else current_scope
        if scope == data.get("ai_key_scope", current_scope):
            saved = str(data.get("ai_key") or "").strip()
            if saved:
                return saved
        provider, base = scope
        if provider in ("deepseek", "openai") and base == PROVIDERS[provider]["base"]:
            named = os.environ.get(provider.upper() + "_API_KEY", "").strip()
            if named:
                return named
        return os.environ.get("VIRALCUT_AI_KEY", "").strip() if scope == current_scope else ""
    except Exception:  # noqa: BLE001
        return ""


def _conf() -> Dict[str, str]:
    try:
        from . import settings

        data = settings.get()
    except Exception:  # noqa: BLE001
        data = {}
    provider = str(data.get("ai_provider") or "deepseek").lower()
    if provider not in PROVIDERS:
        provider = "deepseek"
    preset = PROVIDERS[provider]
    base = str(data.get("ai_base") or preset["base"]).strip().rstrip("/")
    model = str(data.get("ai_model") or preset["model"]).strip()
    return {"provider": provider, "base": base, "model": model,
            "label": preset["label"], "key_url": preset["key_url"]}


def _keyless(provider: str) -> bool:
    """Локальным серверам ключ не нужен."""
    return bool(PROVIDERS.get(provider, {}).get("keyless"))


def configured() -> bool:
    conf = _conf()
    if _keyless(conf["provider"]):
        return bool(conf["base"] and conf["model"])
    return bool(key(conf) and conf["base"] and conf["model"])


def local_server(base: Optional[str] = None) -> Dict[str, Any]:
    """Ищет локальный OpenAI-совместимый сервер (Ollama, LM Studio)."""
    candidates = [base] if base else [
        "http://127.0.0.1:11434/v1",   # Ollama
        "http://127.0.0.1:1234/v1",    # LM Studio
        "http://127.0.0.1:8080/v1",    # llama.cpp / vLLM
    ]
    for url in candidates:
        if not url:
            continue
        try:
            request = urllib.request.Request(f"{url.rstrip('/')}/models", method="GET")
            with urllib.request.urlopen(request, timeout=2) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
        raw = data.get("data") if isinstance(data, dict) else None
        models = [str(item.get("id")) for item in (raw or []) if isinstance(item, dict)]
        if models:
            return {"available": True, "base": url, "models": models[:50]}
    return {"available": False, "base": "", "models": []}


def status() -> Dict[str, Any]:
    """Что показывать в настройках: без самого ключа."""
    conf = _conf()
    saved = key(conf)
    return {
        "provider": conf["provider"],
        "providers": [{"key": k, "label": v["label"], "base": v["base"],
                       "model": v["model"], "key_url": v["key_url"]}
                      for k, v in PROVIDERS.items()],
        "base": conf["base"],
        "model": conf["model"],
        "key_url": conf["key_url"],
        "note": PROVIDERS[conf["provider"]].get("note", ""),
        "configured": configured(),
        "key_hint": (saved[:4] + "…" + saved[-4:]) if len(saved) >= 10 else ("сохранён" if saved else ""),
        "from_env": bool(_env_key()),
    }


_MODELS_CACHE: Dict[str, Any] = {"ts": 0.0, "base": "", "data": []}
_MODELS_TTL = 600.0


def models(force: bool = False, provider: Optional[str] = None) -> Dict[str, Any]:
    """Каталог моделей провайдера (OpenAI-совместимый GET /models).

    `provider` позволяет спросить каталог ещё до сохранения настроек — так
    выпадающий список обновляется сразу при переключении в интерфейсе.
    У Polza.ai список публичный, но ключ, если он есть, отправляем тоже.
    """
    conf = _conf()
    if provider and provider in PROVIDERS and provider != conf["provider"]:
        preset = PROVIDERS[provider]
        saved = {}
        try:
            from . import settings

            saved = settings.get()
        except Exception:  # noqa: BLE001
            saved = {}
        conf = {
            "provider": provider,
            "base": preset["base"],
            "model": preset["model"],
            "label": preset["label"],
            "key_url": preset["key_url"],
        }
        # для custom адрес берём из настроек пользователя
        if provider == "custom":
            conf["base"] = str(saved.get("ai_base") or "").strip().rstrip("/")
    if force:
        _MODELS_CACHE["ts"] = 0.0
    now = time.time()
    if (not force and _MODELS_CACHE["data"] and _MODELS_CACHE["base"] == conf["base"]
            and now - float(_MODELS_CACHE["ts"]) < _MODELS_TTL):
        return {"models": _MODELS_CACHE["data"], "base": conf["base"], "cached": True}
    if not conf["base"]:
        raise AIError("Не выбран адрес API. Укажите базовый URL своего сервера.")
    request = urllib.request.Request(f"{conf['base']}/models", method="GET")
    api_key = key(conf)
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            known = FALLBACK_MODELS.get(conf["provider"]) or [conf["model"]]
            return {"models": [{"id": name, "name": name, "owner": "",
                                "description": ""} for name in known if name],
                    "base": conf["base"], "fallback": True,
                    "warning": ("Ключ не принят сервером — показываю известные "
                                "модели. Проверьте ключ." if api_key else "")}
        raise AIError(f"Каталог моделей недоступен (ошибка {exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise AIError("Не удалось связаться с сервером, чтобы получить список моделей.") from exc
    raw = data.get("data") if isinstance(data, dict) else data
    result: List[Dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "chat")
        if kind != "chat":
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        result.append({
            "id": model_id,
            "name": str(item.get("name") or model_id),
            "owner": str(item.get("owned_by") or ""),
            "description": str(item.get("short_description") or "")[:240],
        })
    result.sort(key=lambda item: (item["owner"], item["id"]))
    _MODELS_CACHE.update({"ts": now, "base": conf["base"], "data": result})
    return {"models": result, "base": conf["base"], "cached": False}


def _post(url: str, payload: Dict[str, Any], api_key: str) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (401, 403):
            raise AIError("Ключ не подошёл: сервер ответил «доступ запрещён». "
                          "Проверьте ключ и выбранного провайдера.") from exc
        if exc.code == 429:
            raise AIError("Слишком много запросов или закончился баланс ключа. "
                          "Попробуйте позже.") from exc
        raise AIError(f"Сервер ИИ ответил ошибкой {exc.code}. {detail}".strip()) from exc
    except urllib.error.URLError as exc:
        raise AIError("Не удалось связаться с сервером ИИ. Проверьте интернет "
                      "или адрес своего сервера.") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise AIError("Сервер ИИ вернул неожиданный ответ.") from exc


def _clip_block(project: Dict[str, Any], clip: Optional[Dict[str, Any]]) -> str:
    """Описание клипа для модели: тайминги, текст субтитров, хук."""
    lines: List[str] = []
    if clip:
        start = float(clip.get("start") or 0.0)
        end = float(clip.get("end") or 0.0)
        lines.append(f"Клип: {start:.1f}–{end:.1f} сек ({max(0.0, end - start):.1f} сек).")
        title = str(clip.get("title") or "").strip()
        if title:
            lines.append(f"Название клипа: {title}")
        hook = str(clip.get("hook") or "").strip()
        if hook:
            lines.append(f"Хук на экране: {hook}")
        words = clip.get("words") or []
        if isinstance(words, list) and words:
            text = " ".join(str(w.get("word") or "") for w in words if isinstance(w, dict))
            lines.append("Субтитры клипа: " + text[:MAX_TRANSCRIPT_CHARS])
        elif clip.get("text"):
            lines.append("Субтитры клипа: " + str(clip.get("text"))[:MAX_TRANSCRIPT_CHARS])
    transcript = project.get("transcript_text") or ""
    if transcript and not lines:
        lines.append("Текст записи: " + str(transcript)[:MAX_TRANSCRIPT_CHARS])
    if not lines:
        lines.append("Данных о клипе мало — дайте общие советы по оформлению "
                     "вертикальных клипов.")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "Ты — редактор коротких вертикальных видео для TikTok, Reels и Shorts. "
    "Отвечай по-русски, коротко и по делу. Не выдумывай фактов, которых нет в тексте. "
    "Давай конкретные действия, которые можно применить в приложении: "
    "текст хука, разбивку на строки, момент начала, длину, что убрать."
)


def build_prompt(project: Dict[str, Any], clip: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Сообщения для API: системная роль + данные проекта."""
    blocks = [
        f"Название записи: {project.get('title') or 'без названия'}",
        f"Язык: {project.get('language') or 'не определён'}",
    ]
    duration = project.get("duration")
    if duration:
        blocks.append(f"Длительность записи: {float(duration):.0f} сек.")
    clips = project.get("clips") or []
    if clips:
        blocks.append(f"Всего клипов в проекте: {len(clips)}")
    blocks.append(_clip_block(project, clip))
    blocks.append(
        "Дай 5 коротких пунктов:\n"
        "1) хук первой строкой — готовый текст;\n"
        "2) где лучше начать/закончить и почему;\n"
        "3) как разбить субтитры на строки;\n"
        "4) что убрать или добавить в монтаже;\n"
        "5) идея подписи к публикации."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


PLAN_LIMIT_CHARS = 16000
PLAN_SCHEMA_EXAMPLE = (
    '{"clips":[{"start":12.5,"end":34.0,"hook":"ОН СДЕЛАЛ ЭТО ЗА НЕДЕЛЮ",'
    '"title":"Почему успел за неделю","caption":"Собрали продукт за 7 дней — вот что вышло"}]}'
)


def _transcript_for_prompt(segments: List[Dict[str, Any]]) -> str:
    """Компактная расшифровка с таймкодами: модель должна видеть, где что сказано."""
    lines: List[str] = []
    for index, seg in enumerate(segments or []):
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start") or 0.0)
            end = float(seg.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        text = " ".join(str(seg.get("text") or "").split())
        if not text:
            continue
        lines.append((index, f"[{start:.1f}-{end:.1f}] {text}"))
    def joined(items: List[tuple]) -> str:
        return "\n".join(line for _, line in items)
    result = joined(lines)
    if len(result) <= PLAN_LIMIT_CHARS:
        return result
    # длинная запись: режем реплики, при необходимости — через одну с пометкой
    shorted = [(i, (f"[{line.split(']')[0][1:]}] " + line.split("] ", 1)[1][:90])
                if "] " in line else line[:110]) for i, line in lines]
    result = joined(shorted)
    if len(result) <= PLAN_LIMIT_CHARS:
        return result
    thinned = [item for item in shorted if item[0] % 2 == 0]
    result = joined(thinned)
    return result[:PLAN_LIMIT_CHARS] + "\n…(расшифровка сокращена)"


def _parse_json_clips(text: str) -> List[Dict[str, Any]]:
    """Достаёт JSON из ответа модели: бывает в ```json и с пояснениями вокруг."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    candidates: List[str] = []
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start:end + 1])
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end > start:
        candidates.append(cleaned[start:end + 1])
    for raw in candidates:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            data = data.get("clips") or data.get("moments") or []
        if not isinstance(data, list):
            continue
        items = [item for item in data if isinstance(item, dict)]
        if items:  # пустой список — пробуем следующий вариант разбора
            return items
    raise AIError("ИИ вернул ответ, который не удалось разобрать как список моментов.")


def _normalize_plan(items: List[Dict[str, Any]], duration: float,
                    min_dur: float, max_dur: float, count: int) -> List[Dict[str, Any]]:
    """Приводит ответ модели к безопасным границам: в кадре и без наложений."""
    limit = max(1, min(int(count or 10), 12))
    low = max(3.0, float(min_dur or 15) * 0.5)
    high = max(low + 1.0, float(max_dur or 60) + 5.0)
    usable = []
    for item in items:
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if duration > 0:
            start = max(0.0, min(start, duration - 1.0))
            end = max(0.0, min(end, duration))
        if end - start < low:
            end = min(duration or end, start + max(low, 8.0))
        if end - start > high:
            end = start + high
        if end - start < 3.0:
            continue
        hook = " ".join(str(item.get("hook") or "").split())[:90]
        title = " ".join(str(item.get("title") or "").split())[:110]
        caption = " ".join(str(item.get("caption") or "").split())[:220]
        usable.append({"start": round(start, 2), "end": round(end, 2),
                       "hook": hook, "title": title, "caption": caption})
    usable.sort(key=lambda c: c["start"])
    result: List[Dict[str, Any]] = []
    for candidate in usable:
        if result and candidate["start"] < result[-1]["end"] - 0.3:
            # наложение: сдвигаем начало, чтобы клипы не пересекались
            candidate["start"] = round(result[-1]["end"] + 0.2, 2)
            if candidate["end"] - candidate["start"] < 3.0:
                continue
        result.append(candidate)
        if len(result) >= limit:
            break
    return result


def plan_clips(segments: List[Dict[str, Any]], duration: float,
               options: Optional[Dict[str, Any]] = None,
               instruction: str = "") -> Dict[str, Any]:
    """Просит модель выбрать вирусные моменты и вернуть их с хуками.

    Возвращает границы клипов в секундах и подписи. Никаких правок в проекте
    не делает — применение остаётся за pipeline.
    """
    conf = _conf()
    api_key = key(conf) or ("local" if _keyless(conf["provider"]) else "")
    if not api_key:
        raise AIError("Не задан ключ ИИ. Откройте настройки → «ИИ-помощник» и вставьте ключ.")
    if not conf["base"] or not conf["model"]:
        raise AIError("Не выбран провайдер или модель ИИ. Проверьте настройки.")
    options = options or {}
    count = max(1, min(int(options.get("count") or 8), 12))
    min_dur = float(options.get("min_dur") or 15)
    max_dur = float(options.get("max_dur") or 60)
    transcript = _transcript_for_prompt(segments)
    if not transcript.strip():
        raise AIError("Расшифровка пустая — выбирать моменты не из чего.")
    user_text = (
        f"Задание от пользователя: {instruction.strip() or 'нарежь самые вирусные моменты'}\n"
        f"Длительность записи: {float(duration):.0f} сек.\n"
        f"Сделай не больше {count} клипов. Длина каждого от {min_dur:.0f} до {max_dur:.0f} сек.\n"
        "Выбирай моменты, которые цепляют с первой секунды: конфликт, цифра, обещание, "
        "неожиданный вывод, эмоция. Не начинай с приветствия, не режь посреди слова.\n"
        "Хук — первая строка на экране: 3–7 слов, без точки в конце.\n"
        "Верни ТОЛЬКО JSON по схеме, без пояснений:\n"
        f"{PLAN_SCHEMA_EXAMPLE}\n\n"
        "Расшифровка с таймкодами:\n" + transcript
    )
    payload = {
        "model": conf["model"],
        "messages": [
            {"role": "system", "content": "Ты — редактор вирусных вертикальных видео. "
                                          "Отвечай только JSON."},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.3,
        "max_tokens": 1600,
    }
    started = time.time()
    data = _post(f"{conf['base']}/chat/completions", payload, api_key)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError("Сервер ИИ вернул ответ без текста.") from exc
    clips = _normalize_plan(_parse_json_clips(str(content or "")), float(duration or 0.0),
                            min_dur, max_dur, count)
    if not clips:
        raise AIError("ИИ не предложил ни одного подходящего момента — попробуйте другую модель "
                      "или измените задание.")
    return {"clips": clips, "model": conf["model"], "provider": conf["provider"],
            "took": round(time.time() - started, 2), "instruction": instruction.strip()}


def chat(messages: List[Dict[str, str]], *, max_tokens: int = 700,
         temperature: float = 0.3) -> Dict[str, Any]:
    """Универсальный запрос к выбранной модели (используется помощником)."""
    conf = _conf()
    api_key = key(conf) or ("local" if _keyless(conf["provider"]) else "")
    if not api_key:
        raise AIError("Не задан ключ ИИ.")
    if not conf["base"] or not conf["model"]:
        raise AIError("Не выбран провайдер или модель ИИ.")
    payload = {"model": conf["model"], "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    data = _post(f"{conf['base']}/chat/completions", payload, api_key)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError("Сервер ИИ вернул ответ без текста.") from exc
    text = str(content or "").strip()
    if not text:
        raise AIError("Сервер ИИ вернул пустой ответ.")
    return {"text": text, "model": conf["model"], "provider": conf["provider"]}


def advice(project: Dict[str, Any], clip: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Совет по проекту/клипу. Бросает AIError с понятным текстом."""
    conf = _conf()
    api_key = key(conf) or ("local" if _keyless(conf["provider"]) else "")
    if not api_key:
        raise AIError("Не задан ключ ИИ. Откройте настройки → «ИИ-помощник» и вставьте ключ.")
    if not conf["base"] or not conf["model"]:
        raise AIError("Не выбран провайдер или модель ИИ. Проверьте настройки.")
    payload = {
        "model": conf["model"],
        "messages": build_prompt(project, clip),
        "temperature": 0.4,
        "max_tokens": 700,
    }
    started = time.time()
    data = _post(f"{conf['base']}/chat/completions", payload, api_key)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError("Сервер ИИ вернул ответ без текста.") from exc
    text = str(content or "").strip()
    if not text:
        raise AIError("Сервер ИИ вернул пустой ответ.")
    return {
        "advice": text,
        "model": conf["model"],
        "provider": conf["provider"],
        "took": round(time.time() - started, 2),
    }
