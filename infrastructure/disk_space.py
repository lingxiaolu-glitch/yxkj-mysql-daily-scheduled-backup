"""磁盘空间预检适配器：使用标准库查看目标目录所在卷可用空间。"""

# 延迟类型注解。
from __future__ import annotations

# shutil 获取磁盘使用情况。
import shutil
from pathlib import Path


class DiskSpaceChecker:
    """目标目录磁盘空间预检（FR-15）。"""

    def __init__(self, min_free_bytes: int) -> None:
        # 最低可用空间要求，默认由配置 backup.min_free_bytes 提供。
        self._min_free_bytes = min_free_bytes

    def free_bytes(self, path: str | Path) -> int:
        """返回目标路径所在卷的可用字节数。"""
        return shutil.disk_usage(Path(path)).free

    def has_enough_space(self, path: str | Path) -> bool:
        """可用空间是否达到最低阈值。"""
        return self.free_bytes(path) >= self._min_free_bytes

    @property
    def required_bytes(self) -> int:
        """当前检查要求的最低可用字节数。"""
        return self._min_free_bytes