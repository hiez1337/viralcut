"""Загрузка исходного видео через yt-dlp (YouTube, TikTok, Instagram, VK, прямые ссылки)."""
from __future__ import annotations

import os
import re
import tempfile
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import custom_debug, custom_downloader, utils
from .config import ASSETS_DIR, ensure_path, ffmpeg, ffmpeg_bin_dir
from .errors import Cancelled
from .utils import subprocess_kwargs

ProgressCb = Optional[Callable[[float, str], None]]

# YouTube отдаёт форматы по-разному в зависимости от клиента. android_vr обычно
# даёт HD без PO-token, android — стабильный SD-запасной вариант.
CLIENTS_HD = ["android_vr", "android", "tv", "ios", "web"]
CLIENTS_SD = ["android", "tv"]

FORMAT_HD = (
    "bv*[ext=mp4][vcodec^=avc1][height<=1080]+ba[ext=m4a]/"
    "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/"
    "bv*[height<=1080]+ba/"
    "b[height<=1080]/b"
)
FORMAT_SD = "18/b[height<=360][ext=mp4]/b[width<=360][ext=mp4]/b[height<=360]/b[width<=360]/b"


class DownloadError(Exception):
    pass


class StalledDownload(Exception):
    """Скорость упала почти до нуля — переподключаемся."""


def is_youtube(url: str) -> bool:
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        return any(host == domain or host.endswith("." + domain)
                   for domain in ("youtube.com", "youtu.be", "youtube-nocookie.com"))
    except Exception:
        return False


def _setup_plugins() -> None:
    """Подключаем PO-token плагин из бандла (в .app нет site-packages)."""
    try:
        from yt_dlp.globals import plugin_dirs

        if (ASSETS_DIR / "plugins" / "yt_dlp_plugins").is_dir():
            plugin_dirs.value = [str(ASSETS_DIR)]
    except Exception:
        pass


_setup_plugins()


def _is_http_503(exc: Exception) -> bool:
    return bool(re.search(r"\b503\b", str(exc)) and re.search(
        r"http|service unavailable|status|\b503\b.*server", str(exc), re.I
    ))


