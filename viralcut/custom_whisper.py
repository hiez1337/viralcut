"""Custom Whisper configuration, model catalog/download manager, and CUDA diagnostics."""
from __future__ import annotations

import json
import os
import sys
import sysconfig
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_WHISPER_MODEL = "small"

SELECTABLE_MODEL_IDS: Tuple[str, ...] = ("small", "medium", "large-v3")

MODEL_ALIASES: Dict[str, str] = {
    "large": "large-v3",
}

REPO_IDS: Dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base": "Systran/faster-whisper-base",
    "base.en": "Systran/faster-whisper-base.en",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

EXPECTED_MODEL_MB: Dict[str, int] = {
    "tiny": 75,
    "tiny.en": 75,
    "base": 145,
    "base.en": 145,
    "small": 465,
    "small.en": 465,
    "medium": 1460,
    "medium.en": 1460,
    "large-v1": 2945,
    "large-v2": 2945,
    "large-v3": 2945,
    "large-v3-turbo": 1540,
}

WHISPER_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "small",
        "label": "Small",
        "repo_id": "Systran/faster-whisper-small",
        "size_mb": 465,
        "vram_hint": "~1–2 ГБ VRAM / RAM",
        "speed_hint": "Быстрая",
        "quality_hint": "Базовая точность · подходит для CPU и GPU",
        "badge": "По умолчанию",
        "gpu_recommended": False,
    },
    {
        "id": "medium",
        "label": "Medium",
        "repo_id": "Systran/faster-whisper-medium",
        "size_mb": 1460,
        "vram_hint": "~3–4 ГБ VRAM / RAM",
        "speed_hint": "Средняя",
        "quality_hint": "Высокая точность · лучше держит русскую речь и термины",
        "badge": "Баланс",
        "gpu_recommended": True,
    },
    {
        "id": "large-v3",
        "label": "Large v3",
        "repo_id": "Systran/faster-whisper-large-v3",
        "size_mb": 2945,
        "vram_hint": "~5–6 ГБ VRAM",
        "speed_hint": "Медленнее",
        "quality_hint": "Максимум точности · рекомендуется NVIDIA GPU (CUDA)",
        "badge": "Максимум",
        "gpu_recommended": True,
    },
]

ALLOW_PATTERNS: List[str] = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]

_STATE_LOCK = threading.Lock()
_DL_LOCK = threading.Lock()
_CUDA_LOCK = threading.Lock()

_CUDA_DLL_HANDLES: List[object] = []
_CUDA_REGISTERED = False
_CUDA_DLLS_OK: Optional[bool] = None

_RUNTIME_MODEL: Optional[str] = None
_RUNTIME_LANGUAGE: Optional[str] = None
DEFAULT_WHISPER_LANGUAGE = "auto"
SUPPORTED_LANGUAGES = [
    {"id": "auto", "label": "Авто", "desc": "Умное определение языка речи"},
    {"id": "ru", "label": "Русский (RU)", "desc": "Русская дорожка и субтитры"},
    {"id": "en", "label": "English (EN)", "desc": "Английская дорожка и субтитры"},
    {"id": "orig", "label": "Оригинал", "desc": "Оригинальная дорожка без автодубляжа"},
]
VALID_LANGUAGE_IDS = {"auto", "ru", "en", "orig"}

DEFAULT_OPTIMIZATION_SETTINGS: Dict[str, Any] = {
    "preset": "auto",
    "device": "auto",
    "compute_type": "auto",
    "cpu_threads": 0,
    "beam_size": 2,
    "batch_size": 16,
    "recheck_limit": 4,
}

VALID_DEVICES = {"auto", "cuda", "cpu"}
VALID_COMPUTE_TYPES = {"auto", "float16", "int8_float16", "int8", "int8_float32", "float32"}

OPTIMIZATION_PRESETS: List[Dict[str, Any]] = [
    {
        "id": "auto",
        "label": "🎯 Авто (Рекомендуемый)",
        "desc": "Умный выбор под ваше железо: FP16 на GPU или INT8 на всех физических ядрах CPU",
        "badge": "Авто",
        "target": "auto",
        "settings": {
            "device": "auto",
            "compute_type": "auto",
            "cpu_threads": 0,
            "beam_size": 2,
            "batch_size": 16,
            "recheck_limit": 4,
        },
    },
    {
        "id": "ultra_fast_gpu",
        "label": "⚡ Ультра-скорость (GPU)",
        "desc": "Максимальный FPS: float16, Beam 1 (жадный), Batch 32, без перепроверки (~5 сек на 12 мин)",
        "badge": "Экстрим",
        "target": "gpu",
        "settings": {
            "device": "cuda",
            "compute_type": "float16",
            "beam_size": 1,
            "batch_size": 32,
            "recheck_limit": 0,
        },
    },
    {
        "id": "balanced_gpu",
        "label": "🎯 Баланс скорости и качества (GPU)",
        "desc": "Идеально для NVIDIA RTX: float16, Beam 2, Batch 16, Recheck 4 (~9.9 сек на 12 мин)",
        "badge": "Для RTX",
        "target": "gpu",
        "settings": {
            "device": "cuda",
            "compute_type": "float16",
            "beam_size": 2,
            "batch_size": 16,
            "recheck_limit": 4,
        },
    },
    {
        "id": "quality_gpu",
        "label": "🔬 Максимум точности (GPU)",
        "desc": "Студийное качество: float16, Beam 5 (все 5 лучей), Batch 8, Recheck 8",
        "badge": "Качество",
        "target": "gpu",
        "settings": {
            "device": "cuda",
            "compute_type": "float16",
            "beam_size": 5,
            "batch_size": 8,
            "recheck_limit": 8,
        },
    },
    {
        "id": "optimized_cpu",
        "label": "🚀 Оптимальный CPU (Многопоточный)",
        "desc": "В 1.8× быстрее базового: int8, Beam 2, Batch 16, Recheck 4, все физические ядра",
        "badge": "Для CPU",
        "target": "cpu",
        "settings": {
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": 0,
            "beam_size": 2,
            "batch_size": 16,
            "recheck_limit": 4,
        },
    },
    {
        "id": "eco_cpu",
        "label": "🔋 Энергосберегающий (CPU 4 ядра)",
        "desc": "Минимум нагрузки: int8, Beam 1, Batch 8, 4 потока (для слабых процессоров)",
        "badge": "Эко",
        "target": "cpu",
        "settings": {
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": 4,
            "beam_size": 1,
            "batch_size": 8,
            "recheck_limit": 0,
        },
    },
]

