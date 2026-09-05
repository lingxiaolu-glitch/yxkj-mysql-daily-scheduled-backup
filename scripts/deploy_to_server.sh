#!/usr/bin/env bash
# 用法:
#   DEPLOY_HOST=1.2.3.4 DEPLOY_USER=root bash scripts/deploy_to_server.sh
# 可选环境变量:
#   DEPLOY_PORT=22
#   REMOTE_DIR=/opt/mysql-daily-scheduled-backup
#   INSTALL_SYSTEMD=1            # 默认不自动安装，仅预检；设为 1 安装 systemd timer
#   CONFIG=configs/instance-a.toml
set -euo pipefail

: "${DEPLOY_HOST:?请设置 DEPLOY_HOST}"
: "${DEPLOY_USER:?请设置 DEPLOY_USER}"
DEPLOY_PORT="${DEPLOY_PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-/opt/mysql-daily-scheduled-backup}"
CONFIG="${CONFIG:-configs/instance-a.toml}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARBALL="$(mktemp -t mdb-backup-deploy.XXXXXX.tar.gz)"
STAGE="/tmp/mysql-backup-deploy-$$"

cleanup() { rm -f "$TARBALL"; ssh -p "$DEPLOY_PORT" "$DEPLOY_USER@$DEPLOY_HOST" "rm -rf '$STAGE'" 2>/dev/null || true; }
trap cleanup EXIT

echo "== 1/4 打包项目 =="
# 排除开发/本地数据目录，保留 configs/.env 用于服务器运行。
tar -C "$ROOT" -czf "$TARBALL" \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='logs' \
  --exclude='backups' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  application domain infrastructure trigger scripts configs docx README.md requirements.txt .gitignore

echo "== 2/4 上传并解压到 $REMOTE_DIR =="
ssh -p "$DEPLOY_PORT" "$DEPLOY_USER@$DEPLOY_HOST" "mkdir -p '$REMOTE_DIR' '$STAGE'"
scp -P "$DEPLOY_PORT" "$TARBALL" "$DEPLOY_USER@$DEPLOY_HOST:$STAGE/deploy.tar.gz"
ssh -p "$DEPLOY_PORT" "$DEPLOY_USER@$DEPLOY_HOST" "tar -xzf '$STAGE/deploy.tar.gz' -C '$REMOTE_DIR' && chmod +x '$REMOTE_DIR/scripts'/*.sh"

echo "== 3/4 服务器部署预检 =="
ssh -p "$DEPLOY_PORT" "$DEPLOY_USER@$DEPLOY_HOST" "bash '$REMOTE_DIR/scripts/verify_deployment.sh' '$CONFIG'"

if [ "${INSTALL_SYSTEMD:-0}" = "1" ]; then
  echo "== 4/4 安装 systemd timer =="
  ssh -p "$DEPLOY_PORT" "$DEPLOY_USER@$DEPLOY_HOST" \
    "sudo bash '$REMOTE_DIR/scripts/install_systemd.sh' '$CONFIG' && sudo systemctl enable --now mysql-backup-$(basename "$CONFIG" .toml).timer"
else
  echo "== 4/4 跳过自动安装（INSTALL_SYSTEMD=${INSTALL_SYSTEMD:-0}）=="
  echo "确认预检通过后，可在服务器执行："
  echo "  sudo bash $REMOTE_DIR/scripts/install_systemd.sh $CONFIG"
  echo "  sudo systemctl enable --now mysql-backup-$(basename "$CONFIG" .toml).timer"
fi

echo "部署完成。"