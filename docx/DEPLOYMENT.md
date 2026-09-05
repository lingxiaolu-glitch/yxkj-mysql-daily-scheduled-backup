# 部署与运维手册

本手册用于 MySQL 每日定时备份系统的 Linux/cron/systemd 部署，以及 Windows 本地开发任务安装。

## 1. 部署前检查

- Python 3.13+（项目仅使用标准库）。
- 服务端 MySQL 8.0，建议确认 `mysqldump --version`。
- 备份账号满足 PRD 9.4：SELECT、SHOW VIEW、TRIGGER、EVENT、PROCESS、REPLICATION CLIENT、LOCK TABLES/RELOAD 等。
- 目标目录所在磁盘空间充足，`backup.min_free_bytes` 建议不低于 5GiB；`backup.mysqldump_path`/`backup.mysql_path` 如不在 PATH，请配置绝对路径。
- 实例 A/B 分别使用 `configs/instance-a.toml`、`configs/instance-b.toml`，端口/账号/密码环境变量不同。

## 2. 配置

```bash
cp configs/.env.example configs/.env
# 编辑 configs/.env，填入真实密码（不要提交）
# 编辑 configs/instance-a.toml 与 instance-b.toml 的 host/port/user/dest_dir
```

验证配置：

```bash
python application/main.py backup --config configs/instance-a.toml
```

生产执行前应先把配置复制到服务器任意受控目录，并确保 `.env` 权限为 600。

## 3. 部署预检

在服务器上先执行：

```bash
bash scripts/verify_deployment.sh configs/instance-a.toml
```

脚本会检查 Python、mysqldump/mysql、配置文件、`configs/.env` 密码环境变量、MySQL 连通性、目标目录和磁盘预检阈值。

## 4. cron 安装


```bash
sudo bash scripts/install_cron.sh configs/instance-a.toml
sudo bash scripts/install_cron.sh configs/instance-b.toml
crontab -l
```

`install_cron.sh` 会从 `[schedule] time` 读取执行时间并生成 02:00/02:30 任务。生产建议通过 systemd 管理，避免服务器重启后漏跑。

## 5. systemd 安装

```bash
sudo bash scripts/install_systemd.sh configs/instance-a.toml
sudo bash scripts/install_systemd.sh configs/instance-b.toml
sudo systemctl enable --now mysql-backup-instance-a.timer
sudo systemctl enable --now mysql-backup-instance-b.timer
systemctl list-timers 'mysql-backup-*'
```

## 6. Windows 本地开发任务

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Config configs\instance-a.toml -Time 02:00
```

脚本会注册 `mysql-daily-backup-instance-a` 计划任务并调用 `scripts/run_backup.ps1`。

## 7. 日常运行与审计

- 日志目录：配置 `[log] dir`，默认 `logs/instance-*`。
- manifest：备份目录内 `manifests/manifest_YYYYMMDD.json` 与 `status_YYYYMMDD.json`。
- 备份产物：`YYYYMMDD/{db}_*.sql.gz`；如果 `schema_only=true`，额外生成 `{db}_schema_*.sql.gz`。
- 保留清理：备份完成后自动执行；也可单独运行：

```bash
python application/main.py cleanup --config configs/instance-a.toml
```

## 8. 恢复

```bash
bash scripts/restore.sh --config configs/instance-a.toml --db shop --mode full
bash scripts/restore.sh --config configs/instance-a.toml --db shop --mode schema
```

恢复前建议先复制目标库或使用影子库演练；`--file` 可指定具体备份文件。

## 9. 常见问题

| 问题 | 检查 |
| --- | --- |
| 备份返回 2 | 查看 `logs`；确认 mysqldump/mysql 在 PATH、磁盘空间、`.env` 密码 |
| 单库失败返回 1 | 检查该库权限/参数，其他库已隔离执行 |
| 磁盘写入失败 | `[backup] min_free_bytes` 调低前先确认磁盘；必要时扩容或错峰 |
| 恢复失败 | 先检查 manifest 中 `availability=available`，再确认目标库/权限 |
| 定时任务未运行 | systemd 查看 `systemctl status mysql-backup-*.timer`；cron 检查时区/日志 |

## 10. 上线演练报告模板

使用 `docx/DRILL_REPORT_TEMPLATE.md` 记录首日备份、目标盘空间、定时任务和恢复演练结果。

## 11. 安全要求

- `configs/.env` 不得提交；提交前执行 `git check-ignore -v configs/.env`。
- 备份目录、日志、manifest 建议 0600/0700 权限。
- 日志/告警不输出密码；发现异常优先检查脱敏后的错误摘要。