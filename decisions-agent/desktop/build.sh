#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
destination="$root/dist"
mkdir -p "$destination"
stage="$(mktemp -d "$destination/.phone-console.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
bundle="$stage/手机控制台.app"
contents="$bundle/Contents"
backend="$contents/Resources/backend"
mkdir -p "$contents/MacOS" "$backend/app" "$backend/source/server" "$backend/source/tooling"

xcrun swiftc -swift-version 5 -target "$(uname -m)-apple-macos14.0" -parse-as-library -O \
  "$root/desktop/Console.swift" "$root/desktop/PhonePreview.swift" -o "$contents/MacOS/PhoneConsole"
for module in codex_provider desktop_backend desktop_discovery desk_models messages_task phone_agent phone_console phone_core phone_once phone_plugins phone_runtime phone_trigger sms_batch sms_vision trigger_settings wechat_desk wechat_tasks; do
  cp "$root/$module.py" "$backend/app/"
done
xcrun swiftc -target "$(uname -m)-apple-macos14.0" -O "$root/ocr.swift" -o "$backend/app/ocr-local"
cp "$root/../source/server/"wda_*.py "$backend/source/server/"
cp "$root/../source/tooling/"{package.json,package-lock.json,forward.mjs} "$backend/source/tooling/"

iconset="$stage/AppIcon.iconset"
mkdir -p "$iconset"
for size in 16 32 128 256 512; do
  double=$((size * 2))
  sips -z "$size" "$size" "$root/desktop/AppIcon.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  sips -z "$double" "$double" "$root/desktop/AppIcon.png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$contents/Resources/AppIcon.icns"

cat > "$contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
    <key>CFBundleDisplayName</key><string>手机控制台</string>
    <key>CFBundleName</key><string>手机控制台</string>
    <key>CFBundleExecutable</key><string>PhoneConsole</string>
    <key>CFBundleIdentifier</key><string>local.phone-console.desktop</string>
    <key>CFBundleIconFile</key><string>AppIcon.icns</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST
plutil -lint "$contents/Info.plist"
codesign --force --sign - "$bundle"
codesign --verify "$bundle"
if [ -d "$destination/手机控制台.app" ]; then
  mv "$destination/手机控制台.app" "$stage/previous.app"
fi
mv "$bundle" "$destination/手机控制台.app"
printf '%s\n' "$destination/手机控制台.app"
