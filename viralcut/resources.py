"""Ограничители ресурсов: сколько мощности машины отдаём обработке.

Приложение живёт на ноутбуках, и «занять все ядра на 40 минут» — плохой
сценарий: интерфейс дёргается, вентиляторы воют, батарея садится, а на слабых
машинах обработка падает по памяти. Поэтому здесь в одном месте считаются:

  * число одновременных рендеров;
  * потоки, которые отдаём каждому ffmpeg;
  * потоки распознавания речи;
  * приоритет дочерних процессов (nice / BELOW_NORMAL).

Всё это выводится из одного числа — «мощность» 0..100 из настроек
(`power` в settings.json). Ползунок в интерфейсе меняет его, а значения ниже
пересчитываются на лету, без перезапуска.

Переменные окружения по-прежнему главнее настроек:
`VIRALCUT_POWER`, `VIRALCUT_RENDER_WORKERS`, `VIRALCUT_FFMPEG_THREADS`,
`VIRALCUT_WHISPER_THREADS`, `VIRALCUT_NICE`, `VIRALCUT_LOW_PRIORITY`.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from typing import Any, Dict, List, Optional

_POWER_CACHE: Dict[str, Any] = {"value": 50, "ts": 0.0}
_POWER_TTL = 1.0


def cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except Exception:
        return max(1, os.cpu_count() or 4)


def _env_int(name: str, default: int, low: int = 1, high: int = 64) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(low, min(high, int(raw)))
    except ValueError:
        return default


def invalidate() -> None:
    """Сбросить кэш мощности: вызывается после сохранения настроек."""
    _POWER_CACHE["ts"] = 0.0


def power() -> int:
    """Мощность 0..100: 0 — экономно, 100 — максимум (по умолчанию 50)."""
    now = time.monotonic()
    if now - float(_POWER_CACHE.get("ts") or 0.0) < _POWER_TTL:
        return int(_POWER_CACHE["value"])
    env = os.environ.get("VIRALCUT_POWER")
    if env is not None:
        try:
            value = max(0, min(100, int(env)))
            _POWER_CACHE.update({"value": value, "ts": now})
            return value
        except ValueError:
            pass
    value = 50
    try:
        from . import settings

        raw = settings.get().get("power", 50)
        value = max(0, min(100, int(raw)))
    except Exception:  # noqa: BLE001 — настройки недоступны: работаем по умолчанию
        value = 50
    _POWER_CACHE.update({"value": value, "ts": now})
    return value


def _level() -> float:
    return power() / 100.0


def cpu_share() -> float:
    """Доля машины, разрешённая жёстким лимитом процессора (0.25…1.0)."""
    env = os.environ.get("VIRALCUT_CPU_LIMIT")
    if env is not None:
        try:
            return max(0.25, min(1.0, int(env) / 100.0))
        except ValueError:
            return 1.0
    try:
        from . import settings

        return max(0.25, min(1.0, int(settings.get().get("cpu_limit", 100)) / 100.0))
    except Exception:  # noqa: BLE001
        return 1.0


def max_render_workers() -> int:
    """Сколько рендеров машина осилит на максимуме (не больше 4)."""
    return max(1, min(4, cpu_count() // 2))


def render_workers() -> int:
    """Сколько клипов рендерить параллельно при текущей мощности.

    Экономно — один рендер, максимум — до четырёх (на 8+ ядрах). Больше не
    нужно: память и диск кончаются раньше, а очередь всё равно длинная.
    """
    env = os.environ.get("VIRALCUT_RENDER_WORKERS")
    if env is not None:
        return _env_int("VIRALCUT_RENDER_WORKERS", 1, 1, 6)
    return max(1, 1 + int(_level() * (max_render_workers() - 1)))


def cpu_budget() -> int:
    """Сколько ядер суммарно разрешено занять ffmpeg-рендерам."""
    cpu = cpu_count()
    budget = cpu * (0.15 + 0.85 * _level()) * cpu_share()
    return max(1, int(round(budget)))


def ffmpeg_threads() -> int:
    """Потоки одному ffmpeg: его доля от выделенного бюджета ядер."""
    env = os.environ.get("VIRALCUT_FFMPEG_THREADS")
    if env is not None:
        return _env_int("VIRALCUT_FFMPEG_THREADS", 1, 1, 32)
    workers = render_workers()
    return max(1, min(32, cpu_budget() // max(1, workers)))


def nice_level() -> int:
    """Насколько «понизить» приоритет дочерних процессов (POSIX)."""
    env = os.environ.get("VIRALCUT_NICE")
    if env is not None:
        return _env_int("VIRALCUT_NICE", 10, 0, 19)
    return max(0, min(19, 19 - int(round(19 * _level()))))


def whisper_threads() -> int:
    """Распознавание: доля ядер, чтобы окно оставалось отзывчивым."""
    env = os.environ.get("VIRALCUT_WHISPER_THREADS")
    if env is not None:
        return _env_int("VIRALCUT_WHISPER_THREADS", 1, 1, 32)
    cpu = cpu_count()
    threads = cpu * (0.2 + 0.6 * _level()) * cpu_share()
    return max(1, min(32, int(round(threads))))


def low_priority() -> bool:
    return os.environ.get("VIRALCUT_LOW_PRIORITY", "1").strip().lower() not in (
        "0", "false", "no", "off")


def wrap_nice(cmd: List[str]) -> List[str]:
    """Запускает команду через `nice` (macOS/Linux), не трогая Popen.

    `preexec_fn` в многопоточном процессе небезопасен (fork без exec), а рендер
    идёт из потоков — поэтому приоритет ставим внешней утилитой.
    """
    if sys.platform == "win32" or not low_priority():
        return cmd
    level = nice_level()
    if level <= 0 or shutil.which("nice") is None:
        return cmd
    return ["nice", "-n", str(level)] + cmd


def spawn_kwargs() -> Dict[str, Any]:
    """Флаги запуска дочернего процесса: на Windows без окна консоли."""
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def subprocess_kwargs() -> Dict[str, Any]:
    """Аргументы Popen: без окна на Windows и с пониженным приоритетом."""
    import subprocess as _sp

    flags = 0
    if sys.platform == "win32":
        flags |= getattr(_sp, "CREATE_NO_WINDOW", 0)
        if low_priority():
            flags |= 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
    return {"creationflags": flags} if flags else {}


def summary() -> Dict[str, Any]:
    """Текущие лимиты — их показывает интерфейс и /api/health."""
    from . import cpu_limit as _cpu_limit

    return {
        "cpu": cpu_count(),
        "power": power(),
        "cpu_limit": _cpu_limit.limit(),
        "render_workers": render_workers(),
        "ffmpeg_threads": ffmpeg_threads(),
        "whisper_threads": whisper_threads(),
        "cpu_budget": cpu_budget(),
        "nice": nice_level() if low_priority() and sys.platform != "win32" else 0,
    }


class ConcurrencyGate:
    """Ограничитель одновременных задач, который можно менять на ходу.

    Пул потоков создаётся один раз (его размер не изменить), а фактическое
    число параллельных рендеров задаётся этим «шлагбаумом»: при снижении
    мощности новые задачи ждут, пока освободится место.
    """

    def __init__(self, limit_reader) -> None:
        self._reader = limit_reader
        self._condition = threading.Condition()
        self._active = 0

    def limit(self) -> int:
        try:
            return max(1, int(self._reader()))
        except Exception:  # noqa: BLE001
            return 1

    def __enter__(self) -> "ConcurrencyGate":
        with self._condition:
            while self._active >= self.limit():
                self._condition.wait(timeout=1.0)
            self._active += 1
        return self

    def __exit__(self, *_exc: Any) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()

    def stats(self) -> Dict[str, int]:
        with self._condition:
            return {"active": self._active, "limit": self.limit()}
