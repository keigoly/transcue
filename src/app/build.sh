#!/bin/bash
# TransCue.app を組み立てる(swiftc → バンドル → アイコン → ad-hoc 署名)。
#   ./build.sh            ビルドだけ
#   ./build.sh --snapshot 全状態の画面を logs/ui/ に PNG で書き出す(見た目の確認用)
set -euo pipefail
cd "$(dirname "$0")"

APP="TransCue.app"
BIN="transcue"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

swiftc -parse-as-library -O main.swift -o "$BIN"

STAGE="$WORK/$APP"
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"
cp "$BIN" "$STAGE/Contents/MacOS/$BIN"
"./$BIN" --write-iconset "$WORK/AppIcon.iconset"
iconutil -c icns "$WORK/AppIcon.iconset" -o "$STAGE/Contents/Resources/AppIcon.icns"
cat > "$STAGE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.transcue.app</string>
  <key>CFBundleName</key><string>TransCue</string>
  <key>CFBundleDisplayName</key><string>TransCue</string>
  <key>CFBundleExecutable</key><string>$BIN</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF
plutil -lint "$STAGE/Contents/Info.plist" >/dev/null
codesign --force --sign - "$STAGE" 2>/dev/null
rm -rf "$APP"
mv "$STAGE" "$APP"
echo "built: $(pwd)/$APP"

if [ "${1:-}" = "--snapshot" ]; then
  OUT="../../logs/ui"
  mkdir -p "$OUT"
  for m in idle off detected active bypass external failed dead pending stopping cloud-off cloud-off-active settings; do
    "./$BIN" --ui-snapshot "$OUT/$m.png" --mock "$m"
  done
  echo "snapshots: $(cd "$OUT" && pwd)"
fi
