"""保留策略纯函数服务：计算清理计划，无任何 IO。"""

# 延迟类型注解。
from __future__ import annotations

# Iterable 表示产物集合输入。
from collections.abc import Iterable
# dataclass 定义不可变的清理计划结果。
from dataclasses import dataclass
# timedelta 用于计算日备保留截止日期。
from datetime import timedelta

# 导入产物实体与带时区时间值对象。
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.value_objects import BackupTime, DomainError


@dataclass(frozen=True)
class CleanupPlan:
    """保留服务计算出的清理计划（纯数据输出，不包含任何 IO）。"""

    to_delete: tuple[BackupArtifact, ...]  # 需要删除的过期产物。
    to_keep: tuple[BackupArtifact, ...]    # 已过期但受周/月保护而保留的产物。

    @property
    def deleted_names(self) -> tuple[str, ...]:
        """供 manifest / 日志记录的删除文件名。"""

        # 返回被清理产物的完整文件名。
        return tuple(str(artifact.file_name) for artifact in self.to_delete)


class RetentionService:
    """日/周/月分级保留策略（纯函数，无副作用、可单测）。"""

    # 周备默认落在每周第一个备份日（周一，weekday()==0）。
    WEEKLY_WEEKDAY = 0
    # 月备默认落在每月 1 日。
    MONTHLY_DAY = 1

    def plan(
        self,
        days: int,
        weekly: int,
        monthly: int,
        artifacts: Iterable[BackupArtifact],
        now: BackupTime,
    ) -> CleanupPlan:
        """按保留档位计算清理计划。

        日备：保留最近 days 天内的产物，更早的进入候选清理；
        周备/月备：weekly/monthly > 0 时，最近 N 份周/月标记产物受保护，
        即使已超过日备期限也不删除（默认关闭即传 0）。
        """
        # 档位不能为负，避免误删或异常语义。
        if days < 0 or weekly < 0 or monthly < 0:
            raise DomainError("保留天数与周/月份数不能为负数")

        # 按创建时间倒序排列，便于取"最近 N 份"。
        items = sorted(artifacts, key=lambda a: a.created_at.value, reverse=True)

        # 日备截止日期：早于该日期的产物视为过期。
        cutoff = (now.value - timedelta(days=days)).date()
        expired = [a for a in items if a.created_at.value.date() < cutoff]

        # 受保护的过期产物（周备/月备标记文件）。
        # BackupArtifact 不可哈希，因此用列表按值比较。
        protected: list[BackupArtifact] = []
        if weekly > 0:
            # 每周标记日产生的备份，额外保留最近 weekly 份。
            weekly_marked = [a for a in items if a.created_at.value.weekday() == self.WEEKLY_WEEKDAY]
            protected.extend(weekly_marked[:weekly])
        if monthly > 0:
            # 每月标记日产生的备份，额外保留最近 monthly 份。
            monthly_marked = [a for a in items if a.created_at.value.day == self.MONTHLY_DAY]
            protected.extend(monthly_marked[:monthly])

        # 过期产物中：受保护的保留，其余删除。
        to_delete = [a for a in expired if a not in protected]
        to_keep = [a for a in expired if a in protected]
        return CleanupPlan(to_delete=tuple(to_delete), to_keep=tuple(to_keep))
