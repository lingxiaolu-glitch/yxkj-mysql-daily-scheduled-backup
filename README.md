# mysql-daily-scheduled-backup

MySQL 每日定时备份系统：全量逻辑备份（mysqldump）+ 校验 + 保留清理 + 恢复脚本。

- 需求与设计：见 [docx/PRD.md](docx/PRD.md)（v0.5，第 13 章评审结论已确认）
- 开发计划：见 [docx/PLAN.md](docx/PLAN.md)（分步实施，每步一个新对话，逐步增量、不破坏前序逻辑）

## 目录结构

```text
mysql-daily-scheduled-backup/
├── configs/                # 每实例一份配置（instance-a/instance-b）+ .env 凭据（.env 已 gitignore）
├── requirements.txt         # v1 为空（纯标准库），预留
├── docx/
│   ├── PRD.md               # 产品需求文档
│   └── PLAN.md              # 开发计划
├── application/             # 应用层：main.py / cli.py（步骤 11）
├── trigger/                 # 触发层：RunBackupCommandHandler 等（步骤 10）
├── domain/
│   ├── events.py            # 步骤 04：领域事件
│   ├── repositories.py      # 步骤 04：仓库/网关/时钟等端口
│   ├── model/
│   │   ├── aggregates/              # 步骤 04：聚合根
│   │   │   └── backup_run.py        # 备份运行聚合根
│   │   ├── entities/                # 步骤 04：实体对象
│   │   │   ├── backup_artifact.py   # 备份产物与恢复门禁
│   │   │   └── database_backup_task.py # 单库任务与重试
│   │   └── value_objects/           # 步骤 04：值对象
│   │       └── value_objects.py     # 领域值对象
│   └── services/                    # 步骤 05：领域服务
│       ├── backup_execution.py      # 备份执行编排（DumpExecutor + 重试）
│       ├── retention.py             # 保留策略纯函数（CleanupPlan）
│       └── verification.py          # L0/L1/L2 校验编排
├── infrastructure/
│   ├── config_loader.py     # 步骤 02：TOML + 环境变量 → 强类型 AppConfig
│   ├── logging_utils.py     # 步骤 03：run_id 日志、轮转与脱敏
│   ├── system_clock.py      # 步骤 06：真实时钟（Clock 端口）
│   ├── run_lock.py          # 步骤 06：运行锁（并发保护 FR-14）
│   ├── file_storage.py      # 步骤 06：本地文件存储（ArtifactStorage 端口）
│   └── compressor.py        # 步骤 06：gzip/noop 压缩（zstd 预留）
├── scripts/                 # 运行包装脚本（依赖 application/main.py，入口在步骤 11 实现）
└── tests/
    └── unit/
        ├── test_config_loader.py  # 配置加载单元测试
        ├── test_logging_utils.py  # 日志与脱敏单元测试
        ├── domain/
        │   ├── test_models.py     # 领域模型、状态机与事件测试
        │   └── test_services.py   # 领域服务（执行/保留/校验）测试
        └── infrastructure/        # 步骤 06：基础设施单元测试
            ├── test_system_clock.py  # 时钟适配器测试
            ├── test_run_lock.py      # 运行锁测试
            ├── test_file_storage.py  # 文件存储测试
            └── test_compressor.py    # 压缩适配器测试
```