_RUNTIME_SETTINGS: Optional[Dict[str, Any]] = None
_CPU_HARDWARE_CACHE: Optional[Dict[str, Any]] = None
_DOWNLOADS: Dict[str, Dict[str, Any]] = {}


def _config_module():
    return sys.modules.get(f"{__package__}.config") if __package__ else None


def _data_dir() -> Path:
    cfg = _config_module()
    if cfg is not None and hasattr(cfg, "DATA_DIR"):
        return Path(cfg.DATA_DIR)
    env = os.environ.get("VIRALCUT_DATA")
    if env:
        return Path(env).resolve()
    return (Path(__file__).resolve().parent.parent / "data").resolve()


def _models_dir() -> Path:
    cfg = _config_module()
    if cfg is not None and hasattr(cfg, "MODELS_DIR"):
        return Path(cfg.MODELS_DIR)
    return _data_dir() / "models"


def _whisper_local_dir() -> str:
    env = os.environ.get("VIRALCUT_WHISPER_DIR")
    if env:
        return env
    cfg = _config_module()
    if cfg is not None and hasattr(cfg, "WHISPER_LOCAL_DIR"):
        return str(cfg.WHISPER_LOCAL_DIR or "")
    return ""


def _state_file() -> Path:
    return _data_dir() / "whisper.json"


def normalize_model_name(name: Optional[str]) -> str:
    """Normalize Whisper model name and apply aliases."""
    raw = str(name or "").strip().lower()
    if not raw:
        return DEFAULT_WHISPER_MODEL
    return MODEL_ALIASES.get(raw, raw)


def model_repo_id(model_name: str) -> str:
    """Return HuggingFace repository ID for a Whisper model name."""
    norm = normalize_model_name(model_name)
    if norm in REPO_IDS:
        return REPO_IDS[norm]
    if "/" in str(model_name):
        return str(model_name).strip()
    return f"Systran/faster-whisper-{norm}"


def expected_size_mb(model_name: str) -> Optional[int]:
    """Return approximate weight size in MB for a known Whisper model."""
    return EXPECTED_MODEL_MB.get(normalize_model_name(model_name))


def _load_saved_model() -> Optional[str]:
    path = _state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            val = normalize_model_name(data.get("model"))
            if data.get("model") and val in REPO_IDS:
                return val
    except (OSError, ValueError):
        pass
    return None


def _load_saved_language() -> Optional[str]:
    path = _state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            val = str(data.get("language") or "").strip().lower()
            if val in VALID_LANGUAGE_IDS:
                return val
    except (OSError, ValueError):
        pass
    return None


def _save_model_choice(model_name: str) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = {}
    if path.is_file():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cur, dict):
                cur = {}
        except Exception:
            cur = {}
    cur["model"] = model_name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_active_language() -> str:
    """Return active language mode: auto, ru, en, orig."""
    global _RUNTIME_LANGUAGE
    if _RUNTIME_LANGUAGE:
        return _RUNTIME_LANGUAGE
    saved = _load_saved_language()
    return saved or DEFAULT_WHISPER_LANGUAGE


def set_active_language(language: str) -> Dict[str, Any]:
    """Validate, persist, and activate one of the supported language modes."""
    global _RUNTIME_LANGUAGE
    raw = str(language or "").strip().lower()
    if raw not in VALID_LANGUAGE_IDS:
        raise ValueError(f"Язык '{language}' не поддерживается. Доступны: auto, ru, en, orig.")
    with _STATE_LOCK:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        cur = {}
        if path.is_file():
            try:
                cur = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(cur, dict):
                    cur = {}
            except Exception:
                cur = {}
        cur["language"] = raw
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        _RUNTIME_LANGUAGE = raw
    return models_payload()


def resolve_whisper_model() -> str:
    """Return configured Whisper model: env override -> saved choice -> small."""
    env = os.environ.get("VIRALCUT_WHISPER")
    if env is not None and env.strip():
        return normalize_model_name(env)
    saved = _load_saved_model()
    if saved:
        return saved
    return DEFAULT_WHISPER_MODEL


def get_active_model() -> str:
    """Return currently active Whisper model (runtime UI selection or resolved default)."""
    if _RUNTIME_MODEL:
        return _RUNTIME_MODEL
    return resolve_whisper_model()


