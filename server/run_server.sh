#!/usr/bin/env bash
# ============================================================
#  启动本地深度/法线服务（Linux / macOS）
#  需先运行 setup_deployment.sh 完成部署
# ============================================================
cd "$(dirname "$0")/.."

# 使用部署脚本创建的服务专用环境（见 setup_deployment.sh 的说明）
if [ -f ".venv-server/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv-server/bin/activate
else
  echo "未找到 .venv-server，请先运行 ./server/setup_deployment.sh 完成部署。" >&2
  exit 1
fi

echo
echo "  服务地址 : http://127.0.0.1:8765"
echo "  健康检查 : http://127.0.0.1:8765/health"
echo "  接口文档 : docs/API接口文档.md"
echo "  按 Ctrl+C 停止服务。"
echo

exec uvicorn server.app:app --host 127.0.0.1 --port 8765