> 当前状态：
>
> - 步骤 01 项目骨架已完成；配置拆分为 `configs/instance-a.toml`、`configs/instance-b.toml` 和被 git 忽略的 `configs/.env`。
> - 步骤 02 配置加载器 `infrastructure/config_loader.py` 已实现：TOML 解析、默认值、类型/枚举校验、备份范围语义校验、密码环境变量解析和脱敏摘要。
> - 步骤 02 单元测试已补齐。
> - 步骤 03 日志与脱敏工具已实现：run_id 贯穿、按大小轮转、目录/日志权限、已知密码遮蔽、命令行凭据遮蔽和常见 URL 凭据脱敏。
> - 步骤 04 领域核心已实现：文件命名值对象、单库任务重试状态机、备份产物恢复门禁、运行聚合成功/部分失败/全部失败退出码、领域事件和端口契约。
> - 步骤 05 领域服务已实现：备份执行编排（DumpExecutor 端口 + 失败重试 + 部分失败隔离）、保留策略纯函数（日/周/月 → CleanupPlan）、校验编排（校验器注入 → Availability 映射）。
> - 步骤 06 基础设施适配已实现：SystemClock（Clock 端口）、RunLock（锁文件并发保护）、LocalFileStorage（写流/列目录/防越界删除、0600/0700）、Gzip/Noop 压缩（zstd 预留），配套 19 个基础设施单测。
> - 步骤 07 MySQL 防腐层与网关已实现：MysqldumpClient 按 MySQL 8.0 参数流式转储并写存储、脱敏错误、映射 DumpResult，schema_only=true 时额外生成 `{db}_schema_*.sql.gz`；MysqlCliClient 提供枚举业务库、表数统计、影子库操作和恢复导入，配套 12 个单测。
> - 步骤 08 manifest 仓库与 L0/L1 校验已实现：按日期保存完整运行/产物/校验状态，支持多运行与损坏文件告警；L0 校验 gzip 完整性和 Dump completed 标记，L1 校验 CREATE TABLE 数量与源库或配置期望值一致，配套 16 个单测。
> - 步骤 09 通知适配已实现：LogNotifier 为 v1 默认日志通道，SmtpNotifier/WebhookNotifier 为可注入测试的预留通道，通知失败只记日志，配套 9 个单测。
> - 步骤 10 触发层已实现：RunBackupCommandHandler/ RestoreBackupCommandHandler/ CleanupCommandHandler，以及 Runtime 注入装配；完整备份现在额外生成 `{db}_schema_*.sql.gz`，支持并发锁、磁盘预检、L0/L1/L2 校验、manifest、保留清理和通知，配套 14 个触发层/校验单测。
> - 步骤 11 应用层入口已实现：`application/main.py` 支持 `backup`、`restore`、`cleanup` 三个子命令，返回码 0/1/2，未知参数返回 2，配套 6 个 CLI 单测。
> - 步骤 12 集成测试与部署脚本已实现：`tests/integration/test_end_to_end.py` 覆盖完整备份、gzip+schema、manifest、保留清理、恢复导入和 L2 影子库演练；提供 cron/systemd/Windows 计划任务安装脚本，配套 3 个端到端测试。
> - 当前 `python -m unittest discover -s tests -v` 共 147 个用例，146 个通过、1 个 Windows 权限用例跳过。

## 部署与运维

详细步骤见 [docx/DEPLOYMENT.md](docx/DEPLOYMENT.md)，上线演练记录模板见 [docx/DRILL_REPORT_TEMPLATE.md](docx/DRILL_REPORT_TEMPLATE.md)。

## 使用 CLI

命令行入口已经可用：

```bash
# 执行备份
python application/main.py backup --config configs/instance-a.toml

# 恢复最近可用完整备份
python application/main.py restore --config configs/instance-a.toml --db shop --mode full

# 仅执行保留清理
python application/main.py cleanup --config configs/instance-a.toml
```

运行前请先把实例对应的环境变量写入 `configs/.env`（该文件已被 git 忽略），或在执行环境中直接设置变量。也可以在代码中直接加载配置查看脱敏摘要：

```python
from pathlib import Path
from infrastructure.config_loader import load_config

config = load_config(Path("configs/instance-a.toml"))
print(config.safe_summary())
```

`safe_summary()` 不包含数据库密码明文。

## 提交与凭据安全

提交前不要移除 `.gitignore` 中的 `.env` 规则。可以使用以下命令确认真实凭据文件未被跟踪：

```bash
git check-ignore -v configs/.env
```

如无输出，必须先修复忽略规则后再提交。
