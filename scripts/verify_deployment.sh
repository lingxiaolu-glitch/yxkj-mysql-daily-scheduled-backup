#!/usr/bin/env bash
# 用法: scripts/verify_deployment.sh [配置文件]
# 例:   scripts/verify_deployment.sh configs/instance-a.toml
#
# 在生产服务器上执行部署前预检：工具、配置、凭据、数据库连接和磁盘空间。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-configs/instance-a.toml}"
CONFIG_ABS="$(realpath "$ROOT/$CONFIG")"
PYTHON="${PYTHON:-python3}"
ENV_FILE="$ROOT/configs/.env"

fail() { echo "  [FAIL] $*" >&2; exit 2; }
ok()   { echo "  [ OK ] $*"; }

echo "== 部署预检：$CONFIG_ABS =="

# 读取配置中的关键信息。
read -r HOST PORT USER PASSWORD_ENV MYSQLDUMP MYSQL DEST MIN_FREE <<<"$($PYTHON - "$CONFIG_ABS" <<'PY'
import sys, tomllib
with open(sys.argv[1], 'rb') as fh:
    d = tomllib.load(fh)
m = d['mysql']; b = d['backup']
print(f"{m['host']} {m['port']} {m['user']} {m['password_env']} {b['mysqldump_path']} {b.get('mysql_path','mysql')} {b['dest_dir']} {b.get('min_free_bytes', 0)}")
PY
)"

# 配置文件。
[ -f "$CONFIG_ABS" ] || fail "配置文件不存在"
ok "配置文件存在"

# Python 版本。
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "需要 Python 3.11+"' || fail "Python 版本过低"
ok "Python $("$PYTHON" --version 2>&1 | sed 's/^Python //')"

# 凭据文件与环境变量。
[ -f "$ENV_FILE" ] || fail "缺少 configs/.env"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
[ -n "${!PASSWORD_ENV:-}" ] || fail "环境变量 $PASSWORD_ENV 为空"
ok "密码环境变量 $PASSWORD_ENV 已设置"

# 可执行文件。
[ -x "$MYSQLDUMP" ] || command -v "$MYSQLDUMP" >/dev/null 2>&1 || fail "mysqldump 不可用: $MYSQLDUMP"
[ -x "$MYSQL" ] || command -v "$MYSQL" >/dev/null 2>&1 || fail "mysql 不可用: $MYSQL"
ok "mysqldump/mysql 可用"

# 版本。
"$MYSQLDUMP" --version | head -n1 || fail "mysqldump 无法执行"
ok "$("$MYSQLDUMP" --version | head -n1)"

# 数据库连接（使用 MYSQL_PWD，密码不进入命令行）。
export MYSQL_PWD="${!PASSWORD_ENV}"
if "$MYSQL" --host="$HOST" --port="$PORT" --user="$USER" --batch --skip-column-names -e "SELECT 1" >/dev/null 2>&1; then
  ok "MySQL 可用: $HOST:$PORT"
else
  fail "MySQL 连接失败: $HOST:$PORT"
fi
unset MYSQL_PWD

# 备份目录与磁盘空间。
mkdir -p "$DEST"
FREE_KB="$(df -Pk "$DEST" | awk 'NR==2 {print $4}')"
FREE_BYTES=$((FREE_KB * 1024))
REQUIRED_BYTES=$MIN_FREE
if [ "$FREE_BYTES" -ge "$REQUIRED_BYTES" ]; then
  ok "磁盘可用 $FREE_BYTES 字节，配置阈值 $REQUIRED_BYTES 字节"
else
  fail "磁盘空间不足: $FREE_BYTES < $REQUIRED_BYTES"
fi

echo "== 预检通过 =="