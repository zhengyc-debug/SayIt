#!/usr/bin/env bash
# SayIt 启动脚本（最简版）
#
# 假定环境已就绪：venv 建好、依赖装好、.env 配好。
# 只做一件事：清掉 PYTHONPATH 后启动 uvicorn。
#
#   ./run.sh                  # http://127.0.0.1:8787
#   ./run.sh --port 9000
#   ./run.sh --host 0.0.0.0
#   ./run.sh --reload         # 改代码自动重启
#
# 额外参数原样透传给 tools/serve.py。
set -euo pipefail

cd "$(dirname "$0")"

# 这台机器上 Hermes Agent 会把 PYTHONPATH 指向它自己的内置环境（3.14 编译），
# 不清掉的话任何 python 都会先 import 到 Hermes 的包。项目 venv 必须隔离。
unset PYTHONPATH
unset PYTHONHOME

# 找解释器：优先项目 .venv，其次 PATH 里的 python3 / python
if [[ -x ".venv/bin/python" ]]; then
  PY=".venv/bin/python"                    # Linux / macOS
elif [[ -x ".venv/Scripts/python.exe" ]]; then
  PY=".venv/Scripts/python.exe"            # Windows (git-bash)
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "[!] 找不到 python，先装 Python 3.10+ 或建好 .venv" >&2
  exit 1
fi

if [[ ! -f ".env" ]]; then
  echo "[!] 找不到 .env —— 先 cp .env.example .env 并填入 Key" >&2
  exit 1
fi

exec "$PY" tools/serve.py "$@"
