"""Жёсткий лимит на процессор: «не занимать больше N% машины».

Слайдер мощности задаёт, сколько рендеров запускать параллельно, но это не
обещает, что ffmpeg не займёт все ядра: один кодировщик с 8 потоками съедает
всё, что дали. Здесь — настоящий потолок.

Платформы разные, поэтому и механизмы разные:

  * **Windows** — Job Object с `JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP`:
    система сама держит процесс (и всех его детей) ниже доли, код опирается на
    планировщик, а не на наши замеры.
  * **macOS/Linux** — своего жёсткого API нет, поэтому работает «диспетчер»:
    раз в 200 мс считаем, сколько процессорного времени израсходовали дочерние
    процессы, и приостанавливаем их (SIGSTOP), когда норма выбрана, а потом
    продолжаем (SIGCONT). Это тот же приём, что у `cpulimit`, и он честно
    ограничивает ffmpeg/Node, не задевая интерфейс.
  * Собственный процесс (распознавание речи) идёт в потоках Python и
    ограничивается числом потоков — останавливать самих себя нельзя.

Лимит берётся из настроек (`cpu_limit`, 25…100) и может быть переопределён
переменной окружения `VIRALCUT_CPU_LIMIT`.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import signal
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_LIMIT = 100
MIN_LIMIT = 25
_SAMPLE_SECONDS = 0.2

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "applied": None,       # какой лимит уже применён
    "throttler": None,     # объект диспетчера (POSIX)
    "job": None,           # handle Job Object (Windows)
}


def _clamp(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(MIN_LIMIT, min(100, number))


def limit() -> int:
    """Текущий потолок в процентах от всей машины (100 — без ограничения)."""
    env = os.environ.get("VIRALCUT_CPU_LIMIT")
    if env is not None:
        return _clamp(env)
    try:
        from . import settings

        return _clamp(settings.get().get("cpu_limit", DEFAULT_LIMIT))
    except Exception:  # noqa: BLE001 — нет настроек: не ограничиваем
        return DEFAULT_LIMIT


def cores_for(percent: Optional[int] = None) -> int:
    """Сколько ядер «разрешено» при таком лимите (минимум одно)."""
    from . import resources

    total = resources.cpu_count()
    share = _clamp(percent if percent is not None else limit()) / 100.0
    return max(1, min(total, int(round(total * share))))


# --------------------------------------------------------------------------- #
# Windows: Job Object с жёстким потолком
# --------------------------------------------------------------------------- #

class _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ControlFlags", ctypes.c_uint32),
        ("CpuRate", ctypes.c_uint32),
        ("Weight", ctypes.c_uint32),
        ("MinRate", ctypes.c_uint32),
        ("MaxRate", ctypes.c_uint32),
    ]


_JOB_CPU_RATE_CONTROL = 15
_CPU_RATE_CONTROL_ENABLE = 0x1
_CPU_RATE_CONTROL_HARD_CAP = 0x4


def _windows_apply(percent: int) -> bool:
    """Ставит жёсткий потолок на процесс и всех его детей."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False
    try:
        if not _state.get("job"):
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return False
            _state["job"] = job
        info = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        info.ControlFlags = _CPU_RATE_CONTROL_ENABLE | _CPU_RATE_CONTROL_HARD_CAP
        # CpuRate — в сотых долях процента
        info.CpuRate = max(1, min(10000, percent * 100))
        ok = kernel32.SetInformationJobObject(
            _state["job"], _JOB_CPU_RATE_CONTROL, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            return False
        kernel32.AssignProcessToJobObject(_state["job"], kernel32.GetCurrentProcess())
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# macOS/Linux: диспетчер на SIGSTOP/SIGCONT
# --------------------------------------------------------------------------- #

def _descendants(root_pid: int) -> List[int]:
    """Все дочерние процессы (без самого root)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                             text=True, timeout=4).stdout
    except Exception:  # noqa: BLE001
        return []
    parent: Dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parent[pid] = ppid
    result: List[int] = []
    for pid in parent:
        cursor, depth = pid, 0
        while cursor in parent and depth < 32:
            cursor = parent[cursor]
            depth += 1
            if cursor == root_pid:
                result.append(pid)
                break
    return result


def _tree_cpu(root_pid: int) -> Dict[int, float]:
    """Потомки и их накопленное CPU-время за один вызов `ps`."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,time="], capture_output=True,
                             text=True, timeout=4).stdout
    except Exception:  # noqa: BLE001
        return {}
    parent: Dict[int, int] = {}
    times: Dict[int, float] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parent[pid] = ppid
        times[pid] = _parse_cpu_time(parts[2])
    result: Dict[int, float] = {}
    for pid in parent:
        cursor, depth = pid, 0
        while cursor in parent and depth < 32:
            cursor = parent[cursor]
            depth += 1
            if cursor == root_pid:
                result[pid] = times.get(pid, 0.0)
                break
    return result


