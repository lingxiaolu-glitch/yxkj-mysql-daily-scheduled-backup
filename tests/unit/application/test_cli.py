"""应用层 CLI 单元测试：子命令分发与退出码。"""

# 延迟类型注解。
from __future__ import annotations

import contextlib
import io
import unittest

from application.cli import HandlerFactory, build_parser, main


class FakeHandler:
    """记录调用参数的假处理器。"""

    def __init__(self, result: int = 0) -> None:
        self.result = result
        self.kwargs = None

    def execute(self, *args, **kwargs):
        self.kwargs = kwargs
        return self.result


class CliTests(unittest.TestCase):
    def test_backup_passes_config_and_returns_handler_code(self) -> None:
        """backup 子命令把参数交给处理器并透传退出码。"""
        handler = FakeHandler(3)
        seen = {}

        def factory(args):
            seen["args"] = args
            return handler

        result = main(["backup", "--config", "instance-a.toml"], handler_factory=factory)
        self.assertEqual(3, result)
        self.assertEqual("instance-a.toml", seen["args"].config)
        self.assertEqual({}, handler.kwargs or {})

    def test_restore_passes_db_file_mode_and_to_host(self) -> None:
        """restore 子命令透传恢复参数。"""
        handler = FakeHandler(2)

        def factory(args):
            return handler

        result = main(
            [
                "restore",
                "--config", "instance-a.toml",
                "--db", "shop",
                "--file", "20260905/shop.sql",
                "--mode", "schema",
                "--to-host", "127.0.0.1",
                "--to-db", "shop_restore",
            ],
            handler_factory=factory,
        )
        self.assertEqual(2, result)
        self.assertEqual("shop", handler.kwargs["db"])
        self.assertEqual("20260905/shop.sql", handler.kwargs["file"])
        self.assertEqual("schema", handler.kwargs["mode"])
        self.assertEqual("127.0.0.1", handler.kwargs["to_host"])
        self.assertEqual("shop_restore", handler.kwargs["to_db"])

    def test_cleanup_passes_config(self) -> None:
        """cleanup 子命令执行处理器。"""
        handler = FakeHandler(0)

        def factory(args):
            return handler

        result = main(["cleanup", "--config", "instance-b.toml"], handler_factory=factory)
        self.assertEqual(0, result)
        self.assertEqual({}, handler.kwargs or {})

    def test_unknown_command_returns_two(self) -> None:
        """未知子命令返回 2。"""
        with contextlib.redirect_stderr(io.StringIO()):
            result = main(["unknown"])
        self.assertEqual(2, result)

    def test_invalid_mode_returns_two(self) -> None:
        """非法 --mode 被 argparse 拒绝并返回 2。"""
        with contextlib.redirect_stderr(io.StringIO()):
            result = main(
                ["restore", "--config", "c.toml", "--db", "shop", "--mode", "bad"]
            )
        self.assertEqual(2, result)

    def test_default_factory_creates_known_handlers(self) -> None:
        """默认工厂能识别备份/恢复/清理子命令。"""
        factory = HandlerFactory(env={}).create
        self.assertIsNotNone(factory(build_parser().parse_args(["backup", "--config", "c.toml"])))
        self.assertIsNotNone(factory(build_parser().parse_args(["cleanup", "--config", "c.toml"])))


if __name__ == "__main__":
    unittest.main()