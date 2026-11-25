"""Пользовательские настройки приложения (data/settings.json).

Храним только то, что человек выбирает сам:
  * encoder — "auto" | "gpu" | "cpu": чем кодировать клипы;
  * download_dir — куда складывать скачанные исходники;
  * clips_dir — куда сохранять готовые клипы;
  * ask_folder — спрашивать папку при каждом сохранении;
  * power — 0..100, сколько мощности машины отдавать обработке;
  * cpu_limit — 25..100, жёсткий потолок загрузки процессора;
  * gpu_preference — auto | integrated | discrete: на какой видеокарте считать;
  * ai_provider / ai_base / ai_model — ИИ-помощник (OpenAI-совместимый API);
  * ai_key — ключ доступа, хранится локально с правами 0600;
  * agent_mode — агент работает по шагам и сам доводит задание до конца.
"""
from __future__ import annotations

import json
import os
import threading
from tempfile import NamedTemporaryFile
from pathlib import Path
from typing import Any, Dict

from .config import DATA_DIR

SETTINGS_FILE = DATA_DIR / "settings.json"
DEFAULTS: Dict[str, Any] = {
    "encoder": "auto",          # auto = GPU, если железо умеет; cpu = всегда libx264
    "download_dir": "",
    "clips_dir": "",
    "ask_folder": True,
    "power": 50,                # 0 — экономно, 100 — максимум
    "cpu_limit": 100,           # 25..100: не занимать больше N% процессора
    "gpu_preference": "auto",   # auto | integrated | discrete
    "ai_provider": "deepseek",  # deepseek | polza | openai | custom
    "ai_base": "",              # пусто — берём адрес провайдера по умолчанию
    "ai_model": "",             # пусто — модель провайдера по умолчанию
    "ai_key": "",               # ключ: на диск с правами 0600, в API не отдаём
    "agent_mode": False,        # агент: несколько шагов до результата
}
_LOCK = threading.Lock()


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {**DEFAULTS, **data}
    except (OSError, ValueError):
        pass
    return dict(DEFAULTS)


def _save(data: Dict[str, Any]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=SETTINGS_FILE.parent,
                            prefix=".settings-", delete=False) as pending:
        path = Path(pending.name)
        try:
            json.dump(data, pending, ensure_ascii=False, indent=2)
            pending.close()
            os.replace(path, SETTINGS_FILE)
        finally:
            path.unlink(missing_ok=True)


def ai_key_scope(data: Dict[str, Any]) -> list[str]:
    from .ai import PROVIDERS

    provider = data.get("ai_provider") or "deepseek"
    if provider not in PROVIDERS:
        provider = "deepseek"
    base = str(data.get("ai_base") or PROVIDERS[provider]["base"]).strip().rstrip("/")
    return [provider, base]


def public(data: Dict[str, Any]) -> Dict[str, Any]:
    """Настройки без секретов — то, что можно отдать в интерфейс."""
    safe = dict(data)
    scope = safe.pop("ai_key_scope", ai_key_scope(data))
    has_key = bool(str(safe.get("ai_key") or "").strip()) and scope == ai_key_scope(data)
    safe["ai_key"] = ""
    safe["ai_key_set"] = has_key
    return safe


def get() -> Dict[str, Any]:
    with _LOCK:
        data = _load()
    # пустые папки не отдаём как валидные пути
    for key in ("download_dir", "clips_dir"):
        value = str(data.get(key) or "")
        data[key] = value if value and Path(value).is_dir() else ""
    return data


def encoder_mode() -> str:
    """auto | gpu | cpu — с учётом переменной окружения как оверрайда."""
    env = os.environ.get("VIRALCUT_HW_ENCODE", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return "cpu"
    if env in ("1", "true", "yes", "on"):
        return "gpu"
    mode = str(get().get("encoder") or "auto")
    return mode if mode in ("auto", "gpu", "cpu") else "auto"


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    with _LOCK:
        data = _load()
        if data.get("ai_key") and "ai_key_scope" not in data:
            data["ai_key_scope"] = ai_key_scope(data)
        if "encoder" in patch and patch["encoder"] in ("auto", "gpu", "cpu"):
            data["encoder"] = patch["encoder"]
        for key in ("download_dir", "clips_dir"):
            if key in patch:
                value = str(patch[key] or "").strip()
                data[key] = value
        if "ask_folder" in patch:
            data["ask_folder"] = bool(patch["ask_folder"])
        if "power" in patch:
            try:
                data["power"] = max(0, min(100, int(patch["power"])))
            except (TypeError, ValueError):
                pass
        if "cpu_limit" in patch:
            try:
                data["cpu_limit"] = max(25, min(100, int(patch["cpu_limit"])))
            except (TypeError, ValueError):
                pass
        if "gpu_preference" in patch and patch["gpu_preference"] in ("auto", "integrated", "discrete"):
            data["gpu_preference"] = patch["gpu_preference"]
        if "ai_provider" in patch and patch["ai_provider"] in (
                "deepseek", "polza", "openai", "local", "custom"):
            # чистим адрес и модель ТОЛЬКО при реальной смене провайдера:
            # иначе обычное сохранение настроек стирало адрес своего сервера,
            # и помощник/агент «теряли ключ» (провайдер становился неготовым)
            if patch["ai_provider"] != data.get("ai_provider"):
                data["ai_base"] = ""
                data["ai_model"] = ""
            data["ai_provider"] = patch["ai_provider"]
        if "ai_base" in patch:
            data["ai_base"] = str(patch["ai_base"] or "").strip()[:300]
        if "ai_model" in patch:
            data["ai_model"] = str(patch["ai_model"] or "").strip()[:120]
        if str(patch.get("ai_key") or "").strip():
            data["ai_key"] = str(patch["ai_key"]).strip()
            data["ai_key_scope"] = ai_key_scope(data)
        if "agent_mode" in patch:
            data["agent_mode"] = bool(patch["agent_mode"])
        _save(data)
    return get()
