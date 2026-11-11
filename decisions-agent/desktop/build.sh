#!/bin/bash
set -euo pipefail

version=0.1.0
release=false
architectures=("$(uname -m)")
if [[ $# -ne 0 ]]; then
  if [[ $# -ne 2 || "$1" != "--release" || ! "$2" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf 'Usage: %s [--release X.Y.Z]\n' "$0" >&2
    exit 2
  fi
  version="$2"
  release=true
  architectures=(arm64 x86_64)
fi

root="$(cd "$(dirname "$0")/.." && pwd)"
destination="$root/dist"
mkdir -p "$destination"
stage="$(mktemp -d "$destination/.phone-console.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
bundle="$stage/手机控制台.app"
contents="$bundle/Contents"
backend="$contents/Resources/backend"
mkdir -p "$contents/MacOS" "$backend/app" "$backend/source/server" "$backend/source/tooling"

for architecture in "${architectures[@]}"; do
  xcrun swiftc -swift-version 5 -target "$architecture-apple-macos14.0" -parse-as-library -O \
    "$root/desktop/Console.swift" "$root/desktop/PhonePreview.swift" -o "$stage/PhoneConsole-$architecture"
  xcrun swiftc -target "$architecture-apple-macos14.0" -O "$root/ocr.swift" -o "$stage/ocr-$architecture"
done
if "$release"; then
  xcrun lipo -create "$stage/PhoneConsole-arm64" "$stage/PhoneConsole-x86_64" -output "$contents/MacOS/PhoneConsole"
  xcrun lipo -create "$stage/ocr-arm64" "$stage/ocr-x86_64" -output "$backend/app/ocr-local"
else
  mv "$stage/PhoneConsole-${architectures[0]}" "$contents/MacOS/PhoneConsole"
  mv "$stage/ocr-${architectures[0]}" "$backend/app/ocr-local"
fi
for module in codex_provider desktop_backend desktop_discovery desk_models messages_task phone_agent phone_console phone_core phone_once phone_plugins phone_runtime phone_trigger sms_batch sms_vision trigger_settings wechat_desk wechat_tasks; do
  cp "$root/$module.py" "$backend/app/"
done
cp "$root/../source/server/"wda_*.py "$backend/source/server/"
cp "$root/../source/tooling/"{package.json,package-lock.json,forward.mjs} "$backend/source/tooling/"
cp "$root/../source/LICENSE" "$contents/Resources/iPhone-Use-LICENSE"

bundle_python() {
  local architecture="$1" target="$2" digest="$3"
  local tag=20261110
  local name="cpython-3.13.18+${tag}-${target}-apple-darwin-install_only_stripped.tar.gz"
  local runtime="$contents/Resources/python/$architecture"
  curl -fsSL "https://github.com/astral-sh/python-build-standalone/releases/download/$tag/$name" -o "$stage/$name"
  printf '%s  %s\n' "$digest" "$stage/$name" | shasum -a 256 --check
  mkdir -p "$runtime"
  tar -xzf "$stage/$name" -C "$runtime" --strip-components 1
}

if "$release"; then
  bundle_python arm64 aarch64 7a2384641584ab9984221fcb0fc9265df465922af71be71410529e2a9c3f0540
  bundle_python x86_64 x86_64 d27d59a722db9f69926eb6d9564071247221f45a04da920ea7f3001c97e32f9b
fi

iconset="$stage/AppIcon.iconset"
mkdir -p "$iconset"
for size in 16 32 128 256 512; do
  double=$((size * 2))
  sips -z "$size" "$size" "$root/desktop/AppIcon.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  sips -z "$double" "$double" "$root/desktop/AppIcon.png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$contents/Resources/AppIcon.icns"

cat > "$contents/Info.plist" <<PLIST
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
    <key>CFBundleShortVersionString</key><string>$version</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST
plutil -lint "$contents/Info.plist"
codesign --force --sign - "$bundle"
codesign --verify --deep --strict "$bundle"
if "$release"; then
  disk_source="$stage/disk"
  disk_name="phone-console-$version-universal.dmg"
  mkdir -p "$disk_source"
  ditto "$bundle" "$disk_source/手机控制台.app"
  ln -s /Applications "$disk_source/Applications"
  hdiutil create -quiet -volname "手机控制台" -srcfolder "$disk_source" -format UDZO "$stage/$disk_name"
  hdiutil verify "$stage/$disk_name"
  mv "$stage/$disk_name" "$destination/$disk_name"
  (cd "$destination" && shasum -a 256 "$disk_name" > "$disk_name.sha256")
  printf '%s\n' "$destination/$disk_name"
fi
if [ -d "$destination/手机控制台.app" ]; then
  mv "$destination/手机控制台.app" "$stage/previous.app"
fi
mv "$bundle" "$destination/手机控制台.app"
printf '%s\n' "$destination/手机控制台.app"
