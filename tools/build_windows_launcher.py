"""Create a Windows x64 GUI launcher for the portable ViralCut archive."""
from importlib.resources import files
from io import BytesIO
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

BOOTSTRAP = """import os, runpy, sys
from pathlib import Path
root = Path(sys.argv[0]).resolve().parent
os.chdir(root)
os.environ.update(PYTHONNET_PYDLL=str(root/'python'/'python312.dll'),
                  PYTHONNET_RUNTIME='netfx',
                  VIRALCUT_WHISPER_DIR=str(root/'models'/'whisper-small'))
runpy.run_path(str(root/'desktop'/'main.py'), run_name='__main__')
"""
SHEBANG = b"#!<launcher_dir>\\python\\pythonw.exe\n"


def build(folder: Path) -> None:
    distlib = files("pip._vendor.distlib")
    launcher = (distlib / "w64.exe").read_bytes()
    if "<launcher_dir>\\".encode("utf-16le") not in launcher:
        raise RuntimeError("The installed distlib launcher lacks relative path support.")
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        bundle.writestr("__main__.py", BOOTSTRAP)
    (folder / "ViralCut.exe").write_bytes(launcher + SHEBANG + archive.getvalue())
    (folder / "LICENSE-launcher.txt").write_bytes(Path(__file__).with_name("LICENSE-distlib-launcher.txt").read_bytes())


if __name__ == "__main__":
    build(Path(sys.argv[1]))