def resolve_model_ref(model_name: str, local_dir: str = "") -> str:
    """Resolve local bundled directory only when it matches the requested model."""
    norm = normalize_model_name(model_name)
    raw_dir = (local_dir or "").strip()
    if raw_dir:
        path = Path(raw_dir)
        try:
            if (path / "model.bin").is_file():
                dir_name = path.name.lower()
                if dir_name == f"whisper-{norm}" or not dir_name.startswith("whisper-"):
                    return str(path)
        except OSError:
            pass
    return norm


def _find_cached_model_bin(model_name: str) -> Optional[Path]:
    norm = normalize_model_name(model_name)
    local_ref = resolve_model_ref(norm, _whisper_local_dir())
    if local_ref != norm:
        candidate = Path(local_ref) / "model.bin"
        try:
            if candidate.is_file() and candidate.stat().st_size > 1024 * 1024:
                return candidate
        except OSError:
            pass

    repo_slug = model_repo_id(norm).replace("/", "--")
    model_dir = _models_dir() / f"models--{repo_slug}"
    if not model_dir.is_dir():
        return None
    for snap in model_dir.glob("snapshots/*/model.bin"):
        try:
            if snap.is_file() and snap.stat().st_size > 1024 * 1024:
                return snap
        except OSError:
            continue
    return None


def model_cache_info(model_name: str) -> Dict[str, Any]:
    """Inspect whether a Whisper model is already cached on disk and its size."""
    norm = normalize_model_name(model_name)
    exp_mb = expected_size_mb(norm)
    model_bin = _find_cached_model_bin(norm)
    if model_bin is not None:
        try:
            size_mb = round(model_bin.stat().st_size / 1048576.0, 1)
        except OSError:
            size_mb = float(exp_mb or 0)
        return {
            "cached": True,
            "cache_mb": size_mb,
            "expected_mb": exp_mb,
        }
    return {
        "cached": False,
        "cache_mb": 0.0,
        "expected_mb": exp_mb,
    }


def is_model_cached(model_name: Optional[str] = None) -> bool:
    """Return True if the specified (or active) Whisper model is locally cached."""
    target = normalize_model_name(model_name) if model_name else get_active_model()
    return bool(_find_cached_model_bin(target) is not None)


def _add_dll_dir(directory: Path, seen: set[str], path_parts: List[str]) -> None:
    try:
        resolved = str(directory.resolve())
    except OSError:
        return
    if resolved in seen or not directory.is_dir():
        return
    seen.add(resolved)
    if hasattr(os, "add_dll_directory"):
        try:
            _CUDA_DLL_HANDLES.append(os.add_dll_directory(resolved))
        except OSError:
            pass
    if resolved not in path_parts:
        path_parts.insert(0, resolved)


def register_cuda_dlls() -> None:
    """Register pip/bundled/system NVIDIA CUDA and cuDNN DLL directories on Windows."""
    global _CUDA_REGISTERED
    if _CUDA_REGISTERED or not sys.platform.startswith("win"):
        return
    with _CUDA_LOCK:
        if _CUDA_REGISTERED:
            return
        _CUDA_REGISTERED = True

        roots: List[Path] = []
        if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
            roots.append(Path(str(sys._MEIPASS)))
        for key in ("platlib", "purelib"):
            path = sysconfig.get_paths().get(key)
            if path:
                roots.append(Path(path))
        roots.append(Path(sys.prefix) / "Lib" / "site-packages")

        seen: set[str] = set()
        current_path = os.environ.get("PATH", "")
        path_parts = current_path.split(os.pathsep) if current_path else []

        for root in roots:
            nvidia_dir = root / "nvidia"
            if not nvidia_dir.is_dir():
                continue
            for bin_dir in sorted(nvidia_dir.glob("*/bin")):
                _add_dll_dir(bin_dir, seen, path_parts)

        cuda_env_dirs: List[Path] = []
        for env_key, env_val in os.environ.items():
            if not env_val:
                continue
            if env_key in ("CUDA_PATH", "CUDNN_PATH") or env_key.startswith("CUDA_PATH_V12"):
                cuda_env_dirs.append(Path(env_val) / "bin")
                cuda_env_dirs.append(Path(env_val))
        for bin_dir in cuda_env_dirs:
            _add_dll_dir(bin_dir, seen, path_parts)

        if path_parts:
            os.environ["PATH"] = os.pathsep.join(path_parts)


def _ct2_cuda_device_count() -> int:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def _cuda_dlls_available() -> bool:
    """Check whether required cuBLAS 12 and cuDNN 8/9 DLLs can be loaded on Windows."""
    global _CUDA_DLLS_OK
    if not sys.platform.startswith("win"):
        return True
    if _CUDA_DLLS_OK is not None:
        return _CUDA_DLLS_OK
    register_cuda_dlls()
    import ctypes

    ok = False
    try:
        ctypes.WinDLL("cublas64_12.dll")
        for cudnn_name in ("cudnn_ops64_9.dll", "cudnn_ops_infer64_8.dll"):
            try:
                ctypes.WinDLL(cudnn_name)
                ok = True
                break
            except OSError:
                continue
    except OSError:
        ok = False
    _CUDA_DLLS_OK = ok
    return ok


def _has_cuda_gpu() -> bool:
    return _ct2_cuda_device_count() > 0 and _cuda_dlls_available()


