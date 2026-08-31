"""实体对象包：拥有唯一标识且生命周期内状态可变的对象。"""

# 实体对象可被聚合根组合，并在生命周期内变更状态。
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.entities.database_backup_task import DatabaseBackupTask

__all__ = ["BackupArtifact", "DatabaseBackupTask"]