def _fmt_err(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if _is_http_503(exc):
        return "Источник временно не ответил (HTTP 503). Повторите попытку позже."
    if "sign in" in low or "login" in low or "cookies" in low or "not a bot" in low:
        return ("Сайт требует авторизацию. Включите «Cookies из браузера» в настройках "
                "или загрузите видео файлом.")
    if "needs to be reloaded" in low:
        return ("YouTube запросил повторную загрузку страницы (антибот-проверка). "
                "Нажмите «Повторить обработку»; если не помогает — включите cookies из браузера.")
    if "requested format is not available" in low:
        return ("Источник не отдал выбранное качество. Перезапустите загрузку — "
                "Вайралкат подберёт формат заново.")
    if "no video formats found" in low:
        return "Источник не отдал ни одного видеоформата. Попробуйте другую ссылку или файл."
    if "private" in low:
        return "Видео приватное — доступ запрещён."
    if "age" in low and ("confirm" in low or "restricted" in low):
        return ("Видео с возрастным ограничением: включите «Cookies из браузера» "
                "или загрузите файл вручную.")
    if "unavailable" in low or "not available" in low:
        return "Видео недоступно: удалено, скрыто или заблокировано по региону."
    if "unsupported url" in low:
        return "Ссылка не поддерживается. Проверьте адрес или загрузите файл."
    if "http error 403" in low or "forbidden" in low:
        return ("Источник отклонил загрузку (403). Обычно помогает повторная попытка "
                "или cookies из браузера.")
    if "ffmpeg" in low:
        return f"Ошибка склейки видео через ffmpeg: {msg[-300:]}"
    return msg[-500:]


def _cookie_opts(browser: Optional[str]) -> Dict[str, Any]:
    if not browser:
        return {}
    return {"cookiesfrombrowser": (browser, None, None, None)}


def ytdlp_ffmpeg(opts: Dict[str, Any]) -> Dict[str, Any]:
    """Готовит окружение yt-dlp к работе с нашим ffmpeg и возвращает opts.

    Две ловушки, из-за которых падало «ffmpeg is not installed» на частичном
    скачивании (download_ranges):
      1. `ffmpeg_location` читают только постпроцессоры. Проверка
         `FFmpegFD.available()` для частичной загрузки смотрит в PATH, поэтому
         папку с каноническим `ffmpeg` обязательно добавляем в PATH.
      2. Даже с правильным PATH постпроцессоры берут путь из contextvar
         FFmpegPostProcessor._ffmpeg_location — выставляем и его: в бандле .app
         PATH может быть урезан до системного.
    """
    ensure_path()
    bin_dir = str(ffmpeg_bin_dir())
    opts["ffmpeg_location"] = bin_dir
    try:
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

        FFmpegPostProcessor._ffmpeg_location.set(bin_dir)
    except Exception:  # noqa: BLE001 — старые версии yt-dlp этого поля не имеют
        pass
    return opts


def ytdlp_ffmpeg_ready() -> bool:
    """Проверяет ровно то, что проверяет yt-dlp перед частичным скачиванием."""
    ensure_path()
    try:
        from yt_dlp.downloader.external import FFmpegFD

        return bool(FFmpegFD.available())
    except Exception:  # noqa: BLE001
        return False


def _ffmpeg_missing_error() -> DownloadError:
    """Понятная ошибка вместо misleading «ffmpeg is not installed»."""
    try:
        binary = ffmpeg()
    except Exception:  # noqa: BLE001
        binary = "не найден"
    return DownloadError(
        "yt-dlp не смог запустить ffmpeg для частичного скачивания. "
        f"Бинарник: {binary}. Проверьте, что он запускается "
        f"({Path(binary).name if binary != 'не найден' else 'ffmpeg'} -version), "
        "или задайте путь в VIRALCUT_FFMPEG. Полный лог — в app.log."
    )


def _clean_partials(dest_dir: Path) -> None:
    for pattern in ("source.*.part", "source.*.ytdl", "source.part", "source.mp4.part"):
        for path in dest_dir.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass
    for pattern in ("audio.*.part", "audio.part", "audio.*.ytdl", "section.*.part", "section.*.ytdl"):
        for path in dest_dir.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass


def _pot_enabled() -> bool:
    return bool(os.environ.get("VIRALCUT_POT_URL") and os.environ.get("VIRALCUT_NODE"))


def _attempt(url: str, dest_dir: Path, fmt: str, clients: Optional[List[str]],
             browser: Optional[str], progress: ProgressCb,
             pot: bool = False, extractor_args: Optional[Dict[str, Any]] = None,
             required_fragments: bool = False) -> Dict[str, Any]:
    import yt_dlp

    ensure_path()
    outtmpl = str(dest_dir / "source.%(ext)s")

    timings: Dict[str, Any] = {"download_started": None, "download_finished": None,
                               "merge_started": None, "merge_finished": None,
                               "bytes": 0, "speed": 0.0, "slow_since": None}

    def hook(d: Dict[str, Any]) -> None:
        status = d.get("status")
        if status == "downloading":
            if timings["download_started"] is None:
                timings["download_started"] = time.time()
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            pct = (got / total * 100) if total else 0
            speed = float(d.get("speed") or 0)
            if speed:
                timings["speed"] = speed
            timings["bytes"] = max(timings["bytes"], got)
            # автопереподключение при «залипании» на низкой скорости
            now = time.time()
            if speed and speed < 45 * 1024 and pct < 95:
                if timings["slow_since"] is None:
                    timings["slow_since"] = now
                elif now - timings["slow_since"] > 45:
                    raise StalledDownload("скорость ниже 45 КБ/с более 45 секунд")
            else:
                timings["slow_since"] = None
            if progress:
                mb = speed / 1024 / 1024
                speed_txt = f"{mb:.2f} МБ/с ({mb*8:.1f} Мбит/с)" if speed else "…"
                eta = d.get("eta")
                eta_txt = ""
                if eta:
                    eta_txt = f" · осталось {int(eta)//60}:{int(eta)%60:02d}"
                progress(min(pct, 99.0) / 100.0, f"Скачивание {pct:.0f}% · {speed_txt}{eta_txt}")
            custom_debug.record_download(
                active=True, kind="video", format_id=fmt,
                speed_mbps=(speed / 1048576.0) if speed else None,
                pct=min(pct, 99.0), eta_s=d.get("eta"),
            )
        elif status == "finished":
            timings["download_finished"] = timings["download_finished"] or time.time()
            custom_debug.record_download(active=True, pct=99.0)
            if progress:
                progress(0.99, "Дорожки скачаны · объединение через FFmpeg…")

    def pp_hook(d: Dict[str, Any]) -> None:
        if d.get("status") == "started":
            timings["merge_started"] = time.time()
            if progress:
                progress(0.995, "Объединение дорожек (FFmpeg, без перекодирования)")
        elif d.get("status") == "finished":
            timings["merge_finished"] = time.time()

    opts: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "format": fmt,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "restrictfilenames": True,
        "retries": 3,
        "fragment_retries": 5,
        "skip_unavailable_fragments": not required_fragments,
        "concurrent_fragment_downloads": 4,
        "writesubtitles": False,
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "socket_timeout": 30,
    }
    if extractor_args:
        opts["extractor_args"] = {k: (list(v) if isinstance(v, list) else v)
                                  for k, v in extractor_args.items()}
    elif clients:
        youtube_args: Dict[str, Any] = {"player_client": clients}
        if pot:
            youtube_args["fetch_pot"] = ["always"]
        opts["extractor_args"] = {"youtube": youtube_args}
    if pot and os.environ.get("VIRALCUT_POT_URL"):
        opts.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {
            "base_url": [os.environ["VIRALCUT_POT_URL"]]}
    node = os.environ.get("VIRALCUT_NODE")
    if node and is_youtube(url):
        opts["js_runtimes"] = {"node": {"path": node}}
    opts.update(_cookie_opts(browser))
    ytdlp_ffmpeg(opts)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else info

    candidates = sorted(dest_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    candidates = [p for p in candidates if p.suffix not in (".part", ".ytdl")]
    if not candidates:
        raise DownloadError("Не удалось сохранить видеофайл.")
    path = candidates[0]

    if path.suffix.lower() != ".mp4":
        mp4 = dest_dir / "source.mp4"
        cmd = [ffmpeg(), "-y", "-v", "error", "-i", str(path), "-c", "copy", str(mp4)]
        subprocess.run(cmd, check=False, **subprocess_kwargs())
        if mp4.exists() and mp4.stat().st_size > 0:
            path.unlink(missing_ok=True)
            path = mp4

    info = info or {}
    height = info.get("height")
    vcodec = info.get("vcodec")
    acodec = info.get("acodec")
    fps = info.get("fps")
    selected = info.get("format_id") or ""
    for rf in info.get("requested_formats") or []:
        if rf.get("height"):
            height = max(height or 0, rf["height"])
        if (rf.get("vcodec") or "none") != "none":
            vcodec = rf.get("vcodec")
            fps = rf.get("fps") or fps
        if (rf.get("acodec") or "none") != "none":
            acodec = rf.get("acodec")

    max_height = 0
    for f in info.get("formats") or []:
        if (f.get("vcodec") or "none") != "none" and f.get("height"):
            max_height = max(max_height, int(f["height"]))

    now = time.time()
    download_seconds = None
    if timings["download_started"] and timings["download_finished"]:
        download_seconds = round(timings["download_finished"] - timings["download_started"], 1)
    merge_seconds = None
    if timings["merge_started"] and timings["merge_finished"]:
        merge_seconds = round(timings["merge_finished"] - timings["merge_started"], 1)
    size = path.stat().st_size if path.exists() else 0
    avg_speed = None
    if download_seconds and download_seconds > 0:
        avg_speed = round((timings["bytes"] or size) / download_seconds / 1024 / 1024, 2)

    return {
        "path": path,
        "title": info.get("title") or url,
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key"),
        "height": height,
        "max_height": max_height or height,
        "fps": fps,
        "vcodec": vcodec,
        "acodec": acodec,
        "selected_format": selected,
        "filesize": size,
        "download_seconds": download_seconds,
        "merge_seconds": merge_seconds,
        "avg_speed_mbps": avg_speed,
    }



# --- YouTube: подбор рабочего клиента ---------------------------------------

def _youtube_candidates(pot: bool) -> List[tuple[str, Dict[str, Any]]]:
    """Наборы клиентов YouTube в порядке проверки.

    Клиенты YouTube ломаются и чинятся каждые пару недель: «web» требует
    PO-token, «android_vr» отдаёт урезанный список форматов, «tv» иногда просит
    перезагрузить страницу. Поэтому формат спрашиваем у нескольких наборов и
    качаем тем, который реально ответил, — а не угадываем один на всех.
    """
    sets: List[tuple[str, Dict[str, Any]]] = [
        ("по умолчанию", {}),
        ("android", {"youtube": {"player_client": ["android_vr", "android"]}}),
    ]
    if pot:
        sets.append(("PO-token", {"youtube": {"player_client": ["mweb", "web"],
                                              "fetch_pot": ["always"]}}))
    sets.append(("tv/ios", {"youtube": {"player_client": ["tv", "ios"]}}))
    return sets


def _youtube_info(url: str, browser: Optional[str], pot: bool,
                  progress: ProgressCb = None, *,
                  preferred_height: int = 1080, exact_quality: bool = False) -> tuple[Dict[str, Any], Dict[str, Any], str]:
    """При доступном провайдере проверяет нужное качество и сохраняет запасной вариант."""
    import yt_dlp

    ensure_path()
    errors: List[str] = []
    best: Optional[tuple[Dict[str, Any], Dict[str, Any], str]] = None
    best_height = 0
    for label, args in _youtube_candidates(pot):
        opts: Dict[str, Any] = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 30,
        }
        if args:
            opts["extractor_args"] = {k: dict(v) for k, v in args.items()}
        if pot and os.environ.get("VIRALCUT_POT_URL"):
            opts.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {
                "base_url": [os.environ["VIRALCUT_POT_URL"]]}
            if os.environ.get("VIRALCUT_NODE"):
                opts["js_runtimes"] = {"node": {"path": os.environ["VIRALCUT_NODE"]}}
        opts.update(_cookie_opts(browser))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {str(exc)[-160:]}")
            print(f"[youtube] клиент «{label}» не сработал: {exc}", file=sys.stderr)
            continue
        if not info:
            errors.append(f"{label}: пустой ответ")
            continue
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            info = entries[0] if entries else info
        _, height = _pick_format(info, preferred_height)
        if not height:
            errors.append(f"{label}: нет видеодорожек")
            continue
        print(f"[youtube] клиент «{label}»: {height}p", file=sys.stderr)
        if height > best_height:
            best, best_height = (info, args, label), height
        if exact_quality:
            if any(_resolution(f) == preferred_height and (f.get("vcodec") or "none") != "none"
                   for f in info.get("formats") or []):
                best = (info, args, label)
                break
        elif height >= preferred_height:
            break
    if best is not None:
        if progress:
            progress(0.02, f"Источник ответил ({best[2]})")
        return best
    detail = " | ".join(errors[-2:])
    raise DownloadError("YouTube не отдал видео ни одному клиенту. " + detail[-350:])


