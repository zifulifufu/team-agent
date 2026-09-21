#!/usr/bin/env bash
# 一次性准备开发环境:Python 虚拟环境 + 后端依赖 + 前端依赖
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
(cd desktop && npm install)
echo "完成。启动:  cd desktop && npm run dev"
