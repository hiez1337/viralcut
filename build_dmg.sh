#!/usr/bin/env bash
# Сборка дистрибутива: DMG (если разрешает система) + ZIP (всегда)
set -euo pipefail
cd "$(dirname "$0")"

VERSION="$(python3 tools/version.py)"
# Архитектурный ярлык и путь к приложению можно переопределить (Intel-сборка).
ARCH_LABEL="${VIRALCUT_ARCH_LABEL:-arm64}"
NAME="ViralCut-${VERSION}-macOS-${ARCH_LABEL}"
DMG_NAME="${VIRALCUT_DMG_NAME:-ViralCut-${VERSION}.dmg}"

APP="${VIRALCUT_APP:-dist/ViralCut.app}"
[ -d "$APP" ] || { echo "Сначала соберите приложение (build_app.sh или tools/build_macos_intel.sh)"; exit 1; }

# ZIP — универсальный вариант, сохраняет подпись и права
ditto -c -k --sequesterRsrc --keepParent "$APP" "dist/${NAME}.zip"
du -sh "dist/${NAME}.zip"

# DMG — красивее, но требует прав на создание образов
STAGE="build/dmg"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
# Инструкция для тестера: Gatekeeper, что проверить, куда смотреть в логе
cp docs/MACOS-TEST.md "$STAGE/ЧИТАЙ-МЕНЯ.txt"
ln -s /Applications "$STAGE/Applications"

if hdiutil create -volname "ViralCut" -srcfolder "$STAGE" -ov -format UDZO \
    "dist/${DMG_NAME}" >/dev/null 2>&1; then
  echo "DMG: dist/${DMG_NAME}"
else
  echo "DMG создать не удалось (нет прав) — используйте ZIP."
fi
rm -rf "$STAGE"
