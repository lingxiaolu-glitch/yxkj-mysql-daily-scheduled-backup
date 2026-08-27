#!/usr/bin/env bash
# 用法: scripts/run_backup.sh <配置文件>
# 例:   scripts/run_backup.sh configs/instance-a.toml
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/configs/.env"
CONFIG="${1:-configs/instance-a.toml}"

if [ ! -f "$ENV_FILE" ]; then
  echo "缺少凭据文件 $ENV_FILE（请从 configs/.env.example 复制并填写）" >&2
  exit 2
fi

# 导入凭据到环境变量（不打印，不回显）
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

exec python "$ROOT/application/main.py" backup --config "$ROOT/$CONFIG"