def cuda_diagnostics() -> Dict[str, Any]:
    """Return runtime GPU/CUDA readiness and human-readable fallback note when on CPU."""
    if sys.platform == "darwin":
        return {
            "gpu_detected": False,
            "dlls_available": True,
            "cuda_ready": False,
            "device_label": "MPS / CPU",
            "reason": "",
        }
    register_cuda_dlls()
    gpu_detected = _ct2_cuda_device_count() > 0
    dlls_ok = _cuda_dlls_available() if gpu_detected else False
    cuda_ready = gpu_detected and dlls_ok
    reason = ""
    if gpu_detected and not dlls_ok:
        reason = (
            "Найдена NVIDIA GPU, но в этой сборке нет CUDA/cuDNN DLL — "
            "Whisper работает на CPU (рекомендуется Small). "
            "Для ускорения на GPU установите CUDA 12 + cuDNN 9 или запускайте через run_windows.bat."
        )
    return {
        "gpu_detected": gpu_detected,
        "dlls_available": dlls_ok,
        "cuda_ready": cuda_ready,
        "device_label": "CUDA (GPU)" if cuda_ready else "CPU",
        "reason": reason,
    }


def get_physical_cpu_cores() -> int:
    """Return estimated physical CPU cores (not logical threads) for optimal AVX2 GEMM."""
    logical = os.cpu_count() or 4
    if logical <= 2:
        return logical
    return max(1, logical // 2)


def get_system_hardware_info() -> Dict[str, Any]:
    """Inspect and cache host CPU/GPU hardware details."""
    global _CPU_HARDWARE_CACHE
    if _CPU_HARDWARE_CACHE is None:
        logical = os.cpu_count() or 4
        physical = get_physical_cpu_cores()
        cpu_name = os.environ.get("PROCESSOR_IDENTIFIER", "CPU")
        if sys.platform.startswith("win"):
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
                cpu_name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                winreg.CloseKey(key)
            except Exception:
                pass
        _CPU_HARDWARE_CACHE = {
            "cpu_name": cpu_name.strip(),
            "cpu_physical_cores": physical,
            "cpu_logical_cores": logical,
        }

    hw = dict(_CPU_HARDWARE_CACHE)
    register_cuda_dlls()
    gpu_detected = _ct2_cuda_device_count() > 0
    dlls_ok = _cuda_dlls_available() if gpu_detected else False
    cuda_ready = gpu_detected and dlls_ok

    gpu_name = "NVIDIA GPU"
    vram_total_mb = 0.0
    vram_free_mb = 0.0
    if gpu_detected:
        try:
            import subprocess
            cmd = ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                parts = [p.strip() for p in res.stdout.strip().split(",")]
                if parts:
                    gpu_name = parts[0]
                if len(parts) >= 3:
                    vram_total_mb = float(parts[1])
                    vram_free_mb = float(parts[2])
        except Exception:
            pass

    hw.update({
        "gpu_detected": gpu_detected,
        "dlls_available": dlls_ok,
        "cuda_ready": cuda_ready,
        "gpu_name": gpu_name if gpu_detected else None,
        "vram_total_mb": vram_total_mb,
        "vram_free_mb": vram_free_mb,
    })
    return hw


def _load_saved_settings() -> Dict[str, Any]:
    path = _state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("settings"), dict):
            merged = dict(DEFAULT_OPTIMIZATION_SETTINGS)
            merged.update(data["settings"])
            return merged
    except (OSError, ValueError):
        pass
    return dict(DEFAULT_OPTIMIZATION_SETTINGS)


def get_whisper_settings() -> Dict[str, Any]:
    """Return current Whisper optimization settings, presets, and detected hardware."""
    global _RUNTIME_SETTINGS
    if _RUNTIME_SETTINGS is None:
        _RUNTIME_SETTINGS = _load_saved_settings()

    settings = dict(_RUNTIME_SETTINGS)
    eff_device = get_effective_device()
    eff_compute = get_effective_compute_type(eff_device)
    eff_threads = get_effective_cpu_threads()
    eff_beam = get_effective_beam_size()
    eff_batch = get_effective_batch_size()
    eff_recheck = get_effective_recheck_limit()

    badge = "Авто (GPU)" if eff_device == "cuda" else f"CPU · {eff_threads} ядер"
    preset_name = settings.get("preset", "auto")
    for p in OPTIMIZATION_PRESETS:
        if p["id"] == preset_name:
            badge = p["badge"]
            break

    return {
        "settings": settings,
        "presets": OPTIMIZATION_PRESETS,
        "hardware": get_system_hardware_info(),
        "effective": {
            "device": eff_device,
            "compute_type": eff_compute,
            "cpu_threads": eff_threads,
            "beam_size": eff_beam,
            "batch_size": eff_batch,
            "recheck_limit": eff_recheck,
            "badge": badge,
        },
    }


