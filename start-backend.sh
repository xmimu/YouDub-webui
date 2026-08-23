#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "错误: 未找到 .venv，请先运行: uv venv --python 3.12 .venv && uv pip install -r requirements.txt"
  exit 1
fi

if [ ! -f .env ]; then
  echo "错误: 未找到 .env，请先运行: cp env.txt.example .env 并配置"
  exit 1
fi

echo "启动后端: http://localhost:8000"
exec .venv/bin/uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
