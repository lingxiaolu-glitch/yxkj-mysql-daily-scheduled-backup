# 开发计划：MySQL 每日定时备份系统

> 配套文档：`docx/PRD.md`（v0.5，第 13 章评审结论已确认）
>
> **用法**：本计划的每个步骤在**一个新的 Codex 对话**中完成。新对话开始时，把对应「步骤卡片」（第 2 节内容）连同当前仓库状态发给该对话即可。
>
> **核心约束（不破坏前序逻辑）**：
> 1. 每步只**新增文件/模块**；不删除、不重写前序步骤已验收的模块；
> 2. 确需演进已有接口时，必须**向后兼容**：只加可选参数、新增类/方法、保留旧接口；
> 3. 每步结束时回归运行**全部已有测试**（`python -m unittest discover -s tests -v`），保证全绿；
> 4. 外部工具（mysqldump / mysql CLI）一律用 mock/fake 做测试，本地 Windows 无 mysqldump；
> 5. 仅使用 Python 3.13 标准库，不引入第三方依赖。

---

## 1. 步骤总览

| # | 步骤 | 主要交付物 | 依赖 | 状态 |
| --- | --- | --- | --- | --- |
| 01 | 项目骨架 | 目录结构、空包、configs/ 多实例配置模板、requirements.txt、.gitignore | — | ✅ |
| 02 | 配置加载与校验 | `infrastructure/config_loader.py` + 完整默认配置 | 01 | ✅ |
| 03 | 日志与脱敏 | `infrastructure/logging_utils.py`（run_id、轮转、敏感信息脱敏） | 01 | ✅ |
| 04 | 领域模型与事件 | `domain/model/*`、`domain/events.py`、`domain/repositories.py` | 02 | ✅ |
| 05 | 领域服务 | `domain/services/backup_execution.py`、`retention.py`、`verification.py` | 04 | ✅ |
| 06 | 时钟/锁/存储/压缩适配 | `system_clock.py`、`run_lock.py`、`file_storage.py`、`compressor.py` | 03, 04 | ✅ |
| 07 | MySQL 防腐层与网关 | `infrastructure/mysqldump_client.py`、`mysql_client.py` | 04, 06 | ✅ |
| 08 | manifest 仓库与 L0/L1 校验实现 | `infrastructure/manifest_repository.py`、`verifiers.py` | 05, 06, 07 | ✅ |
| 09 | 通知适配 | `infrastructure/notifiers.py`（LogNotifier，预留 SMTP/Webhook） | 04, 06 | ✅ |
| 10 | 触发层命令处理器 | `trigger/run_backup.py`、`restore_backup.py`、`cleanup.py` | 05–09 | ⬜ |
| 11 | 应用层入口 | `application/cli.py` + `application/main.py`（装配、子命令、退出码） | 03, 10 | ⬜ |
| 12 | 集成测试与部署脚本 | `tests/integration/*`、`scripts/install_*`、`restore.sh`、`README.md` | 11 | ⬜ |
| 13 | 真实环境部署与演练 | 服务器安装、定时任务、首日备份、恢复演练报告 | 12 | ⬜ |

---

## 2. 步骤卡片

### 步骤 01：项目骨架

- **目标**：建立与 PRD 7.3.9 一致的目录结构，后续所有代码有明确落位。
- **新增**：
  - 目录：`application/`、`trigger/`、`domain/model/`、`domain/services/`、`infrastructure/`、`scripts/`、`tests/unit/`、`tests/integration/`（各目录放 `__init__.py` 空包文件）；
  - `configs/instance-*.toml`（每实例一份配置，见 PRD 8.1 各键默认值：保留 1 天、02:00、schema_only=true、notify type=log 等）；
  - `requirements.txt`（空文件，注明 v1 纯标准库）；`.gitignore`（`logs/`、`*.sql.gz`、`__pycache__/`、`.pwd`）。
- **验收**：目录树与计划一致；`python --version` 显示 3.13.x；`python -m unittest discover -s tests -v` 可运行（0 用例通过即算过）。
- **不回归约束**：暂无前序代码；本步骤不产生业务逻辑。

### 步骤 02：配置加载与校验