def set_whisper_settings(new_settings: Dict[str, Any]) -> Dict[str, Any]:
    """Persist and activate new Whisper optimization settings or preset."""
    global _RUNTIME_SETTINGS
    with _STATE_LOCK:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        cur = {}
        if path.is_file():
            try:
                cur = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(cur, dict):
                    cur = {}
            except Exception:
                cur = {}

        current_settings = cur.get("settings")
        if not isinstance(current_settings, dict):
            current_settings = dict(DEFAULT_OPTIMIZATION_SETTINGS)

        preset_id = str(new_settings.get("preset") or "").strip()
        matched_preset = next((p for p in OPTIMIZATION_PRESETS if p["id"] == preset_id), None)

        if matched_preset and "settings" in matched_preset:
            current_settings = dict(matched_preset["settings"])
            current_settings["preset"] = preset_id
        elif preset_id == "auto":
            current_settings = dict(DEFAULT_OPTIMIZATION_SETTINGS)
            current_settings["preset"] = "auto"
        else:
            if "device" in new_settings and new_settings["device"] in VALID_DEVICES:
                current_settings["device"] = new_settings["device"]
            if "compute_type" in new_settings and new_settings["compute_type"] in VALID_COMPUTE_TYPES:
                current_settings["compute_type"] = new_settings["compute_type"]
            if "cpu_threads" in new_settings and isinstance(new_settings["cpu_threads"], int):
                current_settings["cpu_threads"] = max(0, min(64, new_settings["cpu_threads"]))
            if "beam_size" in new_settings and isinstance(new_settings["beam_size"], int):
                current_settings["beam_size"] = max(1, min(10, new_settings["beam_size"]))
            if "batch_size" in new_settings and isinstance(new_settings["batch_size"], int):
                current_settings["batch_size"] = max(1, min(64, new_settings["batch_size"]))
            if "recheck_limit" in new_settings and isinstance(new_settings["recheck_limit"], int):
                current_settings["recheck_limit"] = max(0, min(32, new_settings["recheck_limit"]))
            current_settings["preset"] = "custom"

        cur["settings"] = current_settings
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        _RUNTIME_SETTINGS = dict(current_settings)

    # Invalidate cached Whisper model if device or compute changed
    try:
        from . import transcribe
        transcribe._reset_model()
    except Exception:
        pass

    return get_whisper_settings()


def get_effective_device() -> str:
    """Resolve compute device: env override -> user setting -> auto (cuda if available else cpu)."""
    env = os.environ.get("VIRALCUT_DEVICE")
    if env and env.strip() and env.strip() != "auto":
        return env.strip().lower()

    global _RUNTIME_SETTINGS
    if _RUNTIME_SETTINGS is None:
        _RUNTIME_SETTINGS = _load_saved_settings()

    choice = _RUNTIME_SETTINGS.get("device", "auto")
    if choice in ("cuda", "cpu"):
        return choice
    return "cuda" if _has_cuda_gpu() else "cpu"


def get_effective_compute_type(device: str) -> str:
    """Resolve compute type: env override -> user setting -> optimal type."""
    env = os.environ.get("VIRALCUT_COMPUTE")
    if env and env.strip() and env.strip() != "auto":
        return env.strip()

    global _RUNTIME_SETTINGS
    if _RUNTIME_SETTINGS is None:
        _RUNTIME_SETTINGS = _load_saved_settings()

    choice = _RUNTIME_SETTINGS.get("compute_type", "auto")
    if choice != "auto":
        return choice

    if device == "cuda":
        return "float16"
    return "int8"


def get_effective_cpu_threads() -> int:
    """Return thread count for CPU inference (physical cores count by default)."""
    env = os.environ.get("VIRALCUT_WHISPER_THREADS")
    if env and env.strip():
        try:
            return max(1, int(env.strip()))
        except ValueError:
            pass

    global _RUNTIME_SETTINGS
    if _RUNTIME_SETTINGS is None:
        _RUNTIME_SETTINGS = _load_saved_settings()

    threads = int(_RUNTIME_SETTINGS.get("cpu_threads") or 0)
    if threads > 0:
        return threads
    return get_physical_cpu_cores()


def get_effective_beam_size(default_beam: int = 5) -> int:
    env = os.environ.get("VIRALCUT_WHISPER_BEAM")
    if env and env.strip():
        try:
            return max(1, int(env.strip()))
        except ValueError:
            pass

    global _RUNTIME_SETTINGS
    if _RUNTIME_SETTINGS is None:
        _RUNTIME_SETTINGS = _load_saved_settings()

    return int(_RUNTIME_SETTINGS.get("beam_size") or 2)


def get_effective_batch_size(default_batch: int = 8) -> int:
    env = os.environ.get("VIRALCUT_WHISPER_BATCH_SIZE")
    if env and env.strip():
        try:
            return max(1, int(env.strip()))
        except ValueError:
            pass

    global _RUNTIME_SETTINGS
    if _RUNTIME_SETTINGS is None:
        _RUNTIME_SETTINGS = _load_saved_settings()

    return int(_RUNTIME_SETTINGS.get("batch_size") or 16)


def get_effective_recheck_limit(default_limit: int = 8) -> int:
    env = os.environ.get("VIRALCUT_WHISPER_RECHECK")
    if env and env.strip():
        try:
            return max(0, int(env.strip()))
        except ValueError:
            pass

    global _RUNTIME_SETTINGS
    if _RUNTIME_SETTINGS is None:
        _RUNTIME_SETTINGS = _load_saved_settings()

    return int(_RUNTIME_SETTINGS.get("recheck_limit") or 4)


