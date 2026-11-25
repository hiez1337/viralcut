#!/usr/bin/env bash
# Сборка ViralCut для Intel Mac (x86_64).
#
# PyInstaller не умеет кросс-компиляцию, поэтому сборка идёт под Rosetta:
# нужен интерпретатор Python 3.12 для x86_64 и venv, собранный им же.
# Такой Python можно взять из python-build-standalone:
#
#   curl -sSL -o py.tar.gz \
#     "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-x86_64-apple-darwin-install_only.tar.gz"
#   tar xzf py.tar.gz
#   arch -x86_64 ./python/bin/python3 -m venv --copies /tmp/vc_x86
#   /tmp/vc_x86/bin/pip install -r requirements-desktop.txt pyinstaller
#
# Дальше: VIRALCUT_INTEL_PYTHON=/tmp/vc_x86/bin/python ./tools/build_macos_intel.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${VIRALCUT_INTEL_PYTHON:-/tmp/vc_x86/bin/python}"
[ -x "$PY" ] || { echo "Нет Python для x86_64: $PY (см. комментарий в начале файла)"; exit 1; }

ARCH="$("$PY" -c 'import platform; print(platform.machine())')"
[ "$ARCH" = "x86_64" ] || { echo "Этот Python не x86_64 ($ARCH) — сборка была бы arm64"; exit 1; }

# x64 node для POT-провайдера: arm64-бинарь на Intel не запустится
VENDOR_SRC="desktop/vendor"
VENDOR_TAR="build/vendor-macos-x64.tar.gz"
NODE_X64="${VIRALCUT_NODE_X64:-}"
if [ -d "$VENDOR_SRC" ]; then
  if [ -n "$NODE_X64" ] && [ -x "$NODE_X64" ]; then
    rm -rf build/vendor-x64 && mkdir -p build/vendor-x64
    cp -R "$VENDOR_SRC/ejs" build/vendor-x64/ 2>/dev/null || true
    cp -R "$VENDOR_SRC/pot" build/vendor-x64/ 2>/dev/null || true
    cp "$NODE_X64" build/vendor-x64/node
    # имя внутри бандла должно быть vendor.tar.gz: его ищет desktop/main.py
    rm -rf build/vendor-bundle && mkdir -p build/vendor-bundle
    tar czf build/vendor-bundle/vendor.tar.gz -C build/vendor-x64 node ejs pot
    export VIRALCUT_VENDOR_TARBALL="$PWD/build/vendor-bundle/vendor.tar.gz"
    echo "Vendor для Intel: $VENDOR_TAR"
  else
    echo "ВНИМАНИЕ: node x64 не задан (VIRALCUT_NODE_X64) — приложение соберётся без POT-провайдера."
    export VIRALCUT_VENDOR_TARBALL=""
  fi
fi

# На Intel Mac с двумя картами по умолчанию считаем на дискретной:
# ключ Info.plist отключает автоматическое переключение (можно выключить
# переменной VIRALCUT_FORCE_DISCRETE_GPU=0).
export VIRALCUT_FORCE_DISCRETE_GPU="${VIRALCUT_FORCE_DISCRETE_GPU:-1}"
export PYINSTALLER_CONFIG_DIR="$PWD/build/pyinstaller-config-intel"
export PYTHONPYCACHEPREFIX="$PWD/.pycache"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

rm -rf dist-intel
"$PY" -m PyInstaller ViralCut.spec --noconfirm --clean \
  --distpath dist-intel --workpath build/pyinstaller-intel

APP="dist-intel/ViralCut.app"
[ -d "$APP" ] || { echo "Сборка не дала .app"; exit 1; }

# Проверяем, что получился именно x86_64, а не случайно arm64
EXE="$APP/Contents/MacOS/ViralCut"
file "$EXE" | grep -q "x86_64" || { echo "Ошибка: бинарь не x86_64"; file "$EXE"; exit 1; }
echo "Архитектура: $(file "$EXE" | sed 's/.*: //')"

echo "Подписываю ad-hoc…"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP" && echo "Подпись в порядке"

du -sh "$APP"
echo "Готово: $APP"
