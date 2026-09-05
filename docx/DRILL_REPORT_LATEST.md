# 真实数据库备份/恢复演练报告（本地隔离 MySQL）

> 记录类型：本地隔离 MySQL 5.7.39 真实数据库备份/恢复演练。
> 本报告仅用于记录本地隔离 MySQL 5.7.39 的备份/恢复验证结果。

## 1. 演练环境

| 项目 | 值 |
| --- | --- |
| MySQL 服务端 | MySQL 5.7.39，临时隔离实例，端口 13307 |
| 操作系统 | Windows 本地开发机 |
| 演练类型 | 本地隔离演练 |

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

## 7. 本地演练验收结果

| AC | 本地演练 | 证据 |
| --- | --- | --- |
| AC-01 手动备份 | ✅ | 退出码 0，manifest success |
| AC-02 内容完整 | ✅ | CREATE TABLE/INSERT/结束标记已校验 |
| AC-03 定时执行 | 本次未验证 | 本次仅执行手工命令 |
| AC-04 失败告警 | ⬜ | 本次未执行错误注入 |
| AC-05 部分失败隔离 | ⬜ | 本次均为成功任务 |
| AC-06 保留清理 | ✅ | 集成测试通过，本地临时实例验证 |
| AC-07 恢复演练 | ✅ | 第 4、5、6 节 |
| AC-08 并发保护 | ⬜ | 本次未执行双任务验证 |
| AC-09 安全 | ✅ | 日志未见密码明文，凭据通过 `MYSQL_PWD` 传递 |
| AC-10 磁盘预检 | ✅ | 本地磁盘预检通过 |

## 8. 结论

- 本地隔离 MySQL 实例已完整验证：完整备份、仅结构备份、L1 校验、全库恢复、仅结构恢复和 L2 影子库校验均通过。
- 本报告仅记录本地隔离 MySQL 演练结果。