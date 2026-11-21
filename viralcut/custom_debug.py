"""Live telemetry and hardware/process diagnostics for the debug inspector."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import MODELS_DIR, WHISPER_COMPUTE, WHISPER_DEVICE, WHISPER_LOCAL_DIR, WHISPER_MODEL
from .utils import subprocess_kwargs

_LOCK = threading.Lock()
_HW_LOCK = threading.Lock()
_WIN32_LOCK = threading.Lock()
_STARTED_AT = time.time()
_GPU_MISSING_UNTIL = 0.0
_IDLE_WHISPER_CANDIDATE: Optional[Tuple[str, str]] = None

_EXPECTED_MODEL_MB: Dict[str, int] = {
    "tiny": 75,
    "base": 145,
    "small": 465,
    "medium": 1460,
    "large-v3": 2945,
    "large-v3-turbo": 1540,
}

_WHISPER: Dict[str, Any] = {
    "model": WHISPER_MODEL,
    "device": WHISPER_DEVICE,
    "compute_type": WHISPER_COMPUTE,
    "status": "idle",
    "fallback_reason": None,
}

_DOWNLOAD: Dict[str, Any] = {
    "active": False,
    "kind": None,
    "client": None,
    "format_id": None,
    "source_height": None,
    "target_height": None,
    "speed_mbps": None,
    "pct": None,
    "eta_s": None,
    "fallback": False,
    "fallback_reason": None,
    "upscale_warning": False,
    "updated_at": None,
}

_CHILDREN: Dict[int, Dict[str, Any]] = {}
_STAGE_TRACK: Dict[str, Tuple[str, float]] = {}

_HW_CACHE: Dict[str, Any] = {"ts": 0.0, "gpu": None, "sys": None}
_CPU_PREV: Dict[str, Any] = {"ts": 0.0, "proc_s": None, "sys_idle": None, "sys_total": None}
_MODEL_DL_PREV: Dict[str, Any] = {"ts": 0.0, "bytes": 0, "path": "", "speed_mbps": 0.0}

# --- Typed Win32 API bindings -------------------------------------------------

_WIN32_READY = False
_K32 = None


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _init_win32() -> None:
    global _WIN32_READY, _K32
    if _WIN32_READY or not sys.platform.startswith("win"):
        return
    with _WIN32_LOCK:
        if _WIN32_READY:
            return
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.argtypes = []
            k32.GetCurrentProcess.restype = wt.HANDLE

            k32.K32GetProcessMemoryInfo.argtypes = [
                wt.HANDLE,
                ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
                wt.DWORD,
            ]
            k32.K32GetProcessMemoryInfo.restype = wt.BOOL

            k32.GetProcessTimes.argtypes = [
                wt.HANDLE,
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
            ]
            k32.GetProcessTimes.restype = wt.BOOL

            k32.GetSystemTimes.argtypes = [
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
            ]
            k32.GetSystemTimes.restype = wt.BOOL

            k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
            k32.GlobalMemoryStatusEx.restype = wt.BOOL

            k32.CreateFileW.argtypes = [
                wt.LPCWSTR,
                wt.DWORD,
                wt.DWORD,
                ctypes.c_void_p,
                wt.DWORD,
                wt.DWORD,
                wt.HANDLE,
            ]
            k32.CreateFileW.restype = wt.HANDLE

            k32.GetFileSizeEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_int64)]
            k32.GetFileSizeEx.restype = wt.BOOL

            k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            k32.OpenProcess.restype = wt.HANDLE

            k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
            k32.GetExitCodeProcess.restype = wt.BOOL

            k32.CloseHandle.argtypes = [wt.HANDLE]
            k32.CloseHandle.restype = wt.BOOL

            _K32 = k32
            _WIN32_READY = True
        except Exception:
            _K32 = None
            _WIN32_READY = False


def _ft_to_sec(ft: wt.FILETIME) -> float:
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) * 1e-7


def _live_file_size(path: Path) -> int:
    """Read real file size even while another handle writes to it on Windows."""
    _init_win32()
    if _K32 is not None:
        invalid = ctypes.c_void_p(-1).value
        handle = _K32.CreateFileW(str(path), 0x80000000, 0x07, None, 3, 0x80, None)
        if handle and handle != invalid:
            try:
                size = ctypes.c_int64(0)
                if _K32.GetFileSizeEx(handle, ctypes.byref(size)):
                    return max(0, int(size.value))
            finally:
                _K32.CloseHandle(handle)
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    _init_win32()
    if _K32 is not None:
        handle = _K32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = wt.DWORD(0)
            if _K32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return False
        finally:
            _K32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_raw_cpu_ram() -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return (proc_cpu_s, proc_ram_mb, sys_idle_s, sys_total_s, sys_ram_used_gb, sys_ram_total_gb)."""
    _init_win32()
    if _K32 is not None:
        proc_cpu_s = None
        proc_ram_mb = None
        sys_idle_s = None
        sys_total_s = None
        ram_used_gb = None
        ram_total_gb = None

        h = _K32.GetCurrentProcess()
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if _K32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            proc_ram_mb = round(pmc.WorkingSetSize / 1048576.0, 1)

        ft_c, ft_e, ft_k, ft_u = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        if _K32.GetProcessTimes(h, ctypes.byref(ft_c), ctypes.byref(ft_e), ctypes.byref(ft_k), ctypes.byref(ft_u)):
            proc_cpu_s = _ft_to_sec(ft_k) + _ft_to_sec(ft_u)

        ft_idle, ft_sk, ft_su = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        if _K32.GetSystemTimes(ctypes.byref(ft_idle), ctypes.byref(ft_sk), ctypes.byref(ft_su)):
            sys_idle_s = _ft_to_sec(ft_idle)
            sys_total_s = _ft_to_sec(ft_sk) + _ft_to_sec(ft_su)

        ms = _MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(ms)
        if _K32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            total_b = float(ms.ullTotalPhys or 0)
            avail_b = float(ms.ullAvailPhys or 0)
            if total_b > 0:
                ram_total_gb = round(total_b / (1024 ** 3), 1)
                ram_used_gb = round((total_b - avail_b) / (1024 ** 3), 1)
        return proc_cpu_s, proc_ram_mb, sys_idle_s, sys_total_s, ram_used_gb, ram_total_gb

    proc_cpu_s = None
    proc_ram_mb = None
    try:
        t = os.times()
        proc_cpu_s = float(t.user + t.system)
    except Exception:
        pass
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        proc_ram_mb = round(rss / (1024 ** 2 if sys.platform == "darwin" else 1024.0), 1)
    except Exception:
        pass
    return proc_cpu_s, proc_ram_mb, None, None, None, None


