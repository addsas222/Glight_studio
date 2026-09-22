#!/usr/bin/env bash
# ============================================================
#  凌日光影棚 Horizon Light Studio —— macOS 打包脚本（.app + .dmg）
#  用法：在仓库根目录执行  bash packaging/build_macos.sh
#
#  前置：Node 18+（构建前端，仅构建期需要）、Xcode 命令行工具
#  产出：
#    dist/凌日光影棚.app
#    dist/凌日光影棚-1.0.0.dmg
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="凌日光影棚"
APP_ID="horizon-light-studio"
VERSION="1.0.0"
DMG="dist/${APP_NAME}-${VERSION}.dmg"
SPEC="packaging/horizon_light_studio.spec"

# 打包使用**独立**的 .venv-build，与开发用的 .venv 分开：
# uv 创建的 .venv 里没有 pip（uv 用自己的安装器），复用会在 pip install 处失败。
# 因此按「pip 是否存在」判断，而不是「目录是否存在」。
VENV=".venv-build"
echo "[1/6] 准备打包虚拟环境（${VENV}）..."
if [ ! -x "${VENV}/bin/pip" ]; then
  rm -rf "${VENV}"
  python3 -m venv "${VENV}"
fi
# shellcheck disable=SC1091
source "${VENV}/bin/activate"

echo "[2/6] 安装依赖（含 WebUI 与随包 ffmpeg）..."
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt pyinstaller

echo "[3/6] 构建前端界面..."
if ! command -v node >/dev/null 2>&1; then
  echo "错误：未找到 Node.js。构建前端需要 Node 18+（仅构建期需要，运行期不需要）。" >&2
  echo "      请安装： https://nodejs.org/" >&2
  exit 1
fi
pushd webui/frontend >/dev/null
[ -d node_modules ] || npm install
npm run build
popd >/dev/null
if [ ! -f webui/frontend/dist/index.html ]; then
  echo "错误：前端构建产物缺失（webui/frontend/dist/index.html）。" >&2
  exit 1
fi

echo "[4/6] 生成应用图标（PNG → ICNS）..."
python packaging/make_icon.py
mkdir -p build/icon.iconset
for sz in 16 32 64 128 256 512; do
  sips -z $sz $sz packaging/icons/appicon.png \
       --out "build/icon.iconset/icon_${sz}x${sz}.png" >/dev/null
  dbl=$((sz * 2))
  if [ "$dbl" -le 1024 ]; then
    sips -z $dbl $dbl packaging/icons/appicon.png \
         --out "build/icon.iconset/icon_${sz}x${sz}@2x.png" >/dev/null
  fi
done
iconutil -c icns build/icon.iconset -o packaging/icons/appicon.icns

echo "[5/6] 打包 .app ..."
pyinstaller "$SPEC" --noconfirm --clean

if [ -d "dist/${APP_NAME}.app" ]; then
  mkdir -p "dist/${APP_NAME}.app/Contents/Resources/docs"
  cp -R docs/. "dist/${APP_NAME}.app/Contents/Resources/docs/" || true
fi

echo "[6/6] 生成 .dmg ..."
rm -f "$DMG"
if command -v create-dmg >/dev/null 2>&1; then
  create-dmg \
    --volname "$APP_NAME" \
    --window-size 640 400 \
    --icon-size 110 \
    --icon "${APP_NAME}.app" 170 190 \
    --app-drop-link 470 190 \
    "$DMG" "dist/${APP_NAME}.app" || \
  hdiutil create -volname "$APP_NAME" -srcfolder "dist/${APP_NAME}.app" \
                 -ov -format UDZO "$DMG"
else
  hdiutil create -volname "$APP_NAME" -srcfolder "dist/${APP_NAME}.app" \
                 -ov -format UDZO "$DMG"
fi

echo
echo "完成！"
echo "  .app : dist/${APP_NAME}.app"
echo "  .dmg : $DMG"
echo
echo "注意：未签名/未公证的 .dmg 在他人机器上首次打开会提示「已损坏」或"
echo "      「无法验证开发者」。正式分发请执行（需 Apple 开发者账号）："
echo "        codesign --deep --force --options runtime \\"
echo "          --sign \"Developer ID Application: <你的名称> (TEAMID)\" \"dist/${APP_NAME}.app\""
echo "        xcrun notarytool submit \"$DMG\" --apple-id <邮箱> --team-id <TEAMID> --wait"
echo "        xcrun stapler staple \"$DMG\""
echo "      本机测试可临时执行： xattr -dr com.apple.quarantine \"dist/${APP_NAME}.app\""
