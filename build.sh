#!/bin/bash
# 把 recorder.swift 编译成一个独立的菜单栏 App（无第三方依赖）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/录屏助手.app"
IDENTITY="ScreenRecorder Self-Signed"

# 没有固定签名身份就先创建（保证重编译不丢屏幕录制权限）
if ! security find-identity -p codesigning 2>/dev/null | grep -q "$IDENTITY"; then
  echo "未找到签名身份，先创建..."
  bash "$DIR/setup-cert.sh"
fi

echo "正在编译..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O "$DIR/recorder.swift" \
  -o "$APP/Contents/MacOS/recorder" \
  -framework AppKit

# 生成 App 文件图标（.icns），若已存在则复用
if [ ! -f "$DIR/AppIcon.icns" ]; then
  echo "生成 App 图标..."
  rm -rf "$DIR/icon.iconset"
  swift "$DIR/make-icon.swift" "$DIR/icon.iconset" >/dev/null
  iconutil -c icns "$DIR/icon.iconset" -o "$DIR/AppIcon.icns"
  rm -rf "$DIR/icon.iconset"
fi
cp "$DIR/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>            <string>录屏助手</string>
  <key>CFBundleDisplayName</key>     <string>录屏助手</string>
  <key>CFBundleIdentifier</key>      <string>com.local.screenrecorder</string>
  <key>CFBundleExecutable</key>      <string>recorder</string>
  <key>CFBundleIconFile</key>        <string>AppIcon</string>
  <key>CFBundlePackageType</key>     <string>APPL</string>
  <key>CFBundleVersion</key>         <string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key>  <string>13.0</string>
  <!-- 菜单栏专用：不显示 Dock 图标 -->
  <key>LSUIElement</key>             <true/>
  <key>NSMicrophoneUsageDescription</key><string>录屏时录制麦克风声音</string>
</dict>
</plist>
PLIST

# 用固定的自签名身份签名：身份稳定 → 重编译不会丢屏幕录制权限
codesign --force --deep --sign "$IDENTITY" "$APP"

# 保险：若退回了临时(adhoc)签名（通常是后台/钥匙串未解锁导致），立即报错，
# 否则会因 cdhash 变化而悄悄丢失屏幕录制权限。请在前台终端重试。
if codesign -dvv "$APP" 2>&1 | grep -q "Signature=adhoc"; then
  echo "❌ 签名退回了临时(adhoc)签名——请在前台终端里重新运行 bash build.sh。" >&2
  exit 1
fi

echo "✅ 构建完成：$APP"
echo "双击它即可启动；菜单栏右上角会出现一个圆点图标。"