def _prime_cpu_baseline() -> None:
    try:
        proc_s, _, sys_idle, sys_total, _, _ = _read_raw_cpu_ram()
        _CPU_PREV.update({
            "ts": time.monotonic(),
            "proc_s": proc_s,
            "sys_idle": sys_idle,
            "sys_total": sys_total,
        })
    except Exception:
        pass


_prime_cpu_baseline()


# --- Public recording hooks ---------------------------------------------------

def record_whisper(
    *,
    model: Optional[str] = None,
    device: Optional[str] = None,
    compute_type: Optional[str] = None,
    status: Optional[str] = None,
    fallback_reason: Optional[str] = None,
) -> None:
    with _LOCK:
        if model is not None:
            _WHISPER["model"] = model
        if device is not None:
            _WHISPER["device"] = device
        if compute_type is not None:
            _WHISPER["compute_type"] = compute_type
        if status is not None:
            _WHISPER["status"] = status
        if fallback_reason is not None:
            _WHISPER["fallback_reason"] = fallback_reason or None


def record_download(
    *,
    active: Optional[bool] = None,
    kind: Optional[str] = None,
    client: Optional[str] = None,
    format_id: Optional[str] = None,
    source_height: Optional[int] = None,
    target_height: Optional[int] = None,
    speed_mbps: Optional[float] = None,
    pct: Optional[float] = None,
    eta_s: Optional[float] = None,
    fallback: Optional[bool] = None,
    fallback_reason: Optional[str] = None,
) -> None:
    with _LOCK:
        if active is not None:
            _DOWNLOAD["active"] = bool(active)
        if kind is not None:
            _DOWNLOAD["kind"] = kind
        if client is not None:
            _DOWNLOAD["client"] = client
        if format_id is not None:
            _DOWNLOAD["format_id"] = format_id
        if source_height is not None:
            _DOWNLOAD["source_height"] = int(source_height) if source_height else None
        if target_height is not None:
            _DOWNLOAD["target_height"] = int(target_height) if target_height else None
        if speed_mbps is not None:
            _DOWNLOAD["speed_mbps"] = round(float(speed_mbps), 2)
        if pct is not None:
            _DOWNLOAD["pct"] = round(float(pct), 1)
        if eta_s is not None:
            _DOWNLOAD["eta_s"] = int(eta_s) if eta_s >= 0 else None
        if fallback is not None:
            _DOWNLOAD["fallback"] = bool(fallback)
        if fallback_reason is not None:
            _DOWNLOAD["fallback_reason"] = fallback_reason or None

        src_h = _DOWNLOAD.get("source_height")
        tgt_h = _DOWNLOAD.get("target_height")
        _DOWNLOAD["upscale_warning"] = bool(src_h and tgt_h and src_h < tgt_h)
        _DOWNLOAD["updated_at"] = round(time.time(), 2)


