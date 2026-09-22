#!/usr/bin/env bash
# ============================================================
#  凌日光影棚 Horizon Light Studio —— Linux 打包脚本（.AppImage）
#  用法：在仓库根目录执行  bash packaging/build_linux.sh
#
#  依赖：Node 18+（构建前端，仅构建期需要）、python3-venv、fuse（运行 AppImage）
#        appimagetool —— 脚本自动下载到 build/（需联网一次）
#  产出：dist/凌日光影棚-1.0.0-x86_64.AppImage
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="凌日光影棚"
APP_ID="horizon-light-studio"
VERSION="1.0.0"
ARCH="$(uname -m)"
APPDIR="build/AppDir"
OUT="dist/${APP_NAME}-${VERSION}-${ARCH}.AppImage"
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

echo "[4/6] 生成应用图标..."
python packaging/make_icon.py

echo "[5/6] 打包桌面程序..."
pyinstaller "$SPEC" --noconfirm --clean

echo "[6/6] 组装 AppDir 并生成 AppImage ..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/512x512/apps" \
         "$APPDIR/usr/share/doc/${APP_ID}"

cp -R dist/HorizonLightStudio/. "$APPDIR/usr/bin/"
cp packaging/icons/appicon.png "$APPDIR/usr/share/icons/hicolor/512x512/apps/${APP_ID}.png"
cp -R docs/. "$APPDIR/usr/share/doc/${APP_ID}/" || true
cp packaging/linux/AppRun "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

sed "s|@APP_ID@|${APP_ID}|g" packaging/linux/app.desktop \
    > "$APPDIR/${APP_ID}.desktop"
cp "$APPDIR/${APP_ID}.desktop" "$APPDIR/usr/share/applications/"
cp packaging/icons/appicon.png "$APPDIR/${APP_ID}.png"

TOOL="build/appimagetool-${ARCH}.AppImage"
if [ ! -x "$TOOL" ]; then
  echo "下载 appimagetool ..."
  mkdir -p build
  curl -fL -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage" \
    || { echo "appimagetool 下载失败：请手动放置到 $TOOL"; exit 1; }
  chmod +x "$TOOL"
fi

mkdir -p dist
ARCH="$ARCH" "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT" 2>/dev/null \
  || ARCH="$ARCH" "$TOOL" "$APPDIR" "$OUT"

echo
echo "完成！产物：$OUT"
echo "运行： chmod +x \"$OUT\" && \"./$OUT\""
echo "（若系统缺少 FUSE，可用 --appimage-extract-and-run 运行）"