> **当前进度**：`infrastructure/config_loader.py` 已实现 TOML 加载、强类型值对象、默认值填充、枚举/路径/标识符校验、备份范围语义校验、密码环境变量解析和 `safe_summary()` 脱敏摘要。计划内 `tests/unit/test_config_loader.py` 已交付，覆盖合法/非法配置、默认值、枚举、备份范围语义、密码脱敏和多实例加载等场景；当前 `python -m unittest discover -s tests -v` 共 15 个用例全部通过。

- **目标**：读取 TOML + 环境变量，输出强类型配置对象，错误信息清晰。
- **新增**：`infrastructure/config_loader.py`，`tests/unit/test_config_loader.py`。
- **建议接口**：
  - `load_config(path: str | Path, env: Mapping[str, str] | None = None) -> AppConfig`
  - `AppConfig` 各区块：`mysql`（host/port/user/password_env）、`backup`（dest_dir/databases/exclude_databases/mysqldump_path/compress/schema_only/retry_times/lock_wait_timeout）、`retention`（days=1/weekly=0/monthly=0/enabled）、`schedule`（time=“02:00”/timezone）、`verify`（level/shadow_db_prefix/sample_tables）、`notify`（enabled/on_success/on_failure/type）、`log`（level/dir/max_bytes/backup_count）。
  - 密码从 `password_env` 指定环境变量读取，**禁止**打印/写入日志；缺省必填项、非法枚举值时抛 `ConfigError`（含文件/键名）。
- **验收**：单测覆盖：合法配置、缺必填项、非法枚举（compress/level/notify.type）、密码来自环境变量、`databases=["all"]` 与列表两种形态、多实例 = 每实例一份配置文件（loader 本身单配置）。
- **不回归约束**：仅新增，不触碰其他模块。

### 步骤 03：日志与脱敏

> **当前进度**：`logging_utils.py` 和 `test_logging_utils.py` 已交付。支持字符串/数值日志级别、目录自动创建、POSIX 目录权限尽力而为、按大小轮转、统一时间格式、run_id 注入；脱敏支持已知值、命令行密码选项、URL userinfo 和常见 query token。当前 30 个单元测试全部通过。

- **目标**：全项目统一日志（结构化字段、run_id、轮转）与敏感信息脱敏工具。
- **新增**：`infrastructure/logging_utils.py`，`tests/unit/test_logging_utils.py`。
- **建议接口**：
  - `setup_logging(level, log_dir, max_bytes, backup_count, run_id) -> logging.Logger`
  - `redact(text: str, secrets: Sequence[str]) -> str`：把密码/URL token 替换为 `***`；`redact_command(argv) -> str` 供命令回显。
  - 日志格式：`2026-08-10 02:00:00 INFO [run_id=...] [db=db1] ...`。
- **验收**：单测验证脱敏（密码、webhook token 不出现在输出中）、轮转参数生效、run_id 贯穿。
- **不回归约束**：不修改 02 的配置加载；日志工具对所有后续步骤开放。

### 步骤 04：领域模型、领域事件与仓库接口

> **当前进度**：值对象（DbName、BackupTime、FileName、Compression、Availability、ExitCode、DumpResult 等）、单库任务重试状态机、备份产物校验/删除/恢复门禁、BackupRun 聚合状态机、七个领域事件和 Repository/DumpExecutor/MySqlGateway/ArtifactStorage/Compressor/Notifier/Clock 端口已交付；52 个单元测试全部通过。

- **目标**：实现 DDD 领域核心（纯规则、零 IO、零 subprocess、时间用 Clock 注入），这是全项目"不被改坏"的地基。
- **新增**：`domain/model/value_objects/value_objects.py`、`domain/model/aggregates/backup_run.py`（聚合根）、`domain/model/entities/database_backup_task.py`、`domain/model/entities/backup_artifact.py`、`domain/events.py`、`domain/repositories.py`，`tests/unit/domain/*`。
- **建议接口**：
  - 值对象：`DbName`、`BackupTime`、`FileName`（命名规则 `{db}_{YYYYMMDD}_{HHMMSS}.sql.gz` / `{db}_schema_...`）、`Compression(GZIP/ZSTD/NONE)`、`BackupScope(ALL/LIST/TABLES)`、`RetentionTier(DAILY/WEEKLY/MONTHLY)`、`VerificationLevel(L0/L1/L2)`、`Availability(Available/Unavailable/PendingVerify)`、`ExitCode(0/1/2)`、`Sha256`、`SizeBytes`、`DumpResult`；
  - 实体/聚合：`BackupRun`（start/finish/mark_task_result/计算整体状态与退出码）、`DatabaseBackupTask`（retry、attempts）、`BackupArtifact`（verify、mark_deleted、可用性门禁）；
  - 事件：`BackupRunStarted`、`DatabaseBackupSucceeded`、`DatabaseBackupFailed`、`BackupRunCompleted`、`VerificationFailed`、`ArtifactDeleted`、`DiskSpaceLow`；
  - 端口：`BackupRunRepository`（save/find）、`DumpExecutor`、`MySqlGateway`、`ArtifactStorage`、`Compressor`、`Notifier`、`Clock`。