def _pick_format(info: Dict[str, Any], max_height: int, language: Optional[str] = None) -> tuple[str, int]:
    """Формат из фактически доступных — вместо «слепой» строки селектора."""
    return custom_downloader.pick_best_format(info, max_height, target_lang=language)


YOUTUBE_QUALITIES = (1080, 720, 480)


def _resolution(info: Dict[str, Any]) -> int:
    dimensions = [int(info[key]) for key in ("width", "height") if info.get(key)]
    return min(dimensions, default=0)


def _retry_503(action: Callable[[], Any], quality: int, progress: ProgressCb):
    for attempt in range(3):
        try:
            return action()
        except Cancelled:
            raise
        except Exception as exc:
            if not _is_http_503(exc) or attempt == 2:
                raise
            if progress:
                progress(0.02, f"Источник ответил 503 · повторяю {quality}p")
            time.sleep(attempt + 1)


def _download_exact_youtube(url: str, dest_dir: Path, quality: int,
                            browser: Optional[str], progress: ProgressCb,
                            language: Optional[str] = None) -> Dict[str, Any]:
    pot = _pot_enabled()
    # У Shorts нужная ступень указана шириной. В обоих случаях ограничиваем
    # вторую сторону снизу, чтобы выбрать разрешение по короткой стороне.
    video = f"[height={quality}][width>={quality}]"
    vertical = f"[width={quality}][height>={quality}]"
    filters = (video, vertical)
    selector = "/".join(
        [f"bv{f}+ba" for f in filters] + [f"b{f}" for f in filters]
    )
    exact_selector = selector
    args = None
    label = "YouTube"
    try:
        info, args, label = _youtube_info(url, browser, pot, progress, preferred_height=quality,
                                          exact_quality=True)
        matching = [f for f in info.get("formats") or []
                    if (f.get("vcodec") or "none") == "none" or _resolution(f) == quality]
        if any((f.get("vcodec") or "none") != "none" for f in matching):
            fmt, _ = _pick_format({"formats": matching}, quality, language=language)
            has_audio = "+" in fmt or any(
                str(f.get("format_id")) == fmt and (f.get("acodec") or "none") != "none"
                for f in matching
            )
            if has_audio:
                selector = f"{fmt}/{selector}"
    except DownloadError as exc:
        print(f"[youtube] проверка {quality}p: {exc}", file=sys.stderr, flush=True)

    custom_debug.record_download(
        active=True, kind="video", client=label, format_id=selector,
        target_height=quality, source_height=0, fallback=False, fallback_reason="", pct=0.0,
    )
    try:
        with tempfile.TemporaryDirectory(prefix=".youtube-", dir=dest_dir) as scratch:
            work = Path(scratch)
            try:
                meta = _retry_503(lambda: _attempt(url, work, selector, None, browser, progress,
                                                  pot=pot, extractor_args=args, required_fragments=True),
                                  quality, progress)
            except Cancelled:
                custom_debug.record_download(active=False)
                raise
            except Exception as exc:
                from . import ytdlp_bin

                if _is_http_503(exc) or not ytdlp_bin.enabled():
                    raise
                print(f"[youtube] {quality}p · встроенный загрузчик: {_fmt_err(exc)}",
                      file=sys.stderr, flush=True)
                try:
                    with tempfile.TemporaryDirectory(prefix=".alternate-", dir=work) as alternate:
                        alt = Path(alternate)
                        meta = _retry_503(lambda: ytdlp_bin.download(
                            url, alt, quality, browser, progress, format_selector=exact_selector,
                            language=language,
                        ), quality, progress)
                        downloaded = Path(meta["path"])
                        meta["path"] = downloaded.replace(work / downloaded.name)
                        label = "yt-dlp"
                except Cancelled:
                    custom_debug.record_download(active=False)
                    raise
                except Exception as fallback_exc:
                    if _is_http_503(fallback_exc):
                        raise
                    print(f"[youtube] {quality}p · запасной загрузчик: {_fmt_err(fallback_exc)}",
                          file=sys.stderr, flush=True)
                    raise exc from fallback_exc
            path = Path(meta["path"])
            actual = utils.probe(path)
            custom_debug.record_download(
                active=False, kind="video", client=label,
                format_id=meta.get("selected_format") or selector,
                source_height=actual.get("height") or quality, target_height=quality,
                speed_mbps=meta.get("avg_speed_mbps"), pct=100.0,
                fallback=False, fallback_reason="",
            )
            resolution = _resolution(actual)
            if resolution != quality or not actual.get("has_audio"):
                raise DownloadError(
                    f"YouTube не отдал {quality}p со звуком. Выберите другое качество или повторите попытку.")
            target = dest_dir / path.name
            path.replace(target)
            meta.update(path=target, height=resolution, max_height=quality, fallback=False,
                        quality=f"{quality}p · {label}")
            return meta
    except Cancelled:
        raise
    except Exception as exc:
        if "requested format is not available" in str(exc).lower():
            raise DownloadError(f"YouTube не отдал {quality}p со звуком. Выберите другое качество.") from exc
        raise DownloadError(f"{quality}p: {_fmt_err(exc)}") from exc


