#!/usr/bin/env bash
# 用法: scripts/restore.sh --config configs/instance-a.toml --db shop [--file FILE] [--mode full|db|schema]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="configs/instance-a.toml"
DB=""
FILE=""
MODE="full"

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --file) FILE="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$DB" ]; then
  echo "缺少 --db" >&2
  exit 2
fi

# wrapper 会加载 configs/.env；restore 使用同一份实例配置。
ENV_FILE="$ROOT/configs/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

ARGS=("$ROOT/application/main.py" restore --config "$ROOT/$CONFIG" --db "$DB" --mode "$MODE")
if [ -n "$FILE" ]; then
  ARGS+=(--file "$FILE")
fi
exec python "${ARGS[@]}"