def register_process(
    pid: int,
    role: str,
    *,
    label: str = "",
    encoder: str = "",
    source_res: str = "",
    target_res: str = "",
) -> None:
    if not pid:
        return
    with _LOCK:
        _CHILDREN[int(pid)] = {
            "pid": int(pid),
            "role": role,
            "label": label,
            "encoder": encoder,
            "source_res": source_res,
            "target_res": target_res,
            "progress": 0.0,
            "started_at": time.time(),
        }


def update_process(
    pid: int,
    *,
    progress: Optional[float] = None,
    encoder: Optional[str] = None,
) -> None:
    if not pid:
        return
    with _LOCK:
        entry = _CHILDREN.get(int(pid))
        if not entry:
            return
        if progress is not None:
            entry["progress"] = round(max(0.0, min(1.0, float(progress))), 3)
        if encoder is not None:
            entry["encoder"] = encoder


def unregister_process(pid: int) -> None:
    if not pid:
        return
    with _LOCK:
        _CHILDREN.pop(int(pid), None)


# --- Live hardware & cache samplers -------------------------------------------

def _sample_cpu_mem() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    cpu_count = os.cpu_count() or 1
    proc_s, proc_ram_mb, sys_idle, sys_total, sys_ram_used, sys_ram_total = _read_raw_cpu_ram()
    now = time.monotonic()

    proc_pct = None
    sys_pct = None
    prev_ts = float(_CPU_PREV.get("ts") or 0.0)
    dt = now - prev_ts
    if dt >= 0.15:
        prev_proc = _CPU_PREV.get("proc_s")
        if proc_s is not None and prev_proc is not None:
            proc_pct = round(max(0.0, min(100.0, (proc_s - prev_proc) / (dt * cpu_count) * 100.0)), 1)
        prev_idle = _CPU_PREV.get("sys_idle")
        prev_total = _CPU_PREV.get("sys_total")
        if sys_idle is not None and sys_total is not None and prev_idle is not None and prev_total is not None:
            d_total = sys_total - prev_total
            d_idle = sys_idle - prev_idle
            if d_total > 0:
                sys_pct = round(max(0.0, min(100.0, (d_total - d_idle) / d_total * 100.0)), 1)
        _CPU_PREV.update({
            "ts": now,
            "proc_s": proc_s,
            "sys_idle": sys_idle,
            "sys_total": sys_total,
        })

    return (
        {"cpu_pct": proc_pct, "ram_mb": proc_ram_mb},
        {
            "cpu_pct": sys_pct,
            "cpu_count": cpu_count,
            "ram_used_gb": sys_ram_used,
            "ram_total_gb": sys_ram_total,
        },
    )


def _parse_float(raw: str) -> Optional[float]:
    val = (raw or "").strip()
    if not val or "N/A" in val.upper():
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _sample_gpu(tracked_pids: List[int]) -> Dict[str, Any]:
    global _GPU_MISSING_UNTIL
    empty = {
        "available": False,
        "name": None,
        "util_pct": None,
        "vram_used_mb": None,
        "vram_total_mb": None,
        "temp_c": None,
        "process_on_gpu": False,
    }
    if time.monotonic() < _GPU_MISSING_UNTIL:
        return empty
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            errors="replace",
            **subprocess_kwargs(),
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return empty
        line = proc.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            return empty
        temp_val = _parse_float(parts[4])
        gpu = {
            "available": True,
            "name": parts[0] or "NVIDIA GPU",
            "util_pct": _parse_float(parts[1]),
            "vram_used_mb": _parse_float(parts[2]),
            "vram_total_mb": _parse_float(parts[3]),
            "temp_c": round(temp_val) if temp_val is not None else None,
            "process_on_gpu": False,
        }
        apps = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=2.0,
            errors="replace",
            **subprocess_kwargs(),
        )
        if apps.returncode == 0 and apps.stdout:
            gpu_pids = set()
            for row in apps.stdout.strip().splitlines():
                head = row.split(",", 1)[0].strip()
                if head.isdigit():
                    gpu_pids.add(int(head))
            watch_pids = {os.getpid(), *(int(p) for p in tracked_pids)}
            gpu["process_on_gpu"] = bool(watch_pids & gpu_pids)
        return gpu
    except FileNotFoundError:
        _GPU_MISSING_UNTIL = time.monotonic() + 60.0
        return empty
    except Exception:
        return empty


