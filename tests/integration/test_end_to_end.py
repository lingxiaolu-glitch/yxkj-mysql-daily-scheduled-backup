"""端到端集成测试：真实存储/压缩/manifest + mock 外部 MySQL 命令。"""

# 延迟类型注解。
from __future__ import annotations

import gzip
import io
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# 领域对象。
from domain.model.aggregates.backup_run import BackupRun
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.entities.database_backup_task import DatabaseBackupTask
from domain.model.value_objects import (
    BackupScope,
    BackupTime,
    Compression,
    DbName,
    DumpResult,
    FileName,
    RunStatus,
    Sha256,
    SizeBytes,
    VerificationLevel,
)
# 基础设施与触发层。
from infrastructure.compressor import GzipCompressor
from infrastructure.config_loader import load_config
from infrastructure.file_storage import LocalFileStorage
from infrastructure.manifest_repository import JsonManifestRepository
from infrastructure.mysqldump_client import MysqldumpClient
from infrastructure.verifiers import FileIntegrityVerifier, RestoreVerifier, StructureVerifier
from trigger.restore_backup import RestoreBackupCommandHandler
from trigger.run_backup import RunBackupCommandHandler
from trigger.runtime import Runtime

# 两个表 + Dump completed 标记。
_TWO_TABLE_SQL = (
    "CREATE TABLE `t1` (`id` INT PRIMARY KEY);\n"
    "CREATE TABLE `t2` (`id` INT PRIMARY KEY);\n"
    "-- Dump completed on 2026-09-05 02:00:00\n"
)


class FakeClock:
    def __init__(self, now: BackupTime):
        self._now = now

    def now(self) -> BackupTime:
        return self._now


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


class FakeGateway:
    """只用于校验和恢复的 MySQL 网关。"""

    def __init__(self, table_count: int = 2):
        self.table_count = table_count
        self.restore_calls = []
        self.dropped = []

    def list_databases(self):
        return (DbName("shop"),)

    def count_tables(self, database: DbName) -> int:
        return self.table_count

    def create_shadow_database(self, source, shadow):
        return None

    def drop_database(self, database):
        self.dropped.append(str(database))

    def count_table_rows(self, database, table):
        return 1

    def restore(self, sql_file, database, one_database=False, rewrite_to_database=None):
        self.restore_calls.append((sql_file, database, one_database, rewrite_to_database))


class FakeNotifier:
    def __init__(self):
        self.events = []

    def notify(self, event):
        self.events.append(event)


class FakeDisk:
    def __init__(self):
        self.required = 1

    def has_enough_space(self, path) -> bool:
        return True

    def free_bytes(self, path) -> int:
        return 999

    @property
    def required_bytes(self) -> int:
        return self.required


class EndToEndTests(unittest.TestCase):
    """mock 外部命令下的完整备份/清理/恢复链路。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "backups"
        self.log_dir = self.root / "logs"
        self.config_path = self.root / "instance.toml"

    def tearDown(self) -> None:
        logging.shutdown()
        self.tmp.cleanup()

    def _config(self, retention_enabled: bool = False, verify_level: str = "L1") -> str:
        text = """
[mysql]
host = "127.0.0.1"
port = 3306
user = "backup_user"
password_env = "MYSQL_BACKUP_PASSWORD"

[backup]
dest_dir = "__DEST__"
databases = ["all"]
exclude_databases = []
mysqldump_path = "mysqldump"
compress = "gzip"
schema_only = true
extra_args = []
retry_times = 0
lock_wait_timeout = 0
min_free_bytes = 1

[retention]
enabled = __RETENTION__
days = 1
weekly = 0
monthly = 0

[schedule]
time = "02:00"
timezone = "Asia/Shanghai"

[verify]
level = "__LEVEL__"
shadow_db_prefix = "restore_check_"
sample_tables = []

[notify]
enabled = false
on_success = false
on_failure = false
type = "log"

