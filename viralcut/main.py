"""FastAPI-приложение ViralCut."""
from __future__ import annotations

import os
import shutil
import sys
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Form, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (agent, ai, assistant, cpu_limit, custom_fonts, custom_whisper,
               db, gpu_pref, jobs, pipeline, render, resources,
               settings as app_settings, system_stats, transcribe, utils)
from ._version import __version__
from .config import ASSETS_DIR, DATA_DIR, PROJECTS_DIR, WEB_DIR, ffmpeg


class ProjectIn(BaseModel):
    url: str
    options: Dict[str, Any] = {}
    prepare_only: bool = False


class PipIn(BaseModel):
    enabled: Optional[bool] = None
    cam: Optional[Dict[str, Any]] = None
    cam_corner: Optional[str] = None
    cam_share: Optional[float] = None
    pos: Optional[List[float]] = None
    size: Optional[float] = None
    radius: Optional[int] = None


class AssistantIn(BaseModel):
    message: str
    history: Optional[List[Dict[str, str]]] = None
    project_id: Optional[str] = None


class AdviceIn(BaseModel):
    project_id: str
    clip_id: Optional[str] = None


class SettingsIn(BaseModel):
    encoder: Optional[str] = None
    download_dir: Optional[str] = None
    clips_dir: Optional[str] = None
    ask_folder: Optional[bool] = None
    power: Optional[int] = Field(None, ge=0, le=100)
    cpu_limit: Optional[int] = Field(None, ge=25, le=100)
    gpu_preference: Optional[str] = None
    ai_provider: Optional[str] = None
    ai_base: Optional[str] = None
    ai_model: Optional[str] = None
    ai_key: Optional[str] = None
    agent_mode: Optional[bool] = None


class UpdateSettingsIn(BaseModel):
    enabled: bool = True


class CleanupIn(BaseModel):
    scope: str = "temp"


class WhisperModelIn(BaseModel):
    model: str


class WhisperLanguageIn(BaseModel):
    language: str


class WhisperSettingsIn(BaseModel):
    preset: Optional[str] = None
    device: Optional[str] = None
    compute_type: Optional[str] = None
    cpu_threads: Optional[int] = None
    beam_size: Optional[int] = None
    batch_size: Optional[int] = None
    recheck_limit: Optional[int] = None


class RetryIn(BaseModel):
    force_hd: bool = False
    prepare_only: bool = False


class StartIn(BaseModel):
    options: Dict[str, Any] = {}


class ProbeIn(BaseModel):
    url: str
    cookies_browser: str = ""


class StreamIn(BaseModel):
    url: str
    options: Dict[str, Any] = Field(default_factory=dict)


class ClipEdit(BaseModel):
    sub_free: Optional[List[float]] = None
    text: Optional[str] = None
    audio_track: Optional[int] = Field(None, ge=0)
    start: Optional[float] = None
    end: Optional[float] = None
    hook: Optional[str] = None
    title: Optional[str] = None
    caption: Optional[str] = None
    aspect: Optional[str] = None
    mode: Optional[str] = None
    subtitles: Optional[bool] = None
    accent: Optional[str] = None
    uppercase: Optional[bool] = None
    show_hook: Optional[bool] = None
    sub_size: Optional[int] = None
    sub_font: Optional[str] = None
    sub_position: Optional[str] = None
    sub_offset: Optional[int] = None
    sub_speed: Optional[str] = None
    cut_pauses: Optional[bool] = None


