"""Транскрипция: локальный faster-whisper с проверкой сомнительных мест.

Качество важнее скорости, но без больших моделей:

* аудио для распознавания чистится (high-pass, low-pass, шумоподавление,
  выравнивание громкости) — движку достаётся речь, а не гул и перепады уровня;
* декодирование идёт beam-search с подсказкой-промптом и запасными
  температурами, чтобы не «залипать» на одной гипотезе;
* сегменты с низкой уверенностью перепроверяются повторно: вырезаем окно,
  декодируем ещё раз с контекстом соседей и оставляем вариант, у которого
  выше вероятность модели.

Всё это не требует скачивать другую модель и работает на том же `small`.
"""
from __future__ import annotations

import gc
import os
import re
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import utils
from .config import (DATA_DIR, MODELS_DIR, WHISPER_COMPUTE, WHISPER_DEVICE,
                     WHISPER_LOCAL_DIR, WHISPER_MODEL, ffmpeg)
from .errors import Cancelled

# Всё, что качает HuggingFace, держим внутри папки проекта.
os.environ.setdefault("HF_HOME", str(DATA_DIR / "hf"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

_MODEL = None
_MODEL_LONG = None
_MODEL_NAME: Optional[str] = None
_MODEL_LONG_NAME: Optional[str] = None
_MODEL_LOCK = threading.Lock()
ProgressCb = Optional[Callable[[float, str], None]]

# --- параметры распознавания -------------------------------------------------

# Beam-search вместо жадного декодирования: заметно меньше ошибок в словах.
BEAM_SIZE = max(1, int(os.environ.get("VIRALCUT_WHISPER_BEAM", "5")))
# Сколько сомнительных сегментов перепроверять (0 — выключить).
RECHECK_LIMIT = max(0, int(os.environ.get("VIRALCUT_WHISPER_RECHECK", "8")))
# Батчевое декодирование: на 6-минутной записи 2.6× быстрее и точнее, потому что
# faster-whisper режет аудио по VAD и подаёт пачки в модель вместо одного потока.
BATCH_SIZE = max(1, int(os.environ.get("VIRALCUT_WHISPER_BATCH_SIZE", "8")))
BATCH_MIN_SECONDS = float(os.environ.get("VIRALCUT_WHISPER_BATCH_MIN", "60"))
# Порог уверенности, ниже которого сегмент считаем сомнительным.
WEAK_LOGPROB = float(os.environ.get("VIRALCUT_WHISPER_WEAK_LOGP", "-0.62"))
# Подсказка для декодера: пунктуация, регистр и типичные для роликов слова.
ASR_PROMPT = os.environ.get("VIRALCUT_WHISPER_PROMPT", (
    "ViralCut, вертикальные клипы, субтитры, подкаст, интервью. "
    "Русская речь с пунктуацией; имена и термины пишутся так же, как в оригинале."
))


class TranscriptionError(Exception):
    pass


class NoSpeechError(TranscriptionError):
    pass


def model_cached(model_name: Optional[str] = None) -> bool:
    """Модель уже есть локально (внутри .app или в кэше — сеть не нужна)."""
    from . import custom_whisper

    return custom_whisper.is_model_cached(model_name)


def model_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _reset_model(long_audio: bool = False) -> None:
    global _MODEL, _MODEL_LONG, _MODEL_NAME, _MODEL_LONG_NAME
    with _MODEL_LOCK:
        if long_audio and sys.platform == "darwin":
            _MODEL_LONG = None
            _MODEL_LONG_NAME = None
        else:
            _MODEL = None
            _MODEL_NAME = None
            if sys.platform != "darwin":
                _MODEL_LONG = None
                _MODEL_LONG_NAME = None
        gc.collect()


def _get_model(
    long_audio: bool = False,
    model_name: Optional[str] = None,
    on_progress: ProgressCb = None,
):
    global _MODEL, _MODEL_LONG, _MODEL_NAME, _MODEL_LONG_NAME
    from . import custom_debug, custom_whisper

    target_model = (
        custom_whisper.normalize_model_name(model_name)
        if model_name
        else custom_whisper.get_active_model()
    )
    use_long_slot = bool(long_audio and sys.platform == "darwin")
    with _MODEL_LOCK:
        if use_long_slot and _MODEL_LONG is not None and _MODEL_LONG_NAME == target_model:
            return _MODEL_LONG
        if not use_long_slot and _MODEL is not None and _MODEL_NAME == target_model:
            return _MODEL
        if use_long_slot and _MODEL_LONG is not None:
            _MODEL_LONG = None
            _MODEL_LONG_NAME = None
            gc.collect()
        elif not use_long_slot and _MODEL is not None:
            _MODEL = None
            _MODEL_NAME = None
            gc.collect()

        from faster_whisper import WhisperModel

        model_ref = custom_whisper.resolve_model_ref(target_model, WHISPER_LOCAL_DIR)
        if model_ref == target_model and not custom_whisper.is_model_cached(target_model):
            custom_whisper.ensure_model_downloaded(target_model, on_progress=on_progress)

        candidates = custom_whisper.whisper_candidates(use_long_slot, WHISPER_DEVICE, WHISPER_COMPUTE)
        custom_debug.record_whisper(
            model=target_model,
            status="downloading" if not custom_whisper.is_model_cached(target_model) else "loading",
            fallback_reason="",
        )
        last_exc = None
        model = None
        for device, compute in candidates:
            try:
                model_kwargs: Dict[str, Any] = {
                    "download_root": str(MODELS_DIR),
                    "num_workers": 1,
                }
                if device == "cpu":
                    model_kwargs["cpu_threads"] = custom_whisper.get_effective_cpu_threads()
                try:
                    model = WhisperModel(
                        model_ref,
                        device=device,
                        compute_type=compute,
                        **model_kwargs,
                    )
                except TypeError:
                    model_kwargs.pop("cpu_threads", None)
                    model_kwargs.pop("num_workers", None)
                    model = WhisperModel(
                        model_ref,
                        device=device,
                        compute_type=compute,
                        **model_kwargs,
                    )
                model._vc_device = device  # type: ignore[attr-defined]
                custom_debug.record_whisper(
                    model=target_model, device=device, compute_type=compute, status="ready",
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                model = None
                custom_debug.record_whisper(fallback_reason=f"{device}/{compute}: {str(exc)[:160]}")
                print(f"[whisper] {device}/{compute} недоступен: {str(exc)[:200]}",
                      file=sys.stderr, flush=True)
        if model is None:
            raise TranscriptionError(
                "Локальная модель распознавания речи недоступна. "
                f"Причина: {str(last_exc)[:200]}"
            )
        if use_long_slot:
            _MODEL_LONG = model
            _MODEL_LONG_NAME = target_model
        else:
            _MODEL = model
            _MODEL_NAME = target_model
        return model


def _decode(model, audio: Path, language: Optional[str], vad: bool,
            prompt: Optional[str] = None, beam: Optional[int] = None,
            temperature=None, condition: bool = True):
    """Один проход декодирования с настройками под качество."""
    from . import custom_whisper

    beam_val = beam or custom_whisper.get_effective_beam_size(BEAM_SIZE)
    return model.transcribe(
        str(audio),
        language=language or None,
        task="transcribe",
        beam_size=beam_val,
        best_of=beam_val,
        temperature=[0.0, 0.2, 0.4, 0.6] if temperature is None else temperature,
        length_penalty=1.0,
        repetition_penalty=1.05,
        no_repeat_ngram_size=3,
        condition_on_previous_text=condition,
        prompt_reset_on_temperature=0.5,
        initial_prompt=(prompt or ASR_PROMPT),
        vad_filter=vad,
        vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200} if vad else None,
        word_timestamps=True,
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        language_detection_threshold=0.5,
        language_detection_segments=2,
    )


def _decode_batched(model, audio: Path, language: Optional[str], vad: bool):
    """Пачечное декодирование (faster-whisper BatchedInferencePipeline)."""
    from faster_whisper import BatchedInferencePipeline
    from . import custom_whisper

    pipe = BatchedInferencePipeline(model=model)
    beam_val = custom_whisper.get_effective_beam_size(BEAM_SIZE)
    batch_val = custom_whisper.get_effective_batch_size(BATCH_SIZE)
    kwargs = dict(
        language=language or None,
        task="transcribe",
        beam_size=beam_val,
        condition_on_previous_text=True,
        initial_prompt=ASR_PROMPT,
        vad_filter=vad,
        word_timestamps=True,
        batch_size=batch_val,
    )
    try:
        return pipe.transcribe(str(audio), **kwargs)
    except TypeError:
        # в других версиях части аргументов нет — повторяем с минимумом
        for key in ("condition_on_previous_text", "initial_prompt"):
            kwargs.pop(key, None)
        return pipe.transcribe(str(audio), **kwargs)


def _segment_payload(seg, offset: float) -> Dict[str, Any]:
    text = (seg.text or "").strip()
    words = []
    for word in (seg.words or []):
        token = (word.word or "").strip()
        if not token:
            continue
        words.append({
            "word": token,
            "start": round(float(word.start) + offset, 3),
            "end": round(float(word.end) + offset, 3),
        })
    if not words and text:
        tokens = text.split()
        step = max(0.05, (seg.end - seg.start) / max(1, len(tokens)))
        words = [{
            "word": token,
            "start": round(seg.start + i * step + offset, 3),
            "end": round(seg.start + (i + 1) * step + offset, 3),
        } for i, token in enumerate(tokens)]
    return {
        "start": round(float(seg.start) + offset, 3),
        "end": round(float(seg.end) + offset, 3),
        "text": text,
        "words": words,
        "quality": {
            "avg_logprob": round(float(getattr(seg, "avg_logprob", 0.0) or 0.0), 3),
            "no_speech_prob": round(float(getattr(seg, "no_speech_prob", 0.0) or 0.0), 3),
        },
    }


def _consume(model, path: Path, language: Optional[str], use_vad: bool,
             offset: float, total_duration: float,
             on_progress: ProgressCb, batched: bool = False) -> Tuple[List[Dict[str, Any]], Any]:
    if batched:
        try:
            segments, tinfo = _decode_batched(model, path, language, use_vad)
        except Exception as exc:  # noqa: BLE001 — пачечный режим не должен ломать распознавание
            print(f"[whisper] пачечный режим не сработал: {str(exc)[:160]}", file=sys.stderr, flush=True)
            segments, tinfo = _decode(model, path, language, use_vad)
    else:
        segments, tinfo = _decode(model, path, language, use_vad)
    out: List[Dict[str, Any]] = []
    for seg in segments:
        payload = _segment_payload(seg, offset)
        if not payload["text"]:
            continue
        out.append(payload)
        if on_progress and total_duration:
            on_progress(min(0.99, (offset + float(seg.end)) / total_duration),
                        "Распознавание речи")
    return out, tinfo


def _transcribe_span(model, path: Path, language: Optional[str], offset: float,
                     total_duration: float, on_progress: ProgressCb, batched: bool = False):
    """Пробует с VAD, при ошибке ONNX повторяет без VAD."""
    try:
        return _consume(model, path, language, True, offset, total_duration, on_progress, batched)
    except Exception as exc:  # noqa: BLE001
        low = str(exc).lower()
        if "onnxruntime" not in low and "vad" not in low and "fusedconv" not in low:
            raise
        traceback.print_exc()
        if on_progress:
            on_progress(min(0.99, offset / max(1.0, total_duration)),
                        "VAD недоступен · распознавание без фильтра тишины")
        return _consume(model, path, language, False, offset, total_duration, on_progress, batched)


_PROBE_SECONDS = 60.0
_PROBE_MIN_PROBABILITY = 0.55
# Языки с кириллицей: если Whisper «увидел» английский, а текст кириллический,
# значит язык определён неверно — перераспознаём на русском.
_CYRILLIC_LANGS = ("ru", "uk", "be", "kk", "bg", "sr", "mk")
_CYR_RE = re.compile(r"[А-Яа-яЁё]")
_LAT_RE = re.compile(r"[A-Za-z]")


def _probe_windows(duration: float) -> List[Tuple[float, float]]:
    """Окна для определения языка: начало и середина записи.

    Одно окно может попасть на музыку или тишину — поэтому берём два и
    складываем вероятности: так интро не перевешивает всю запись.
    """
    if duration <= _PROBE_SECONDS * 1.5:
        return [(0.0, duration)]
    first = min(60.0, duration * 0.15)
    second_start = min(max(0.0, duration * 0.55), duration - _PROBE_SECONDS)
    return [(first, first + _PROBE_SECONDS), (second_start, second_start + _PROBE_SECONDS)]


def _probe_language(model, wav: Path, duration: float,
                    workdir: Path) -> Optional[Tuple[str, float]]:
    """Определяет язык по фрагментам с речью, а не по первым 30 секундам.

    Whisper смотрит на начало записи: если там музыка, интро или тишина, язык
    может «уехать» в английский. Здесь язык голосуется по двум окнам, а
    основной проход потом идёт уже с явным языком.
    """
    if duration < 2.0 or not wav.exists():
        return None
    totals: Dict[str, float] = {}
    votes = 0
    for index, (start, end) in enumerate(_probe_windows(duration)):
        span = max(0.0, min(end, duration) - start)
        if span < 2.0:
            continue
        clip = workdir / f"probe_{index}_{int(start)}_{os.urandom(2).hex()}.wav"
        try:
            if not _cut_window(wav, clip, start, start + span):
                continue
            _segments, info = _decode(model, clip, None, True, beam=1,
                                      temperature=[0.0], condition=False)
            lang = getattr(info, "language", None)
            probability = float(getattr(info, "language_probability", 0.0) or 0.0)
            if lang:
                totals[str(lang)] = totals.get(str(lang), 0.0) + probability
                votes += 1
        except Exception:  # noqa: BLE001 — проба не должна ломать распознавание
            continue
        finally:
            try:
                clip.unlink(missing_ok=True)
            except OSError:
                pass
    if not totals:
        return None
    best = max(totals.items(), key=lambda item: item[1])
    score = best[1] / votes
    if score < _PROBE_MIN_PROBABILITY:
        return None
    return best[0], round(score, 3)


def _script_counts(text: str) -> Tuple[int, int]:
    return len(_CYR_RE.findall(text or "")), len(_LAT_RE.findall(text or ""))


def _expected_script(language: str) -> str:
    code = (language or "").lower()[:2]
    if code in _CYRILLIC_LANGS:
        return "cyr"
    if code == "en":
        return "lat"
    return ""


def subtitle_script_mismatch(segments: List[Dict[str, Any]], language: str) -> bool:
    cyr, lat = _script_counts(" ".join(str(seg.get("text") or "") for seg in segments))
    total = cyr + lat
    expected = _expected_script(language)
    return total >= 20 and (
        (expected == "cyr" and cyr < 0.35 * total) or
        (expected == "lat" and lat < 0.35 * total)
    )


def _fix_language_mismatch(model, wav: Path, workdir: Path, segments: List[Dict[str, Any]],
                           detected: Optional[str], on_progress: ProgressCb,
                           probe: Optional[str] = None
                           ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """При несовпадении письменности повторяет расшифровку на языке аудио."""
    target = (probe or detected or "").strip()
    if not subtitle_script_mismatch(segments, target):
        return segments, detected
    if on_progress:
        on_progress(0.997, f"Проверка языка · повторно на «{target}»")
    print(f"[whisper] текст не на языке {target!r} — перераспознаю на {target!r}",
          file=sys.stderr, flush=True)
    duration = max(0.5, float(segments[-1].get("end") or 0.0))
    try:
        fresh, _info = _transcribe_span(model, wav, target, 0.0, duration, None, False)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return segments, detected
    if not fresh:
        return segments, detected
    new_text = " ".join(str(seg.get("text") or "") for seg in fresh)
    new_cyr, new_lat = _script_counts(new_text)
    new_total = new_cyr + new_lat
    if new_total < 20:
        return segments, detected
    good = (new_cyr if _expected_script(target) == "cyr" else new_lat) / new_total
    if good < 0.6:
        return segments, detected
    print(f"[whisper] язык исправлен: {detected!r} → {target!r} "
          f"(правильный алфавит {good:.0%})", file=sys.stderr, flush=True)
    return fresh, target


def _cut_window(src: Path, dst: Path, start: float, end: float) -> bool:
    """Вырезает фрагмент аудио для повторной проверки."""
    cmd = [ffmpeg(), "-y", "-v", "error", "-ss", f"{max(0.0, start):.3f}",
           "-to", f"{max(start + 0.2, end):.3f}", "-i", str(src),
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]
    result = utils.run(cmd)
    return result.returncode == 0 and dst.exists() and dst.stat().st_size > 512


def _recheck(model, wav: Path, workdir: Path, segments: List[Dict[str, Any]],
             language: Optional[str], on_progress: ProgressCb, limit: int) -> int:
    """Перепроверяет сегменты с низкой уверенностью; возвращает число правок.

    Окно вокруг сегмента декодируется заново с контекстом соседних реплик.
    Замена принимается, только если модель стала увереннее и текст не «разъехался»
    по длине — так мы чиним отдельные слова, но не портим удачные сегменты.
    """
    if limit <= 0 or not segments:
        return 0
    weak = [s for s in segments
            if s.get("quality", {}).get("avg_logprob", 0.0) < WEAK_LOGPROB]
    if not weak:
        return 0
    weak.sort(key=lambda s: s["quality"]["avg_logprob"])
    fixed = 0
    for index, seg in enumerate(weak[:limit]):
        clip = workdir / f"recheck_{os.urandom(3).hex()}.wav"
        try:
            if not _cut_window(wav, clip, seg["start"] - 0.30, seg["end"] + 0.40):
                continue
            before = [s["text"] for s in segments if s["end"] <= seg["start"]][-2:]
            context = " ".join(before)[-220:]
            prompt = (context + " " + ASR_PROMPT)[-400:]
            try:
                parts, _ = _decode(model, clip, language, False, prompt=prompt,
                                   beam=BEAM_SIZE, temperature=[0.0], condition=False)
                new_text = " ".join(p.text.strip() for p in parts).strip()
                payloads = [_segment_payload(p, max(0.0, seg["start"] - 0.30)) for p in parts]
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                continue
            if not new_text or not payloads:
                continue
            new_logprob = sum(p["quality"]["avg_logprob"] for p in payloads) / len(payloads)
            old_logprob = seg["quality"]["avg_logprob"]
            ratio = len(new_text) / max(1, len(seg["text"]))
            if new_logprob <= old_logprob + 0.04 or not (0.45 <= ratio <= 2.2):
                continue
            seg["text"] = new_text
            seg["words"] = [w for p in payloads for w in p["words"]]
            seg["start"] = payloads[0]["start"]
            seg["end"] = payloads[-1]["end"]
            seg["quality"]["avg_logprob"] = round(new_logprob, 3)
            seg["quality"]["rechecked"] = True
            fixed += 1
            if on_progress:
                # Прогресс-колбэки снаружи ждут float, поэтому не None.
                on_progress(0.996, f"Проверка спорных фрагментов · {index + 1}")
        finally:
            try:
                clip.unlink(missing_ok=True)
            except OSError:
                pass
    return fixed


def _run_with_timeout(fn, timeout: float):
    """Запускает распознавание части в потоке; при зависании отдаёт None."""
    box = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _split_audio(wav: Path, workdir: Path, chunk_seconds: int) -> tuple[List[tuple[Path, float, float]], Path]:
    folder = workdir / f"whisper_chunks_{os.urandom(4).hex()}"
    folder.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg(), "-y", "-v", "error", "-i", str(wav),
           "-f", "segment", "-segment_time", str(chunk_seconds), "-c", "copy",
           str(folder / "chunk_%03d.wav")]
    result = utils.run(cmd)
    files = sorted(folder.glob("chunk_*.wav"))
    if result.returncode or not files:
        shutil.rmtree(folder, ignore_errors=True)
        return [], folder
    spans = []
    offset = 0.0
    for path in files:
        length = utils.media_duration(path)
        if length <= 0:
            continue
        spans.append((path, offset, length))
        offset += length
    return spans, folder


def transcribe(media: Path, workdir: Path, language: Optional[str] = None,
               on_progress: ProgressCb = None, model_name: Optional[str] = None,
               *, audio_track: Optional[int] = None,
               window: Optional[tuple[float, float]] = None) -> Dict[str, Any]:
    from . import custom_whisper

    target_model = (
        custom_whisper.normalize_model_name(model_name)
        if model_name
        else custom_whisper.get_active_model()
    )
    started = time.time()
    if on_progress:
        on_progress(0.005, "Подготовка звука для распознавания…")
    try:
        info = utils.probe(media)
    except utils.MediaError:
        info = {"duration": utils.media_duration(media), "has_audio": True,
                "width": 0, "height": 0}
    duration = window[1] - window[0] if window is not None else info["duration"]

    if not info["has_audio"]:
        raise TranscriptionError("В видео нет аудиодорожки — нарезки с субтитрами невозможны.")

    # Чистим звук: убираем низкочастотный гул и шипение, выравниваем громкость.
    wav = utils.extract_asr_audio(media, workdir / "audio16k.wav",
                                  audio_track=audio_track, window=window)
    chunks_folder: Optional[Path] = None
    try:
        chunk_seconds = max(120, int(os.environ.get("VIRALCUT_WHISPER_CHUNK", "600")))
        trigger = max(chunk_seconds, int(os.environ.get("VIRALCUT_WHISPER_CHUNK_MIN", "1800")))
        long_audio = duration > trigger
        batched = (duration >= BATCH_MIN_SECONDS
                   and os.environ.get("VIRALCUT_WHISPER_BATCH", "1").strip() not in ("0", "false", "no", "off"))

        def _dl_cb(frac: float, msg: str) -> None:
            if on_progress:
                on_progress(0.005 + max(0.0, min(1.0, frac)) * 0.035, msg)

        model = _get_model(long_audio=long_audio, model_name=target_model, on_progress=_dl_cb)
        spans: List[tuple[Path, float, float]] = [(wav, 0.0, duration)]
        if duration > trigger:
            split, chunks_folder = _split_audio(wav, workdir, chunk_seconds)
            if split:
                spans = split

        out: List[Dict[str, Any]] = []
        detected_language = language
        language_probability = None
        probe_language: Optional[str] = None
        if language is None:
            # Сначала определяем язык по фрагменту с речью и дальше декодируем уже
            # с явным языком: так интро с музыкой не уводит субтитры в английский.
            probe = _probe_language(model, wav, duration, workdir)
            if probe and probe[1] >= _PROBE_MIN_PROBABILITY:
                detected_language, language_probability = probe
                probe_language = detected_language
                print(f"[whisper] язык: {detected_language} ({language_probability:.2f}) "
                      f"— по фрагментам с речью", file=sys.stderr, flush=True)
        for index, (path, offset, length) in enumerate(spans):
            if not path.exists():
                traceback.print_exc()
                continue
            timeout = max(240.0, length * 40)
            try:
                result = _run_with_timeout(
                    lambda: _transcribe_span(model, path, detected_language, offset,
                                             duration, on_progress, batched),
                    timeout,
                )
                if result is None:
                    # часть зависла (например, в драйвере Metal) — перезапускаем модель и идём дальше
                    print(f"whisper chunk {index} timed out; skipping", file=sys.stderr)
                    _reset_model(long_audio)
                    model = _get_model(long_audio=long_audio, model_name=target_model)
                    if on_progress:
                        on_progress(min(0.99, (offset + length) / max(1.0, duration)),
                                    f"Часть {index + 1} зависла · пропускаю")
                    continue
                part, tinfo = result
                out.extend(part)
                t_lang = getattr(tinfo, "language", None)
                t_prob = round(float(getattr(tinfo, "language_probability", 0.0) or 0.0), 3)
                if not detected_language and t_lang:
                    # Защита от ложного срабатывания на шуме (арабский/иврит/урду/валлийский) при низкой уверенности
                    is_suspicious_noise = t_lang in ("ar", "he", "ur", "cy", "fa", "hi") and t_prob < 0.65
                    if not is_suspicious_noise or len(spans) == 1:
                        detected_language = t_lang
                        language_probability = t_prob
                    else:
                        print(f"[whisper] подозрительный язык {t_lang} ({t_prob:.2f}) на первом фрагменте пропущен", file=sys.stderr)
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                continue
            if len(spans) > 1 and on_progress:
                on_progress(min(0.99, (offset + length) / max(1.0, duration)),
                            f"Распознавание речи · часть {index + 1} из {len(spans)}")

        if not out:
            raise NoSpeechError("Речь не распознана: возможно, в видео нет голоса.")

        # Второй проход по спорным местам — только когда аудио не разрезано на части.
        fixed = 0
        effective_recheck = custom_whisper.get_effective_recheck_limit(RECHECK_LIMIT)
        if len(spans) == 1 and effective_recheck > 0:
            if on_progress:
                on_progress(0.995, "Проверка спорных фрагментов…")
            try:
                fixed = _recheck(model, wav, workdir, out, detected_language, on_progress, effective_recheck)
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001
                traceback.print_exc()

        expected_language = language or probe_language
        if not expected_language and (language_probability or 0.0) >= _PROBE_MIN_PROBABILITY:
            expected_language = detected_language
        if expected_language and len(spans) == 1:
            out, detected_language = _fix_language_mismatch(
                model, wav, workdir, out, detected_language, on_progress, expected_language)
        if expected_language and subtitle_script_mismatch(out, expected_language):
            raise TranscriptionError("Текст субтитров не совпал с языком речи. Проверьте выбранную аудиодорожку.")
    finally:
        if chunks_folder is not None:
            shutil.rmtree(chunks_folder, ignore_errors=True)
        try:
            wav.unlink(missing_ok=True)
        except OSError:
            pass

    out.sort(key=lambda s: s["start"])
    print(f"[whisper] итоговый язык: {detected_language or '?'} "
          f"| сегментов: {len(out)}", file=sys.stderr, flush=True)
    logprobs = [s.get("quality", {}).get("avg_logprob", 0.0) for s in out]
    weak = sum(1 for value in logprobs if value < WEAK_LOGPROB)
    confidence = round(sum(logprobs) / len(logprobs), 3) if logprobs else 0.0

    return {
        "language": (detected_language or language or "ru"),
        "duration": duration,
        "segments": out,
        "engine": f"whisper-{target_model}",
        "took": round(time.time() - started, 1),
        "quality": {
            "confidence": confidence,
            "weak_segments": weak,
            "rechecked": fixed,
            "language_probability": language_probability,
        },
    }