def _cpu_times(pids: List[int]) -> Dict[int, float]:
    """Накопленное процессорное время каждого pid в секундах."""
    if not pids:
        return {}
    try:
        out = subprocess.run(["ps", "-o", "pid=,time=", "-p", ",".join(str(p) for p in pids)],
                             capture_output=True, text=True, timeout=4).stdout
    except Exception:  # noqa: BLE001
        return {}
    result: Dict[int, float] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        result[pid] = _parse_cpu_time(parts[1])
    return result


def _parse_cpu_time(raw: str) -> float:
    """`ps` отдаёт MM:SS.ss или HH:MM:SS.ss — переводим в секунды."""
    try:
        chunks = raw.split(":")
        seconds = float(chunks[-1])
        if len(chunks) >= 2:
            seconds += int(chunks[-2]) * 60
        if len(chunks) >= 3:
            seconds += int(chunks[-3]) * 3600
        return seconds
    except (ValueError, IndexError):
        return 0.0


class Throttler:
    """Держит суммарную загрузку дочерних процессов ниже нормы.

    Раз в 200 мс считаем, сколько CPU-времени съели дети за прошедший интервал.
    Если больше нормы — приостанавливаем их до конца интервала. Пауза и
    продолжение — обычные сигналы, ffmpeg переносит их спокойно.
    """

    def __init__(self, percent: int, pid: Optional[int] = None) -> None:
        self.percent = percent
        self.pid = pid or os.getpid()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stopped: List[int] = []
        self._last: Dict[int, float] = {}
        self._last_ts = time.monotonic()
        self._budget_cores = percent / 100.0 * _cpu_total()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="cpu-throttle", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._resume()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _resume(self) -> None:
        for pid in self._stopped:
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass
        self._stopped = []
        for pid in list(self._last):
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass

    def _loop(self) -> None:
        """Цикл «поработали / приостановили».

        За окно T измеряем, сколько CPU-времени съели дети. Если больше нормы,
        приостанавливаем их ровно на такую долю окна, чтобы средняя загрузка
        вышла на потолок. Ничего не делаем, если процессы и так мало грузят
        (например, ждут диск).
        """
        while not self._stop.is_set():
            self._resume()
            started = time.monotonic()
            if self._stop.wait(_SAMPLE_SECONDS):
                break
            span = max(0.02, time.monotonic() - started)
            times = _tree_cpu(self.pid)
            if not times:
                self._last = {}
                self._last_ts = time.monotonic()
                continue
            pids = list(times)
            used = 0.0
            for pid, total in times.items():
                previous = self._last.get(pid)
                if previous is not None and total >= previous:
                    used += total - previous
                self._last[pid] = total
            for pid in list(self._last):
                if pid not in times:
                    self._last.pop(pid, None)
            self._last_ts = time.monotonic()
            budget = self._budget_cores * span
            if used <= budget:
                continue
            excess = min(1.5, max(0.01, (used - budget) / used * span))
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGSTOP)
                except OSError:
                    continue
                self._stopped.append(pid)
            self._stop.wait(excess)
            self._resume()


def _cpu_total() -> int:
    try:
        from . import resources

        return resources.cpu_count()
    except Exception:  # noqa: BLE001
        return max(1, os.cpu_count() or 4)


# --------------------------------------------------------------------------- #
# Публичный интерфейс
# --------------------------------------------------------------------------- #

def apply(percent: Optional[int] = None) -> Optional[int]:
    """Включает лимит (или снимает его при 100). Возвращает применённый процент."""
    target = _clamp(percent if percent is not None else limit())
    with _lock:
        if _state["applied"] == target:
            return target

        # сначала останавливаем прежний механизм
        throttler = _state.get("throttler")
        if throttler is not None:
            throttler.stop()
            _state["throttler"] = None

        if sys.platform == "win32":
            if target >= 100:
                _state["applied"] = target
                return target
            if not _windows_apply(target):
                _state["applied"] = None
                return None
            _state["applied"] = target
            return target

        if target >= 100:
            _state["applied"] = target
            return target
        throttler = Throttler(target)
        throttler.start()
        _state["throttler"] = throttler
        _state["applied"] = target
        return target


def stop() -> None:
    with _lock:
        throttler = _state.get("throttler")
        if throttler is not None:
            throttler.stop()
            _state["throttler"] = None
        _state["applied"] = None


def status() -> Dict[str, Any]:
    """Что включено — показываем в /api/health и настройках."""
    return {
        "limit": limit(),
        "applied": _state.get("applied"),
        "mechanism": ("job_object" if sys.platform == "win32"
                      else "sigstop" if sys.platform == "darwin" else "sigstop"),
        "duty_cyclic": sys.platform != "win32",
    }
