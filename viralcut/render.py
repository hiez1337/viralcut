"""Рендер вертикальных клипов: кадрирование, фон, субтитры, звук."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import ffmpeg
from . import custom_debug, custom_fonts
from .reframe import crop_expressions
from .utils import MediaError, probe, run, subprocess_kwargs

ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080), "4:5": (1080, 1350)}


def _esc(path: Path) -> str:
    # экранирование для аргумента фильтра ffmpeg
    p = str(path).replace("\\", "/")
    p = p.replace(":", "\\:").replace("'", "\\'")
    return p


def _bg_chain(w: int, h: int, focus_x: float = 0.5, sharpen: bool = False) -> str:
    sharp = ",unsharp=5:5:0.7:5:5:0.0" if sharpen else ""
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"gblur=sigma=34:steps=2,eq=brightness=-0.12:saturation=0.8[bgb];"
        f"[fg]scale={w}:-2:flags=lanczos{sharp}[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2:shortest=1[base]"
    )


def _crop_chain(w: int, h: int, focus_x: float = 0.5, focus_y: float = 0.45,
                sharpen: bool = False) -> str:
    sharp = ",unsharp=5:5:0.7:5:5:0.0" if sharpen else ""
    return (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase{sharp},"
        f"crop={w}:{h}:(iw-{w})*{focus_x:.3f}:(ih-{h})*{focus_y:.3f}[base]"
    )


def _auto_chain(w: int, h: int, crop_path: Dict[str, Any], sharpen: bool = False) -> str:
    """Заливка кадра с плавающим окном по лицам."""
    x_expr, y_expr = crop_expressions(crop_path)
    cw = int(crop_path["crop_w"])
    ch = int(crop_path["crop_h"])
    sharp = ",unsharp=5:5:0.7:5:5:0.0" if sharpen else ""
    return (
        f"[0:v]crop={cw}:{ch}:x='{x_expr}':y='{y_expr}',"
        f"scale={w}:{h}:flags=lanczos,setsar=1{sharp},format=yuv420p[base]"
    )


_HW_CACHE: Dict[str, Any] = {}


def _hw_args(encoder: str, crf: int) -> List[str]:
    """Аргументы аппаратного энкодера (у каждого свои понятия качества)."""
    if encoder == "h264_videotoolbox":
        return ["-c:v", encoder, "-b:v", "9M", "-maxrate", "14M", "-bufsize", "14M",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", "p5", "-rc", "vbr", "-cq", str(crf),
                "-b:v", "0", "-pix_fmt", "yuv420p"]
    if encoder == "h264_qsv":
        return ["-c:v", encoder, "-global_quality", str(crf), "-pix_fmt", "nv12"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-profile:v", "high", "-pix_fmt", "yuv420p"]


def _hw_works(encoder: str) -> bool:
    """Реально ли работает железо, а не только «энкодер есть в сборке ffmpeg».

    `-encoders` показывает скомпилированные кодеки: nvenc есть в списке даже на
    ноутбуке без NVIDIA, videotoolbox — не на каждом чипе. Поэтому пробуем
    закодировать несколько кадров и только потом доверяем энкодеру.
    """
    key = f"works:{encoder}"
    if key in _HW_CACHE:
        return bool(_HW_CACHE[key])
    cmd = [ffmpeg(), "-hide_banner", "-v", "error", "-f", "lavfi",
           "-i", "color=c=black:s=1080x1920:r=30:d=0.4", "-frames:v", "8",
           *_hw_args(encoder, 21), "-f", "null", "-"]
    ok = False
    try:
        proc = run(cmd, timeout=15)
        ok = proc.returncode == 0
    except Exception:  # noqa: BLE001 — таймаут/падение трактуем как «не работает»
        ok = False
    _HW_CACHE[key] = ok
    return ok


def hw_encoder() -> Optional[str]:
    """Аппаратный энкодер для быстрого рендера: videotoolbox / nvenc / qsv.

    Возвращает имя только если ffmpeg его знает И железо отвечает на пробном
    кадре. При любой ошибке рендер один раз повторится на libx264.
    """
    if "value" in _HW_CACHE:
        return _HW_CACHE["value"]
    if os.environ.get("VIRALCUT_HW_ENCODE", "auto").strip().lower() in ("0", "false", "no", "off"):
        _HW_CACHE["value"] = None
        return None
    preferred = {"darwin": ["h264_videotoolbox"], "win32": ["h264_nvenc", "h264_qsv"],
                 "linux": ["h264_nvenc", "h264_qsv"]}.get(sys.platform, [])
    try:
        proc = run([ffmpeg(), "-hide_banner", "-encoders"])
        text = (proc.stdout or "") + (proc.stderr or "")
    except Exception:
        text = ""
    found = next((name for name in preferred if name in text and _hw_works(name)), None)
    _HW_CACHE["value"] = found
    return found


def _video_args(encoder: Optional[str], crf: int, preset: str) -> List[str]:
    if encoder:
        return _hw_args(encoder, crf)
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-profile:v", "high", "-pix_fmt", "yuv420p"]


def _run_encode(
    cmd: List[str],
    duration: float,
    on_progress: Optional[Callable[[float], None]],
    *,
    label: str = "",
    encoder: str = "",
    source_res: str = "",
    target_res: str = "",
) -> Tuple[int, List[str], Optional[str]]:
    """Запускает ffmpeg, качает прогресс, сторожит зависание и шлёт телеметрию."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            errors="replace", bufsize=1, **subprocess_kwargs())
    assert proc.stdout is not None and proc.stderr is not None
    custom_debug.register_process(
        proc.pid,
        "ffmpeg",
        label=label,
        encoder=encoder,
        source_res=source_res,
        target_res=target_res,
    )
    tail: List[str] = []
    stopped: List[str] = []

    def _drain() -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
        except Exception:
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    limit = max(180.0, duration * 8.0)

    def _watchdog() -> None:
        if proc.poll() is not None:
            return
        stopped.append(f"нет прогресса больше {limit:.0f} с")
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(limit, _watchdog)
    timer.daemon = True
    timer.start()
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                prog = round(min(0.99, done / duration), 3)
                custom_debug.update_process(proc.pid, progress=prog)
                if on_progress:
                    on_progress(prog)
        proc.wait()
    finally:
        timer.cancel()
        reader.join(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        custom_debug.unregister_process(proc.pid)
    rc = int(proc.returncode) if proc.returncode is not None else -1
    return rc, tail, (stopped[0] if stopped else None)


def _measure_loudness(source: Path, start: float, duration: float) -> Optional[Dict[str, Any]]:
    """Первый проход loudnorm: измеряем громкость фрагмента."""
    try:
        proc = subprocess.run(
            [ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
             "-i", str(source), "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, errors="replace",
            timeout=max(30.0, duration * 2.5),
            **subprocess_kwargs(),
        )
        text = proc.stderr or ""
        start_idx = text.rfind("{")
        end_idx = text.rfind("}")
        if start_idx == -1 or end_idx <= start_idx:
            return None
        data = json.loads(text[start_idx:end_idx + 1])
        keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        if not all(k in data for k in keys):
            return None
        values = []
        for key in keys:
            try:
                value = float(data[key])
            except (TypeError, ValueError):
                return None
            if value in (float("inf"), float("-inf")) or not math.isfinite(value):
                return None
            values.append(value)
        if not (-99.0 <= values[0] <= 0.0):
            return None
        return data
    except Exception:
        return None


def render_clip(source: Path, clip_start: float, clip_end: float, out_path: Path,
                ass_path: Optional[Path] = None, aspect: str = "9:16",
                mode: str = "blur", focus_x: float = 0.5,
                on_progress: Optional[Callable[[float], None]] = None,
                crf: int = 21, preset: str = "veryfast",
                crop_path: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    w, h = ASPECTS.get(aspect, ASPECTS["9:16"])
    duration = max(0.4, clip_end - clip_start)
    info = probe(source)
    if info["height"] >= 1000:
        crf = min(crf, 20)
        preset = "veryfast"

    sharpen = info["width"] < w * 0.75
    if mode == "auto" and crop_path:
        sharpen = float(crop_path.get("crop_w", info["width"])) < w * 1.15
        parts = [_auto_chain(w, h, crop_path, sharpen)]
    elif mode == "blur":
        parts = [_bg_chain(w, h, focus_x, sharpen)]
    else:
        parts = [_crop_chain(w, h, focus_x, sharpen=sharpen)]
    if ass_path is not None:
        parts.append(
            f"[base]ass=filename='{_esc(ass_path)}':fontsdir='{_esc(custom_fonts.libass_dir())}':alpha=1,"
            f"format=yuv420p[v]"
        )
        vlabel = "[v]"
    else:
        parts.append("[base]scale=in_range=auto:out_range=tv,format=yuv420p[v]")
        vlabel = "[v]"

    filter_complex = ";".join(parts)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    measured = _measure_loudness(source, clip_start, duration) if info["has_audio"] else None

    def _compose(video_args: List[str]) -> List[str]:
        """Собирает команду целиком: так откат на другой энкодер — это пересборка."""
        built = [ffmpeg(), "-y", "-hide_banner", "-v", "error", "-nostats",
                 "-progress", "pipe:1",
                 "-ss", f"{clip_start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
        built += ["-filter_complex", filter_complex, "-map", vlabel]
        if info["has_audio"]:
            if measured:
                audio_filter = (
                    "loudnorm=I=-14:TP=-1.5:LRA=11"
                    f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
                    f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
                    f":offset={measured['target_offset']}:linear=true"
                )
            else:
                audio_filter = "loudnorm=I=-14:TP=-1.5:LRA=11"
            fade_out = max(0.0, duration - 0.06)
            audio_filter += f",afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out:.3f}:d=0.05"
            built += ["-map", "0:a:0", "-af", audio_filter,
                      "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        else:
            built += ["-an"]
        built += video_args + ["-r", "30", "-movflags", "+faststart", "-shortest", str(out_path)]
        return built

    hw = hw_encoder()
    plan: List[Optional[str]] = ([hw] if hw else []) + [None]
    source_res = f"{info['width']}x{info['height']}"
    target_res = f"{w}x{h}"
    tail_text = ""
    stop_reason: Optional[str] = None
    code = 1
    for attempt, encoder in enumerate(plan):
        out_path.unlink(missing_ok=True)
        cmd = _compose(_video_args(encoder, crf, preset))
        code, tail, stop_reason = _run_encode(
            cmd,
            duration,
            on_progress if attempt == 0 else None,
            label=out_path.name,
            encoder=encoder or "libx264",
            source_res=source_res,
            target_res=target_res,
        )
        tail_text = "".join(tail)
        if stop_reason:
            break
        if code == 0 and out_path.exists() and out_path.stat().st_size >= 10000:
            break
        if encoder is not None:
            # железо не справилось — повторяем на CPU, не ломая команду
            print(f"[render] {encoder} не сработал, повтор на libx264", file=sys.stderr, flush=True)

    if stop_reason:
        out_path.unlink(missing_ok=True)
        raise MediaError(f"Рендер остановлен: {stop_reason}. Попробуйте ещё раз.")
    if code != 0 or not out_path.exists() or out_path.stat().st_size < 10000:
        hint = ""
        if hw and "libx264" not in tail_text:
            hint = " Попробуйте отключить аппаратный энкодер: VIRALCUT_HW_ENCODE=0."
        raise MediaError("Не удалось отрендерить клип: " + tail_text[-600:] + hint)

    if on_progress:
        on_progress(1.0)
    return {"path": out_path, "duration": round(duration, 2), "width": w, "height": h}


def make_preview(clip_path: Path, out_path: Path, width: int = 540) -> Optional[Path]:
    """Лёгкая копия для быстрого предпросмотра в интерфейсе."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg(), "-y", "-v", "error", "-i", str(clip_path),
           "-vf", f"scale={width}:-2", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
           str(out_path)]
    proc = run(cmd)
    return out_path if proc.returncode == 0 and out_path.exists() else None


def make_thumb(clip_path: Path, out_path: Path, at: float = 0.6) -> Optional[Path]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg(), "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(clip_path),
           "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "3", str(out_path)]
    proc = run(cmd)
    return out_path if proc.returncode == 0 and out_path.exists() else None
