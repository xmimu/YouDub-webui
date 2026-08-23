#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d apps/web/node_modules ]; then
  echo "错误: 未找到前端依赖，请先运行: npm --prefix apps/web install"
  exit 1
fi

echo "启动前端: http://localhost:3000"
exec npm --prefix apps/web run dev -- --hostname 0.0.0.0 --port 3000
