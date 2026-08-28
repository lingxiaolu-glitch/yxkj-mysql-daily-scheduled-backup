"""步骤 03 日志与脱敏工具的单元测试。

验证目标：

- 轮转日志目录、文件和格式正确；
- run_id 贯穿每次输出；
- 日志级别和参数边界生效；
- 已知密码与常见 URL/Webhook 凭据会被脱敏；
- 常见 mysqldump/mysql 命令形态不会泄露密码。
"""

# 启用延迟类型注解，避免运行期解析较新的类型语法。
from __future__ import annotations

# 标准库 logging：读取日志级别、操作进程级 logger 和 handler。
import logging
# 正则模块：检查日志行格式、统计 run_id 出现次数。
import re
# 临时目录模块：为每个测试创建隔离的日志目录。
import tempfile
# 单元测试框架：提供断言、子测试和清理钩子。
import unittest
# 路径对象：拼接并检查临时目录、日志文件。
from pathlib import Path

# 导入被测模块的常量与公共 API。
from infrastructure.logging_utils import (
    LOGGER_NAME,        # 固定 logger 名称，用于测试后清理 handler。
    REDACTED,           # 统一脱敏占位符，应为 "***"。
    redact,             # 测试文本脱敏。
    redact_command,     # 测试命令行脱敏。
    setup_logging,      # 测试日志初始化、级别、轮转和 run_id。
)


