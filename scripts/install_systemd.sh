#!/usr/bin/env bash
# 用法: sudo scripts/install_systemd.sh [配置文件]
# 例:   sudo scripts/install_systemd.sh configs/instance-a.toml
#
# 说明：生成 systemd oneshot service + Persistent timer，并在 /etc/systemd/system
# 下安装；默认只重载，不自动启动，避免在交付前意外开始备份。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-configs/instance-a.toml}"
CONFIG_ABS="$(realpath "$ROOT/$CONFIG")"
INSTANCE="$(basename "$CONFIG" .toml)"
PYTHON="${PYTHON:-python3}"

# 解析配置中的每日执行时间。
read -r HOUR MINUTE < <("$PYTHON" - "$CONFIG_ABS" <<'PY'
import sys, tomllib
with open(sys.argv[1], 'rb') as fh:
    data = tomllib.load(fh)
hour, minute = data.get('schedule', {}).get('time', '02:00').split(':')
print(f'{int(hour)} {int(minute)}')
PY
)

SERVICE_FILE="/etc/systemd/system/mysql-backup-${INSTANCE}.service"
TIMER_FILE="/etc/systemd/system/mysql-backup-${INSTANCE}.timer"

# 配置文件与 wrapper 脚本。
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=MySQL daily logical backup for ${INSTANCE}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash ${ROOT}/scripts/run_backup.sh ${CONFIG_ABS}
TimeoutStartSec=6h
EOF

cat > "$TIMER_FILE" <<EOF
[Unit]
Description=Run MySQL daily backup for ${INSTANCE} at ${HOUR}:${MINUTE}

[Timer]
OnCalendar=*-*-* ${HOUR}:${MINUTE}:00
Persistent=true
Unit=mysql-backup-${INSTANCE}.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload

echo "已生成 systemd 单元："
echo "  $SERVICE_FILE"
echo "  $TIMER_FILE"
echo "如需立即启用，请执行："
echo "  sudo systemctl enable --now mysql-backup-${INSTANCE}.timer"