# Лимиты загрузки: интерфейс проверяет 4 ГБ, но сервер обязан проверять сам.
ALLOWED_UPLOAD_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts", ".mts", ".m2ts",
    ".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac",
}
# Локальная нарезка не должна упираться в 4 ГБ: длинные записи бывают по 20–50 ГБ.
# Предел настраивается, по умолчанию 64 ГБ; места на диске всё равно нужно вдвое
# больше (исходник + дорожки).
MAX_UPLOAD_BYTES = int(float(os.environ.get("VIRALCUT_MAX_UPLOAD_GB", "64")) * 1024 ** 3)
MAX_FONT_BYTES = 24 * 1024 * 1024


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Побочные эффекты старта — здесь, а не на импорте модуля."""
    db.init_db()
    db.reset_stale_jobs()
    custom_fonts.sync()
    # жёсткий потолок загрузки процессора: включается один раз на процесс
    try:
        cpu_limit.apply()
    except Exception:  # noqa: BLE001 — лимит не должен мешать запуску
        traceback.print_exc()
    # пробный кадр GPU — в фоне: /api/health не должен ждать энкодер
    import threading as _threading

    _threading.Thread(target=render.hw_encoder, name="hw-probe", daemon=True).start()
    yield


# CORS не нужен: страница отдаётся с того же origin, что и API. Без него чужая
# вкладка в браузере не может дёргать локальный сервер (SSRF/забивка диска).
app = FastAPI(title="ViralCut", version=__version__, lifespan=_lifespan)


@app.middleware("http")
async def _static_cache_headers(request, call_next):
    """Шрифты неизменяемы в пределах сессии, а статика перепроверяется по ETag."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/fonts/"):
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif path.startswith(("/userfonts/", "/static/")):
        # index/JS должны перепроверяться: иначе после обновления WebView2
        # поднимает старый закешированный app.js и экран остаётся пустым
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def _clean_clip_edit(body: ClipEdit) -> Dict[str, Any]:
    """Проверяет правку клипа до постановки в очередь: 400 вместо «клип упал».

    Раньше значения уезжали в фоновую задачу, и клип молча переходил в error.
    """
    from . import render, subtitles

    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if "aspect" in data and data["aspect"] not in render.ASPECTS:
        raise HTTPException(400, "Неизвестный формат кадра")
    if "mode" in data and data["mode"] not in ("auto", "blur", "crop"):
        raise HTTPException(400, "Неизвестный режим кадрирования")
    if "accent" in data and data["accent"] not in subtitles.ACCENTS:
        raise HTTPException(400, "Неизвестный цвет акцента")
    if data.get("sub_free"):
        try:
            x, y = (float(v) for v in data["sub_free"][:2])
        except (TypeError, ValueError):
            raise HTTPException(400, "Неверная позиция субтитров") from None
        data["sub_free"] = [min(1.0, max(0.0, x)), min(1.0, max(0.0, y))]
    if "sub_position" in data and data["sub_position"] not in subtitles.POSITIONS:
        raise HTTPException(400, "Неизвестное положение субтитров")
    if "sub_speed" in data and data["sub_speed"] not in subtitles.SPEEDS:
        raise HTTPException(400, "Неизвестная скорость субтитров")
    if "sub_font" in data and not subtitles.font_exists(data["sub_font"]):
        raise HTTPException(400, "Шрифт не найден")
    if "sub_size" in data:
        try:
            data["sub_size"] = max(70, min(180, int(data["sub_size"])))
        except (TypeError, ValueError):
            raise HTTPException(400, "Некорректный размер субтитров") from None
    if "sub_offset" in data:
        try:
            data["sub_offset"] = max(-120, min(180, int(data["sub_offset"])))
        except (TypeError, ValueError):
            raise HTTPException(400, "Некорректное смещение субтитров") from None
    if "text" in data:
        data["text"] = str(data["text"] or "").strip()[:4000]
    for key in ("start", "end"):
        if key in data:
            try:
                data[key] = max(0.0, float(data[key]))
            except (TypeError, ValueError):
                raise HTTPException(400, "Некорректные границы клипа") from None
    return data


@app.get("/api/health")
def health() -> Dict[str, Any]:
    """Готовность приложения: эндпоинт дёргает и интерфейс, и smoke-тесты.

    Пути к ffmpeg/ffprobe и версия нужны, чтобы по одному ответу понять, что
    за сборка запущена и чем она собрана (важно для тестовых Windows-сборок).
    """
    from . import config as app_config

    ok_ffmpeg = True
    binary = ""
    try:
        binary = ffmpeg()
    except Exception:
        ok_ffmpeg = False
    try:
        probe = app_config.ffprobe() or ""
    except Exception:  # noqa: BLE001
        probe = ""
    return {
        "ok": ok_ffmpeg,
        "version": __version__,
        "platform": sys.platform,
        "frozen": bool(getattr(sys, "frozen", False)),
        "ffmpeg": ok_ffmpeg,
        "ffmpeg_path": binary,
        "ffprobe": probe,
        "whisper": transcribe.model_available(),
        "whisper_model": custom_whisper.get_active_model(),
        "model": transcribe.model_cached(),
        # диагностика рендера: только из кэша, чтобы не тормозить интерфейс
        # пробным кадром GPU (на Windows без NVENC это секунды ожидания)
        "hw": render.hw_cached(),
        "limits": resources.summary(),
        "cpu_cap": cpu_limit.status(),
        "projects": db.count_projects(),
    }