class SetupLoggingTestCase(unittest.TestCase):
    """针对 setup_logging 的目录、格式、轮转和边界检查。"""

    def setUp(self) -> None:
        # 每个用例使用独立临时目录，避免不同测试写入同一份日志。
        self._temp_dir = tempfile.TemporaryDirectory()

        # 注册清理动作：测试结束时无论成功失败都删除临时目录。
        self.addCleanup(self._temp_dir.cleanup)

        # 保存临时目录的 Path 对象，后续测试用它拼接日志目录。
        self.root = Path(self._temp_dir.name)

        # setup_logging 操作进程级单例 logger；用例结束后彻底清空其处理器，
        # 防止下一个测试继续持有已被临时目录清理逻辑关闭的文件句柄。
        self.addCleanup(self._remove_handlers)

    def _remove_handlers(self) -> None:
        """清理备份 logger 上的所有 handler，保证测试之间互不影响。"""

        # 获取 setup_logging 使用的固定 logger。
        logger = logging.getLogger(LOGGER_NAME)

        # 先复制 handler 列表，再逐个移除并关闭，避免边遍历边修改。
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    def read_log_text(self, log_dir: Path) -> str:
        """读取指定目录下 backup.log 的完整文本。"""

        # 返回 UTF-8 解码后的日志内容，供各测试断言。
        return (log_dir / "backup.log").read_text(encoding="utf-8")

    def test_creates_directory_and_formats_structured_message(self) -> None:
        """验证自动建目录、UTF-8 写入和统一结构化日志格式。"""

        # 每个实例应有自己的日志目录。
        log_dir = self.root / "instance-a"

        # 字符串级别应转换成标准库数值级别；Path 目录可由函数自行创建。
        logger = setup_logging("INFO", log_dir, 1024 * 1024, 3, "run-20260827-001")

        # 写一条带业务上下文的消息，模拟备份开始事件。
        logger.info("[db=db1] dump started")

        # 目录必须已经被 setup_logging 自动创建。
        self.assertTrue(log_dir.is_dir())

        # 首条日志写入后，固定命名的日志文件必须存在。
        self.assertTrue((log_dir / "backup.log").is_file())

        # 读取第一行日志，检查时间、级别、run_id、业务字段和消息。
        line = self.read_log_text(log_dir).splitlines()[0]

        # 正则要求整行严格符合 PRD 中的结构化格式。
        self.assertRegex(
            line,
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO "
            r"\[run_id=run-20260827-001\] \[db=db1\] dump started$",
        )

    def test_accepts_numeric_level_and_string_log_dir(self) -> None:
        """验证标准库数字级别和字符串目录参数都被接受。"""

        # 使用多级目录，确认 parents=True 生效。
        log_dir = self.root / "logs" / "numeric"

        # 传入 logging.WARNING 数字级别和字符串路径。
        logger = setup_logging(logging.WARNING, str(log_dir), 2048, 1, "run-num")

        # WARNING 达到当前级别，应写入文件。
        logger.warning("disk low")

        # INFO 低于当前级别，不应写入文件。
        logger.info("too verbose")

        # 读取实际日志文本，验证级别过滤行为。
        text = self.read_log_text(log_dir)
        self.assertIn("disk low", text)
        self.assertNotIn("too verbose", text)

        # logger 自身级别必须保留为标准库 WARNING 数值。
        self.assertEqual(logging.WARNING, logger.level)

    def test_repeated_setup_replaces_previous_handlers(self) -> None:
        """验证重复初始化时替换旧 handler，而不是累计多个输出目标。"""

        # 准备两次初始化分别使用的日志目录。
        first_dir = self.root / "first"
        second_dir = self.root / "second"

        # 第一次初始化使用 INFO 级别，并先写入一条日志确保旧文件存在。
        first_logger = setup_logging("INFO", first_dir, 2048, 1, "run-first")
        first_logger.info("before reinstall")

        # 第一次配置后必须只有一个 handler。
        self.assertEqual(1, len(first_logger.handlers))

        # 第二次初始化应作用于同一个 logger，并替换第一次配置。
        second_logger = setup_logging("WARNING", second_dir, 4096, 2, "run-second")

        # 固定 logger 名称意味着二次调用必须替换旧配置，而不是累计多个 handler。
        self.assertIs(first_logger, second_logger)

        # 旧 handler 被替换后，仍然只能保留一个新 handler。
        self.assertEqual(1, len(second_logger.handlers))

        # 新级别应来自第二次初始化参数。
        self.assertEqual(logging.WARNING, second_logger.level)

        # 备份日志不应继续向 root logger 冒泡。
        self.assertFalse(second_logger.propagate)

        # 新消息只能进入第二次配置的日志目录。
        second_logger.warning("after reinstall")

        # 验证新日志确实写入新目录。
        self.assertIn("run-second", self.read_log_text(second_dir))

        # 如果旧 handler 未被移除且仍保持打开状态，同一条记录可能写回第一个目录。
        self.assertNotIn("after reinstall", self.read_log_text(first_dir))

    def test_rotating_handler_rolls_files_by_size(self) -> None:
        """验证达到 max_bytes 后会轮转，并遵守 backup_count。"""

        # 每个测试使用独立轮转目录。
        log_dir = self.root / "rotate"

        # 故意设置很小上限，确保无需准备大体积日志即可触发轮转。
        logger = setup_logging("DEBUG", log_dir, 96, 2, "run-rotation")

        # 写入 12 条较长日志，足以触发多次按大小轮转。
        for number in range(12):
            padding = "x" * 32
            logger.info(f"backup-entry-{number}-{padding}")

        # 超过上限后至少出现一个历史文件；轮转也必须遵守保留数量。
        self.assertTrue((log_dir / "backup.log.1").is_file())
        historical_files = list(log_dir.glob("backup.log.*"))
        self.assertLessEqual(len(historical_files), 2)

        # 活跃文件仍能继续接收最新数据。
        self.assertIn("backup-entry-11", self.read_log_text(log_dir))

    def test_run_id_is_injected_into_every_record(self) -> None:
        """验证同一次运行的每条日志都携带同一个 run_id。"""

        # 使用独立目录，避免读取其他用例输出。
        log_dir = self.root / "context"

        # 初始化为 DEBUG，确保所有测试事件都会写入。
        logger = setup_logging("DEBUG", log_dir, 8192, 7, "multi-line-run")

        # 连续写四条不同事件，模拟同一 run 的多个日志点。
        for number in range(4):
            logger.info(f"event-{number}")

        # 读取全部日志文本。
        text = self.read_log_text(log_dir)

        # run_id 必须精确出现四次，一次对应一条日志。
        occurrences = re.findall(r"\[run_id=multi-line-run\]", text)
        self.assertEqual(4, len(occurrences))

    def test_invalid_arguments_are_rejected(self) -> None:
        """验证级别解析和轮转参数边界校验。"""

        # 无效参数用例不需要真正写日志，但仍准备一个目录参数。
        valid_dir = self.root / "invalid-cases"

        # 每项是：setup_logging 参数 + 预期错误消息片段。
        cases = [
            (("BAD_LEVEL", valid_dir, 1024, 1, "run-1"), "非法日志级别"),
            (("INFO", valid_dir, 0, 1, "run-1"), "max_bytes 必须为正整数"),
            (("INFO", valid_dir, -1, 1, "run-1"), "max_bytes 必须为正整数"),
            (("INFO", valid_dir, 1024, -1, "run-1"), "backup_count 不能为负数"),
            (("INFO", valid_dir, 1024, 1, ""), "run_id 不能为空"),
            (("INFO", valid_dir, 1024, 1, "   "), "run_id 不能为空"),
        ]

        # 逐个调用非法参数组合；subTest 让单个失败不影响其他参数组合的报告。
        for arguments, expected_message in cases:
            with self.subTest(arguments=arguments):

                # 该参数组合必须抛出 ValueError。
                with self.assertRaises(ValueError) as ctx:
                    setup_logging(*arguments)

                # 异常文本必须包含明确原因，便于调用方定位配置错误。
                self.assertIn(expected_message, str(ctx.exception))