def _sample_hw_cached(tracked_pids: List[int]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    with _HW_LOCK:
        now = time.monotonic()
        if now - float(_HW_CACHE.get("ts") or 0.0) < 1.0 and _HW_CACHE.get("gpu") is not None:
            srv, sys_info = _HW_CACHE["sys"]
            return dict(srv), dict(sys_info), dict(_HW_CACHE["gpu"])
        srv, sys_info = _sample_cpu_mem()
        gpu = _sample_gpu(tracked_pids)
        _HW_CACHE["ts"] = now
        _HW_CACHE["sys"] = (srv, sys_info)
        _HW_CACHE["gpu"] = gpu
        return dict(srv), dict(sys_info), dict(gpu)


def _safe_mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def _resolved_idle_whisper() -> Tuple[str, str]:
    global _IDLE_WHISPER_CANDIDATE
    if _IDLE_WHISPER_CANDIDATE is not None:
        return _IDLE_WHISPER_CANDIDATE
    try:
        from .custom_whisper import whisper_candidates

        cands = whisper_candidates(False, WHISPER_DEVICE, WHISPER_COMPUTE)
        pair = cands[0] if cands else (WHISPER_DEVICE, WHISPER_COMPUTE)
    except Exception:
        pair = (WHISPER_DEVICE, WHISPER_COMPUTE)
    _IDLE_WHISPER_CANDIDATE = pair
    return pair


def _inspect_model_cache(model_name: str) -> Dict[str, Any]:
    expected_mb = _EXPECTED_MODEL_MB.get(model_name)
    if WHISPER_LOCAL_DIR:
        local_bin = Path(WHISPER_LOCAL_DIR) / "model.bin"
        try:
            if local_bin.is_file():
                return {
                    "cached": True,
                    "cache_mb": round(local_bin.stat().st_size / 1048576.0, 1),
                    "expected_mb": expected_mb,
                    "downloading": False,
                    "download_speed_mbps": 0.0,
                }
        except OSError:
            pass

    model_dir = MODELS_DIR / f"models--Systran--faster-whisper-{model_name}"
    if model_dir.is_dir():
        for snap in model_dir.glob("snapshots/*/model.bin"):
            try:
                sz = snap.stat().st_size
            except OSError:
                sz = 0
            if sz > 1024 * 1024:
                return {
                    "cached": True,
                    "cache_mb": round(sz / 1048576.0, 1),
                    "expected_mb": expected_mb,
                    "downloading": False,
                    "download_speed_mbps": 0.0,
                }

        blobs_dir = model_dir / "blobs"
        if blobs_dir.is_dir():
            incompletes = sorted(blobs_dir.glob("*.incomplete"), key=_safe_mtime, reverse=True)
            if incompletes:
                target = incompletes[0]
                size_bytes = _live_file_size(target)
                now = time.monotonic()
                with _LOCK:
                    speed = float(_MODEL_DL_PREV.get("speed_mbps") or 0.0)
                    if _MODEL_DL_PREV.get("path") == str(target):
                        dt = now - float(_MODEL_DL_PREV.get("ts") or 0.0)
                        if dt >= 0.25:
                            delta = max(0, size_bytes - int(_MODEL_DL_PREV.get("bytes") or 0))
                            speed = round(delta / dt / 1048576.0, 2)
                            _MODEL_DL_PREV.update({
                                "ts": now,
                                "bytes": size_bytes,
                                "speed_mbps": speed,
                            })
                    else:
                        _MODEL_DL_PREV.update({
                            "ts": now,
                            "bytes": size_bytes,
                            "path": str(target),
                            "speed_mbps": 0.0,
                        })
                        speed = 0.0
                return {
                    "cached": False,
                    "cache_mb": round(size_bytes / 1048576.0, 1),
                    "expected_mb": expected_mb,
                    "downloading": True,
                    "download_speed_mbps": speed,
                }

    return {
        "cached": False,
        "cache_mb": 0.0,
        "expected_mb": expected_mb,
        "downloading": False,
        "download_speed_mbps": 0.0,
    }


def _collect_active_projects() -> List[Dict[str, Any]]:
    from . import db, jobs

    active_ids = list(jobs.active_project_ids())
    seen = set(active_ids)
    projects: List[Dict[str, Any]] = []

    for pid in active_ids:
        proj = db.get_project(pid)
        if proj:
            projects.append(proj)

    for item in db.list_projects(12):
        if item["id"] not in seen and item.get("status") in ("queued", "preparing", "running"):
            seen.add(item["id"])
            proj = db.get_project(item["id"]) or item
            projects.append(proj)

    now = time.time()
    out: List[Dict[str, Any]] = []
    with _LOCK:
        active_set = {p["id"] for p in projects}
        for stale_id in [k for k in _STAGE_TRACK if k not in active_set]:
            _STAGE_TRACK.pop(stale_id, None)

        for p in projects:
            pid = p["id"]
            stage = p.get("stage") or ""
            prev = _STAGE_TRACK.get(pid)
            if prev is None or prev[0] != stage:
                _STAGE_TRACK[pid] = (stage, now)
                elapsed_s = 0.0
            else:
                elapsed_s = round(now - prev[1], 1)

            meta = p.get("meta") or {}
            out.append({
                "id": pid,
                "title": p.get("title") or p.get("url") or pid,
                "status": p.get("status"),
                "stage": stage,
                "progress": p.get("progress") or 0,
                "elapsed_s": elapsed_s,
                "meta": {
                    "source_height": meta.get("source_height") or meta.get("height"),
                    "segments_mode": bool(meta.get("segments_mode")),
                    "engine": meta.get("engine"),
                    "fallback": bool(meta.get("fallback")),
                },
            })
    return out


def snapshot() -> Dict[str, Any]:
    """Return a JSON-serializable live status snapshot of the ViralCut server."""
    from . import pipeline, render

    now = time.time()
    with _LOCK:
        for dead_pid in [pid for pid in list(_CHILDREN) if not _pid_alive(pid)]:
            _CHILDREN.pop(dead_pid, None)
        children = [
            {
                **info,
                "elapsed_s": round(max(0.0, now - float(info.get("started_at") or now)), 1),
            }
            for info in _CHILDREN.values()
        ]
        whisper_state = dict(_WHISPER)
        download_state = dict(_DOWNLOAD)

    tracked_pids = [int(c["pid"]) for c in children]
    srv_hw, sys_hw, gpu_hw = _sample_hw_cached(tracked_pids)

    try:
        render_workers = pipeline._render_workers()
    except Exception:
        render_workers = 1
    try:
        hw_enc = render.hw_encoder()
    except Exception:
        hw_enc = None

    cache_info = _inspect_model_cache(str(whisper_state.get("model") or WHISPER_MODEL))
    if cache_info.get("downloading") and whisper_state.get("status") in ("idle", "loading"):
        whisper_state["status"] = "downloading"
    if whisper_state.get("status") == "idle" and whisper_state.get("device") == "auto":
        dev, comp = _resolved_idle_whisper()
        whisper_state["device"] = dev
        whisper_state["compute_type"] = comp

    active_projects = _collect_active_projects()

    try:
        from . import custom_cleanup

        storage = custom_cleanup.storage_summary()
    except Exception:
        storage = None

    return {
        "ts": round(now, 3),
        "server": {
            "pid": os.getpid(),
            "uptime_s": round(now - _STARTED_AT, 1),
            "cpu_pct": srv_hw.get("cpu_pct"),
            "ram_mb": srv_hw.get("ram_mb"),
            "render_workers": render_workers,
            "hw_encoder": hw_enc,
        },
        "system": sys_hw,
        "gpu": gpu_hw,
        "whisper": {**whisper_state, **cache_info},
        "download": download_state,
        "active_projects": active_projects,
        "children": children,
        "storage": storage,
    }