def download(url: str, dest_dir: Path, progress: ProgressCb = None,
             browser: Optional[str] = None, max_height: int = 1080,
             force_hd: bool = False, exact_quality: bool = False,
             language: Optional[str] = None) -> Dict[str, Any]:
    """Скачивает видео. По умолчанию цель — 1080p, при блокировке видимый откат к SD."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    if is_youtube(url) and (force_hd or exact_quality) and max_height in YOUTUBE_QUALITIES:
        return _download_exact_youtube(url, dest_dir, max_height, browser, progress, language=language)

    if max_height <= 360:
        hd_format = FORMAT_SD
    else:
        # width-варианты нужны для вертикальных видео (Shorts, Twitter): у них
        # «height» — длинная сторона, и фильтр height<=1080 отбрасывает 1080p.
        hd_format = (
            f"bv*[ext=mp4][vcodec^=avc1][height<={max_height}]+ba[ext=m4a]/"
            f"bv*[ext=mp4][vcodec^=avc1][width<={max_height}]+ba[ext=m4a]/"
            f"bv*[ext=mp4][height<={max_height}]+ba[ext=m4a]/"
            f"bv*[ext=mp4][width<={max_height}]+ba[ext=m4a]/"
            f"bv*[height<={max_height}]+ba/bv*[width<={max_height}]+ba/"
            f"b[height<={max_height}]/b[width<={max_height}]/b"
        )

    attempts: List[tuple] = []
    if not is_youtube(url):
        # Twitter/VK/TikTok: сначала пробуем прямой HTTPS-файл — он качается одним
        # потоком и не требует склейки HLS-фрагментов (в разы быстрее).
        direct = f"b[width<={max_height}]/b[height<={max_height}]/b"
        attempts.append((f"{max_height}p", None, direct + "/" + hd_format, False, None))
        if not force_hd:
            attempts.append(("SD", None, FORMAT_SD, False, None))
    else:
        pot = _pot_enabled()
        # Сначала спрашиваем форматы у нескольких клиентов и качаем выбранным.
        picked = None
        try:
            info, args, client_label = _youtube_info(url, browser, pot, progress, preferred_height=max_height)
            picked = (_pick_format(info, max_height, language=language), args, client_label)
        except DownloadError as exc:
            print(f"[youtube] метаданные не получены: {exc}", file=sys.stderr)
        if picked is not None:
            (fmt, real_height), args, client_label = picked
            pot_args = bool(args and "fetch_pot" in str(args))
            label_height = real_height or max_height
            attempts.append((f"{label_height}p · {client_label}", None, fmt,
                             pot_args and pot, args))
        if force_hd:
            if not attempts:
                raise DownloadError(
                    "YouTube не отдал HD-форматы. Перезапустите загрузку; "
                    "если повторяется — включите «Cookies из браузера».")
            attempts = attempts[:1]
        else:
            attempts += [
                ("SD", None, FORMAT_SD, False, None),
                ("резервный клиент", None, hd_format, False, None),
            ]

    last_error: Optional[Exception] = None
    failed_reasons: List[str] = []
    custom_debug.record_download(
        active=True, kind="video", target_height=max_height, source_height=0,
        fallback=False, fallback_reason="", pct=0.0,
    )
    for idx, (label, clients, fmt, pot, extra_args) in enumerate(attempts):
        if idx > 0:
            _clean_partials(dest_dir)
        try:
            custom_debug.record_download(active=True, kind="video", client=label, format_id=fmt)
            if progress:
                progress(0.01, f"Скачивание ({label})")
            meta = None
            for reconnect in range(2):
                try:
                    meta = _attempt(url, dest_dir, fmt, clients, browser, progress,
                                    pot=pot, extractor_args=extra_args)
                    break
                except StalledDownload as exc:
                    print(f"download stalled ({exc}); reconnecting", file=sys.stderr)
                    if progress:
                        progress(0.01, "Скорость упала · переподключаюсь к источнику…")
                    if reconnect == 1:
                        raise
            assert meta is not None
            meta["quality"] = label
            meta["fallback"] = bool(failed_reasons)
            meta["fallback_reason"] = failed_reasons[0] if failed_reasons else None
            custom_debug.record_download(
                active=False, kind="video", client=label,
                format_id=meta.get("selected_format") or fmt,
                source_height=meta.get("height") or 0, target_height=max_height,
                speed_mbps=meta.get("avg_speed_mbps"), pct=100.0,
                fallback=meta["fallback"], fallback_reason=meta["fallback_reason"] or "",
            )
            print(f"download ok: {label} height={meta.get('height')} format={meta.get('selected_format')}",
                  file=sys.stderr)
            return meta
        except Cancelled:
            custom_debug.record_download(active=False)
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            failed_reasons.append(_fmt_err(exc))
            print(f"download attempt {label} failed: {exc}", file=sys.stderr)
            if idx == len(attempts) - 1:
                break
            if progress and not force_hd:
                progress(0.01, f"{label} не удалось · пробую ниже качеством")

    custom_debug.record_download(
        active=False, kind="video", fallback=True,
        fallback_reason=_fmt_err(last_error or DownloadError("Не удалось скачать видео")),
    )
    # Запасной движок: отдельный бинарник yt-dlp, который обновляется сам.
    # Встроенный yt-dlp стареет быстрее, чем выходят релизы приложения.
    try:
        from . import ytdlp_bin

        if ytdlp_bin.enabled():
            if progress:
                progress(0.02, "Пробую запасной движок загрузки…")
            meta = ytdlp_bin.download(url, dest_dir, max_height, browser, progress, language=language)
            try:
                info = utils.probe(meta["path"])
                meta["height"] = info.get("height") or None
                meta["duration"] = info.get("duration") or None
            except Exception:  # noqa: BLE001
                pass
            meta["quality"] = f"{meta.get('height') or max_height}p · yt-dlp"
            meta["fallback"] = True
            meta["fallback_reason"] = _fmt_err(last_error or DownloadError(""))
            custom_debug.record_download(
                active=False, kind="video", client="yt-dlp",
                format_id=meta.get("selected_format") or "bin",
                source_height=meta.get("height") or 0, target_height=max_height,
                pct=100.0, fallback=True, fallback_reason=meta["fallback_reason"] or "",
            )
            print(f"download ok (запасной движок): height={meta.get('height')}",
                  file=sys.stderr)
            return meta
    except Cancelled:
        custom_debug.record_download(active=False)
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[ytdlp-bin] не помог: {str(exc)[:200]}", file=sys.stderr)

    reason = _fmt_err(last_error or DownloadError("Не удалось скачать видео"))
    if force_hd:
        raise DownloadError(reason)
    raise DownloadError(reason)


def download_file(url: str, dest_dir: Path, progress: ProgressCb = None) -> Dict[str, Any]:
    """Прямая ссылка на mp4/webm/mov без yt-dlp."""
    import urllib.request

    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "source.mp4"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(min(got / total, 0.99), f"Скачивание {got/total*100:.0f}%")
    except Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"Не удалось скачать файл по ссылке: {exc}") from exc
    return {"path": path, "title": Path(url.split("?")[0]).name or "video", "duration": None,
            "uploader": None, "webpage_url": url, "extractor": "direct", "quality": "file",
            "height": None}


def pick_subtitle_file(files: List[Path], language: str = "ru") -> Optional[str]:
    """Выбирает только субтитры на языке исходной речи."""
    wanted = (language or "").strip().lower().replace("_", "-")
    if not wanted:
        return None
    regional = None
    for path in files:
        if "." not in path.stem:
            continue
        tag = path.stem.rsplit(".", 1)[-1].lower().replace("_", "-")
        if tag == wanted or tag == wanted + "-orig":
            return str(path)
        if regional is None and "-" not in wanted and tag.startswith(wanted + "-"):
            regional = str(path)
    return regional


def fetch_subtitles(url: str, dest_dir: Path, browser: Optional[str] = None,
                    language: str = "ru") -> Optional[str]:
    """Пробует скачать готовые субтитры (ru/en) — резервный путь без Whisper."""
    import yt_dlp

    ensure_path()
    wanted = (language or "").strip().lower()
    if not wanted:
        return None
    opts: Dict[str, Any] = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [rf"{re.escape(wanted)}(?:[-_][A-Za-z0-9]+)*"],
        "extractor_args": {"youtube": {"skip": ["translated_subs"]}},
        "subtitlesformat": "vtt",
        "quiet": True,
        "no_warnings": True,
    }
    opts.update(_cookie_opts(browser))
    for client in (None, ["android_vr", "android"]):
        attempt = dict(opts)
        if client:
            attempt["extractor_args"] = {"youtube": {"player_client": client, "skip": ["translated_subs"]}}
        with tempfile.TemporaryDirectory(prefix=".subs-", dir=dest_dir) as scratch:
            attempt["outtmpl"] = str(Path(scratch) / "subs.%(ext)s")
            try:
                with yt_dlp.YoutubeDL(attempt) as ydl:
                    ydl.download([url])
            except Exception:  # noqa: BLE001
                continue
            chosen = pick_subtitle_file(sorted(Path(scratch).glob("subs.*.vtt")), wanted)
            if chosen:
                target = dest_dir / Path(chosen).name
                Path(chosen).replace(target)
                return str(target)
    return None


def is_direct_media(url: str) -> bool:
    return bool(re.search(r"\.(mp4|mov|webm|mkv|m4v)(\?|$)", url, re.I))


def _pot_token(content_binding: str = "") -> Optional[str]:
    """Запрашивает GVS PO-token у локального провайдера."""
    base = os.environ.get("VIRALCUT_POT_URL")
    if not base:
        return None
    try:
        import json
        import urllib.request

        body = json.dumps({"content_binding": content_binding}).encode()
        req = urllib.request.Request(base.rstrip("/") + "/get_pot", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode()).get("poToken")
    except Exception:
        return None


def probe(url: str, browser: Optional[str] = None) -> Dict[str, Any]:
    """Метаданные и оценки размера по качествам — без скачивания видео."""
    import yt_dlp

    ensure_path()
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "extractor_args": {},
    }
    if is_youtube(url):
        # Раньше здесь жёстко включался PO-token-клиент; когда он перестаёт
        # отвечать, «недоступно» показывалось на любую ссылку. Теперь идём тем же
        # перебором клиентов, что и загрузка.
        info, _args, _label = _youtube_info(url, browser, _pot_enabled())
    else:
        node = os.environ.get("VIRALCUT_NODE")
        if node:
            opts["js_runtimes"] = {"node": {"path": node}}
        opts.update(_cookie_opts(browser))
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else info
    duration = float(info.get("duration") or 0)
    formats = info.get("formats") or []

    def estimate(video: Dict[str, Any], audio: Optional[Dict[str, Any]]) -> int:
        def part(fmt: Optional[Dict[str, Any]]) -> int:
            if not fmt:
                return 0
            size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
            if size <= 0:
                tbr = float(fmt.get("tbr") or 0)
                if tbr and duration:
                    size = int(tbr * 1000 / 8 * duration)
            if size <= 0 and duration:
                guess = {1080: 5.5, 720: 3.0, 480: 1.6, 360: 0.9}.get(
                    min((1080, 720, 480, 360),
                        key=lambda h: abs(h - (fmt.get("height") or 720))), 3.0)
                size = int(guess * 1000 * 1000 / 8 * duration)
            return max(0, size)

        return part(video) + part(audio)

    # Предлагаем только те ступени, которые источник реально отдаёт: иначе для
    # ролика 240p интерфейс обещал 1080p и подставлял размер 240p.
    video_heights = [int(f["height"]) for f in formats
                     if f.get("height") and (f.get("vcodec") or "none") != "none"]
    max_available = max(video_heights) if video_heights else 0
    youtube = is_youtube(url)
    steps = (YOUTUBE_QUALITIES if youtube else
             [h for h in (1080, 720, 360) if h <= max_available] or ([max_available] if max_available else []))

    options = []
    for height in steps:
        vids = [f for f in formats if (f.get("vcodec") or "none") != "none" and
                (_resolution(f) == height if youtube else f.get("height") and int(f["height"]) <= height)]
        if not vids:
            if youtube:
                options.append({"height": height, "size_mb": None, "confirmed": False})
            continue
        def vscore(f: Dict[str, Any]) -> tuple:
            codec = (f.get("vcodec") or "")
            return (f.get("height") or 0,
                    1 if (f.get("ext") == "mp4" and codec.startswith("avc1")) else 0,
                    1 if (f.get("filesize") or f.get("filesize_approx")) else 0,
                    0 if codec.startswith("av01") else 1,
                    f.get("tbr") or 0)

        video = max(vids, key=vscore)
        audios = [f for f in formats
                  if (f.get("acodec") or "none") not in ("none",)
                  and (f.get("vcodec") or "none") in ("none",)
                  and (f.get("filesize") or f.get("filesize_approx") or f.get("tbr"))]
        audio = max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0) if audios else None
        size = estimate(video, audio)
        options.append({"height": height, "size": size,
                        "size_mb": round(size / 1024 / 1024) if size else 0,
                        "format_id": video.get("format_id"), "confirmed": True})

    if not options:
        raise DownloadError("Не удалось получить форматы видео.")

    # Качество — приоритет: рекомендуем лучшее доступное (до 1080p).
    # Раньше для длинных видео предлагался 360p «чтобы быстрее», и люди
    # случайно получали мыльные клипы. Экономия трафика — это «Экономно»,
    # а не снижение разрешения.
    available = {int(o["height"]) for o in options if o.get("confirmed")} or {1080}
    up_to_hd = [h for h in available if h <= 1080]
    recommended = max(up_to_hd) if up_to_hd else max(available)

    for o in options:
        o["recommended"] = o["height"] == recommended

    audio_only = [f for f in formats
                  if (f.get("acodec") or "none") not in ("none",)
                  and (f.get("vcodec") or "none") in ("none",)]
    audio_size = 0
    for a in audio_only:
        size = int(a.get("filesize") or a.get("filesize_approx") or 0)
        if not size and a.get("tbr") and duration:
            size = int(float(a["tbr"]) * 1000 / 8 * duration)
        audio_size = max(audio_size, size)

    return {
        "title": info.get("title") or url,
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "options": options,
        "recommended": recommended,
        "audio_size_mb": round(audio_size / 1024 / 1024) if audio_size else 0,
    }


AUDIO_FORMAT = "ba[abr<=64]/ba[abr<=96]/ba/b[height<=360]/b"


def _extract_direct_audio(url: str, dest_dir: Path, progress: ProgressCb = None) -> Dict[str, Any]:
    """Звук из прямого файла — обычным ffmpeg, без yt-dlp."""
    from .config import ffmpeg

    out = dest_dir / "audio.m4a"
    if progress:
        progress(0.1, "Извлечение звука")
    cmd = [ffmpeg(), "-y", "-v", "error", "-rw_timeout", "30000000", "-i", url,
           "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "aac", "-b:a", "96k", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          timeout=1800, **subprocess_kwargs())
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 5000:
        raise DownloadError("Не удалось получить звук: " + (proc.stderr or "")[-300:])
    if progress:
        progress(0.99, "Звук готов · распознавание речи")
    return {"path": out, "title": Path(url.split("?")[0]).name or "audio", "duration": None,
            "uploader": None, "extractor": "direct", "size": out.stat().st_size}


def download_audio(url: str, dest_dir: Path, progress: ProgressCb = None,
                   browser: Optional[str] = None, language: Optional[str] = None) -> Dict[str, Any]:
    """Скачивает только звук — в разы меньше полного видео."""
    import yt_dlp

    ensure_path()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if is_direct_media(url):
        return _extract_direct_audio(url, dest_dir, progress)
    outtmpl = str(dest_dir / "audio.%(ext)s")
    timings = {"started": None, "finished": None, "bytes": 0, "slow_since": None}

    target_format = AUDIO_FORMAT
    norm_lang = (language or "").lower().strip()
    if norm_lang and norm_lang not in ("auto", "orig", "original"):
        target_format = f"ba[language^={norm_lang}]/ba[language_preference>0]/{AUDIO_FORMAT}"
    else:
        target_format = f"ba[language_preference>0]/{AUDIO_FORMAT}"

    def hook(d: Dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            if timings["started"] is None:
                timings["started"] = time.time()
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            timings["bytes"] = max(timings["bytes"], got)
            speed = float(d.get("speed") or 0)
            pct_raw = (got / total * 100) if total else 0
            now = time.time()
            if speed and speed < 45 * 1024 and pct_raw < 95:
                if timings["slow_since"] is None:
                    timings["slow_since"] = now
                elif now - timings["slow_since"] > 45:
                    raise StalledDownload("аудио: скорость ниже 45 КБ/с")
            else:
                timings["slow_since"] = None
            pct = (got / total * 100) if total else 0
            if progress:
                mb = speed / 1024 / 1024
                sp = f"{mb:.2f} МБ/с" if speed else "…"
                progress(min(pct, 99) / 100.0, f"Скачивание аудио {pct:.0f}% · {sp}")
            custom_debug.record_download(
                active=True, kind="audio", client="audio", format_id=target_format,
                speed_mbps=(speed / 1048576.0) if speed else None,
                pct=min(pct, 99.0), eta_s=d.get("eta"),
            )
        elif d.get("status") == "finished":
            timings["finished"] = time.time()
            custom_debug.record_download(active=False, kind="audio", pct=100.0)
            if progress:
                progress(0.99, "Аудио скачано · распознавание речи")

    opts: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "format": target_format,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "retries": 3,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
    }
    if is_youtube(url) and _pot_enabled() and os.environ.get("VIRALCUT_POT_URL"):
        opts["extractor_args"] = {
            "youtube": {"player_client": ["mweb", "web"], "fetch_pot": ["always"]},
            "youtubepot-bgutilhttp": {"base_url": [os.environ["VIRALCUT_POT_URL"]]},
        }
        if os.environ.get("VIRALCUT_NODE"):
            opts["js_runtimes"] = {"node": {"path": os.environ["VIRALCUT_NODE"]}}
    opts.update(_cookie_opts(browser))
    ytdlp_ffmpeg(opts)
    info = None
    try:
        for reconnect in range(2):
            try:
                if progress:
                    progress(0.01, "Скачивание аудио")
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                break
            except StalledDownload:
                if reconnect == 1:
                    raise
                if progress:
                    progress(0.01, "Скорость упала · переподключаюсь к источнику…")
    finally:
        custom_debug.record_download(active=False, kind="audio")
    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else info
    candidates = [p for p in dest_dir.glob("audio.*") if p.suffix not in (".part", ".ytdl")]
    if not candidates:
        raise DownloadError("Не удалось скачать аудио.")
    path = max(candidates, key=lambda p: p.stat().st_size)
    duration = float((info or {}).get("duration") or 0)
    return {
        "path": path, "title": (info or {}).get("title"), "duration": duration,
        "uploader": (info or {}).get("uploader"), "size": path.stat().st_size,
        "download_seconds": round((timings["finished"] or 0) - (timings["started"] or 0), 1)
        if timings["started"] else None,
    }


def _probe_dims(path: Path) -> Dict[str, Any]:
    """Ширина/высота файла без полного ffprobe-парсинга (ffmpeg -i в stderr)."""
    try:
        proc = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)],
                              capture_output=True, text=True, errors="replace", timeout=20,
                              **subprocess_kwargs())
        text = proc.stderr or ""
        m = re.search(r"Video:\s+\w+.*?,\s+(\d{2,5})x(\d{2,5})", text)
        if m:
            return {"width": int(m.group(1)), "height": int(m.group(2))}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _cut_direct_section(url: str, dest_dir: Path, start: float, end: float,
                        progress: ProgressCb = None, max_height: int = 1080,
                        quality: Optional[Dict[str, Any]] = None) -> Path:
    """Вырезает фрагмент из прямого HTTP/локального файла без yt-dlp."""
    from .config import ffmpeg

    out = dest_dir / "section.mp4"
    duration = max(0.5, end - start)
    height = "" if max_height >= 1080 else f",scale=-2:{int(max_height)}"
    if progress:
        progress(0.1, "Нарезка фрагмента")
    cmd = [ffmpeg(), "-y", "-v", "error", "-rw_timeout", "30000000",
           "-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}", "-i", url,
           "-vf", f"scale=trunc(iw/2)*2:trunc(ih/2)*2{height}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          timeout=max(120.0, duration * 6), **subprocess_kwargs())
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 10000:
        raise DownloadError("Не удалось вырезать фрагмент: " + (proc.stderr or "")[-300:])
    if progress:
        progress(0.99, "Фрагмент готов")
    if quality is not None:
        info = _probe_dims(out)
        quality["height"] = int(info.get("height") or 0)
        quality["label"] = "direct"
    return out


def inspect_url(url: str, browser: Optional[str] = None) -> Dict[str, Any]:
    """Проверка источников: какой клиент YouTube отвечает и с каким качеством.

    Нужна для диагностики «не скачивается»: сразу видно, дело в клиенте,
    в cookies или в самом видео.
    """
    import yt_dlp

    ensure_path()
    results: List[Dict[str, Any]] = []
    pot = _pot_enabled()
    for label, args in _youtube_candidates(pot):
        item: Dict[str, Any] = {"client": label, "ok": False, "height": 0, "error": ""}
        opts: Dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True,
                                "noplaylist": True, "socket_timeout": 20}
        if args:
            opts["extractor_args"] = {k: dict(v) for k, v in args.items()}
        opts.update(_cookie_opts(browser))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info and "entries" in info:
                entries = [e for e in info["entries"] if e]
                info = entries[0] if entries else info
            _fmt, height = _pick_format(info or {}, 1080)
            item.update({"ok": bool(height), "height": height,
                         "title": (info or {}).get("title")})
        except Exception as exc:  # noqa: BLE001
            item["error"] = _fmt_err(exc)
        results.append(item)
    bin_status: Dict[str, Any] = {}
    try:
        from . import ytdlp_bin

        bin_status = ytdlp_bin.status()
    except Exception:  # noqa: BLE001
        bin_status = {}
    return {"url": url, "pot": pot, "clients": results, "binary": bin_status,
            "ytdlp": getattr(yt_dlp.version, "__version__", "")}


def download_section(url: str, dest_dir: Path, start: float, end: float,
                     progress: ProgressCb = None, browser: Optional[str] = None,
                     max_height: int = 1080, force_hd: bool = False,
                     meta_out: Optional[Dict[str, Any]] = None,
                     language: Optional[str] = None,
                     quality: Optional[Dict[str, Any]] = None) -> Path:
    """Скачивает только фрагмент видео (для экономного режима)."""
    if is_direct_media(url):
        ensure_path()
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = _cut_direct_section(url, dest_dir, start, end, progress, max_height, quality)
        if meta_out is not None:
            info = utils.probe(path)
            h = int(info.get("height") or 0)
            meta_out.update({
                "width": int(info.get("width") or 0),
                "height": h,
                "source_height": h,
                "max_height": max_height,
                "quality": f"{h}p" if h else None,
                "selected_format": "direct",
                "fallback": False,
                "fallback_reason": None,
            })
        return path
    ensure_path()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not ytdlp_ffmpeg_ready():
        raise _ffmpeg_missing_error()
    path, meta = custom_downloader.download_section_smart(
        url, dest_dir, start, end,
        progress=progress, browser=browser,
        max_height=max_height, force_hd=force_hd,
        language=language,
    )
    if meta_out is not None:
        meta_out.update(meta)
    if quality is not None:
        quality["height"] = int(meta.get("height") or meta.get("source_height") or 0)
        quality["label"] = meta.get("quality") or ""
    return path
