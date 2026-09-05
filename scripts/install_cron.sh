#!/usr/bin/env bash
# 用法: sudo scripts/install_cron.sh [配置文件]
# 例:   sudo scripts/install_cron.sh configs/instance-a.toml
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-configs/instance-a.toml}"
PYTHON="${PYTHON:-python3}"

# 解析 TOML 中的 schedule.time，防止手工编写错误。
read -r HOUR MINUTE < <("$PYTHON" - "$CONFIG" <<'PY'
import sys, tomllib
with open(sys.argv[1], 'rb') as fh:
    data = tomllib.load(fh)
time = data.get('schedule', {}).get('time', '02:00')
hour, minute = time.split(':')
print(f'{int(hour)} {int(minute)}')
PY
)

MARKER="mysql-daily-backup:$(realpath "$ROOT/$CONFIG")"
LINE="$MINUTE $HOUR * * * cd $ROOT && bash scripts/run_backup.sh $CONFIG >> $ROOT/logs/cron.log 2>&1 # $MARKER"

# 保证日志目录存在。
mkdir -p "$ROOT/logs"

# 先删除旧任务，再加新任务，避免重复安装。
( crontab -l 2>/dev/null | grep -v "$MARKER" || true; echo "$LINE" ) | crontab -

echo "已安装 cron：$LINE"