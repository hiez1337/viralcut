"""Запасной движок загрузки: отдельный бинарник yt-dlp.

Встроенный yt-dlp хорош тем, что не требует ничего доустанавливать, но он
стареет: YouTube ломает извлечение каждые несколько недель, и тогда «ничего не
скачивается» до выхода новой версии приложения.

Здесь мы держим второй движок — официальный standalone-бинарник yt-dlp. Он
скачивается один раз в папку данных, обновляется раз в неделю и используется,
когда встроенный не смог. Это даёт пользователю свежий yt-dlp без переустановки
приложения: бинарник обновляется отдельно от нас.

Отключить можно переменной `VIRALCUT_YTDLP_BIN=0`.
"""
from __future__ import annotations

import json
import os
import platform
import re
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DATA_DIR

BIN_DIR = DATA_DIR / "bin"
UPDATE_SECONDS = 7 * 24 * 3600
RELEASE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"

_ASSETS = {
    "darwin": ("yt-dlp_macos", "yt-dlp"),
    "win32": ("yt-dlp.exe", "yt-dlp.exe"),
    "linux": ("yt-dlp_linux", "yt-dlp"),
}


def enabled() -> bool:
    return os.environ.get("VIRALCUT_YTDLP_BIN", "1").strip().lower() not in (
        "0", "false", "no", "off")


def binary_path() -> Path:
    asset, filename = _ASSETS.get(sys.platform, _ASSETS["linux"])
    return BIN_DIR / filename


def version() -> str:
    path = binary_path()
    if not path.exists():
        return ""
    try:
        out = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                             timeout=20, **_spawn())
        return (out.stdout or out.stderr or "").strip().splitlines()[0][:40]
    except Exception:  # noqa: BLE001
        return ""


def _spawn() -> Dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def ensure(force: bool = False) -> Optional[Path]:
    """Скачивает/обновляет бинарник. Возвращает путь или None."""
    if not enabled():
        return None
    path = binary_path()
    try:
        if path.exists() and not force:
            age = time.time() - path.stat().st_mtime
            if age < UPDATE_SECONDS:
                return path
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        asset, filename = _ASSETS.get(sys.platform, _ASSETS["linux"])
        url = RELEASE_URL + asset
        tmp = path.with_suffix(path.suffix + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "ViralCut"})
        with urllib.request.urlopen(request, timeout=120) as response, open(tmp, "wb") as fh:
            while True:
                chunk = response.read(1024 * 512)
                if not chunk:
                    break
                fh.write(chunk)
        if tmp.stat().st_size < 1024 * 1024:
            tmp.unlink(missing_ok=True)
            return path if path.exists() else None
        tmp.replace(path)
        if sys.platform != "win32":
            path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        print(f"[ytdlp-bin] обновлён: {path} ({version()})", file=sys.stderr, flush=True)
        return path
    except Exception as exc:  # noqa: BLE001 — запасной движок не должен мешать
        print(f"[ytdlp-bin] не удалось обновить: {str(exc)[:160]}", file=sys.stderr, flush=True)
        return path if path.exists() else None


def _cookie_args(browser: Optional[str]) -> List[str]:
    if not browser:
        return []
    return ["--cookies-from-browser", browser]


def _fmt_for(max_height: int) -> str:
    h = int(max_height or 1080)
    return (f"bv*[ext=mp4][vcodec^=avc1][height<={h}]+ba[ext=m4a]/"
            f"bv*[ext=mp4][height<={h}]+ba[ext=m4a]/"
            f"bv*[height<={h}]+ba/b[height<={h}]/b")


def download(url: str, dest_dir: Path, max_height: int = 1080,
             browser: Optional[str] = None,
             progress=None, section: Optional[tuple] = None,
             clients: Optional[List[str]] = None,
             timeout: float = 1800.0, format_selector: Optional[str] = None) -> Dict[str, Any]:
    """Скачивает видео запасным бинарником. Бросает RuntimeError при неудаче."""
    path = ensure()
    if not path:
        raise RuntimeError("запасной yt-dlp недоступен")
    from .config import ffmpeg_bin_dir

    dest_dir.mkdir(parents=True, exist_ok=True)
    out_tpl = str(dest_dir / "source.%(ext)s")
    cmd = [str(path), "--no-playlist", "--newline", "--no-warnings",
           "-f", format_selector or _fmt_for(max_height), "--merge-output-format", "mp4",
           "-o", out_tpl, "--progress"]
    if format_selector:
        cmd.append("--abort-on-unavailable-fragments")
    if clients:
        cmd += ["--extractor-args", "youtube:player_client=" + ",".join(clients)]
    ffdir = ffmpeg_bin_dir()
    if ffdir:
        cmd += ["--ffmpeg-location", str(ffdir)]
    cmd += _cookie_args(browser)
    if section:
        start, end = section
        cmd += ["--download-sections", f"*{max(0.0, start):.1f}-{max(0.0, end):.1f}"]
        cmd += ["--force-keyframes-at-cuts"]
    cmd.append(url)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", bufsize=1, **_spawn())
    tail: List[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
            match = re.search(r"(\d{1,3}(?:\.\d+)?)%", line)
            if match and progress:
                progress(min(0.98, float(match.group(1)) / 100.0), f"Скачивание {match.group(1)}%")
        code = proc.wait(timeout=timeout)
    finally:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    if code != 0:
        raise RuntimeError((tail[-1] if tail else "yt-dlp завершился с ошибкой")[-300:])
    files = sorted(dest_dir.glob("source.*"))
    media = [f for f in files if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4a")]
    if not media:
        raise RuntimeError("файл не появился")
    result = media[0]
    return {"path": result, "title": url, "duration": None, "uploader": None,
            "webpage_url": url, "extractor": "yt-dlp-bin", "quality": "bin",
            "height": None, "selected_format": "bin"}


def info(url: str, browser: Optional[str] = None,
         clients: Optional[List[str]] = None, timeout: float = 60.0) -> Dict[str, Any]:
    """Метаданные через запасной бинарник (JSON)."""
    path = ensure()
    if not path:
        raise RuntimeError("запасной yt-dlp недоступен")
    cmd = [str(path), "-J", "--no-playlist", "--no-warnings", "--skip-download"]
    if clients:
        cmd += ["--extractor-args", "youtube:player_client=" + ",".join(clients)]
    cmd += _cookie_args(browser)
    cmd.append(url)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **_spawn())
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "не удалось получить метаданные")[-300:])
    try:
        return json.loads(out.stdout)
    except ValueError as exc:
        raise RuntimeError("бинарник вернул не JSON") from exc


def status() -> Dict[str, Any]:
    path = binary_path()
    return {"enabled": enabled(), "path": str(path), "present": path.exists(),
            "version": version(), "platform": platform.system()}