def whisper_candidates(
    long_audio: bool = False,
    whisper_device: str = "auto",
    whisper_compute: str = "auto",
) -> List[Tuple[str, str]]:
    """Select (device, compute_type) candidates, preferring CUDA when DLLs are present."""
    register_cuda_dlls()
    eff_device = get_effective_device()
    eff_compute = get_effective_compute_type(eff_device)

    if whisper_device != "auto":
        eff_device = whisper_device
    if whisper_compute != "auto" and whisper_compute != "int8":
        eff_compute = whisper_compute

    if eff_device == "cpu":
        return [("cpu", eff_compute)]

    if eff_device == "cuda":
        return [("cuda", eff_compute), ("cuda", "int8_float16"), ("cpu", "int8")]

    if sys.platform == "darwin":
        if long_audio:
            return [("cpu", eff_compute)]
        return [("mps", "float16"), ("cpu", eff_compute)]

    if _has_cuda_gpu():
        return [("cuda", eff_compute), ("cuda", "int8_float16"), ("cpu", "int8")]
    return [("cpu", eff_compute)]


def _notify_debug(model: str, status: str, fallback_reason: Optional[str] = None) -> None:
    try:
        from . import custom_debug

        kwargs: Dict[str, Any] = {"model": model, "status": status}
        if fallback_reason is not None:
            kwargs["fallback_reason"] = fallback_reason
        custom_debug.record_whisper(**kwargs)
    except Exception:
        pass


def _invalidate_cleanup_cache() -> None:
    try:
        from . import custom_cleanup

        custom_cleanup.invalidate_cache()
    except Exception:
        pass


def _make_progress_tqdm(model_name: str, entry: Dict[str, Any]):
    """Build a standalone tqdm-compatible class forwarding byte progress to _DOWNLOADS."""
    expected_bytes = (expected_size_mb(model_name) or 0) * 1048576

    class ProgressTqdm:
        _shared_lock = threading.Lock()

        def __init__(self, *args: Any, **kwargs: Any):
            name = str(kwargs.pop("name", "") or "")
            desc = str(kwargs.pop("desc", "") or "")
            unit = str(kwargs.pop("unit", "") or "")
            kwargs.pop("unit_scale", None)
            kwargs.pop("bar_format", None)
            kwargs.pop("disable", None)
            total = kwargs.pop("total", None)
            initial = kwargs.pop("initial", 0) or 0

            self.total = int(total) if total is not None else 0
            self.n = int(initial)
            self.disable = False
            self.unit = unit
            self.desc = desc
            self._name = name
            is_transfer = (
                name == "huggingface_hub.snapshot_download.transfer"
                or desc.lower().startswith("downloading bytes")
            )
            self._track_bytes = (unit == "B") and not is_transfer
            if self._track_bytes and (self.total or self.n):
                self._emit()

        @classmethod
        def get_lock(cls):
            return cls._shared_lock

        @classmethod
        def set_lock(cls, lock) -> None:
            cls._shared_lock = lock

        @property
        def format_dict(self) -> Dict[str, Any]:
            with _DL_LOCK:
                speed = float(entry.get("speed_mbps") or 0.0)
            return {
                "n": self.n,
                "total": self.total,
                "rate": speed * 1048576.0 if speed > 0 else None,
            }

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb) -> None:
            self.close()

        def set_description(self, desc: Optional[str] = None, refresh: bool = True) -> None:
            if desc is not None:
                self.desc = str(desc)

        def set_description_str(self, desc: Optional[str] = None, refresh: bool = True) -> None:
            self.set_description(desc, refresh=refresh)

        def set_postfix(self, ordered_dict=None, refresh: bool = True, **kwargs: Any) -> None:
            pass

        def set_postfix_str(self, s: str = "", refresh: bool = True) -> None:
            pass

        def clear(self, *args: Any, **kwargs: Any) -> None:
            pass

        def refresh(self, *args: Any, **kwargs: Any) -> None:
            if self._track_bytes:
                self._emit()

        def close(self) -> None:
            if self._track_bytes:
                self._emit(force=True)

        def update(self, n: int | float | None = 1) -> None:
            if n:
                self.n += int(n)
            if self._track_bytes:
                self._emit()

        def _emit(self, force: bool = False) -> None:
            now = time.monotonic()
            total = int(self.total or 0)
            done = max(0, int(self.n or 0))
            if total <= 0 and expected_bytes > 0:
                total = expected_bytes
            elif total > 0 and expected_bytes > 0 and total < expected_bytes * 0.25 and done > total:
                total = max(total, expected_bytes)

            listeners: List[Callable[[float, str], None]] = []
            frac = 0.0
            stage_text = ""

            with _DL_LOCK:
                if entry.get("state") != "downloading":
                    return
                if total > int(entry.get("total_bytes") or 0):
                    entry["total_bytes"] = total
                eff_total = max(int(entry.get("total_bytes") or 0), done)
                if done > int(entry.get("downloaded_bytes") or 0):
                    entry["downloaded_bytes"] = done

                last_ts = float(entry.get("_speed_ts") or 0.0)
                last_bytes = int(entry.get("_speed_bytes") or 0)
                dt = now - last_ts
                if dt >= 0.25:
                    delta = max(0, entry["downloaded_bytes"] - last_bytes)
                    inst_mbps = delta / dt / 1048576.0
                    prev_mbps = float(entry.get("speed_mbps") or 0.0)
                    smooth = inst_mbps if prev_mbps <= 0 else (prev_mbps * 0.6 + inst_mbps * 0.4)
                    entry["speed_mbps"] = round(smooth, 2)
                    entry["_speed_ts"] = now
                    entry["_speed_bytes"] = entry["downloaded_bytes"]

                speed_mbps = float(entry.get("speed_mbps") or 0.0)
                if eff_total > 0:
                    frac = min(0.999, max(0.0, entry["downloaded_bytes"] / eff_total))
                    entry["pct"] = round(frac * 100.0, 1)
                else:
                    frac = 0.0
                    entry["pct"] = 0.0

                if speed_mbps > 0.05 and eff_total > entry["downloaded_bytes"]:
                    rem_mb = (eff_total - entry["downloaded_bytes"]) / 1048576.0
                    entry["eta_s"] = max(1, int(round(rem_mb / speed_mbps)))
                else:
                    entry["eta_s"] = None

                last_notify = float(entry.get("_last_notify") or 0.0)
                if force or (now - last_notify) >= 0.2:
                    entry["_last_notify"] = now
                    listeners = list(entry.get("listeners") or [])
                    done_mb = round(entry["downloaded_bytes"] / 1048576.0)
                    tot_mb = round(eff_total / 1048576.0) if eff_total > 0 else (expected_size_mb(model_name) or 0)
                    pct_int = int(round(entry["pct"]))
                    speed_txt = f" · {speed_mbps:.1f} МБ/с" if speed_mbps > 0 else ""
                    stage_text = f"Скачиваю Whisper {model_name}: {pct_int}% ({done_mb}/{tot_mb} МБ{speed_txt})"

            if listeners and stage_text:
                for cb in listeners:
                    try:
                        cb(frac, stage_text)
                    except Exception:
                        pass

    return ProgressTqdm


