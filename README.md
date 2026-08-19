# mysql-daily-scheduled-backup

MySQL 每日定时备份系统：全量逻辑备份（mysqldump）+ 校验 + 保留清理 + 恢复脚本。

- 需求与设计：见 [PRD.md](PRD.md)（v0.5，第 13 章评审结论已确认）
- 开发计划：见 [PLAN.md](PLAN.md)（分步实施，每步一个新对话，逐步增量、不破坏前序逻辑）

## 目录结构

```text
mysql-daily-scheduled-backup/
├── config.toml              # 默认配置模板（每实例一份）
├── requirements.txt         # v1 为空（纯标准库），预留
├── PRD.md                   # 产品需求文档
├── PLAN.md                  # 开发计划
├── application/             # 应用层：main.py / cli.py（步骤 11）
├── trigger/                 # 触发层：RunBackupCommandHandler 等（步骤 10）
├── domain/                  # 领域层：model / services / events / repositories（步骤 04-05）
├── infrastructure/          # 基础设施层：mysqldump/mysql/文件/压缩/通知等适配（步骤 02-03, 06-09）
├── scripts/                 # 部署与恢复脚本（步骤 12）
└── tests/                   # unit / integration（随步骤新增）
```

> 当前状态：步骤 01 项目骨架（目录 + 配置模板）已完成，业务代码按 [PLAN.md](PLAN.md) 逐步实现。
