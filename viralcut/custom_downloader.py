"""Custom YouTube format ranking and section downloader fixes for the personal fork."""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import custom_debug, utils
from .config import ffmpeg, ffmpeg_bin_dir
from .errors import Cancelled


def pick_best_format(info: Dict[str, Any], max_height: int) -> Tuple[str, int]:
    """Select the highest-resolution format up to max_height (height-first ranking)."""
    from . import downloader

    formats = [f for f in (info.get("formats") or []) if f.get("format_id")]
    videos = [
        f for f in formats
        if (f.get("vcodec") or "none") != "none" and int(f.get("height") or 0) > 0
    ]
    if not videos:
        return (downloader.FORMAT_HD if max_height > 360 else downloader.FORMAT_SD), 0

    fitting = [f for f in videos if int(f.get("height") or 0) <= max_height] or videos

    def rank(f: Dict[str, Any]) -> tuple:
        codec = f.get("vcodec") or ""
        proto = (f.get("protocol") or "").lower()
        return (
            int(f.get("height") or 0),
            1 if codec.startswith("avc1") else 0,
            1 if f.get("ext") == "mp4" else 0,
            1 if proto in ("https", "http") else 0,
            1 if (f.get("acodec") or "none") != "none" else 0,
            float(f.get("tbr") or 0),
        )

    video = max(fitting, key=rank)
    height = int(video.get("height") or 0)
    if (video.get("acodec") or "none") != "none":
        return str(video["format_id"]), height

    audios = [
        f for f in formats
        if (f.get("vcodec") or "none") == "none" and (f.get("acodec") or "none") != "none"
    ]
    if audios:
        audio = max(
            audios,
            key=lambda f: (
                1 if f.get("ext") == "m4a" else 0,
                1 if (f.get("protocol") or "").lower() in ("https", "http") else 0,
                float(f.get("abr") or f.get("tbr") or 0),
            ),
        )
        return f"{video['format_id']}+{audio['format_id']}", height
    return str(video["format_id"]), height


