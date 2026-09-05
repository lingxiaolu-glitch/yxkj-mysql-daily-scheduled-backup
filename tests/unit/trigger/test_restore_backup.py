"""RestoreBackupCommandHandler 单元测试：可用性门禁与导入。"""

# 延迟类型注解。
from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain.model.aggregates.backup_run import BackupRun
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.entities.database_backup_task import DatabaseBackupTask
from domain.model.value_objects import (
    Availability,
    BackupScope,
    BackupTime,
    Compression,
    DbName,
    DumpResult,
    FileName,
    Sha256,
    SizeBytes,
    VerificationLevel,
)
from infrastructure.config_loader import load_config
from infrastructure.file_storage import LocalFileStorage
from infrastructure.manifest_repository import JsonManifestRepository
from trigger.restore_backup import RestoreBackupCommandHandler
from trigger.runtime import Runtime


class FakeClock:
    def __init__(self, now: BackupTime):
        self._now = now

    def now(self) -> BackupTime:
        return self._now


class FakeNotifier:
    def notify(self, event) -> None:
        return None


class FakeDisk:
    def has_enough_space(self, path) -> bool:
        return True

    def free_bytes(self, path) -> int:
        return 1

    @property
    def required_bytes(self) -> int:
        return 1


class FakeGateway:
    def __init__(self):
        self.restore_calls = []

    def restore(self, sql_file, database, one_database=False, rewrite_to_database=None):
        # 保存调用时临时文件内容，供测试断言。
        self.restore_calls.append(
            (
                sql_file,
                database,
                one_database,
                rewrite_to_database,
                Path(sql_file).read_bytes(),
            )
        )
        return None

    def count_tables(self, database):
        return 1

    def create_shadow_database(self, source, shadow):
        return None

    def drop_database(self, database):
        return None

    def count_table_rows(self, database, table):
        return 1


class RestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "backups"
        self.log_dir = self.root / "logs"
        self.config_path = self.root / "instance.toml"
        self.config_path.write_text(self._config_text(), encoding="utf-8")
        self.config = load_config(self.config_path, env={"MYSQL_BACKUP_PASSWORD": "x"})
        zone = timezone(timedelta(hours=8))
        self.now = BackupTime(datetime(2026, 9, 5, 2, 0, 0, tzinfo=zone))
        self.clock = FakeClock(self.now)
        self.storage = LocalFileStorage(self.dest)
        self.repo = JsonManifestRepository(self.dest)
        self.gateway = FakeGateway()
        self.runtime = Runtime(
            config=self.config,
            clock=self.clock,
            storage=self.storage,
            compressor=None,
            dump_executor=None,
            gateway=self.gateway,
            manifest_repository=self.repo,
            verifiers={},
            notifier=FakeNotifier(),
            disk_checker=FakeDisk(),
        )

    def tearDown(self) -> None:
        logging.shutdown()
        self.tmp.cleanup()

    def _config_text(self) -> str:
        return """
[mysql]
host = "127.0.0.1"
port = 3306
user = "backup_user"
password_env = "MYSQL_BACKUP_PASSWORD"

[backup]
dest_dir = "__DEST__"
databases = ["shop"]
exclude_databases = []
mysqldump_path = "mysqldump"
compress = "none"
schema_only = false
extra_args = []
retry_times = 0
lock_wait_timeout = 0
min_free_bytes = 1

[retention]
enabled = false
days = 1
weekly = 0
monthly = 0

[schedule]
time = "02:00"
timezone = "Asia/Shanghai"

[verify]
level = "L1"
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
""".replace("__DEST__", str(self.dest).replace("\\", "/")).replace("__LOGS__", str(self.log_dir).replace("\\", "/"))

    def _artifact(self, schema_only=False, availability=Availability.AVAILABLE) -> BackupArtifact:
        file_name = FileName(DbName("shop"), self.now, Compression.NONE, schema_only=schema_only)
        raw = b"CREATE TABLE t (id int);\n-- Dump completed\n"
        stored = self.storage.write_chunks(file_name, [raw])
        artifact = BackupArtifact(
            db_name=DbName("shop"),
            file_name=file_name,
            relative_path=stored.relative_path,
            size_bytes=SizeBytes(stored.size_bytes),
            sha256=Sha256(stored.sha256),
            created_at=self.now,
            availability=availability,
            schema_only=schema_only,
        )
        if availability is Availability.AVAILABLE:
            artifact.verify(VerificationLevel.L0, True, self.now)
        return artifact

    def _save(self, artifact: BackupArtifact) -> None:
        task = DatabaseBackupTask(DbName("shop"), retry_times=0)
        task.apply_dump_result(DumpResult(True, 0, 1.0, ""), artifact)
        if artifact.file_name.schema_only:
            task.attach_schema_artifact(artifact)
        run = BackupRun.start("restore-run", self.now, (task,), BackupScope.LIST)
        run.finish(self.now)
        self.repo.save(run)

    def test_restore_full_mode_imports_available_artifact(self) -> None:
        """全量恢复：选择最近可用完整文件并调用 mysql 导入。"""
        artifact = self._artifact(schema_only=False)
        self._save(artifact)

        handler = RestoreBackupCommandHandler(runtime=self.runtime)
        result = handler.execute(self.config_path, db="shop", mode="full")

        self.assertEqual(0, result)
        self.assertEqual(1, len(self.gateway.restore_calls))
        sql_file, target, one_database, rewrite, content = self.gateway.restore_calls[0]
        self.assertEqual("shop", str(target))
        self.assertTrue(one_database)
        self.assertEqual(DbName("shop"), rewrite)
        self.assertIn(b"CREATE TABLE", content)

    def test_restore_schema_mode_selects_schema_artifact(self) -> None:
        """schema 模式只选择仅结构文件。"""
        full = self._artifact(False)
        schema = self._artifact(True)
        task = DatabaseBackupTask(DbName("shop"), retry_times=0)
        task.apply_dump_result(DumpResult(True, 0, 1.0, ""), full)
        task.attach_schema_artifact(schema)
        run = BackupRun.start("restore-schema", self.now, (task,), BackupScope.LIST)
        run.finish(self.now)
        self.repo.save(run)

        handler = RestoreBackupCommandHandler(runtime=self.runtime)
        result = handler.execute(self.config_path, db="shop", mode="schema")

        self.assertEqual(0, result)
        sql_file, target, one_database, rewrite, content = self.gateway.restore_calls[0]
        self.assertEqual("shop", str(target))
        self.assertTrue(content.startswith(b"CREATE TABLE"))

    def test_unavailable_artifact_is_rejected(self) -> None:
        """不可用产物必须被恢复门禁拒绝。"""
        artifact = self._artifact(False, Availability.UNAVAILABLE)
        self._save(artifact)
        handler = RestoreBackupCommandHandler(runtime=self.runtime)
        result = handler.execute(self.config_path, db="shop", mode="full")
        self.assertEqual(2, result)
        self.assertEqual([], self.gateway.restore_calls)


if __name__ == "__main__":
    unittest.main()
