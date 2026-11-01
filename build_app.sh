#!/bin/zsh
# 构建 看书宝.app（改完源码重跑一次即可）
set -e
cd "$(dirname "$0")"
APP="看书宝.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 签名一旦变化，macOS 的摄像头授权就会失效。源码没改就不重建、不重签。
# 戳放 bundle 外面，避免改动已签名内容。
STAMP="$PWD/.build_stamp"
NEW=$(cat macapp/main.swift macapp/Info.plist icons/看书宝.icns | shasum -a 256 | awk '{print $1}')
if [[ -x "$APP/Contents/MacOS/KanShuBao" && -f "$STAMP" && "$(cat "$STAMP")" == "$NEW" ]]; then
  echo "源码未变，跳过重建（保持签名与摄像头授权有效）"
  exit 0
fi

xcrun swiftc -O -module-cache-path /tmp/ksb-swift-cache \
  -framework AppKit -framework WebKit \
  macapp/main.swift -o "$APP/Contents/MacOS/KanShuBao"
cp macapp/Info.plist "$APP/Contents/Info.plist"
cp icons/看书宝.icns "$APP/Contents/Resources/看书宝.icns"
echo "$NEW" > "$STAMP"
codesign --force --deep --sign - "$APP" 2>/dev/null || true
echo "构建完成：$(pwd)/$APP"
