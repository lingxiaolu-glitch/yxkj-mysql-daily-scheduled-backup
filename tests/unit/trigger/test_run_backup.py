"""RunBackupCommandHandler 单元测试：成功、部分失败、预检与校验。"""

# 延迟类型注解。
from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain.events import BackupRunSkipped, DiskSpaceLow, VerificationFailed
from domain.model.entities.database_backup_task import DatabaseBackupTask
from domain.model.value_objects import (
    BackupScope,
    BackupTime,
    Compression,
    DbName,
    DumpResult,
    FileName,
    RunStatus,
    VerificationLevel,
)
from domain.repositories import StoredArtifact
from domain.services.verification import VerificationResult
from infrastructure.config_loader import load_config
from infrastructure.file_storage import LocalFileStorage
from infrastructure.manifest_repository import JsonManifestRepository
from trigger.run_backup import RunBackupCommandHandler
from trigger.runtime import Runtime


class FakeClock:
    """固定时钟。"""

    def __init__(self, now: BackupTime) -> None:
        self._now = now

    def now(self) -> BackupTime:
        return self._now


class FakeGateway:
    """简单 MySQL 网关。"""

    def __init__(self) -> None:
        self.list_result = (DbName("shop"), DbName("crm"))

    def list_databases(self):
        return self.list_result

    def count_tables(self, database: DbName) -> int:
        return 2

    def restore(self, *args, **kwargs):
        return None

    def create_shadow_database(self, *args, **kwargs):
        return None

    def drop_database(self, *args, **kwargs):
        return None

    def count_table_rows(self, *args, **kwargs):
        return 0


class FakeDumpExecutor:
    """记录成功/失败结果的假转储器。"""

    def __init__(self, now: BackupTime, fail_names: set[str] | None = None):
        self.now = now
        self.fail_names = fail_names or set()
        self.last_stored: StoredArtifact | None = None
        self.last_file_name: FileName | None = None
        self.last_schema_stored: StoredArtifact | None = None
        self.last_schema_file_name: FileName | None = None
        self.calls = []

    def dump(self, task: DatabaseBackupTask) -> DumpResult:
        self.calls.append(str(task.db_name))
        if str(task.db_name) in self.fail_names:
            return DumpResult(False, 2, 0.1, "fake failure")

        # 生成完整产物。
        self.last_file_name = FileName(task.db_name, self.now, Compression.NONE, False)
        self.last_stored = StoredArtifact(
            relative_path=f"{self.now.date_key}/{self.last_file_name.value}",
            size_bytes=10,
            sha256="a" * 64,
        )
        # 配置 schema_only=true 时生成额外结构产物。
        self.last_schema_file_name = FileName(task.db_name, self.now, Compression.NONE, True)
        self.last_schema_stored = StoredArtifact(
            relative_path=f"{self.now.date_key}/{self.last_schema_file_name.value}",
            size_bytes=5,
            sha256="b" * 64,
        )
        return DumpResult(True, 0, 0.1, "")


class FakeNotifier:
    """记录事件的通知器。"""

    def __init__(self):
        self.events = []

    def notify(self, event) -> None:
        self.events.append(event)


class FakeDisk:
    """预设磁盘空间结果。"""

    def __init__(self, enough: bool = True, free: int = 1024):
        self.enough = enough
        self.free = free
        self.required = 10

    def has_enough_space(self, path) -> bool:
        return self.enough

    def free_bytes(self, path) -> int:
        return self.free

    @property
    def required_bytes(self) -> int:
        return self.required


