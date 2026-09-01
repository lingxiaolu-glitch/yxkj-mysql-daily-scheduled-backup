"""系统时钟适配器：把领域 Clock 端口落地为真实系统时钟。"""

# 延迟类型注解。
from __future__ import annotations

# datetime 提供真实当前时间。
from datetime import datetime

# 导入领域时间值对象。
from domain.model.value_objects import BackupTime


class SystemClock:
    """实现领域 Clock 端口：返回带本地时区的当前时间。

    测试中可注入 FakeClock 替换本类，领域代码不直接依赖系统时钟。
    """

    def now(self) -> BackupTime:
        """返回带本地时区的当前备份时间。"""

        # astimezone() 补齐本地时区偏移，满足 BackupTime 必须携带时区的约束。
        return BackupTime(datetime.now().astimezone())