def download_section_smart(
    url: str,
    dest_dir: Path,
    start: float,
    end: float,
    *,
    progress: Optional[Any] = None,
    browser: Optional[str] = None,
    max_height: int = 1080,
    force_hd: bool = False,
) -> Tuple[Path, Dict[str, Any]]:
    """Download only a clip range while preserving real HD client selection."""
    import os
    import yt_dlp
    from yt_dlp.utils import download_range_func

    from . import downloader

    downloader.ensure_path()
    dest_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dest_dir / "section.%(ext)s")

    if max_height <= 360:
        hd_format = downloader.FORMAT_SD
    else:
        hd_format = (
            f"bv*[ext=mp4][vcodec^=avc1][height<={max_height}]+ba[ext=m4a]/"
            f"bv*[ext=mp4][vcodec^=avc1][width<={max_height}]+ba[ext=m4a]/"
            f"bv*[ext=mp4][height<={max_height}]+ba[ext=m4a]/"
            f"bv*[ext=mp4][width<={max_height}]+ba[ext=m4a]/"
            f"bv*[height<={max_height}]+ba/bv*[width<={max_height}]+ba/"
            f"b[height<={max_height}]/b[width<={max_height}]/b"
        )

    attempts: List[Tuple[str, str, bool, Optional[Dict[str, Any]]]] = []
    if not downloader.is_youtube(url):
        fmt = f"b[width<={max_height}]/b[height<={max_height}]/b/" + hd_format
        attempts.append((f"{max_height}p", fmt, False, None))
        if not force_hd:
            attempts.append(("SD", downloader.FORMAT_SD, False, None))
    elif max_height <= 360:
        attempts.append(("360p · по умолчанию", downloader.FORMAT_SD, False, None))
        attempts.append(("SD · android/tv", downloader.FORMAT_SD, False, {"youtube": {"player_client": ["android", "tv"]}}))
    else:
        pot = downloader._pot_enabled()
        picked = None
        try:
            info, args, client_label = downloader._youtube_info(url, browser, pot, progress)
            picked = (pick_best_format(info, max_height), args, client_label)
        except downloader.DownloadError as exc:
            print(f"[youtube] section metadata failed: {exc}", file=sys.stderr)

        has_default_attempt = False
        if picked is not None:
            (fmt, real_height), args, client_label = picked
            pot_flag = bool(args and "fetch_pot" in str(args) and pot)
            label_height = real_height or max_height
            attempts.append((f"{label_height}p · {client_label}", fmt, pot_flag, args or None))
            has_default_attempt = not args

        if not has_default_attempt and not (force_hd and attempts):
            attempts.append(("резервный HD", hd_format, False, None))
        elif picked is not None and not force_hd:
            attempts.append(("резервный HD", hd_format, False, None))

        if not force_hd:
            attempts.append(("SD · android/tv", downloader.FORMAT_SD, False, {"youtube": {"player_client": ["android", "tv"]}}))

    last_error: Optional[Exception] = None
    failed_reasons: List[str] = []

    custom_debug.record_download(
        active=True,
        kind="section",
        target_height=max_height,
        source_height=0,
        fallback=False,
        fallback_reason="",
        pct=0.0,
    )

    for label, fmt_try, pot_flag, extra_args in attempts:
        downloader._clean_partials(dest_dir)
        timings = {"started": None, "finished": None, "bytes": 0, "speed": 0.0}

        def hook(d: Dict[str, Any]) -> None:
            status = d.get("status")
            if status == "downloading":
                if timings["started"] is None:
                    timings["started"] = time.time()
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                timings["bytes"] = max(timings["bytes"], int(got))
                pct = (got / total * 100.0) if total else 0.0
                speed = float(d.get("speed") or 0.0)
                if speed:
                    timings["speed"] = speed
                eta = d.get("eta")
                custom_debug.record_download(
                    active=True,
                    kind="section",
                    client=label,
                    format_id=fmt_try,
                    target_height=max_height,
                    speed_mbps=(speed / 1048576.0) if speed else None,
                    pct=min(pct, 99.0),
                    eta_s=int(eta) if eta is not None else None,
                )
                if progress:
                    progress(min(pct, 99.0) / 100.0, f"Скачивание момента ({label})")
            elif status == "finished":
                timings["finished"] = timings["finished"] or time.time()
                custom_debug.record_download(active=True, pct=99.0)
                if progress:
                    progress(0.99, "Момент скачан · рендер")

        opts: Dict[str, Any] = {
            "outtmpl": outtmpl,
            "format": fmt_try,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [hook],
            "retries": 3,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 2,
            "download_ranges": download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": True,
            "ffmpeg_location": str(ffmpeg_bin_dir()),
            "socket_timeout": 30,
        }
        if extra_args:
            opts["extractor_args"] = {
                k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                for k, v in extra_args.items()
            }
        if pot_flag and os.environ.get("VIRALCUT_POT_URL"):
            opts.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {
                "base_url": [os.environ["VIRALCUT_POT_URL"]],
            }
            if os.environ.get("VIRALCUT_NODE"):
                opts["js_runtimes"] = {"node": {"path": os.environ["VIRALCUT_NODE"]}}
        opts.update(downloader._cookie_opts(browser))

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True) or {}
            candidates = [
                p for p in dest_dir.glob("section.*")
                if p.suffix not in (".part", ".ytdl") and p.stat().st_size > 1024
            ]
            if not candidates:
                raise downloader.DownloadError("Не удалось скачать фрагмент видео.")

            path = max(candidates, key=lambda p: p.stat().st_size)
            if path.suffix.lower() != ".mp4":
                mp4 = dest_dir / "section.mp4"
                subprocess.run(
                    [ffmpeg(), "-y", "-v", "error", "-i", str(path), "-c", "copy", str(mp4)],
                    check=False,
                    **utils.subprocess_kwargs(),
                )
                if mp4.exists() and mp4.stat().st_size > 0:
                    path.unlink(missing_ok=True)
                    path = mp4

            if info and "entries" in info:
                entries = [e for e in info["entries"] if e]
                info = entries[0] if entries else info

            info_height = int(info.get("height") or 0)
            vcodec = info.get("vcodec")
            selected = info.get("format_id") or ""
            for rf in info.get("requested_formats") or []:
                if rf.get("height"):
                    info_height = max(info_height, int(rf["height"]))
                if (rf.get("vcodec") or "none") != "none":
                    vcodec = rf.get("vcodec")

            try:
                probed = utils.probe(path)
                probed_height = int(probed.get("height") or 0)
                probed_width = int(probed.get("width") or 0)
            except Exception:
                probed_height = info_height
                probed_width = int(info.get("width") or 0)

            real_height = probed_height or info_height
            size = path.stat().st_size if path.exists() else 0
            dl_sec = None
            if timings["started"] and timings["finished"]:
                dl_sec = round(max(0.1, timings["finished"] - timings["started"]), 1)
            avg_speed = None
            if dl_sec:
                avg_speed = round((timings["bytes"] or size) / dl_sec / 1048576.0, 2)

            height_downgraded = bool(max_height >= 720 and real_height > 0 and real_height < max_height)
            fallback = bool(failed_reasons) or height_downgraded
            fallback_reason = None
            if failed_reasons:
                fallback_reason = failed_reasons[0]
            elif height_downgraded:
                fallback_reason = f"Источник отдал {real_height}p вместо {max_height}p"

            meta = {
                "width": probed_width,
                "height": real_height,
                "source_height": real_height,
                "max_height": max_height,
                "quality": label,
                "client": label,
                "selected_format": selected,
                "vcodec": vcodec,
                "download_seconds": dl_sec,
                "avg_speed_mbps": avg_speed,
                "fallback": fallback,
                "fallback_reason": fallback_reason,
            }
            custom_debug.record_download(
                active=False,
                kind="section",
                client=label,
                format_id=selected or fmt_try,
                source_height=real_height,
                target_height=max_height,
                speed_mbps=avg_speed,
                pct=100.0,
                fallback=fallback,
                fallback_reason=fallback_reason or "",
            )
            return path, meta
        except Cancelled:
            custom_debug.record_download(active=False, kind="section")
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            failed_reasons.append(downloader._fmt_err(exc))
            print(f"[download_section] attempt {label} failed: {exc}", file=sys.stderr)

    custom_debug.record_download(
        active=False,
        kind="section",
        fallback=True,
        fallback_reason=downloader._fmt_err(last_error) if last_error else "Ошибка загрузки",
    )
    if last_error:
        raise last_error
    raise downloader.DownloadError("Не удалось скачать фрагмент видео.")


