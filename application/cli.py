"""命令行参数解析与入口分发。"""

# 延迟类型注解。
from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    """构建备份系统 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="mysql-daily-backup",
        description="MySQL 每日定时备份、恢复与保留清理",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # backup：执行一次备份。
    backup = subparsers.add_parser("backup", help="执行一次每日备份")
    backup.add_argument("--config", required=True, help="实例 TOML 配置文件路径")

    # restore：恢复到目标数据库。
    restore = subparsers.add_parser("restore", help="恢复最近可用备份")
    restore.add_argument("--config", required=True, help="实例 TOML 配置文件路径")
    restore.add_argument("--db", required=True, help="目标数据库名")
    restore.add_argument("--file", default=None, help="指定备份文件名或相对路径")
    restore.add_argument(
        "--mode",
        choices=("full", "db", "schema"),
        default="full",
        help="恢复模式：完整库/单库/仅表结构",
    )
    restore.add_argument("--to-host", default=None, help="v2 预留：恢复目标主机")

    # cleanup：只执行保留清理。
    cleanup = subparsers.add_parser("cleanup", help="执行保留策略清理")
    cleanup.add_argument("--config", required=True, help="实例 TOML 配置文件路径")

    return parser


class HandlerFactory:
    """CLI 到触发层处理器的默认工厂。"""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env

    def create(self, args: argparse.Namespace):
        """按子命令创建处理器。"""
        # 延迟导入，避免解析 --help 时加载全部依赖。
        if args.command == "backup":
            from trigger.run_backup import RunBackupCommandHandler
            return RunBackupCommandHandler(config_path=Path(args.config), env=self._env)
        if args.command == "restore":
            from trigger.restore_backup import RestoreBackupCommandHandler
            return RestoreBackupCommandHandler(config_path=Path(args.config), env=self._env)
        if args.command == "cleanup":
            from trigger.cleanup import CleanupCommandHandler
            return CleanupCommandHandler(config_path=Path(args.config), env=self._env)
        raise ValueError(f"未知子命令：{args.command}")


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    handler_factory: Callable[[argparse.Namespace], Any] | None = None,
) -> int:
    """CLI 入口：解析参数并把退出码透传给调度器。"""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse 对 --help 返回 0，对参数错误返回 2。
        return int(exc.code) if isinstance(exc.code, int) else 2

    factory = handler_factory or HandlerFactory(env).create
    try:
        handler = factory(args)
        if args.command == "backup":
            return int(handler.execute())
        if args.command == "restore":
            return int(
                handler.execute(
                    db=args.db,
                    file=args.file,
                    mode=args.mode,
                    to_host=args.to_host,
                )
            )
        return int(handler.execute())
    except Exception:
        # 所有未处理异常都映射为 PRD 约定的 2（系统失败）。
        return 2