@app.get("/api/whisper/models")
def whisper_models() -> Dict[str, Any]:
    return custom_whisper.models_payload()


@app.post("/api/whisper/model")
def whisper_set_model(body: WhisperModelIn) -> Dict[str, Any]:
    try:
        return custom_whisper.set_active_model(body.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/whisper/language")
def whisper_set_language(body: WhisperLanguageIn) -> Dict[str, Any]:
    try:
        return custom_whisper.set_active_language(body.language)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/whisper/download")
def whisper_download_model(body: WhisperModelIn) -> Dict[str, Any]:
    try:
        return custom_whisper.start_model_download(body.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/whisper/settings")
def whisper_settings() -> Dict[str, Any]:
    return custom_whisper.get_whisper_settings()


@app.post("/api/whisper/settings")
def whisper_set_settings(body: WhisperSettingsIn) -> Dict[str, Any]:
    try:
        data = {k: v for k, v in body.model_dump().items() if v is not None}
        return custom_whisper.set_whisper_settings(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/debug")
def debug_status() -> Dict[str, Any]:
    from . import custom_debug

    return custom_debug.snapshot()


@app.post("/api/debug/cleanup")
def debug_cleanup(body: CleanupIn = CleanupIn()) -> Dict[str, Any]:
    from . import custom_cleanup

    try:
        return custom_cleanup.run_cleanup(body.scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class ManualClipIn(BaseModel):
    start: str
    end: str
    trim_silence: bool = True


@app.post("/api/projects/{pid}/clips")
def project_add_clip(pid: str, body: ManualClipIn) -> Dict[str, Any]:
    """Клип по таймкоду: пользователь сам указывает начало и конец."""
    start = pipeline.parse_timecode(body.start)
    end = pipeline.parse_timecode(body.end)
    if start is None or end is None:
        raise HTTPException(400, "Время в формате мм:сс или секунды")
    if end <= start:
        raise HTTPException(400, "Конец должен быть позже начала")
    try:
        return pipeline.add_manual_clip(pid, start, end, body.trim_silence)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/projects/{pid}/audio")
def project_audio(pid: str) -> Dict[str, Any]:
    """Звуковые дорожки исходника: язык, название, каналы."""
    project = db.get_project(pid)
    if not project or not project.get("source"):
        return {"tracks": [], "selected": 0}
    tracks = utils.audio_tracks(Path(project["source"]))
    return {"tracks": tracks, "selected": int((project.get("options") or {}).get("audio_track") or 0)}


@app.post("/api/projects/{pid}/audio")
def project_audio_set(pid: str, body: Dict[str, Any]) -> Dict[str, Any]:
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "Проект не найден")
    try:
        track = max(0, int(body.get("index") or 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "Неверная дорожка") from None
    source = project.get("source")
    if not source or track >= len(utils.audio_tracks(Path(source))):
        raise HTTPException(400, "Выбранная аудиодорожка отсутствует в исходнике")
    options = dict(project.get("options") or {})
    options["audio_track"] = track
    db.update_project(pid, options=options)
    return {"selected": track}


@app.get("/api/projects/{pid}/pip")
def project_pip(pid: str) -> Dict[str, Any]:
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "Проект не найден")
    return {"pip": (project.get("options") or {}).get("pip") or {}}


@app.post("/api/projects/{pid}/pip")
def project_pip_set(pid: str, body: PipIn) -> Dict[str, Any]:
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "Проект не найден")
    options = dict(project.get("options") or {})
    current = dict(options.get("pip") or {})
    if body.enabled is not None:
        current["enabled"] = body.enabled
    if body.cam:
        try:
            current["cam"] = {k: int(body.cam[k]) for k in ("x", "y", "w", "h")}
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Неверные координаты камеры")
    if body.cam_corner in ("top-left", "top-right", "bottom-left", "bottom-right"):
        current["cam_corner"] = body.cam_corner
        current.pop("cam", None)
    if body.cam_share is not None:
        current["cam_share"] = max(0.15, min(0.45, float(body.cam_share)))
    if body.pos:
        current["pos"] = [max(0.0, min(1.0, float(body.pos[0]))), max(0.0, min(1.0, float(body.pos[1])))]
    if body.size is not None:
        current["size"] = max(0.12, min(0.45, float(body.size)))
    if body.radius is not None:
        current["radius"] = max(0, min(60, int(body.radius)))
    options["pip"] = current
    db.update_project(pid, options=options)
    return {"pip": current}


@app.get("/api/projects/{pid}/frame.jpg")
def project_frame(pid: str, t: float = 0.0, width: int = 900) -> FileResponse:
    project = db.get_project(pid)
    if not project or not project.get("source"):
        raise HTTPException(404, "Кадр недоступен")
    workdir = PROJECTS_DIR / pid
    out = workdir / f"frame_{int(t)}_{width}.jpg"
    if not out.exists():
        try:
            render.snapshot(Path(project["source"]), out, t=t, width=width)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Не удалось получить кадр: {exc}")
    return FileResponse(out, media_type="image/jpeg")


@app.get("/api/settings")
def settings_get() -> Dict[str, Any]:
    from . import settings as app_settings

    data = app_settings.public(app_settings.get())
    data["hw_available"] = render.hw_encoder()
    data["limits"] = resources.summary()
    data["cpu_cap"] = cpu_limit.status()
    data["ai"] = ai.status()
    try:
        from . import system_stats

        data["gpus"] = system_stats.adapters()
    except Exception:  # noqa: BLE001 — список карт не критичен для настроек
        data["gpus"] = {"list": [], "picked": "", "discrete": ""}
    return data


@app.post("/api/settings")
def settings_set(body: SettingsIn) -> Dict[str, Any]:
    from . import settings as app_settings

    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    data = app_settings.update(patch)
    # новый уровень мощности применяется сразу: шлагбаум рендеров читает его
    # из настроек на каждой задаче, а кэш сбрасываем, чтобы не ждать секунду
    resources.invalidate()
    # новый потолок CPU и выбор видеокарты применяем сразу
    try:
        cpu_limit.apply()
        gpu_pref.apply()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    data = app_settings.public(data)
    data["limits"] = resources.summary()
    data["cpu_cap"] = cpu_limit.status()
    data["ai"] = ai.status()
    data["hw_available"] = render.hw_encoder()
    if sys.platform == "win32" and data["encoder"] == "gpu" and not data["hw_available"]:
        data["encoder_warning"] = "Аппаратный кодировщик недоступен. Клипы будут кодироваться на процессоре."
    return data


@app.get("/api/ai")
def ai_status() -> Dict[str, Any]:
    """Провайдер, модель и есть ли ключ (сам ключ не отдаём)."""
    return ai.status()


def _assistant_context(project_id: Optional[str] = None) -> Dict[str, Any]:
    """Факты о приложении для помощника: что настроено и что происходит."""
    from . import system_stats

    gpu_name = ""
    try:
        gpu_info = system_stats.gpu()
        gpu_name = str(gpu_info.get("name") or "")
    except Exception:  # noqa: BLE001
        gpu_name = ""
    context: Dict[str, Any] = {
        "version": __version__,
        "platform": sys.platform,
        "ai": ai.status(),
        "limits": resources.summary(),
        "gpu": gpu_name,
        "hw": render.hw_cached(),
        "model_cached": transcribe.model_cached(),
    }
    if project_id:
        project = db.get_project(project_id)
        if project:
            clips = db.list_clips(project_id)
            context["project"] = {
                "id": project.get("id"),
                "status": project.get("status"),
                "language": project.get("language"),
                "clips": len(clips),
                "clip_index": assistant.clip_index(clips),
            }
    return context


@app.get("/api/assistant")
def assistant_start(project_id: Optional[str] = None) -> Dict[str, Any]:
    """Приветствие и подсказки: помощник открывается сразу с делом."""
    context = _assistant_context(project_id)
    return {
        "welcome": assistant.welcome(context),
        "suggestions": assistant.suggestions(),
        "configured": bool(context["ai"].get("configured")),
        "sections": [{"key": s["key"], "title": s["title"]} for s in assistant.SECTIONS],
    }


@app.post("/api/assistant")
def assistant_ask(body: AssistantIn) -> Dict[str, Any]:
    """Помощник или агент: в агент-режиме задание выполняется по шагам."""
    context = _assistant_context(body.project_id)
    if bool(app_settings.get().get("agent_mode")):
        return agent.run(body.message, context, body.history)
    return assistant.ask(body.message, context, body.history)


@app.post("/api/agent")
def agent_run(body: AssistantIn) -> Dict[str, Any]:
    """Явный запуск агента: несколько шагов до результата."""
    context = _assistant_context(body.project_id)
    return agent.run(body.message, context, body.history)


@app.get("/api/download/check")
def download_check(url: str, browser: Optional[str] = None) -> Dict[str, Any]:
    """Диагностика загрузки: какие клиенты отвечают и с каким качеством."""
    from . import downloader as downloader_module

    if not url.strip():
        raise HTTPException(400, "Нужна ссылка")
    try:
        return downloader_module.inspect_url(url.strip(), browser or None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)[:300]) from exc


@app.get("/api/download/engines")
def download_engines() -> Dict[str, Any]:
    """Какие движки загрузки доступны: встроенный и запасной бинарник."""
    from . import ytdlp_bin
    from . import downloader as downloader_module
    import yt_dlp

    return {"ytdlp": getattr(yt_dlp.version, "__version__", ""),
            "binary": ytdlp_bin.status(),
            "pot": bool(os.environ.get("VIRALCUT_POT_URL")),
            "ffmpeg_ready": downloader_module.ytdlp_ffmpeg_ready()}


@app.get("/api/ai/local")
def ai_local(base: Optional[str] = None) -> Dict[str, Any]:
    """Проверяет, есть ли локальный OpenAI-совместимый сервер (Ollama, LM Studio)."""
    return ai.local_server(base)


@app.get("/api/ai/models")
def ai_models(force: bool = False, provider: Optional[str] = None) -> Dict[str, Any]:
    """Список моделей провайдера — для выпадающего списка в настройках."""
    try:
        return ai.models(force=force, provider=provider)
    except ai.AIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ai/advice")
def ai_advice(body: AdviceIn) -> Dict[str, Any]:
    """Совет по проекту или конкретному клипу — по кнопке в интерфейсе."""
    project = db.get_project(body.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден")
    clip = None
    if body.clip_id:
        clip = db.get_clip(body.clip_id)
        if not clip or clip.get("project_id") != body.project_id:
            raise HTTPException(status_code=404, detail="Клип не найден")
    clips = db.list_clips(body.project_id)
    if clip is None and clips:
        # без выбранного клипа советуем по самому сильному по оценке
        clip = max(clips, key=lambda item: float(item.get("score") or 0.0))
    payload: Dict[str, Any] = {
        "title": project.get("title"),
        "language": project.get("language"),
        "duration": project.get("duration"),
        "clips": [{"start": c.get("start"), "end": c.get("end"),
                   "score": c.get("score")} for c in clips],
    }
    try:
        return ai.advice(payload, clip)
    except ai.AIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/system")
def system_state() -> Dict[str, Any]:
    """Нагрузка машины для виджета: CPU, память, видеокарта."""
    from . import system_stats

    data = system_stats.snapshot({
        "hw": render.hw_cached(),
        "limits": resources.summary(),
        "encoder_fallback": render.cpu_fallback_notice(),
    })
    data["job"] = system_stats.active_job(db.list_projects(5))
    return data


@app.get("/api/diagnostics")
def diagnostics_log(tail: int = 40) -> Dict[str, Any]:
    """Разбор app.log: падение, последний этап, ошибки — для окна «Диагностика»."""
    from . import diagnostics

    system: Dict[str, Any] = {}
    try:
        system = health()
    except Exception:  # noqa: BLE001 — разбор лога важнее телеметрии
        system = {}
    return diagnostics.report(data_dir=DATA_DIR, tail_lines=max(1, min(400, int(tail))),
                              system=system)


@app.get("/api/update")
def update_state(force: bool = False) -> Dict[str, Any]:
    """Состояние проверки обновлений; сеть — только в фоне или по force."""
    from . import updater

    if force:
        return updater.check(force=True)
    updater.maybe_check_async()
    return updater.payload()


@app.post("/api/update/settings")
def update_settings(body: UpdateSettingsIn) -> Dict[str, Any]:
    from . import updater

    return updater.set_enabled(body.enabled)


@app.get("/api/fonts")
def fonts() -> List[Dict[str, Any]]:
    from .subtitles import fonts_payload

    return list(fonts_payload()) + custom_fonts.payload()


@app.post("/api/fonts")
async def font_upload(file: UploadFile = File(...)) -> Dict[str, Any]:
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await file.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FONT_BYTES:
            raise HTTPException(413, "Файл шрифта больше 24 МБ")
        chunks.append(chunk)
    data = b"".join(chunks)
    try:
        return custom_fonts.save(file.filename or "font.ttf", data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Не удалось прочитать шрифт: {str(exc)[:200]}") from exc


@app.delete("/api/fonts/{key}")
def font_delete(key: str) -> Dict[str, Any]:
    if not custom_fonts.delete(key):
        raise HTTPException(404, "Шрифт не найден")
    return {"ok": True}


@app.get("/api/fonts.css")
def fonts_css() -> Response:
    lines = []
    for font in custom_fonts.list_custom():
        family = custom_fonts._clean_family(font.get("css") or f"VCCustom_{font['key']}").replace(" ", "_")
        lines.append(
            f'@font-face {{ font-family: "{family}"; '
            f'src: url("/userfonts/{font["file"]}"); font-display: swap; }}'
        )
    return Response("\n".join(lines), media_type="text/css")


@app.get("/api/projects")
def projects() -> List[Dict[str, Any]]:
    return db.list_projects(100)


@app.post("/api/projects")
def create_project(body: ProjectIn) -> Dict[str, Any]:
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(400, "Добавьте ссылку на видео")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Ссылка должна начинаться с http:// или https://")
    project = db.create_project(url, pipeline.normalize_options(body.options))
    if body.prepare_only:
        jobs.submit_prepare(project["id"])
    else:
        jobs.submit_project(project["id"])
    return project


@app.post("/api/probe")
def probe_video(body: ProbeIn) -> Dict[str, Any]:
    from . import downloader

    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Некорректная ссылка")
    try:
        return downloader.probe(url, browser=body.cookies_browser or None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Не удалось получить информацию о видео: {str(exc)[:300]}") from exc


@app.post("/api/streams")
def stream_create(body: StreamIn) -> Dict[str, Any]:
    from . import live

    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Ссылка должна начинаться с http:// или https://")
    options = pipeline.normalize_options(body.options)
    project = db.create_project(url, options, title="Прямой эфир")
    db.update_project(project["id"], status="running", stage="Подключаюсь к эфиру…",
                      meta={"live": True, "live_status": "connecting", "live_processed": 0,
                            "live_clips": 0, "ad_intervals": []})
    jobs.submit_stream(project["id"], options)
    return db.get_project(project["id"])


@app.get("/api/streams")
def streams_list() -> List[Dict[str, Any]]:
    return [p for p in db.list_projects(100) if (p.get("meta") or {}).get("live")]


@app.get("/api/streams/{pid}")
def stream_detail(pid: str) -> Dict[str, Any]:
    project = db.get_project(pid)
    if not project or not (project.get("meta") or {}).get("live"):
        raise HTTPException(404, "Стрим не найден")
    clips = db.list_clips(pid)
    for clip in clips:
        preview = PROJECTS_DIR / pid / "previews" / f"{clip['id']}.mp4"
        clip["has_preview"] = preview.exists()
    return {"project": project, "clips": clips}


@app.post("/api/streams/{pid}/stop")
def stream_stop(pid: str) -> Dict[str, Any]:
    from . import live

    project = db.get_project(pid)
    if not project or not (project.get("meta") or {}).get("live"):
        raise HTTPException(404, "Стрим не найден")
    live.stop_stream(pid)
    db.update_project(pid, stage="Останавливаю запись…")
    return {"ok": True}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), options: Optional[str] = Form(None)) -> Dict[str, Any]:
    if not file.filename:
        raise HTTPException(400, "Файл не выбран")
    suffix = Path(file.filename).suffix.lower() or ".mp4"
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(400, f"Формат {suffix} не поддерживается — загрузите видео или аудио")
    # формат и прочие настройки приходят с формой: раньше они молча терялись
    # и проект всегда создавался с настройками по умолчанию
    chosen: Dict[str, Any] = {}
    if options:
        try:
            import json as _json

            chosen = _json.loads(options)
        except ValueError:
            chosen = {}
    project = db.create_project("local:source", pipeline.normalize_options(chosen),
                                title=file.filename)
    workdir = PROJECTS_DIR / project["id"]
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / f"source{suffix}"
    written = 0
    try:
        with open(target, "wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    size_hint = (f"{MAX_UPLOAD_BYTES / 1024 ** 3:.1f} ГБ"
                                 if MAX_UPLOAD_BYTES >= 1024 ** 3
                                 else f"{MAX_UPLOAD_BYTES / 1024 ** 2:.0f} МБ")
                    raise HTTPException(
                        413,
                        f"Файл больше {size_hint} — загрузите фрагмент короче "
                        "или поднимите VIRALCUT_MAX_UPLOAD_GB",
                    )
                fh.write(chunk)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        db.delete_project(project["id"])
        raise
    if target.stat().st_size < 1024:
        shutil.rmtree(workdir, ignore_errors=True)
        db.delete_project(project["id"])
        raise HTTPException(400, "Файл пустой или повреждён")
    # Проверяем, что это действительно медиа: иначе в списке появлялся бы
    # проект, который сразу уходит в error.
    try:
        from . import utils

        probed = utils.probe(target)
        if not probed.get("duration"):
            raise ValueError("нет длительности")
    except Exception:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        db.delete_project(project["id"])
        raise HTTPException(400, "Файл не похож на видео или аудио — проверьте содержимое") from None
    db.update_project(project["id"], source=str(target), title=file.filename)
    jobs.submit_prepare(project["id"])
    return db.get_project(project["id"]) or project


@app.get("/api/projects/{pid}")
def project_detail(pid: str) -> Dict[str, Any]:
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "Проект не найден")
    clips = db.list_clips(pid)
    for clip in clips:
        preview = PROJECTS_DIR / pid / "previews" / f"{clip['id']}.mp4"
        clip["has_preview"] = preview.exists()
    source = project.get("source")
    preview = PROJECTS_DIR / pid / "preview.mp4"
    if preview.exists():
        project["source_url"] = f"/media/{pid}/preview.mp4"
    elif source:
        src_path = Path(source)
        audio_only_suffixes = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}
        is_audio_only = (
            src_path.suffix.lower() in audio_only_suffixes
            or src_path.name.startswith("audio.")
            or bool((project.get("meta") or {}).get("segments_mode"))
        )
        if not is_audio_only:
            try:
                from urllib.parse import quote
                rel = src_path.relative_to(PROJECTS_DIR).as_posix()
                project["source_url"] = "/media/" + quote(rel)
            except ValueError:
                pass
    return {"project": project, "clips": clips}