- **验收**：单测覆盖聚合状态机（SUCCESS/PARTIAL/FAILED 与退出码 0/1/2）、重试边界、命名规则、可用性门禁；**不允许** import subprocess/os/网络。
- **不回归约束**：本步骤之后，领域接口为该系统的公共契约；后续只能兼容性演进。

### 步骤 05：领域服务

> **当前进度**：`BackupExecutionService`（遍历任务 → DumpExecutor 端口 → 更新任务状态 → 失败重试 → 聚合完成，部分失败隔离）、`RetentionService.plan -> CleanupPlan`（日备过期 + 周/月标记保护，纯函数无 IO）、`VerificationService`（注入 L0/L1/L2 校验器 → 编排 → 映射 Availability）已交付；`tests/unit/domain/test_services.py` 16 个用例覆盖部分失败隔离、重试次数、CleanupPlan 计算、校验结果到 Availability 映射；当前 `python -m unittest discover -s tests -v` 共 68 个用例全部通过。

- **目标**：备份执行编排、保留策略纯函数、校验编排——全部纯领域逻辑。
- **新增**：`domain/services/backup_execution.py`、`retention.py`、`verification.py`，`tests/unit/domain/test_services.py`。
- **建议接口**：
  - `BackupExecutionService`：遍历任务 → 通过 `DumpExecutor` 端口 dump → 按 `DumpResult` 更新任务状态 → 失败重试（默认 1 次）→ 聚合完成；
  - `RetentionService.plan(days, weekly, monthly, artifacts) -> CleanupPlan`（纯函数，无 IO；周/月备默认关闭；保护标记文件）；
  - `VerificationService.verify(artifact, level, mysql_gateway, storage) -> VerificationResult`（L0/L1/L2 编排，具体校验由基础设施实现注入）。
- **验收**：单测覆盖：部分失败隔离（单库失败其余继续）、重试次数、CleanupPlan 计算、校验结果到 Availability 的映射。
- **不回归约束**：不修改 04 领域模型；服务只依赖领域接口。

### 步骤 06：时钟、运行锁、文件存储、压缩适配

> **当前进度**：`SystemClock`（Clock 端口）、`RunLock`（锁文件 O_CREAT|O_EXCL 互斥、lock_wait_timeout 等待/跳过）、`LocalFileStorage`（写流/读/删/列目录，双重防越界 + resolve 兜底，0600/0700 Windows 尽力而为）、`GzipCompressor/NoopCompressor`（zstd 预留）已交付；`tests/unit/infrastructure/*` 19 个用例覆盖锁重入/超时跳过、删除越界拦截、压缩可解压且非空、0600 权限（Windows 跳过）；当前 `python -m unittest discover -s tests -v` 共 87 个用例全部通过。

- **目标**：把领域端口落地为基础设施实现（真实 IO 全部在此层）。
- **新增**：`infrastructure/system_clock.py`、`infrastructure/run_lock.py`、`infrastructure/file_storage.py`、`infrastructure/compressor.py`，`tests/unit/infrastructure/*`。
- **建议接口**：
  - `SystemClock`（注入测试用 `FakeClock`）；
  - `RunLock`：锁文件获取/释放/超时（`lock_wait_timeout`，0=直接跳过），并发保护（FR-14）；
  - `LocalFileStorage`：写流、列目录、按名删除（删除仅限 dest_dir 内、路径规范化校验）、文件权限 0600、目录 0700（Windows 尽力而为）；
  - `GzipCompressor / ZstdCompressor / NoopCompressor`：流式压缩，产出不落盘未压缩中间文件。
- **验收**：单测：锁的重入/超时跳过、删除路径越界拦截、压缩产物可解压且非空、0600 权限（Windows 跳过）。
- **不回归约束**：不修改 domain；测试用临时目录。

