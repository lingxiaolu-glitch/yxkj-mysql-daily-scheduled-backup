"""运行锁：基于锁文件的并发保护，防止同一实例同时执行多个备份任务。"""

# 延迟类型注解。
from __future__ import annotations

# os 负责锁文件的排他创建与 PID 写入。
import os
# time 负责等待与超时判断。
import time
# Path 表示锁文件路径。
from pathlib import Path


class RunLock:
    """基于锁文件的运行锁（FR-14 并发保护）。

    通过 O_CREAT|O_EXCL 排他创建锁文件实现互斥；
    lock_wait_timeout=0 表示拿不到锁就立即跳过，>0 时最多等待该秒数。
    """

    # 每次轮询等待间隔（秒）。
    _POLL_INTERVAL = 0.1

    def __init__(self, lock_path: Path, lock_wait_timeout: float = 0) -> None:
        # 锁文件路径必须存在父目录，由调用方保证。
        self._lock_path = lock_path
        # 获取锁的最长等待时间（秒）。
        self._lock_wait_timeout = lock_wait_timeout

    def acquire(self) -> bool:
        """尝试获取运行锁；成功返回 True，超时或拿不到返回 False。"""

        # 计算等待截止时刻（monotonic 不受系统改时间影响）。
        deadline = time.monotonic() + self._lock_wait_timeout

        while True:
            try:
                # O_EXCL：仅当锁文件不存在时创建成功，否则抛 FileExistsError。
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                # 写入当前进程 PID，便于人工排查持锁者。
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                # 拿到锁。
                return True
            except FileExistsError:
                # 已有人持锁：超时则本轮跳过，否则等待后重试。
                if time.monotonic() >= deadline:
                    return False
                time.sleep(self._POLL_INTERVAL)

    def release(self) -> None:
        """释放运行锁；锁文件不存在时静默忽略。"""

        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            # 锁已被清理，无需处理。
            pass

    def __enter__(self) -> "RunLock":
        # 上下文管理器：进入时获取锁。
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # 上下文管理器：无论成功失败都释放锁。
        self.release()