@app.post("/api/projects/{pid}/start")
def project_start(pid: str, body: StartIn = StartIn()) -> Dict[str, Any]:
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "Проект не найден")
    if project.get("status") != "review":
        raise HTTPException(400, "Видео ещё не готово к настройке")
    if jobs.is_running(pid):
        raise HTTPException(409, "Обработка уже идёт — дождитесь окончания или отмените")
    options = pipeline.normalize_options(body.options or project.get("options"))
    pipeline.clear_cancel(pid)
    pending = [c for c in db.list_clips(pid) if c["status"] == "pending"]
    if not (options.get("segments") and pending):
        db.clear_clips(pid)
    db.update_project(pid, status="queued", stage="В очереди", progress=0, error=None,
                      options=options)
    jobs.submit_process(pid)
    return db.get_project(pid) or project


@app.post("/api/projects/{pid}/cancel")
def project_cancel(pid: str) -> Dict[str, Any]:
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "Проект не найден")
    pipeline.request_cancel(pid)
    db.update_project(pid, status="cancelled", stage="Отменено", error=None)
    return {"ok": True}


@app.post("/api/projects/{pid}/retry")
def project_retry(pid: str, body: RetryIn = RetryIn()) -> Dict[str, Any]:
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "Проект не найден")
    print(f"[api] Пересборка проекта {pid} (force_hd={body.force_hd}, "
          f"prepare_only={body.prepare_only})", file=sys.stderr, flush=True)
    options = dict(project.get("options") or {})
    options["force_hd"] = bool(body.force_hd)
    if body.force_hd:
        # HD-повтор всегда целится в 1080p, даже если проект создавали в 360p
        options["max_height"] = max(1080, int(options.get("max_height") or 0))
    meta = project.get("meta") or {}
    segments_hd = bool(body.force_hd and meta.get("segments_mode"))
    running = jobs.is_running(pid)
    if running:
        # текущая обработка ещё останавливается — поставим повтор в очередь после отмены
        pipeline.request_cancel(pid)
    else:
        pipeline.clear_cancel(pid)
        if segments_hd:
            db.reset_clips_pending(pid)
        else:
            db.clear_clips(pid)
    stage = "Готовлю HD-версии клипов" if segments_hd else "В очереди"
    db.update_project(pid, status="queued", stage=stage, progress=0, error=None,
                      options=options)
    jobs.submit_retry(pid, prepare_only=body.prepare_only, segments_hd=segments_hd)
    return db.get_project(pid) or project