### 步骤 07：MySQL 防腐层与网关

- **目标**：封装 mysqldump / mysql CLI（外部工具唯一接触点），翻译为领域对象。

> **当前进度**：`infrastructure/mysqldump_client.py`、`mysql_client.py` 已实现并通过 mock 子进程单测；当前 `python -m unittest discover -s tests -v` 共 99 个用例，98 通过、1 个 Windows 权限用例跳过。

- **新增**：`infrastructure/mysqldump_client.py`、`infrastructure/mysql_client.py`，`tests/unit/infrastructure/test_mysql_*.py`（mock 子进程）。
- **建议接口**：
  - `MysqldumpClient`（ACL）：按版本生成参数（MySQL 8.0：`--set-gtid-purged=OFF`；`--single-transaction --quick --routines --triggers --events --databases db`；schema_only 时加 `--no-data`），流式管道 `mysqldump ... | gzip > xxx.sql.gz`，捕获 stderr（脱敏）、退出码，翻译 `DumpResult` / 抛 `DumpFailed`；
  - `MysqlCliClient`（MySqlGateway）：`list_databases()`（排除系统库）、`count_tables(db)`、`restore(file_or_sql, db, one_database/仅结构)`、影子库创建/DROP/行数比对。
- **验收**：mock 子进程断言命令参数正确（8.0 参数、schema_only、retry）、stderr 脱敏、真实调用失败映射为领域异常；本地无 mysqldump 也能全绿。
- **不回归约束**：适配器只实现领域端口，不反向依赖触发/应用层。

### 步骤 08：manifest 仓库与 L0/L1 校验实现

> **当前进度**：`JsonManifestRepository` 已支持按日期落盘 manifest/status、原子写入、损坏文件校验；`FileIntegrityVerifier` 与 `StructureVerifier` 已实现 L0/L1 校验；当前共 115 个用例，114 个通过、1 个 Windows 权限用例跳过。

- **目标**：备份清单落盘（可审计）+ L0 文件级 / L1 结构级校验。
- **新增**：`infrastructure/manifest_repository.py`、`infrastructure/verifiers.py`，`tests/unit/infrastructure/*`。
- **建议接口**：
  - `JsonManifestRepository`（BackupRunRepository）：`manifest_YYYYMMDD.json`（run_id、起止时间、库列表：db/file/size/sha256/tables_count/status/retried/elapsed、result、exit_code）、`status_YYYYMMDD.json`；读取时校验结构；
  - L0：文件非空、`gzip -t` 合法（或不依赖外部命令的 gzip 头/尾校验）、解压尾部含 `Dump completed`；
  - L1：解压统计 `CREATE TABLE` 数量与 `MySqlGateway.count_tables` 比对。
- **验收**：单测：manifest 往返一致、损坏 json 报错、L0 各失败分支、L1 数量不一致标记 Availability=Unavailable。
- **不回归约束**：不修改 domain 服务；如领域服务接口不足，走兼容性演进（见总览约束 2）。

### 步骤 09：通知适配

> **当前进度**：`LogNotifier`、`SmtpNotifier`、`WebhookNotifier`、`NoopNotifier` 与 `create_notifier` 已实现；SMTP/Webhook 均支持注入传输并确认失败只记日志不改变退出码；当前共 124 个用例，123 个通过、1 个 Windows 权限用例跳过。

- **目标**：告警通道（v1 默认日志兜底；QQ 邮箱 SMTP 与 Webhook 预留，v2 启用）。
- **新增**：`infrastructure/notifiers.py`，`tests/unit/infrastructure/test_notifiers.py`。
- **建议接口**：
  - `LogNotifier`（v1 默认）：事件 → 结构化日志/告警事件；
  - `SmtpNotifier`（预留，QQ 邮箱：smtp.qq.com:465、授权码）与 `WebhookNotifier`（预留）实现同一 `Notifier` 端口，v1 不接线（config `notify.type="log"`）；
  - 通知失败仅记日志，不影响退出码。
- **验收**：单测：LogNotifier 输出事件与脱敏；SmtpNotifier 在不发真邮件的条件下（注入 transport mock）可测。
- **不回归约束**：仅新增；不改变领域事件定义。

### 步骤 10：触发层命令处理器