def _new_download_entry(model_name: str, on_progress: Optional[Callable[[float, str], None]] = None) -> Dict[str, Any]:
    now = time.monotonic()
    exp_mb = expected_size_mb(model_name) or 0
    return {
        "model": model_name,
        "state": "downloading",
        "downloaded_bytes": 0,
        "total_bytes": exp_mb * 1048576,
        "pct": 0.0,
        "speed_mbps": 0.0,
        "eta_s": None,
        "error": None,
        "event": threading.Event(),
        "listeners": [on_progress] if on_progress is not None else [],
        "_speed_ts": now,
        "_speed_bytes": 0,
        "_last_notify": 0.0,
    }


def _run_download_entry(norm: str, own_entry: Dict[str, Any], *, raise_on_error: bool) -> None:
    """Execute snapshot_download against a pre-registered _DOWNLOADS entry."""
    os.environ.setdefault("HF_HOME", str(_data_dir() / "hf"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    models_dir = _models_dir()
    models_dir.mkdir(parents=True, exist_ok=True)
    _notify_debug(norm, "downloading", "")

    try:
        import huggingface_hub

        tqdm_cls = _make_progress_tqdm(norm, own_entry)
        huggingface_hub.snapshot_download(
            model_repo_id(norm),
            cache_dir=str(models_dir),
            allow_patterns=ALLOW_PATTERNS,
            tqdm_class=tqdm_cls,
        )
        with _DL_LOCK:
            total = max(int(own_entry.get("total_bytes") or 0), int(own_entry.get("downloaded_bytes") or 0))
            if total > 0:
                own_entry["total_bytes"] = total
                own_entry["downloaded_bytes"] = total
            own_entry["pct"] = 100.0
            own_entry["eta_s"] = 0
            own_entry["state"] = "done"
            own_entry["error"] = None
            listeners = list(own_entry.get("listeners") or [])
        _invalidate_cleanup_cache()
        _notify_debug(norm, "idle", "")
        for cb in listeners:
            try:
                cb(1.0, f"Модель Whisper {norm} готова")
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        err_msg = str(exc)[:300] or "Не удалось скачать модель Whisper"
        with _DL_LOCK:
            own_entry["state"] = "error"
            own_entry["error"] = err_msg
        _notify_debug(norm, "idle", err_msg)
        if raise_on_error:
            raise
    finally:
        with _DL_LOCK:
            own_entry["listeners"] = []
        own_entry["event"].set()


def ensure_model_downloaded(
    model_name: str,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> None:
    """Download the requested Whisper model once with progress tracking and deduplication."""
    norm = normalize_model_name(model_name)
    if is_model_cached(norm):
        return

    wait_entry: Optional[Dict[str, Any]] = None
    own_entry: Optional[Dict[str, Any]] = None

    with _DL_LOCK:
        existing = _DOWNLOADS.get(norm)
        if existing and existing.get("state") == "downloading":
            wait_entry = existing
            if on_progress is not None:
                existing.setdefault("listeners", []).append(on_progress)
        else:
            own_entry = _new_download_entry(norm, on_progress=on_progress)
            _DOWNLOADS[norm] = own_entry

    if wait_entry is not None:
        wait_entry["event"].wait()
        if wait_entry.get("state") == "error" and wait_entry.get("error"):
            raise RuntimeError(str(wait_entry["error"]))
        return

    assert own_entry is not None
    _run_download_entry(norm, own_entry, raise_on_error=True)


def start_model_download(model_name: str) -> Dict[str, Any]:
    """Start a background download for one of the selectable Whisper models."""
    raw = str(model_name or "").strip()
    norm = normalize_model_name(raw)
    if not raw or norm not in SELECTABLE_MODEL_IDS:
        raise ValueError(f"Неизвестная модель Whisper: {model_name}")

    if is_model_cached(norm):
        return models_payload()

    already_downloading = False
    with _DL_LOCK:
        for active_name, info in _DOWNLOADS.items():
            if info.get("state") == "downloading":
                if active_name == norm:
                    already_downloading = True
                    break
                raise RuntimeError(f"Уже скачивается модель {active_name} — дождитесь завершения")

        if not already_downloading:
            entry = _new_download_entry(norm)
            _DOWNLOADS[norm] = entry

    if already_downloading:
        return models_payload()

    thread = threading.Thread(
        target=_run_download_entry,
        args=(norm, entry),
        kwargs={"raise_on_error": False},
        name=f"whisper-dl-{norm}",
        daemon=True,
    )
    thread.start()
    return models_payload()


def _incomplete_bytes(model_name: str) -> int:
    repo_slug = model_repo_id(model_name).replace("/", "--")
    blobs_dir = _models_dir() / f"models--{repo_slug}" / "blobs"
    if not blobs_dir.is_dir():
        return 0
    best = 0
    try:
        from . import custom_debug

        size_fn = custom_debug._live_file_size
    except Exception:
        def size_fn(p: Path) -> int:
            try:
                return int(p.stat().st_size)
            except OSError:
                return 0
    try:
        for inc in blobs_dir.glob("*.incomplete"):
            best += size_fn(inc)
    except OSError:
        pass
    return best


def get_download_info(model_name: str) -> Optional[Dict[str, Any]]:
    """Return a JSON-safe snapshot of download progress for a model, if tracked."""
    norm = normalize_model_name(model_name)
    with _DL_LOCK:
        entry = _DOWNLOADS.get(norm)
        if not entry:
            return None
        snap = {
            "model": norm,
            "state": entry.get("state"),
            "downloaded_bytes": int(entry.get("downloaded_bytes") or 0),
            "total_bytes": int(entry.get("total_bytes") or 0),
            "pct": float(entry.get("pct") or 0.0),
            "speed_mbps": float(entry.get("speed_mbps") or 0.0),
            "eta_s": entry.get("eta_s"),
            "error": entry.get("error"),
        }
    if snap["state"] == "downloading" and snap["downloaded_bytes"] == 0:
        inc_bytes = _incomplete_bytes(norm)
        if inc_bytes > 0:
            snap["downloaded_bytes"] = inc_bytes
            total = snap["total_bytes"] or (expected_size_mb(norm) or 0) * 1048576
            if total > 0:
                snap["pct"] = round(min(99.0, inc_bytes / total * 100.0), 1)
    return snap


def active_download_info() -> Optional[Dict[str, Any]]:
    """Return download progress for whichever model is currently downloading, if any."""
    with _DL_LOCK:
        active_name = next(
            (name for name, info in _DOWNLOADS.items() if info.get("state") == "downloading"),
            None,
        )
    return get_download_info(active_name) if active_name else None


def set_active_model(model_name: str) -> Dict[str, Any]:
    """Validate, persist, and activate one of the selectable Whisper models."""
    global _RUNTIME_MODEL
    raw = str(model_name or "").strip()
    norm = normalize_model_name(raw)
    if not raw or norm not in SELECTABLE_MODEL_IDS:
        raise ValueError(f"Модель '{model_name}' не поддерживается. Доступны: small, medium, large-v3.")

    with _STATE_LOCK:
        _save_model_choice(norm)
        _RUNTIME_MODEL = norm

    cfg = _config_module()
    if cfg is not None:
        cfg.WHISPER_MODEL = norm

    try:
        from . import custom_debug

        with custom_debug._LOCK:
            busy = custom_debug._WHISPER.get("status") in ("downloading", "loading", "transcribing")
        if not busy:
            custom_debug.record_whisper(model=norm)
    except Exception:
        pass

    return models_payload()


def models_payload() -> Dict[str, Any]:
    """Return full Whisper model catalog with local cache status and live download progress."""
    active = get_active_model()
    downloading_model: Optional[str] = None
    items: List[Dict[str, Any]] = []

    for meta in WHISPER_CATALOG:
        mid = meta["id"]
        cache = model_cache_info(mid)
        dl = get_download_info(mid)
        is_downloading = bool(dl and dl.get("state") == "downloading")
        if is_downloading:
            downloading_model = mid

        if cache["cached"]:
            status = "ready"
        elif is_downloading:
            status = "downloading"
        elif dl and dl.get("state") == "error":
            status = "error"
        else:
            status = "not_downloaded"

        total_bytes = (dl.get("total_bytes") if dl else 0) or (meta["size_mb"] * 1048576)
        dl_bytes = (dl.get("downloaded_bytes") if dl else 0) or 0
        items.append({
            **meta,
            "active": mid == active,
            "status": status,
            "cached": cache["cached"],
            "cache_mb": cache["cache_mb"],
            "pct": 100.0 if cache["cached"] else (dl["pct"] if is_downloading else 0.0),
            "downloaded_mb": cache["cache_mb"] if cache["cached"] else round(dl_bytes / 1048576.0, 1),
            "total_mb": round(total_bytes / 1048576.0, 1),
            "speed_mbps": dl["speed_mbps"] if is_downloading else 0.0,
            "eta_s": dl["eta_s"] if is_downloading else None,
            "error": dl.get("error") if (dl and status == "error") else None,
        })

    return {
        "active": active,
        "default": DEFAULT_WHISPER_MODEL,
        "active_language": get_active_language(),
        "languages": SUPPORTED_LANGUAGES,
        "downloading_model": downloading_model,
        "runtime": cuda_diagnostics(),
        "models": items,
    }