@app.delete("/api/projects/{pid}")
def project_delete(pid: str) -> Dict[str, Any]:
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "Проект не найден")
    from . import live

    # Сначала гасим запись/рендер и даём потокам увидеть отмену: иначе ffmpeg
    # продолжает писать в папку, которую мы сейчас удаляем.
    jobs.cancel_pending(pid)
    if (project.get("meta") or {}).get("live"):
        live.stop_stream(pid)
    if jobs.is_running(pid):
        pipeline.request_cancel(pid)
    db.delete_project(pid)
    pipeline.delete_project_files(pid)
    return {"ok": True}


@app.get("/api/clips/{cid}/subtitles.srt")
def clip_srt(cid: str) -> Response:
    """SRT для монтажа в своём редакторе: правленый текст уже внутри."""
    clip = db.get_clip(cid)
    if not clip:
        raise HTTPException(404, "Клип не найден")
    try:
        body = pipeline.clip_srt(clip)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not body.strip():
        raise HTTPException(400, "В клипе нет субтитров")
    title = (clip.get("title") or "clip").replace("/", " ").replace('"', "").strip()[:60] or "clip"
    # кириллица в заголовке не влезает в latin-1 — отдаём имя по RFC 5987
    from urllib.parse import quote

    disposition = (f"attachment; filename=\"clip.srt\"; "
                   f"filename*=UTF-8''{quote(title + '.srt')}")
    return Response(content=body, media_type="application/x-subrip",
                    headers={"Content-Disposition": disposition})


