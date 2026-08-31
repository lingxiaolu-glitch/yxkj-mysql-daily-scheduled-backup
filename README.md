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
│   └── model/
│       ├── aggregates/              # 步骤 04：聚合根
│       │   └── backup_run.py        # 备份运行聚合根
│       ├── entities/                # 步骤 04：实体对象
│       │   ├── backup_artifact.py   # 备份产物与恢复门禁
│       │   └── database_backup_task.py # 单库任务与重试
│       └── value_objects/           # 步骤 04：值对象
│           └── value_objects.py     # 领域值对象
├── infrastructure/
│   ├── config_loader.py     # 步骤 02：TOML + 环境变量 → 强类型 AppConfig
│   └── logging_utils.py     # 步骤 03：run_id 日志、轮转与脱敏
├── scripts/                 # 运行包装脚本（依赖 application/main.py，入口在步骤 11 实现）
└── tests/
    └── unit/
        ├── test_config_loader.py  # 配置加载单元测试
        ├── test_logging_utils.py  # 日志与脱敏单元测试
        └── domain/
            └── test_models.py     # 领域模型、状态机与事件测试
```

> 当前状态：
>
> - 步骤 01 项目骨架已完成；配置拆分为 `configs/instance-a.toml`、`configs/instance-b.toml` 和被 git 忽略的 `configs/.env`。
> - 步骤 02 配置加载器 `infrastructure/config_loader.py` 已实现：TOML 解析、默认值、类型/枚举校验、备份范围语义校验、密码环境变量解析和脱敏摘要。
> - 步骤 02 单元测试已补齐。
> - 步骤 03 日志与脱敏工具已实现：run_id 贯穿、按大小轮转、目录/日志权限、已知密码遮蔽、命令行凭据遮蔽和常见 URL 凭据脱敏。
> - 步骤 04 领域核心已实现：文件命名值对象、单库任务重试状态机、备份产物恢复门禁、运行聚合成功/部分失败/全部失败退出码、领域事件和端口契约。
> - 当前 `python -m unittest discover -s tests -v` 共 52 个用例全部通过。
> - 命令行入口（步骤 11）尚未实现，因此 `scripts/run_backup.*` 目前只是部署流程的预留包装脚本。

## 使用配置加载器

CLI 将在后续步骤提供。当前可先在代码或临时脚本中调用 loader：

```python
from pathlib import Path
from infrastructure.config_loader import load_config

config = load_config(Path("configs/instance-a.toml"))
print(config.safe_summary())
```

真实运行前，请先把实例对应的环境变量写入 `configs/.env`（该文件已被 git 忽略），或在执行环境中直接设置变量。`safe_summary()` 不包含数据库密码明文。

## 提交与凭据安全

提交前不要移除 `.gitignore` 中的 `.env` 规则。可以使用以下命令确认真实凭据文件未被跟踪：

```bash
git check-ignore -v configs/.env
```

如无输出，必须先修复忽略规则后再提交。
