#!/usr/bin/env bash
# ============================================================
#  本地深度服务一键部署脚本（Linux / macOS）
#
#  使用**独立**的 .venv-server，与开发用的 .venv 分开：
#  .venv 可能由 uv 管理（uv 自有安装器、无 pip），用 python3 -m venv
#  覆盖它会改写 pyvenv.cfg 并注入 pip，随后 uv sync 又会把多余包清掉，
#  两个工具互相打架。因此按「pip 是否存在」判断，并单独用一套环境。
# ============================================================
set -e
cd "$(dirname "$0")/.."

VENV=".venv-server"
echo "== 1/3 创建虚拟环境（${VENV}）=="
if [ ! -x "${VENV}/bin/pip" ]; then
  rm -rf "${VENV}"
  python3 -m venv "${VENV}"
fi
# shellcheck disable=SC1091
source "${VENV}/bin/activate"

echo "== 2/3 安装依赖 =="
python -m pip install --upgrade pip >/dev/null
python -m pip install -r server/requirements.txt

echo "== 3/3 下载深度模型 Depth Anything V2 Small（约 94MB）=="
# 模型是**可选**的：没有它软件会降级为「仅预览」，之后也能从界面导入。
# 此时依赖已装好，下载失败不应让整个部署失败（与 setup_deployment.bat 保持一致）。
# 注意 `set -e` 下必须用 if 包裹，否则失败会直接中止脚本。
if python -c "
import sys; sys.path.insert(0, '.')
from core.preprocess import download_depth_model
try:
    print('模型已就绪:', download_depth_model())
except Exception as e:
    # 打印一行可读提示而不是原始 traceback：脚本随后会走「有警告但成功」分支，
    # 抛 traceback 会让人以为整个部署崩了。
    print(f'模型下载失败：{type(e).__name__}: {e}', file=sys.stderr)
    raise SystemExit(1)
"; then
  echo ""
  echo "部署完成！启动服务："
  echo "  source ${VENV}/bin/activate && uvicorn server.app:app --host 127.0.0.1 --port 8765"
  echo "（也可直接运行 ./server/run_server.sh）"
else
  echo ""
  echo "------------------------------------------------------------------"
  echo "警告：模型下载失败（本网络下 huggingface.co 常不可达）。"
  echo "环境与全部依赖都已安装好，只缺这个可选的深度模型。"
  echo ""
  echo "三种补救方式："
  echo "  1) 用镜像下载："
  echo "       export HLS_DEPTH_MODEL_URL=https://<镜像>/model.onnx"
  echo "     然后重跑本脚本。（MiDaS 用 HLS_MODEL_URL）"
  echo "  2) 手动下载后放到："
  echo "       ~/.horizon_light_studio/models/depth-anything-v2-small.onnx"
  echo "     （MiDaS-small 命名为 model-small.onnx 放同一目录亦可）"
  echo "  3) 启动软件，在「AI 后端设置」里用「离线导入模型…」。"
  echo ""
  echo "没有模型软件仍可运行（仅预览深度）。"
  echo "------------------------------------------------------------------"
  echo ""
  echo "部署完成（有警告）。启动服务："
  echo "  source ${VENV}/bin/activate && uvicorn server.app:app --host 127.0.0.1 --port 8765"
  echo "（也可直接运行 ./server/run_server.sh）"
fi