@app.post("/api/projects/{pid}/pip/apply")
def project_pip_apply(pid: str) -> Dict[str, Any]:
    """Пересобирает готовые клипы проекта с текущими настройками окна камеры."""
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "Проект не найден")
    clips = [c for c in db.list_clips(pid) if c.get("path")]
    if not clips:
        raise HTTPException(400, "Нет готовых клипов — сначала запустите нарезку")
    for clip in clips:
        jobs.submit_rerender(clip["id"], {})
    return {"ok": True, "clips": len(clips)}


@app.post("/api/clips/{cid}/rerender")
def clip_rerender(cid: str, body: ClipEdit) -> Dict[str, Any]:
    clip = db.get_clip(cid)
    if not clip:
        raise HTTPException(404, "Клип не найден")
    project = db.get_project(clip["project_id"])
    if not project:
        raise HTTPException(404, "Проект не найден")
    payload = _clean_clip_edit(body)
    new_start = float(payload.get("start", clip["start"]))
    new_end = float(payload.get("end", clip["end"]))
    if new_end - new_start < 3.0:
        raise HTTPException(400, "Длина клипа должна быть не меньше 3 секунд")
    is_segments = bool((clip.get("settings") or {}).get("segments"))
    if is_segments:
        if not str(project.get("url") or "").startswith(("http://", "https://")):
            raise HTTPException(400, "Нельзя перерендерить экономный клип без ссылки на исходное видео")
    elif not project.get("source"):
        # клип из прямого эфира: режем из записанных сегментов
        if not (PROJECTS_DIR / project["id"] / "segments" / "times.jsonl").exists():
            raise HTTPException(400, "Запись эфира не найдена — перерендер невозможен")
        recorded = float((project.get("meta") or {}).get("live_duration") or 0)
        if new_end and recorded and new_end > recorded + 0.5:
            raise HTTPException(400, "Конец клипа выходит за пределы записанной части эфира")
    elif not Path(str(project["source"])).exists():
        raise HTTPException(400, "Исходное видео не найдено на диске — перерендер невозможен")
    payload = _clean_clip_edit(body)
    if not jobs.submit_rerender(cid, payload):
        raise HTTPException(409, "Клип уже пересобирается")
    return {"ok": True, "clip": db.get_clip(cid)}


@app.get("/api/projects/{pid}/zip")
def project_zip(pid: str) -> FileResponse:
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "Проект не найден")
    dest = PROJECTS_DIR / pid / f"viralcut_{pid}.zip"
    try:
        pipeline.export_zip(pid, dest)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(dest, filename="viralcut_clips.zip", media_type="application/zip")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


custom_fonts.CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/fonts", StaticFiles(directory=str(ASSETS_DIR / "fonts")), name="fonts")
app.mount("/userfonts", StaticFiles(directory=str(custom_fonts.CUSTOM_DIR)), name="userfonts")
app.mount("/media", StaticFiles(directory=str(PROJECTS_DIR)), name="media")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
