"""系统时钟适配器单元测试。"""

# 延迟类型注解。
from __future__ import annotations

# datetime 用于比较时间窗口。
from datetime import datetime, timedelta, timezone
# unittest 提供测试类与断言。
import unittest

# 导入被测适配器与领域值对象。
from infrastructure.system_clock import SystemClock
from domain.model.value_objects import BackupTime


class SystemClockTests(unittest.TestCase):
    """SystemClock 必须返回带时区的合法 BackupTime。"""

    def test_now_returns_timezone_aware_backup_time(self) -> None:
        """now() 返回带时区的 BackupTime。"""

        # 调用真实时钟。
        now = SystemClock().now()

        # 类型与时区约束。
        self.assertIsInstance(now, BackupTime)
        self.assertIsNotNone(now.value.tzinfo)
        self.assertIsNotNone(now.value.utcoffset())

    def test_now_is_recent(self) -> None:
        """now() 返回接近当前时刻的时间。"""

        # 取调用前后 5 秒窗口，转 UTC 比较避免时区差异。
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        now_utc = SystemClock().now().value.astimezone(timezone.utc)
        after = datetime.now(timezone.utc) + timedelta(seconds=5)

        # 落在窗口内。
        self.assertGreaterEqual(now_utc, before)
        self.assertLessEqual(now_utc, after)


# 支持直接运行。
if __name__ == "__main__":
    unittest.main()
