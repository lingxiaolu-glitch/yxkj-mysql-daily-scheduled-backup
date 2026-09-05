"""mysql CLI 网关单元测试：mock 子进程，覆盖库/表枚举、影子库与恢复。"""

# 延迟类型注解。
from __future__ import annotations

# Path 定位仓库配置。
from pathlib import Path
# SimpleNamespace 构造假的 subprocess.run 返回对象。
from types import SimpleNamespace
# tempfile 提供临时 SQL 文件。
import tempfile
# unittest 提供测试类与断言。
import unittest
# mock 替换 subprocess.run。
from unittest import mock

# 导入被测模块。
from domain.model.value_objects import DbName
from infrastructure.config_loader import load_config
from infrastructure.mysql_client import MySqlCliError, MysqlCliClient


class MysqlCliClientTests(unittest.TestCase):
    """MysqlCliClient：枚举库、统计表、影子库、恢复、错误脱敏。"""

    def setUp(self) -> None:
        # 使用仓库自带的实例 A 配置（测试实例已提供）。
        repo_root = Path(__file__).resolve().parents[3]
        self.config = load_config(
            repo_root / "configs" / "instance-a.toml",
            env={"MYSQL_BACKUP_PASSWORD_A": "topsecret"},
        )
        # 被测网关。
        self.client = MysqlCliClient(self.config.mysql)

    @staticmethod
    def _ok(stdout: str = "", stderr: str = "") -> SimpleNamespace:
        # 构造成功的 subprocess.run 返回对象。
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)

    def test_list_databases_filters_system_databases(self) -> None:
        """枚举业务库：自动排除系统库，按行解析。"""

        # 模拟 SHOW DATABASES 输出（含系统库）。
        out = "information_schema\nshop\ncrm\nmysql\nperformance_schema\nsys\nuser\n"
        with mock.patch("subprocess.run", return_value=self._ok(stdout=out)) as run:
            databases = self.client.list_databases()

        # 只保留业务库且顺序稳定。
        self.assertEqual((DbName("shop"), DbName("crm"), DbName("user")), databases)

        # 命令参数正确。
        args = run.call_args.args[0]
        self.assertIn("-e", args)
        self.assertIn("SHOW DATABASES", args)
        self.assertIn("--password=topsecret", args)

    def test_count_tables_parses_integer(self) -> None:
        """统计表数量：解析第一行为整数。"""

        # 模拟返回 42。
        with mock.patch("subprocess.run", return_value=self._ok(stdout="42\n")) as run:
            count = self.client.count_tables(DbName("shop"))

        # 解析正确，SQL 带库名过滤。
        self.assertEqual(42, count)
        sql = run.call_args.args[0][-1]
        self.assertIn("information_schema.tables", sql)
        self.assertIn("table_schema = 'shop'", sql)

    def test_create_and_drop_shadow_database(self) -> None:
        """创建/删除影子库：SQL 幂等且库名反引号包裹。"""

        # 连续调用两次。
        with mock.patch("subprocess.run", return_value=self._ok()) as run:
            self.client.create_shadow_database(DbName("shop"), DbName("restore_check_shop"))
            self.client.drop_database(DbName("restore_check_shop"))

        # 提取两次调用的 SQL。
        sqls = [call.args[0][-1] for call in run.call_args_list]
        self.assertEqual("CREATE DATABASE IF NOT EXISTS `restore_check_shop`", sqls[0])
        self.assertEqual("DROP DATABASE IF EXISTS `restore_check_shop`", sqls[1])

    def test_count_table_rows_parses_integer(self) -> None:
        """统计表行数：解析为整数。"""

        # 模拟返回 123。
        with mock.patch("subprocess.run", return_value=self._ok(stdout="123\n")):
            self.assertEqual(123, self.client.count_table_rows(DbName("shop"), "orders"))

    def test_failure_raises_sanitized_error(self) -> None:
        """非零退出码：抛 MySqlCliError，且 stderr 中的密码被脱敏。"""

        # stderr 故意包含密码。
        failed = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Access denied using password topsecret",
        )
        with mock.patch("subprocess.run", return_value=failed):
            with self.assertRaises(MySqlCliError) as ctx:
                self.client.count_tables(DbName("shop"))

        # 密码不泄漏、占位符出现。
        self.assertNotIn("topsecret", str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_restore_with_one_database(self) -> None:
        """恢复：--one-database 位于目标库之前，stdin 传入 SQL 文件。"""

        # 先准备真实存在的 SQL 文件。
        with tempfile.TemporaryDirectory() as d:
            sql_file = Path(d) / "backup.sql"
            sql_file.write_text("CREATE TABLE t (id INT);", encoding="utf-8")

            # 模拟成功恢复。
            with mock.patch("subprocess.run", return_value=self._ok()) as run:
                self.client.restore(str(sql_file), DbName("shop"), one_database=True)

            # 参数形态与 stdin。
            argv = run.call_args.args[0]
            self.assertIn("--one-database", argv)
            self.assertEqual("shop", argv[-1])
            self.assertIn("stdin", run.call_args.kwargs)

    def test_restore_plain_target_database(self) -> None:
        """恢复（不带 --one-database）：目标库直接作为位置参数。"""

        # 先准备真实存在的 SQL 文件。
        with tempfile.TemporaryDirectory() as d:
            sql_file = Path(d) / "backup.sql"
            sql_file.write_text("CREATE TABLE t (id INT);", encoding="utf-8")

            # 模拟成功恢复。
            with mock.patch("subprocess.run", return_value=self._ok()) as run:
                self.client.restore(str(sql_file), DbName("shop"))

            # 无 --one-database，目标库在末尾。
            argv = run.call_args.args[0]
            self.assertNotIn("--one-database", argv)
            self.assertEqual("shop", argv[-1])


# 支持直接运行。
if __name__ == "__main__":
    unittest.main()
