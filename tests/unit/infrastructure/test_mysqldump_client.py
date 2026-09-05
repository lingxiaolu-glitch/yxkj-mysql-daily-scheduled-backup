"""mysqldump 防腐层单元测试：mock 子进程，断言命令参数/脱敏/失败映射。"""

# 延迟类型注解。
from __future__ import annotations

# io 构造假进程的字节流 stdout/stderr。
import io
# tempfile 提供临时存储目录。
import tempfile
# dataclasses.replace 复制配置并替换字段。
from dataclasses import replace
# datetime 构造固定测试时间。
from datetime import datetime, timedelta, timezone
# unittest 提供测试类与断言。
import unittest
# mock 替换 subprocess.Popen。
from unittest import mock

# 导入被测模块与依赖。
from domain.model.entities.database_backup_task import DatabaseBackupTask
from domain.model.value_objects import BackupTime, DbName
from infrastructure.compressor import NoopCompressor
from infrastructure.config_loader import load_config
from infrastructure.file_storage import LocalFileStorage
from infrastructure.mysqldump_client import DumpFailed, MysqldumpClient


class FakeProcess:
    """假的 subprocess.Popen 返回值：提供 stdout/stderr 字节流与退出码。"""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        # 保存输出字节流（BytesIO 支持 read(n)/read()）。
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        # 保存退出码。
        self.returncode = returncode

    def wait(self) -> int:
        # 直接返回预设退出码。
        return self.returncode


class FakeClock:
    """固定时间端口。"""

    def __init__(self, now: BackupTime) -> None:
        # 保存固定时间。
        self._now = now

    def now(self) -> BackupTime:
        # 每次返回同一个时间。
        return self._now


