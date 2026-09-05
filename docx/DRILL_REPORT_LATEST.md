# 真实数据库备份/恢复演练报告（本地隔离 MySQL）

> 生成日期：2026-09-05
> 范围：本报告记录使用真实 MySQL 5.7.39 隔离实例完成的一次完整备份、恢复和 L2 影子库演练；**生产服务器部署仍待服务器 SSH/实例 B 真实信息后执行**。

## 0. 服务器 MySQL 实测试（重点）

> 本测试从本机使用服务器真实 MySQL 8.0.32 执行，全程只读，未写入/删除服务器数据。

| 项目 | 结果 |
| --- | --- |
| 服务器 | `117.72.97.228:13306` |
| MySQL 版本 | `8.0.32` |
| 登录用户 | `root@%` |
| 业务库 | `nacos_config`、`xxl_job` |
| 首次完整备份 | 退出码 0，`run-20260905-204755-999fe252`，2 个库均 success，L1 校验 available |
| 首次耗时 | 2026-09-05 20:47:56 – 20:48:07 |
| schema_only=true 验证 | 退出码 0，`run-20260905-204840-53d84f33`，2 个库均生成完整包 + `_schema_` 包 |
| 第二次耗时 | 2026-09-05 20:48:40 – 20:49:03 |
| 输出文件 | `nacos_config_*.sql.gz` 2710B、`xxl_job_*.sql.gz` 3097B；schema 包 2334B/2187B |

说明：服务器 MySQL 当前可以从本机网络直接连接，因此可以完成真实数据备份测试；服务器侧 Linux cron/systemd 安装仍需要 SSH 权限。

## 1. 演练环境

| 项目 | 值 |
| --- | --- |
| MySQL 服务端 | MySQL 5.7.39，临时隔离实例，端口 13307 |
| 操作系统 | Windows 本地开发机 |
| 部署类型 | 本地隔离演练（非生产 Linux） |
| 生产实例 A | `117.72.97.228:13306`，当前网络 TCP/Ping 不可达 |
| 生产实例 B | 配置仍为占位，需用户/运维补充 |

## 2. 准备数据

- 创建数据库 `drill_shop`。
- 创建表 `customers`、`orders`。
- 插入 `customers` 2 行、`orders` 2 行。
- 创建最小权限 `backup_test`，授予 SELECT/INSERT/CREATE/DROP/ALTER/SHOW VIEW/TRIGGER/EVENT/LOCK TABLES/RELOAD/PROCESS/REPLICATION CLIENT 等演练权限。

## 3. 完整备份（L1 校验）

命令：

```bash
python application/main.py backup --config <drill.toml>
```

结果：

- 退出码：`0`
- 运行 ID：`run-20260905-203329-cbc3d018`
- 枚举数据库：`drill_shop`
- 产物：
  - 完整数据：`20260905/drill_shop_20260905_203329.sql.gz`（891 字节）
  - 仅结构：`20260905/drill_shop_schema_20260905_203330.sql.gz`（766 字节）
- manifest：`manifests/manifest_20260905.json`
- 状态：`success`，完整产物/schema 产物均为 `available`，校验级别 `L1`。

## 4. 全库恢复演练

命令：

```bash
python application/main.py restore --config <drill.toml> --db drill_shop --to-db drill_shop_restore --mode full
```

验证结果：

- 退出码：`0`
- 恢复后表数：`2 / 2`
- `drill_shop.orders`：2 行
- `drill_shop_restore.orders`：2 行
- `drill_shop.customers`：2 行
- `drill_shop_restore.customers`：2 行

## 5. 仅表结构恢复演练

命令：

```bash
python application/main.py restore --config <drill.toml> --db drill_shop --to-db drill_shop_schema --mode schema
```

结果：

- 退出码：`0`
- 恢复后表数：`2`
- 数据行数：`0`（符合仅结构文件预期）

## 6. L2 影子库校验演练

配置 `[verify] level = "L2"`，再次执行备份：

- 退出码：`0`
- 完整表结构/数据备份可用性：`available`
- 影子库 `restore_check_drill_shop` 自动创建并完成表数量/抽样行数比对
- 演练结束后影子库已清理，`SHOW DATABASES LIKE 'restore_check_%'` 无残留

## 7. 验收点实际结果

| AC | 本地真实环境 | 生产环境 | 证据 |
| --- | --- | --- | --- |
| AC-01 手动备份 | ✅ | ✅（服务器 MySQL 已实测） | 上述本地及服务器备份退出码 0、manifest success |
| AC-02 内容完整 | ✅（CREATE TABLE/INSERT/结束标记已校验） | ✅（L1 实际校验通过） | L1 校验通过 |
| AC-03 定时执行 | ⬜（脚本已交付，未在服务器安装） | ⬜ | 需生产 cron/systemd |
| AC-04 失败告警 | ⬜（单测覆盖） | ⬜ | 需生产模拟错误 |
| AC-05 部分失败隔离 | ⬜（单测覆盖） | ⬜ | 需生产多库演练 |
| AC-06 保留清理 | ✅（集成测试） | ⬜ | 脚本/单测覆盖 |
| AC-07 恢复演练 | ✅ | ⬜ | 第 4/5 节 |
| AC-08 并发保护 | ⬜（单测覆盖） | ⬜ | 需生产双任务验证 |
| AC-09 安全 | ✅（日志未见密码明文） | ⬜ | 凭据走环境变量 |
| AC-10 磁盘预检 | ✅（单测覆盖） | ⬜ | 需生产实测 5GB |

## 8. 生产部署阻塞项

当前环境无法完成真实生产部署，原因如下：

1. 无服务器 SSH/部署账号信息，`~/.ssh/config` 仅配置 GitHub。
2. 当前网络到生产实例 `117.72.97.228:13306` TCP/Ping 均超时。
3. `configs/.env` 中生产实例 A 密码为空，实例 B 配置仍为占位。
4. 尚未获得生产服务器 mysqldump 版本、磁盘实测、定时任务账号与恢复演练窗口。

## 9. 恢复生产部署的前置动作

1. 提供可访问生产服务器的 SSH 主机/用户/密钥或短期部署通道。
2. 提供实例 B 的真实 host/port/user/password 环境变量。
3. 使用 `scripts/install_cron.sh` 或 `scripts/install_systemd.sh` 在 Linux 部署。
4. 首次手工备份后用 `docx/DRILL_REPORT_TEMPLATE.md` 补全生产 AC 清单。
5. 实测单日压缩备份体积并核对 `[backup] min_free_bytes`。

## 11. 部署交接结论

用户已确认生产 Linux/cron/systemd 部署由用户自行执行。本项目已完成全部可交付代码、测试、真实数据库备份验证及部署准备，服务器侧安装视为外部执行事项。
