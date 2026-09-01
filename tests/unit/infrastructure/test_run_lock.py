"""运行锁单元测试：获取/释放、重入拒绝、超时跳过、上下文管理器。"""

# 延迟类型注解。
from __future__ import annotations

# time 用于测量超时等待时长。
import time
# unittest 提供测试类与断言。
import unittest
# Path/TemporaryDirectory 提供临时锁文件目录。
from pathlib import Path
from tempfile import TemporaryDirectory

# 导入被测运行锁。
from infrastructure.run_lock import RunLock


class RunLockTests(unittest.TestCase):
    """RunLock：锁文件互斥语义。"""

    def test_acquire_and_release(self) -> None:
        """获取后锁文件存在，释放后消失。"""

        # 独立临时目录避免污染工作区。
        with TemporaryDirectory() as d:
            lock_path = Path(d) / "backup.lock"
            lock = RunLock(lock_path, lock_wait_timeout=0)

            # 获取成功且锁文件已创建。
            self.assertTrue(lock.acquire())
            self.assertTrue(lock_path.exists())

            # 释放后锁文件被删除。
            lock.release()
            self.assertFalse(lock_path.exists())

    def test_reacquire_rejected_while_held(self) -> None:
        """锁被持有时，第二次获取立即失败（timeout=0）。"""

        # 两个锁对象竞争同一个锁文件。
        with TemporaryDirectory() as d:
            lock_path = Path(d) / "backup.lock"
            first = RunLock(lock_path, lock_wait_timeout=0)
            second = RunLock(lock_path, lock_wait_timeout=0)

            # 第一个拿到锁。
            self.assertTrue(first.acquire())

            # 第二个立刻被拒。
            self.assertFalse(second.acquire())

            # 释放后第二个可以拿到。
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_timeout_skip_after_wait(self) -> None:
        """持锁超过 lock_wait_timeout 后返回 False。"""

        # 先持锁，再让另一个只等 0.2 秒。
        with TemporaryDirectory() as d:
            lock_path = Path(d) / "backup.lock"
            holder = RunLock(lock_path, lock_wait_timeout=0)
            self.assertTrue(holder.acquire())

            # 等待 0.2 秒后仍拿不到锁 → False。
            waiter = RunLock(lock_path, lock_wait_timeout=0.2)
            start = time.monotonic()
            self.assertFalse(waiter.acquire())

            # 确实等了至少约 0.2 秒（轮询粒度 0.1s）。
            self.assertGreaterEqual(time.monotonic() - start, 0.19)

            holder.release()

    def test_context_manager_releases_lock(self) -> None:
        """with 块退出后锁自动释放。"""

        # 进入时拿锁，退出时释放。
        with TemporaryDirectory() as d:
            lock_path = Path(d) / "backup.lock"
            with RunLock(lock_path, lock_wait_timeout=0):
                self.assertTrue(lock_path.exists())
            self.assertFalse(lock_path.exists())

    def test_release_missing_lock_is_ignored(self) -> None:
        """释放不存在的锁文件不抛异常。"""

        # 未获取过直接释放。
        with TemporaryDirectory() as d:
            RunLock(Path(d) / "backup.lock").release()


# 支持直接运行。
if __name__ == "__main__":
    unittest.main()