class RedactionTestCase(unittest.TestCase):
    """redact 与 redact_command 的敏感值遮蔽行为。"""

    def test_redacts_known_secrets_and_prefers_longest_match(self) -> None:
        """验证显式敏感值会被替换，并且长值优先于短前缀。"""

        # 如果短字符串先替换，会把长密码切成“***123456”，导致剩余部分仍泄露。
        self.assertEqual(
            f"login {REDACTED} failed",
            redact("login password123456 failed", ["password", "password123456"]),
        )

        # 空字符串和纯空白值没有脱敏意义，也可能造成替换行为异常。
        self.assertEqual("plain text", redact("plain text", ["", "   "]))

    def test_redacts_url_userinfo_but_keeps_username(self) -> None:
        """验证 URL 中只隐藏密码，保留协议和用户名便于排查。"""

        # 构造带 userinfo 密码的 MySQL URL。
        actual = redact(
            "mysql://backup_user:S3cret@db.example.test:3306/app",
            [],
        )

        # 期望密码变成 ***，但 backup_user 仍可见。
        self.assertEqual(
            f"mysql://backup_user:{REDACTED}@db.example.test:3306/app",
            actual,
        )

        # 原密码明文必须完全消失。
        self.assertNotIn("S3cret", actual)

    def test_redacts_webhook_query_tokens_case_insensitively(self) -> None:
        """验证 URL query 中的 Token/refresh_token 都会被遮蔽。"""

        # 同一 URL 包含普通 token 和 refresh_token，并保留一个无关参数。
        url = "https://qyapi.example/hook?Token=Aaa111&refresh_token=Bbb222&plain=value"

        # 不提供显式 secrets，只依赖常见 query 参数识别规则。
        actual = redact(url, [])

        # 参数名和分隔符应保留，值应替换为 ***。
        expected = (
            f"https://qyapi.example/hook?Token={REDACTED}"
            f"&refresh_token={REDACTED}&plain=value"
        )

        # 整个 URL 的脱敏结果必须精确匹配。
        self.assertEqual(expected, actual)

        # 两个原始 Token 值都不能留在日志文本中。
        self.assertNotIn("Aaa111", actual)
        self.assertNotIn("Bbb222", actual)

    def test_empty_text_returns_empty_result(self) -> None:
        """验证空文本短路返回，不进行无意义脱敏处理。"""

        self.assertEqual("", redact("", ["password"]))

    def test_redacts_compact_mysql_password_option(self) -> None:
        """验证 mysqldump 的 -pSecret 紧凑密码选项。"""

        # 模拟常见 mysqldump 命令，密码紧跟 -p。
        actual = redact_command(["mysqldump", "-pS3cret!", "--databases", "shop"])

        # 密码值必须替换，只保留 -p 前缀。
        self.assertIn("-p***", actual)
        self.assertNotIn("S3cret!", actual)

        # 普通选项和库名应保留，证明只脱敏密码参数。
        self.assertIn("--databases shop", actual)

    def test_redacts_separate_long_password_value(self) -> None:
        """验证 --password 和下一个独立参数形式的密码值。"""

        # 模拟密码作为独立 argv 项传递的形态。
        actual = redact_command(
            ["mysqldump", "--password", "TopSecret", "--databases", "shop"]
        )

        # 选项名应保留，密码值必须替换为 ***。
        self.assertIn("--password", actual)
        self.assertIn("***", actual)
        self.assertNotIn("TopSecret", actual)

    def test_redacts_long_password_value_after_equal_sign(self) -> None:
        """验证 --password-file=/path 的等号值形态。"""

        # 模拟带敏感文件路径的长选项。
        actual = redact_command(
            ["mysqldump", "--password-file=/secure/password.txt", "shop"]
        )

        # 等号后的敏感路径必须替换，同时保留选项名。
        self.assertIn("--password-file=***", actual)
        self.assertNotIn("/secure/password.txt", actual)

    def test_trailing_standalone_password_option_does_not_crash(self) -> None:
        """验证独立 --password 出现在 argv 末尾时不越界。"""

        # 此时没有下一个参数可遮蔽，函数必须安全返回。
        actual = redact_command(["mysql", "--password"])

        # 选项名仍应保留在安全命令文本中。
        self.assertIn("--password", actual)

    def test_explicit_secrets_override_all_command_positions(self) -> None:
        """验证调用方显式传入的 secrets 会在最终命令文本中兜底替换。"""

        # 命令本身不使用 password 参数，但消息里包含两个已知敏感词。
        command = [
            "mysql",
            "--host=db",
            "--user=backup_user",
            "--execute=SELECT 1; -- p@ss token",
        ]

        # 将两个字符串显式声明为敏感值。
        actual = redact_command(command, ["p@ss", "token"])

        # 明文敏感词都不能出现在日志回显中。
        self.assertNotIn("p@ss", actual)
        self.assertNotIn("token", actual)

        # 至少应出现统一脱敏占位符。
        self.assertIn(f"{REDACTED}", actual)


# 支持直接运行该测试文件：python tests/unit/test_logging_utils.py。
if __name__ == "__main__":
    unittest.main()