- **目标**：把外部触发（CLI/调度器）翻译为用例调用——PRD 7.3 触发层落位。
- **新增**：`trigger/run_backup.py`（`RunBackupCommandHandler`）、`trigger/restore_backup.py`（`RestoreBackupCommandHandler`）、`trigger/cleanup.py`（`CleanupCommandHandler`），`tests/unit/trigger/*`。
- **建议接口**：
  - `RunBackupCommandHandler.execute(config_path) -> int`：加载配置 → 预检（RunLock、磁盘空间 FR-15、mysqldump 可用）→ 枚举库 → 领域服务执行 → L0/L1/L2 校验 → manifest 落盘 → 保留清理 → 通知 → 返回退出码 0/1/2；
  - `RestoreBackupCommandHandler.execute(config_path, db, file=None, mode=full|db|schema, to_host=...) -> int`：按 Availability 可用性门禁拒绝不可用产物；全库/单库/仅表结构；
  - `CleanupCommandHandler.execute(config_path) -> int`：仅执行保留策略清理。
- **验收**：单测用 mock 适配器走完整链路：全成功→0、单库失败→1、全失败→2、磁盘不足拒绝、锁冲突跳过。
- **不回归约束**：处理器是"装配者"，不改领域与基础设施已验收实现。

### 步骤 11：应用层入口（main.py / cli.py）

- **目标**：命令行入口与依赖装配（PRD 7.3 应用层落位）。
- **新增**：`application/cli.py`、`application/main.py`，`tests/unit/application/test_cli.py`。
- **建议接口**：
  - `python application/main.py backup --config configs/instance-a.toml`
  - `python application/main.py restore --config ... --db db1 [--file ...] [--mode full|db|schema]`
  - `python application/main.py cleanup --config ...`；`--help` 输出子命令说明；
  - 返回码 0/1/2 透传；未知参数/异常 → 2；
  - 装配：load_config → logging_utils → 各适配器 → 触发层 Handler → 执行。
- **验收**：单测覆盖参数解析、退出码映射；手工 `python application/main.py --help` 正常。
- **不回归约束**：入口薄，不承载业务规则；不改 10 的处理器接口。

### 步骤 12：集成测试与部署脚本

- **目标**：mock 命令的端到端测试 + 可上架部署脚本 + README。
- **新增**：`tests/integration/test_end_to_end.py`（用 fake mysqldump/mysql 可执行文件或 mock）、`scripts/install_cron.sh`、`scripts/install_systemd.sh`、`scripts/install_task.ps1`（cron/systemd/timer 02:00，指向 `application/main.py` backup）、`scripts/restore.sh`、`README.md`（快速开始/部署/恢复/FAQ）。
- **验收**：集成测试覆盖：一次完整备份（生成 .sql.gz + schema 文件 + manifest）、保留清理（制造过期文件后被删除）、恢复演练（影子库比对）；脚本为示例可执行。
- **不回归约束**：测试与脚本只调用已验收的公开入口。

### 步骤 13：真实环境部署与演练（服务器侧）

- **目标**：在 Linux 生产环境完成部署与验证（本地 Windows 仅开发/测试）。
- **动作**：确认 mysqldump 版本与 8.0 参数 → 按 PRD 9.4 创建最小权限备份账号 → 部署产物与配置文件 → 安装 cron/systemd 定时任务（02:00，2 实例各一份配置）→ 首日手工执行与自动执行 → 目标盘空间实测（单日压缩备份体积，验证 5GB 是否够用）→ 全库/单库/仅结构恢复演练 → 输出演练报告。
- **验收**：AC-01~AC-10 逐项过（PRD 第 10 章）；磁盘不足时按 PRD 12 章对策处理（扩容/错峰）。
- **不回归约束**：部署步骤不修改代码模块；如发现缺陷，作为新步骤/修复对话处理并回归测试。

---

## 3. 全局验收与收尾

- 每步结束：`python -m unittest discover -s tests -v` 全绿；`git status`（如启用版本管理）只应出现本步骤新增/预期变更文件。
- 全部完成后：README 可依据 → 部署 → 3 个实例级配置跑通 → AC 清单勾选完毕 → PRD 升版 v1.0。
- 计划文件本身允许随时追加「补充步骤」（如：磁盘空间实测后新增容量对策、QQ 邮箱告警 v2 开发步骤），新增步骤按同样规则编号。
