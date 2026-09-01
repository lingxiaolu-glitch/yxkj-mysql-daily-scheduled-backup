"""本地文件存储单元测试：写读删、列目录、路径越界拦截、权限。"""

# 延迟类型注解。
from __future__ import annotations

# os/sys 用于权限断言与 Windows 跳过。
import os
import sys
# datetime 构造固定测试时间。
from datetime import datetime, timedelta, timezone
# unittest 提供测试类与断言。
import unittest
# Path/TemporaryDirectory 提供临时目录。
from pathlib import Path
from tempfile import TemporaryDirectory

# 导入被测适配器与领域值对象。
from infrastructure.file_storage import LocalFileStorage
from domain.model.value_objects import (
    BackupTime,
    Compression,
    DbName,
    DomainError,
    FileName,
)


class LocalFileStorageTests(unittest.TestCase):
    """LocalFileStorage：写流、读、列目录、删、防越界。"""

    def setUp(self) -> None:
        # 固定 UTC+8 时间与临时根目录。
        self.zone = timezone(timedelta(hours=8))
        self.tmp = TemporaryDirectory()
        self.dest = Path(self.tmp.name)
        self.fs = LocalFileStorage(self.dest)

        # 固定文件名。
        self.file_name = FileName(
            db_name=DbName("shop"),
            backup_time=BackupTime(datetime(2026, 8, 10, 2, 0, 0, tzinfo=self.zone)),
            compression=Compression.GZIP,
        )

    def tearDown(self) -> None:
        # 清理临时目录。
        self.tmp.cleanup()

    def test_write_chunks_returns_stored_artifact(self) -> None:
        """write_chunks 返回相对路径、大小与 SHA256。"""

        # 写入两块字节。
        stored = self.fs.write_chunks(self.file_name, [b"hello ", b"world"])

        # 相对路径 = 日期目录 / 文件名；大小正确；SHA256 为 64 位十六进制。
        self.assertEqual("20260810/shop_20260810_020000.sql.gz", stored.relative_path)
        self.assertEqual(11, stored.size_bytes)
        self.assertEqual(64, len(stored.sha256))

    def test_exists_and_read_bytes_roundtrip(self) -> None:
        """写入后可读取，内容一致。"""

        # 写入并回读。
        stored = self.fs.write_chunks(self.file_name, [b"abc", b"def"])
        self.assertTrue(self.fs.exists(stored.relative_path))
        self.assertEqual(b"abcdef", self.fs.read_bytes(stored.relative_path))

    def test_list_relative_paths(self) -> None:
        """列目录返回相对路径，且不含目录条目。"""

        # 写入一个产物后枚举。
        self.fs.write_chunks(self.file_name, [b"data"])
        self.assertEqual(
            ("20260810/shop_20260810_020000.sql.gz",),
            self.fs.list_relative_paths(),
        )

    def test_delete_removes_file(self) -> None:
        """删除后文件不存在。"""

        # 写入后删除。
        stored = self.fs.write_chunks(self.file_name, [b"data"])
        self.fs.delete(stored.relative_path)
        self.assertFalse(self.fs.exists(stored.relative_path))

    def test_delete_missing_file_is_ignored(self) -> None:
        """删除不存在的文件不抛异常。"""

        # 直接删除不存在的相对路径。
        self.fs.delete("20260810/not_exist.sql.gz")

    def test_path_traversal_rejected(self) -> None:
        """越界/绝对/空路径一律拒绝。"""

        # 常见攻击路径样本。
        bad_paths = (
            "../evil.sql.gz",
            "/abs/evil",
            "a/../../evil",
            "",
            "..\\..\\evil",
        )
        for bad in bad_paths:
            with self.subTest(path=bad):
                # 删除与读取都要被拦截。
                with self.assertRaises(DomainError):
                    self.fs.delete(bad)
                with self.assertRaises(DomainError):
                    self.fs.read_bytes(bad)

    def test_file_permission_0600_on_posix(self) -> None:
        """POSIX 上产物文件权限为 0600（Windows 跳过）。"""

        # Windows chmod 语义有限，跳过权限断言。
        if sys.platform.startswith("win"):
            self.skipTest("Windows 权限语义有限，跳过")

        # 写入后检查文件权限。
        stored = self.fs.write_chunks(self.file_name, [b"data"])
        mode = os.stat(self.dest / stored.relative_path).st_mode & 0o777
        self.assertEqual(0o600, mode)


# 支持直接运行。
if __name__ == "__main__":
    unittest.main()
