"""CleanupCommandHandler 单元测试：按 manifest 清理过期产物。"""

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
    BackupScope,
    BackupTime,
    Compression,
    DbName,
    DumpResult,
    ExitCode,
    FileName,
    RunStatus,
    Sha256,
    SizeBytes,
)
from infrastructure.compressor import NoopCompressor
from infrastructure.config_loader import load_config
from infrastructure.file_storage import LocalFileStorage
from infrastructure.manifest_repository import JsonManifestRepository
from trigger.cleanup import CleanupCommandHandler, execute_cleanup
from trigger.runtime import Runtime


class FakeClock:
    def __init__(self, now: BackupTime):
        self._now = now

    def now(self) -> BackupTime:
        return self._now


class FakeNoopNotifier:
    def notify(self, event) -> None:
        return None


class FakeDisk:
    def has_enough_space(self, path) -> bool:
        return True

    def free_bytes(self, path) -> int:
        return 999

    @property
    def required_bytes(self) -> int:
        return 1


class CleanupTests(unittest.TestCase):
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
        self.compressor = NoopCompressor()
        self.repo = JsonManifestRepository(self.dest)
        self.runtime = Runtime(
            config=self.config,
            clock=self.clock,
            storage=self.storage,
            compressor=self.compressor,
            dump_executor=None,
            gateway=None,
            manifest_repository=self.repo,
            verifiers={},
            notifier=FakeNoopNotifier(),
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
enabled = true
days = 1
weekly = 0
monthly = 0

[schedule]
time = "02:00"
timezone = "Asia/Shanghai"

[verify]
level = "L0"
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

    def _save_run(self, run_id: str, created_at: BackupTime, db: str = "shop") -> BackupArtifact:
        file_name = FileName(DbName(db), created_at, Compression.NONE)
        stored = self.storage.write_chunks(file_name, [b"CREATE TABLE t (id int);\n"])
        artifact = BackupArtifact(
            db_name=DbName(db),
            file_name=file_name,
            relative_path=stored.relative_path,
            size_bytes=SizeBytes(stored.size_bytes),
            sha256=Sha256(stored.sha256),
            created_at=created_at,
        )
        task = DatabaseBackupTask(DbName(db), retry_times=0)
        task.apply_dump_result(DumpResult(True, 0, 1.0, ""), artifact)
        run = BackupRun.start(run_id, created_at, (task,), BackupScope.LIST)
        run.finish(created_at)
        self.repo.save(run)
        return artifact

    def test_execute_cleanup_deletes_old_and_keeps_recent(self) -> None:
        """超过日保留期的旧产物被删除，当日产物保留。"""
        old = self._save_run("run-old", BackupTime(self.now.value - timedelta(days=2)))
        recent = self._save_run("run-new", self.now)

        outcome = execute_cleanup(self.runtime)

        self.assertEqual(1, outcome.deleted)
        self.assertEqual(0, outcome.kept)
        self.assertFalse(self.storage.exists(old.relative_path))
        self.assertTrue(self.storage.exists(recent.relative_path))

        # 旧 run 的 manifest 应标记删除。
        old_run = self.repo.find("run-old")
        self.assertIsNotNone(old_run)
        assert old_run is not None
        self.assertTrue(old_run.tasks[0].all_artifacts[0].deleted)

    def test_cleanup_handler_returns_zero(self) -> None:
        """CleanupCommandHandler 入口返回 0。"""
        self._save_run("run-old", BackupTime(self.now.value - timedelta(days=2)))
        handler = CleanupCommandHandler(runtime=self.runtime)
        self.assertEqual(int(ExitCode.SUCCESS), handler.execute(self.config_path))


if __name__ == "__main__":
    unittest.main()