[log]
level = "INFO"
dir = "__LOGS__"
max_bytes = 1024
backup_count = 1
""".replace("__DEST__", str(self.dest).replace("\\", "/")).replace("__LOGS__", str(self.log_dir).replace("\\", "/")).replace(
            "__RETENTION__", "true" if retention_enabled else "false"
        ).replace("__LEVEL__", verify_level)
        self.config_path.write_text(text, encoding="utf-8")
        return text

    def _runtime(self, retention_enabled: bool = False, verify_level: str = "L1"):
        config = load_config(self.config_path, env={"MYSQL_BACKUP_PASSWORD": "secret"})
        zone = timezone(timedelta(hours=8))
        now = BackupTime(datetime(2026, 9, 5, 2, 0, 0, tzinfo=zone))
        clock = FakeClock(now)
        storage = LocalFileStorage(self.dest)
        compressor = GzipCompressor()
        gateway = FakeGateway(2)
        dump = MysqldumpClient(config.mysql, config.backup, storage, compressor, clock)
        repo = JsonManifestRepository(self.dest)
        verifiers = {
            VerificationLevel.L0: FileIntegrityVerifier(storage),
            VerificationLevel.L1: StructureVerifier(storage, gateway),
            VerificationLevel.L2: RestoreVerifier(
                storage, gateway, config.verify.shadow_db_prefix, config.verify.sample_tables
            ),
        }
        return Runtime(
            config=config,
            clock=clock,
            storage=storage,
            compressor=compressor,
            dump_executor=dump,
            gateway=gateway,
            manifest_repository=repo,
            verifiers=verifiers,
            notifier=FakeNotifier(),
            disk_checker=FakeDisk(),
        ), now, gateway, repo

    @staticmethod
    def _popen_success():
        return FakeProcess(_TWO_TABLE_SQL.encode("utf-8"))

    def test_backup_end_to_end_creates_gzip_schema_and_manifest(self) -> None:
        """完整备份生成两个 gzip 产物、manifest，并可恢复。"""
        self._config(retention_enabled=False)
        runtime, now, gateway, repo = self._runtime()
        handler = RunBackupCommandHandler(runtime=runtime, check_binaries=False)

        with mock.patch("subprocess.Popen", side_effect=[self._popen_success(), self._popen_success()]):
            result = handler.execute(self.config_path)

        self.assertEqual(0, result)
        paths = runtime.storage.list_relative_paths()
        gz_paths = [path for path in paths if path.endswith(".sql.gz")]
        self.assertEqual(2, len(gz_paths))
        self.assertTrue(any("shop_" in path and "schema" not in path for path in gz_paths))
        self.assertTrue(any("shop_schema_" in path for path in gz_paths))
        self.assertEqual(1, len(repo.find_all()))

        # 恢复演练接入真实 handler 和 fake mysql 网关。
        restore = RestoreBackupCommandHandler(runtime=runtime)
        restore_result = restore.execute(self.config_path, db="shop", mode="full")
        self.assertEqual(0, restore_result)
        self.assertEqual(1, len(gateway.restore_calls))

    def test_backup_with_retention_cleans_old_files(self) -> None:
        """备份后保留清理删除 2 天前的旧产物。"""
        self._config(retention_enabled=True)
        runtime, now, _, repo = self._runtime()

        # 先制造 2 天前旧产物并写入 manifest。
        old_time = BackupTime(now.value - timedelta(days=2))
        old_name = FileName(DbName("shop"), old_time, Compression.GZIP)
        old_data = gzip.compress(_TWO_TABLE_SQL.encode("utf-8"))
        old_stored = runtime.storage.write_chunks(old_name, [old_data])
        old_artifact = BackupArtifact(
            db_name=DbName("shop"),
            file_name=old_name,
            relative_path=old_stored.relative_path,
            size_bytes=SizeBytes(old_stored.size_bytes),
            sha256=Sha256(old_stored.sha256),
            created_at=old_time,
        )
        old_artifact.verify(VerificationLevel.L0, True, old_time)
        old_task = DatabaseBackupTask(DbName("shop"), retry_times=0)
        old_task.apply_dump_result(DumpResult(True, 0, 1.0, ""), old_artifact)
        old_run = BackupRun.start("old-run", old_time, (old_task,), BackupScope.LIST)
        old_run.finish(old_time)
        repo.save(old_run)
        self.assertTrue(runtime.storage.exists(old_stored.relative_path))

        handler = RunBackupCommandHandler(runtime=runtime, check_binaries=False)
        with mock.patch("subprocess.Popen", side_effect=[self._popen_success(), self._popen_success()]):
            result = handler.execute(self.config_path)

        self.assertEqual(0, result)
        self.assertFalse(runtime.storage.exists(old_stored.relative_path))
        # 当前备份仍存在两个 gzip 产物。
        self.assertEqual(2, len([p for p in runtime.storage.list_relative_paths() if p.endswith(".sql.gz")]))

    def test_l2_restore_drill_comparison(self) -> None:
        """L2 影子库恢复校验可执行并清理影子库。"""
        self._config(retention_enabled=False, verify_level="L2")
        runtime, _, gateway, _ = self._runtime()
        handler = RunBackupCommandHandler(runtime=runtime, check_binaries=False)
        with mock.patch("subprocess.Popen", side_effect=[self._popen_success(), self._popen_success()]):
            result = handler.execute(self.config_path)
        self.assertEqual(0, result)
        self.assertEqual(1, len(gateway.restore_calls))
        self.assertEqual("restore_check_shop", str(gateway.restore_calls[0][1]))
        self.assertIn("restore_check_shop", gateway.dropped)


if __name__ == "__main__":
    unittest.main()