def rerender_section_clip(
    project: Dict[str, Any],
    clip: Dict[str, Any],
    workdir: Path,
    settings: Dict[str, Any],
    on_progress: Optional[Any] = None,
) -> None:
    """Re-download only the target section and re-render an eco-mode clip cleanly."""
    from . import downloader, pipeline

    url = str(project.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("Нельзя перерендерить экономный клип без ссылки на исходное видео.")

    opts = project.get("options") or {}
    duration = float(project.get("duration") or 0)
    clip_start = float(clip["start"])
    clip_end = float(clip["end"])
    sec_start = max(0.0, clip_start - 0.5)
    sec_end = min(duration or clip_end + 1.0, clip_end + 0.5)
    sec_dir = workdir / "sections" / f"rerender_{clip['id']}"

    try:
        part = downloader.download_section(
            url,
            sec_dir,
            sec_start,
            sec_end,
            browser=opts.get("cookies_browser") or None,
            max_height=int(opts.get("max_height", 1080)),
            force_hd=bool(opts.get("force_hd", False)),
        )
        actual = utils.media_duration(part)
        offset = max(0.0, clip_start - sec_start)
        words = [
            {**w, "start": float(w["start"]) - sec_start, "end": float(w["end"]) - sec_start}
            for w in (clip.get("words") or [])
        ]
        virtual = {
            **clip,
            "start": offset,
            "end": min(actual, offset + (clip_end - clip_start)),
            "words": words,
        }
        pipeline._render_one(part, workdir, virtual, settings, on_progress)
    finally:
        shutil.rmtree(sec_dir, ignore_errors=True)
