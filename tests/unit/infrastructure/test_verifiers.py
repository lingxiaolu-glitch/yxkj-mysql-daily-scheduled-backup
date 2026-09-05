"""L0/L1 校验器单元测试：文件完整性、gzip 损坏、建表数比对。"""

# 延迟类型注解。
from __future__ import annotations

# gzip/tempfile/unittest。
import gzip
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 领域产物与值对象。
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.value_objects import (
    BackupTime,
    Compression,
    DbName,
    FileName,
    Sha256,
    SizeBytes,
    VerificationLevel,
)
from infrastructure.file_storage import LocalFileStorage
from infrastructure.verifiers import FileIntegrityVerifier, StructureVerifier

# L0/L1 成功样例 SQL。
GOOD_SQL = (
    "CREATE TABLE `t1` (`id` INT PRIMARY KEY);\n"
    "CREATE TABLE `t2` (`id` INT PRIMARY KEY);\n"
    "-- Dump completed on 2026-09-05 02:00:00\n"
)


class _FakeGateway:
    """简单 MySqlGateway fake，返回预设表数量。"""

    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.calls = 0

    def count_tables(self, database: DbName) -> int:
        self.calls += 1
        return self.count


def _time() -> BackupTime:
    # 固定 UTC+8 时间。
    zone = timezone(timedelta(hours=8))
    return BackupTime(datetime(2026, 9, 5, 2, 0, 0, tzinfo=zone))


def _artifact(storage: LocalFileStorage, file_name: FileName, now: BackupTime, raw: bytes):
    """往存储写入内容并构造对应产物。"""
    stored = storage.write_chunks(file_name, [raw])
    return BackupArtifact(
        db_name=file_name.db_name,
        file_name=file_name,
        relative_path=stored.relative_path,
        size_bytes=SizeBytes(stored.size_bytes),
        sha256=Sha256(stored.sha256),
        created_at=now,
    )


class VerifierTests(unittest.TestCase):
    """L0/L1 校验器。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.storage = LocalFileStorage(self.root)
        self.now = _time()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _file(self, db: str = "shop") -> FileName:
        return FileName(DbName(db), self.now, Compression.GZIP)

    def test_l0_success_for_valid_gzip(self) -> None:
        """合法 gzip 且包含结束标记 -> L0 成功。"""
        artifact = _artifact(
            self.storage,
            self._file(),
            self.now,
            gzip.compress(GOOD_SQL.encode("utf-8")),
        )
        result = FileIntegrityVerifier(self.storage).verify(artifact)
        self.assertTrue(result.success)
        self.assertEqual(VerificationLevel.L0, result.level)

    def test_l0_rejects_empty_file(self) -> None:
        """空文件 -> L0 失败。"""
        artifact = _artifact(self.storage, self._file(), self.now, b"")
        result = FileIntegrityVerifier(self.storage).verify(artifact)
        self.assertFalse(result.success)
        self.assertIn("为空", result.reason)

    def test_l0_rejects_missing_file(self) -> None:
        """文件不存在 -> L0 失败。"""
        artifact = _artifact(
            self.storage,
            self._file(),
            self.now,
            gzip.compress(GOOD_SQL.encode("utf-8")),
        )
        self.storage.delete(artifact.relative_path)
        result = FileIntegrityVerifier(self.storage).verify(artifact)
        self.assertFalse(result.success)
        self.assertIn("不存在", result.reason)

    def test_l0_rejects_corrupt_gzip(self) -> None:
        """损坏 gzip -> L0 失败。"""
        artifact = _artifact(self.storage, self._file(), self.now, b"not-a-gzip-file")
        result = FileIntegrityVerifier(self.storage).verify(artifact)
        self.assertFalse(result.success)
        self.assertIn("损坏", result.reason)

    def test_l0_rejects_missing_dump_completed_marker(self) -> None:
        """gzip 合法但无结束标记 -> L0 失败。"""
        raw = b"CREATE TABLE `t` (`id` INT);\n"
        artifact = _artifact(self.storage, self._file(), self.now, gzip.compress(raw))
        result = FileIntegrityVerifier(self.storage).verify(artifact)
        self.assertFalse(result.success)
        self.assertIn("Dump completed", result.reason)

    def test_l1_success_matches_gateway_count(self) -> None:
        """两个建表语句与源库表数一致 -> L1 成功。"""
        artifact = _artifact(
            self.storage,
            self._file(),
            self.now,
            gzip.compress(GOOD_SQL.encode("utf-8")),
        )
        gateway = _FakeGateway(2)
        result = StructureVerifier(self.storage, gateway).verify(artifact)
        self.assertTrue(result.success)
        self.assertEqual(VerificationLevel.L1, result.level)
        self.assertEqual(1, gateway.calls)

    def test_l1_failure_on_count_mismatch(self) -> None:
        """产物建表数与源库表数不一致 -> L1 失败。"""
        artifact = _artifact(
            self.storage,
            self._file(),
            self.now,
            gzip.compress(GOOD_SQL.encode("utf-8")),
        )
        result = StructureVerifier(self.storage, _FakeGateway(3)).verify(artifact)
        self.assertFalse(result.success)
        self.assertIn("不一致", result.reason)

    def test_l1_uses_expected_count_for_table_mode(self) -> None:
        """指定表备份时使用配置期望表数，不查询全库。"""
        artifact = _artifact(
            self.storage,
            self._file(),
            self.now,
            gzip.compress(b"CREATE TABLE `t1` (`id` INT);\n-- Dump completed\n"),
        )
        gateway = _FakeGateway(999)
        verifier = StructureVerifier(self.storage, gateway, {"shop": 1})
        result = verifier.verify(artifact)
        self.assertTrue(result.success)
        self.assertEqual(0, gateway.calls)


if __name__ == "__main__":
    unittest.main()