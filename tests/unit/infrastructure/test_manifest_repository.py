"""JsonManifestRepository 单元测试：往返、多运行、损坏文件。"""

# 延迟类型注解。
from __future__ import annotations

# json/tempfile/pathlib/unittest。
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 领域聚合、实体与值对象。
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
    TaskStatus,
    VerificationLevel,
)
from infrastructure.manifest_repository import JsonManifestRepository, ManifestError


def _time(hour: int = 2) -> BackupTime:
    """构造固定 Asia/Shanghai 时间。"""
    zone = timezone(timedelta(hours=8))
    return BackupTime(datetime(2026, 9, 5, hour, 0, 0, tzinfo=zone))


def _file_name(db: str, now: BackupTime, schema_only: bool = False) -> FileName:
    """构造领域文件名。"""
    return FileName(DbName(db), now, Compression.GZIP, schema_only=schema_only)


def make_run(run_id: str, db: str = "shop", hour: int = 2):
    """构造一个已完成成功运行及可用产物。"""
    now = _time(hour)
    file_name = _file_name(db, now)
    artifact = BackupArtifact(
        db_name=DbName(db),
        file_name=file_name,
        relative_path=f"{now.date_key}/{file_name.value}",
        size_bytes=SizeBytes(42),
        sha256=Sha256("a" * 64),
        created_at=now,
    )
    artifact.verify(VerificationLevel.L0, True, now)

    task = DatabaseBackupTask(db_name=DbName(db), retry_times=1)
    task.apply_dump_result(DumpResult(True, 0, 1.5, ""), artifact)

    run = BackupRun.start(run_id, now, (task,), BackupScope.LIST)
    run.finish(now)
    return run, artifact


class JsonManifestRepositoryTests(unittest.TestCase):
    """manifest 仓库核心行为。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = JsonManifestRepository(self.root)
        self.date_key = _time().date_key

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_find_and_find_by_date_round_trip(self) -> None:
        """保存后能按 run_id 和日期读回完整领域对象。"""
        run, artifact = make_run("run-001", "shop")

        self.repo.save(run)

        loaded = self.repo.find("run-001")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual("run-001", loaded.run_id)
        self.assertEqual(RunStatus.SUCCESS, loaded.status)
        self.assertEqual(ExitCode.SUCCESS, loaded.exit_code)
        self.assertEqual(("shop",), loaded.database_names)
        self.assertEqual(TaskStatus.SUCCESS, loaded.tasks[0].status)
        self.assertEqual("shop", str(loaded.tasks[0].artifact.db_name))
        self.assertEqual(artifact.file_name.value, loaded.tasks[0].artifact.file_name.value)
        self.assertEqual(str(artifact.sha256), str(loaded.tasks[0].artifact.sha256))
        self.assertEqual(VerificationLevel.L0, loaded.tasks[0].artifact.verification_level)

        by_date = self.repo.find_by_date(self.date_key)
        self.assertEqual(1, len(by_date))
        self.assertEqual("run-001", by_date[0].run_id)

    def test_multiple_runs_are_preserved(self) -> None:
        """同一日期多次运行不会互相覆盖。"""
        run_a, _ = make_run("run-a", "shop", 2)
        run_b, _ = make_run("run-b", "crm", 3)
        self.repo.save(run_a)
        self.repo.save(run_b)

        loaded = self.repo.find_by_date(self.date_key)
        self.assertEqual(2, len(loaded))
        self.assertEqual({"run-a", "run-b"}, {item.run_id for item in loaded})
        self.assertIsNotNone(self.repo.find("run-a"))
        self.assertIsNotNone(self.repo.find("run-b"))

    def test_rerun_same_run_id_updates_manifest(self) -> None:
        """相同 run_id 再次保存应覆盖旧记录。"""
        first, _ = make_run("run-same", "shop", 2)
        second, _ = make_run("run-same", "crm", 3)
        self.repo.save(first)
        self.repo.save(second)

        self.assertEqual(1, len(self.repo.find_by_date(self.date_key)))
        self.assertIn("crm", self.repo.find("run-same").database_names)

    def test_missing_date_returns_empty_tuple(self) -> None:
        """不存在日期时返回空元组，而不是失败。"""
        self.assertEqual((), self.repo.find_by_date("20260101"))

    def test_corrupt_json_raises_manifest_error(self) -> None:
        """损坏 JSON 必须显式报错，防止静默丢失审计信息。"""
        path = self.repo.manifest_path(self.date_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")

        with self.assertRaises(ManifestError):
            self.repo.find_by_date(self.date_key)

    def test_wrong_root_shape_raises_manifest_error(self) -> None:
        """顶层不是 version/runs 结构时拒绝加载。"""
        path = self.repo.manifest_path(self.date_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"runs": "bad"}), encoding="utf-8")
        with self.assertRaises(ManifestError):
            self.repo.find_by_date(self.date_key)

    def test_status_summary_file_is_written(self) -> None:
        """轻量 status 文件应落盘，便于人工审计。"""
        run, _ = make_run("run-status", "shop")
        self.repo.save(run)

        status_path = self.repo.status_path(self.date_key)
        self.assertTrue(status_path.exists())
        data = json.loads(status_path.read_text(encoding="utf-8"))
        summaries = data["runs"]
        self.assertEqual("run-status", summaries[0]["run_id"])
        self.assertEqual(1, summaries[0]["success_count"])
        self.assertEqual(0, summaries[0]["failed_count"])
        self.assertEqual(1, summaries[0]["artifact_count"])

    def test_invalid_date_key_is_rejected(self) -> None:
        """非法日期键不能用于构造路径。"""
        with self.assertRaises(ManifestError):
            self.repo.manifest_path("../../etc")


if __name__ == "__main__":
    unittest.main()