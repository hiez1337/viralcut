"""Сборка облегчённого форка ViralCut для Windows 10/11 (x64) без CUDA DLL и вшитых моделей."""
import re as _re
import sys
from pathlib import Path as _Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


def _read_version() -> str:
    base = _Path(globals().get("SPECPATH") or ".")
    source = base / "viralcut" / "_version.py"
    try:
        match = _re.search(r'__version__\s*=\s*"([^"]+)"', source.read_text(encoding="utf-8"))
    except OSError:
        return "0.0.0"
    return match.group(1) if match else "0.0.0"


_APP_VERSION = _read_version()
IS_WIN = sys.platform.startswith("win")

datas = [("web", "web"), ("assets", "assets")]
binaries = []

if IS_WIN:
    _py_dll = _Path(sys.base_prefix) / f"python{sys.version_info[0]}{sys.version_info[1]}.dll"
    if _py_dll.is_file():
        binaries.append((str(_py_dll), "pythonnet/runtime"))

hiddenimports = (
    [
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "anyio._backends._asyncio",
    ]
    + collect_submodules("viralcut")
    + collect_submodules("yt_dlp_plugins")
    + collect_submodules("fontTools")
)

if IS_WIN:
    for pkg in ("webview", "pythonnet", "clr_loader"):
        pkg_datas, pkg_binaries, _pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_binaries
    hiddenimports += [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr",
        "clr_loader",
        "pythonnet",
    ]

for package in (
    "ctranslate2",
    "faster_whisper",
    "tokenizers",
    "onnxruntime",
    "av",
    "huggingface_hub",
    "imageio_ffmpeg",
    "cv2",
):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ["desktop/main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["desktop/pyi_rth_pythonnet.py"] if IS_WIN else [],
    excludes=[
        "tkinter",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide6",
        "IPython",
        "pytest",
        "notebook",
        "jupyter",
        "nvidia",
    ],
    noarchive=False,
    optimize=0,
)

# Вырезаем любые CUDA/cuDNN библиотеки (в т.ч. cudnn64_9.dll из ctranslate2),
# чтобы сборка оставалась лёгкой и предсказуемой без CUDA DLL.
_CUDA_EXCLUDE_RE = _re.compile(
    r"(?:^|[\\/._-])(nvidia|cublas|cudnn|cudart|nvrtc|cufft|cusolver|cusparse|curand)(?:$|[\\/._-]|64|32)",
    _re.I,
)


def _keep_entry(entry) -> bool:
    dest, src = entry[0], entry[1]
    return not (_CUDA_EXCLUDE_RE.search(dest or "") or _CUDA_EXCLUDE_RE.search(src or ""))


a.binaries = [item for item in a.binaries if _keep_entry(item)]
a.datas = [item for item in a.datas if _keep_entry(item)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ViralCut",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    disable_windowed_traceback=False,
    icon="assets/ViralCut.ico" if IS_WIN else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ViralCut",
)