class MysqldumpClientTests(unittest.TestCase):
    """MysqldumpClient：命令组装、流式写入、结果翻译、失败清理。"""

    def setUp(self) -> None:
        # 固定 UTC+8 时间。
        self.zone = timezone(timedelta(hours=8))
        self.now = BackupTime(datetime(2026, 8, 10, 2, 0, 0, tzinfo=self.zone))

        # 使用仓库自带的实例 A 配置（测试实例已提供）。
        repo_root = __import__("pathlib").Path(__file__).resolve().parents[3]
        self.config = load_config(
            repo_root / "configs" / "instance-a.toml",
            env={"MYSQL_BACKUP_PASSWORD_A": "topsecret"},
        )

        # 临时存储目录 + 不压缩 + 固定时钟。
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = LocalFileStorage(__import__("pathlib").Path(self.tmp.name))
        self.compressor = NoopCompressor()
        self.clock = FakeClock(self.now)

        # 被测防腐层。
        self.client = MysqldumpClient(
            self.config.mysql,
            self.config.backup,
            self.storage,
            self.compressor,
            self.clock,
        )

    def tearDown(self) -> None:
        # 清理临时目录。
        self.tmp.cleanup()

    def task(self, name: str = "shop") -> DatabaseBackupTask:
        # 构造单库任务。
        return DatabaseBackupTask(db_name=DbName(name), retry_times=0)

    def test_dump_success_streams_to_storage_with_mysql80_args(self) -> None:
        """成功转储：命令含 MySQL 8.0 参数，输出写入存储，结果成功。"""

        # 假进程：stdout 有数据，退出码 0。
        fake = FakeProcess(stdout=b"CREATE TABLE t (id INT);\n", stderr=b"", returncode=0)
        with mock.patch("subprocess.Popen", return_value=fake) as popen:
            result = self.client.dump(self.task())

        # 结果翻译正确。
        self.assertTrue(result.success)
        self.assertEqual(0, result.return_code)
        self.assertEqual("", result.error_digest)

        # 命令参数完整（MySQL 8.0 规范）。
        argv = popen.call_args.args[0]
        self.assertEqual("mysqldump", argv[0])
        self.assertIn("--set-gtid-purged=OFF", argv)
        self.assertIn("--single-transaction", argv)
        self.assertIn("--quick", argv)
        self.assertIn("--routines", argv)
        self.assertIn("--triggers", argv)
        self.assertIn("--events", argv)
        self.assertIn("--databases", argv)
        self.assertIn("shop", argv)
        # 凭据与连接参数。
        self.assertIn(f"--host={self.config.mysql.host}", argv)
        self.assertIn(f"--port={self.config.mysql.port}", argv)
        self.assertIn(f"--user={self.config.mysql.user}", argv)
        self.assertNotIn("--password=topsecret", argv)
        self.assertEqual("topsecret", popen.call_args.kwargs["env"]["MYSQL_PWD"])

        # 输出确实写入了存储，且内容一致。
        self.assertIsNotNone(self.client.last_stored)
        self.assertTrue(self.storage.exists(self.client.last_stored.relative_path))
        self.assertEqual(
            b"CREATE TABLE t (id INT);\n",
            self.storage.read_bytes(self.client.last_stored.relative_path),
        )

    def test_schema_only_generates_full_and_schema_without_data(self) -> None:
        """schema_only 配置：先完整备份，再额外输出带 --no-data 的 schema 文件。"""

        # 实例 A schema_only=true；两次子进程都成功。
        fake = FakeProcess(stdout=b"x", returncode=0)
        with mock.patch("subprocess.Popen", return_value=fake) as popen:
            result = self.client.dump(self.task())

        self.assertTrue(result.success)
        # 第一次调用是完整备份，不能包含 --no-data。
        full_args = popen.call_args_list[0].args[0]
        self.assertNotIn("--no-data", full_args)
        self.assertIn("shop", full_args)

        # 第二次调用是额外 schema 文件，必须包含 --no-data。
        schema_args = popen.call_args_list[1].args[0]
        self.assertIn("--no-data", schema_args)
        self.assertIn("shop", schema_args)

        # 两个产物都落盘：完整文件无 schema 前缀，schema 文件有前缀。
        self.assertIn("shop_", self.client.last_stored.relative_path)
        self.assertIn("shop_schema_", self.client.last_schema_stored.relative_path)
        self.assertNotIn("shop_schema_", self.client.last_stored.relative_path)

    def test_schema_failure_removes_full_artifact(self) -> None:
        """额外 schema 文件失败时，完整文件也必须清理，避免不可恢复产物被误用。"""

        # 第一次完整成功，第二次 schema 失败。
        full = FakeProcess(stdout=b"full", returncode=0)
        schema_failed = FakeProcess(
            stdout=b"partial-schema",
            stderr=b"schema failed",
            returncode=3,
        )
        with mock.patch("subprocess.Popen", side_effect=[full, schema_failed]):
            result = self.client.dump(self.task())

        self.assertFalse(result.success)
        self.assertEqual(3, result.return_code)
        self.assertIsNone(self.client.last_stored)
        self.assertIsNone(self.client.last_schema_stored)
        self.assertEqual((), self.storage.list_relative_paths())

    def test_tables_scope_uses_db_and_table_args(self) -> None:
        """库表组合：不用 --databases，改用「库名 + 表名列表」。"""

        # 复制配置并替换为库表组合。
        backup = replace(self.config.backup, databases=("shop:t1,t2",))
        client = MysqldumpClient(self.config.mysql, backup, self.storage, self.compressor, self.clock)

        fake = FakeProcess(stdout=b"x", returncode=0)
        with mock.patch("subprocess.Popen", return_value=fake) as popen:
            client.dump(self.task())

        # 断言参数形态。
        argv = popen.call_args.args[0]
        self.assertNotIn("--databases", argv)
        self.assertIn("shop", argv)
        self.assertIn("t1", argv)
        self.assertIn("t2", argv)

    def test_dump_failure_sanitizes_stderr_and_cleans_partial(self) -> None:
        """失败转储：错误摘要脱敏，半成品文件被清理。"""

        # stderr 里故意包含密码。
        fake = FakeProcess(
            stdout=b"partial",
            stderr=b"Access denied using password topsecret",
            returncode=2,
        )
        with mock.patch("subprocess.Popen", return_value=fake):
            result = self.client.dump(self.task())

        # 结果失败、退出码透传。
        self.assertFalse(result.success)
        self.assertEqual(2, result.return_code)

        # 密码已被脱敏。
        self.assertNotIn("topsecret", result.error_digest)
        self.assertIn("***", result.error_digest)

        # 半成品被清理，last_stored 无效。
        self.assertIsNone(self.client.last_stored)
        self.assertEqual((), self.storage.list_relative_paths())

    def test_dump_missing_binary_raises_dump_failed(self) -> None:
        """mysqldump 不存在：抛 DumpFailed 致命异常。"""

        # Popen 抛 FileNotFoundError。
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError("mysqldump")):
            with self.assertRaises(DumpFailed):
                self.client.dump(self.task())


# 支持直接运行。
if __name__ == "__main__":
    unittest.main()
