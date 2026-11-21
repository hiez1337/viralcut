"""Custom Whisper configuration and CUDA support for the personal fork."""
from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path
from typing import List, Tuple

DEFAULT_WHISPER_MODEL = "large-v3"
_CUDA_DLL_HANDLES: List[object] = []
_CUDA_REGISTERED = False


def resolve_whisper_model() -> str:
    """Return the configured Whisper model, defaulting to large-v3 on this fork."""
    return os.environ.get("VIRALCUT_WHISPER", DEFAULT_WHISPER_MODEL)


def register_cuda_dlls() -> None:
    """Register pip-installed NVIDIA cuBLAS/cuDNN DLL directories on Windows."""
    global _CUDA_REGISTERED
    if _CUDA_REGISTERED or not sys.platform.startswith("win"):
        return
    _CUDA_REGISTERED = True

    roots = []
    for key in ("platlib", "purelib"):
        path = sysconfig.get_paths().get(key)
        if path:
            roots.append(Path(path))
    roots.append(Path(sys.prefix) / "Lib" / "site-packages")

    seen = set()
    current_path = os.environ.get("PATH", "")
    path_parts = current_path.split(os.pathsep) if current_path else []

    for root in roots:
        nvidia_dir = root / "nvidia"
        if not nvidia_dir.is_dir():
            continue
        for bin_dir in sorted(nvidia_dir.glob("*/bin")):
            resolved = str(bin_dir.resolve())
            if resolved in seen or not bin_dir.is_dir():
                continue
            seen.add(resolved)
            if hasattr(os, "add_dll_directory"):
                try:
                    _CUDA_DLL_HANDLES.append(os.add_dll_directory(resolved))
                except OSError:
                    pass
            if resolved not in path_parts:
                path_parts.insert(0, resolved)

    if path_parts:
        os.environ["PATH"] = os.pathsep.join(path_parts)


def _has_cuda_gpu() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def whisper_candidates(
    long_audio: bool,
    whisper_device: str,
    whisper_compute: str,
) -> List[Tuple[str, str]]:
    """Select (device, compute_type) candidates, preferring CUDA on Windows/Linux."""
    register_cuda_dlls()
    if whisper_device != "auto":
        return [(whisper_device, whisper_compute)]

    if sys.platform == "darwin":
        if long_audio:
            return [("cpu", whisper_compute)]
        return [("mps", "float16"), ("cpu", whisper_compute)]

    if _has_cuda_gpu():
        return [("cuda", "float16"), ("cuda", "int8_float16"), ("cpu", whisper_compute)]
    return [("cpu", whisper_compute)]
