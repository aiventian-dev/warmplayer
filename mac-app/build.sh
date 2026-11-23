#!/bin/bash
# WarmPlayer mac app 组装脚本（路线 1：WKWebView 壳 + 包内 Python/ffmpeg 子进程）
# 依赖：Xcode command line tools（swiftc）、~/.warmplayer/buildvenv（pyinstaller+numpy）、/usr/local/bin/ffmpeg|ffprobe
# 用法：bash build.sh [输出.app路径]   默认 ../WarmPlayer.app
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
SVC="$HERE/.."
OUT="${1:-$HERE/../WarmPlayer.app}"
BV="$HOME/.warmplayer/buildvenv"
WORK="${WPBUILD:-/tmp/wpbuild}"

mkdir -p "$WORK"
# 1) Python helper（onedir）
if [ ! -x "$BV/bin/pyinstaller" ]; then
  /usr/bin/python3 -m venv "$BV"
  "$BV/bin/pip" install -q -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller numpy==2.0.2
fi
"$BV/bin/pyinstaller" --noconfirm --onedir --name warmserver \
  --distpath "$WORK/dist" --workpath "$WORK/work" --specpath "$WORK" "$SVC/vinyl_server.py" >/dev/null

# 2) Swift 壳
swiftc -O "$HERE/main.swift" -o "$WORK/WarmPlayer" \
  -framework Cocoa -framework WebKit -framework MediaPlayer -framework ServiceManagement

# 3) 图标（缺省时用 sips 从 icon.png 生成）
if [ ! -f "$WORK/AppIcon.icns" ] && [ -f "$HERE/icon.png" ]; then
  mkdir -p "$WORK/AppIcon.iconset"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$HERE/icon.png" --out "$WORK/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$HERE/icon.png" --out "$WORK/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$WORK/AppIcon.iconset" -o "$WORK/AppIcon.icns"
fi

# 4) 组装 bundle
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources/pages"
cp "$WORK/WarmPlayer" "$OUT/Contents/MacOS/WarmPlayer"
cp -R "$WORK/dist/warmserver" "$OUT/Contents/Resources/helper"
cp -L /usr/local/bin/ffmpeg "$OUT/Contents/Resources/ffmpeg"
cp -L /usr/local/bin/ffprobe "$OUT/Contents/Resources/ffprobe"
cp "$SVC/唱片机.html" "$SVC/磁带机.html" "$OUT/Contents/Resources/pages/"
[ -f "$WORK/AppIcon.icns" ] && cp "$WORK/AppIcon.icns" "$OUT/Contents/Resources/AppIcon.icns"
cat > "$OUT/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.warmplayer.mac</string>
  <key>CFBundleName</key><string>WarmPlayer</string>
  <key>CFBundleDisplayName</key><string>WarmPlayer</string>
  <key>CFBundleExecutable</key><string>WarmPlayer</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.83.0</string>
  <key>CFBundleVersion</key><string>830</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSHumanReadableCopyright</key><string>田瑞航 (Ryan)</string>
</dict>
</plist>
PLIST

# 5) ad-hoc 签名（本机运行足够；上架/公证需 Apple Developer 账号另行处理）
codesign --force --deep --sign - "$OUT" >/dev/null 2>&1 || true
echo "BUILD_OK $OUT"
du -sh "$OUT"