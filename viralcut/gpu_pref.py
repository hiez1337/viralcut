"""Выбор видеокарты: встроенная или дискретная.

На ноутбуках два адаптера, и по умолчанию система часто считает на встроенном.
Для рендера это важно: аппаратный кодировщик дискретной карты (NVENC/AMF)
заметно быстрее, но и греет сильнее.

Что делаем в зависимости от платформы:

  * **Windows** — пишем предпочтение в реестр для своего `.exe` и для ffmpeg:
    `HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences`, значение
    `GpuPreference=2` (высокая производительность) или `1` (экономия энергии).
    Windows 10 2004+ уважает эту настройку и запускает процесс на нужной карте.
  * **macOS Intel** — переключение GPU управляется ключом
    `NSSupportsAutomaticGraphicsSwitching` в Info.plist приложения; он ставится
    при сборке Intel-версии, а здесь мы только сообщаем, что менять нечего.
  * Порядок аппаратных кодировщиков тоже зависит от предпочтения: дискретная
    карта → NVENC/AMF первыми, встроенная → Quick Sync первым.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

PREFERENCES = ("auto", "integrated", "discrete")

# ключи реестра: 1 — экономия энергии (встроенная), 2 — высокая производительность
_REG_VALUE = {"integrated": "GpuPreference=1;", "discrete": "GpuPreference=2;"}
_REG_PATH = r"Software\Microsoft\DirectX\UserGpuPreferences"


def preference() -> str:
    env = os.environ.get("VIRALCUT_GPU_PREFERENCE", "").strip().lower()
    if env in PREFERENCES:
        return env
    try:
        from . import settings

        value = str(settings.get().get("gpu_preference") or "auto").strip().lower()
        return value if value in PREFERENCES else "auto"
    except Exception:  # noqa: BLE001
        return "auto"


def encoder_order(base: List[str]) -> List[str]:
    """Порядок аппаратных кодировщиков с учётом предпочтения.

    На Windows `h264_qsv` — это Intel Quick Sync (встроенная), а
    `h264_nvenc`/`h264_amf` — дискретные NVIDIA/AMD.
    """
    mode = preference()
    if mode == "integrated":
        integrated = [name for name in base if "qsv" in name]
        rest = [name for name in base if name not in integrated]
        return integrated + rest
    if mode == "discrete":
        discrete = [name for name in base if "qsv" not in name]
        rest = [name for name in base if name in integrated_names(base)]
        return discrete + rest
    return list(base)


def integrated_names(base: List[str]) -> List[str]:
    return [name for name in base if "qsv" in name]


def _windows_targets() -> List[str]:
    """Пути процессов, для которых пишем предпочтение."""
    targets: List[str] = []
    try:
        targets.append(str(sys.executable))
    except Exception:  # noqa: BLE001
        pass
    try:
        from .config import ffmpeg

        binary = ffmpeg()
        if binary:
            targets.append(str(binary))
    except Exception:  # noqa: BLE001
        pass
    return [path for path in dict.fromkeys(targets) if path]


def _apply_windows(mode: str) -> None:
    value = _REG_VALUE.get(mode)
    if not value:
        return
    try:
        import winreg  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _REG_PATH, 0,
                                 winreg.KEY_SET_VALUE)
    except OSError:
        return
    try:
        for target in _windows_targets():
            try:
                winreg.SetValueEx(key, target, 0, winreg.REG_SZ, value)
            except OSError:
                continue
    finally:
        try:
            winreg.CloseKey(key)
        except OSError:
            pass


def apply(mode: Optional[str] = None) -> Dict[str, Any]:
    """Применяет предпочтение. Возвращает, что удалось сделать."""
    target = (mode or preference()).strip().lower()
    if target not in PREFERENCES:
        target = "auto"
    result: Dict[str, Any] = {"preference": target, "applied": False, "platform": sys.platform}
    if target in ("integrated", "discrete"):
        if sys.platform == "win32":
            _apply_windows(target)
            result["applied"] = True
            result["mechanism"] = "реестр UserGpuPreferences"
        elif sys.platform == "darwin":
            # На macOS выбор карты задаётся в Info.plist при сборке
            # (NSSupportsAutomaticGraphicsSwitching). Сообщаем это интерфейсу.
            result["mechanism"] = "Info.plist (NSSupportsAutomaticGraphicsSwitching)"
            result["note"] = ("На macOS карта выбирается при сборке приложения; "
                              "для Intel-версии автопереключение отключено.")
        else:
            result["mechanism"] = "не поддерживается"
    else:
        result["mechanism"] = "системное решение"
    return result


def status() -> Dict[str, Any]:
    return {"preference": preference(), "platform": sys.platform}
