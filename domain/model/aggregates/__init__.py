"""聚合根包：领域中的一致性边界对象。"""

# 聚合根在生命周期内维护整体状态、事件与退出码。
from domain.model.aggregates.backup_run import BackupRun

__all__ = ["BackupRun"]