class RunBackupHandlerTests(unittest.TestCase):
    """RunBackupCommandHandler 核心链路。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "backups"
        self.log_dir = self.root / "logs"
        self.config_path = self.root / "instance.toml"
        self.config_path.write_text(self._config_text(), encoding="utf-8")
        self.env = {"MYSQL_BACKUP_PASSWORD": "secret"}
        self.config = load_config(self.config_path, env=self.env)
        zone = timezone(timedelta(hours=8))
        self.now = BackupTime(datetime(2026, 9, 5, 2, 0, 0, tzinfo=zone))
        self.clock = FakeClock(self.now)
        self.storage = LocalFileStorage(self.dest)
        self.dump = FakeDumpExecutor(self.now)
        self.gateway = FakeGateway()
        self.repo = JsonManifestRepository(self.dest)
        self.notifier = FakeNotifier()
        self.disk = FakeDisk()

    def tearDown(self) -> None:
        # 关闭日志 handler，避免 Windows 下临时日志文件被占用。
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
databases = ["all"]
exclude_databases = ["information_schema", "performance_schema", "sys", "mysql"]
mysqldump_path = "mysqldump"
compress = "none"
schema_only = true
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
level = "L0"
shadow_db_prefix = "restore_check_"
sample_tables = []

[notify]
enabled = true
on_success = true
on_failure = true
type = "log"

[log]
level = "INFO"
dir = "__LOGS__"
max_bytes = 1024
backup_count = 1
""".replace("__DEST__", str(self.dest).replace("\\", "/")).replace("__LOGS__", str(self.log_dir).replace("\\", "/"))

    def _runtime(self, verifier=None, gateway=None, dump=None, disk=None) -> Runtime:
        verifier = verifier or (lambda artifact: VerificationResult(VerificationLevel.L0, True))
        return Runtime(
            config=self.config,
            clock=self.clock,
            storage=self.storage,
            compressor=None,
            dump_executor=dump or self.dump,
            gateway=gateway or self.gateway,
            manifest_repository=self.repo,
            verifiers={VerificationLevel.L0: verifier},
            notifier=self.notifier,
            disk_checker=disk or self.disk,
        )

    def test_success_backup_returns_zero_and_persists_artifacts(self) -> None:
        """全成功：退出码 0，manifest 保存完整与 schema 产物。"""
        handler = RunBackupCommandHandler(runtime=self._runtime(), check_binaries=False)
        result = handler.execute(self.config_path)

        self.assertEqual(0, result)
        runs = self.repo.find_by_date(self.now.date_key)
        self.assertEqual(1, len(runs))
        run = runs[0]
        self.assertEqual(RunStatus.SUCCESS, run.status)
        self.assertEqual(2, len(run.tasks))
        self.assertIsNotNone(run.tasks[0].artifact)
        self.assertIsNotNone(run.tasks[0].schema_artifact)
        self.assertEqual(1, len(self.notifier.events))
        # BackupRunCompleted 是最后一条事件，on_success=true 时通知。
        self.assertTrue(any(type(event).__name__ == "BackupRunCompleted" for event in self.notifier.events))

    def test_partial_failure_returns_one(self) -> None:
        """单个库失败：其他库成功，返回 1。"""
        fail_dump = FakeDumpExecutor(self.now, fail_names={"crm"})
        handler = RunBackupCommandHandler(runtime=self._runtime(dump=fail_dump), check_binaries=False)
        result = handler.execute(self.config_path)

        self.assertEqual(1, result)
        run = self.repo.find_by_date(self.now.date_key)[0]
        self.assertEqual(RunStatus.PARTIAL_SUCCESS, run.status)
        self.assertEqual(1, sum(1 for task in run.tasks if task.status.value == "success"))
        self.assertEqual(1, sum(1 for task in run.tasks if task.status.value == "failed"))

    def test_disk_low_returns_two_and_notifies(self) -> None:
        """磁盘不足：拒绝执行并发送 DiskSpaceLow。"""
        runtime = self._runtime(disk=FakeDisk(enough=False, free=0))
        handler = RunBackupCommandHandler(runtime=runtime, check_binaries=False)
        result = handler.execute(self.config_path)

        self.assertEqual(2, result)
        self.assertTrue(any(isinstance(event, DiskSpaceLow) for event in self.notifier.events))

    def test_lock_conflict_returns_two(self) -> None:
        """并发锁冲突：第二次运行直接跳过。"""
        self.dest.mkdir(parents=True, exist_ok=True)
        (self.dest / ".backup.lock").write_text("123", encoding="utf-8")
        handler = RunBackupCommandHandler(runtime=self._runtime(), check_binaries=False)
        result = handler.execute(self.config_path)
        self.assertEqual(2, result)
        self.assertTrue(any(isinstance(event, BackupRunSkipped) for event in self.notifier.events))

    def test_verification_failure_returns_partial_success(self) -> None:
        """校验失败：任务成功但退出码/状态降级为部分成功，并产生事件。"""
        def bad_verifier(artifact):
            return VerificationResult(VerificationLevel.L0, False, reason="bad checksum")

        handler = RunBackupCommandHandler(runtime=self._runtime(verifier=bad_verifier), check_binaries=False)
        result = handler.execute(self.config_path)

        self.assertEqual(1, result)
        run = self.repo.find_by_date(self.now.date_key)[0]
        self.assertEqual(RunStatus.PARTIAL_SUCCESS, run.status)
        self.assertTrue(any(isinstance(event, VerificationFailed) for event in self.notifier.events))

    def test_missing_binary_returns_two(self) -> None:
        """生产默认开启命令预检：找不到 mysqldump 时返回 2。"""
        handler = RunBackupCommandHandler(runtime=self._runtime(), check_binaries=True)
        self.config_path.write_text(
            self._config_text().replace('mysqldump_path = "mysqldump"', 'mysqldump_path = "__definitely_missing__"'),
            encoding="utf-8",
        )
        config = load_config(self.config_path, env=self.env)
        runtime = self._runtime()
        runtime.config = config
        result = handler.execute(self.config_path)
        self.assertEqual(2, result)


if __name__ == "__main__":
    unittest.main()