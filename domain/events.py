"""备份领域的领域事件定义。

事件是已经发生的事实的不可变描述；后续通知、清单投影
和审计逻辑只消费这些事件，不反向修改领域对象。
"""

# 延迟类型注解。
from __future__ import annotations

# frozen dataclass 保证事件发布后不能被篡改。
from dataclasses import dataclass

# 事件字段使用领域值对象，避免散落的裸字符串和数字。
from domain.model.value_objects import (
    BackupTime,          # 事件时间。
    DbName,              # 数据库名。
    ExitCode,            # 聚合退出码。
    FileName,            # 备份文件名。
    RunStatus,           # 聚合状态。
    SizeBytes,           # 产物大小。
    VerificationLevel,   # 校验级别。
)


# 所有事件共同携带 run_id 和发生时间。
@dataclass(frozen=True)
class DomainEvent:
    """所有领域事件的公共字段。"""

    run_id: str        # 关联的备份运行。
    occurred_at: BackupTime # 带时区事件时间。


# 备份运行开始事件。
@dataclass(frozen=True)
class BackupRunStarted(DomainEvent):
    """一次备份运行开始。"""

    scope: str # 本次运行范围，例如 all/list/tables。


# 单库成功事件，包含可恢复产物摘要。
@dataclass(frozen=True)
class DatabaseBackupSucceeded(DomainEvent):
    """单个数据库备份成功。"""

    db_name: DbName         # 成功数据库。
    file_name: FileName     # 领域生成的文件名。
    size_bytes: SizeBytes   # 产物大小。
    elapsed_seconds: float  # 本次转储耗时。


# 单库失败事件；will_retry 用于区分告警策略。
@dataclass(frozen=True)
class DatabaseBackupFailed(DomainEvent):
    """单个数据库备份失败；RETRYING 阶段也可发布用于告警。"""

    db_name: DbName          # 失败数据库。
    attempts: int            # 当前已尝试次数。
    will_retry: bool         # 是否仍会重试。
    error_digest: str        # 已脱敏错误摘要。
    elapsed_seconds: float   # 本次尝试耗时。


# 聚合完成事件，同时暴露状态与退出码。
@dataclass(frozen=True)
class BackupRunCompleted(DomainEvent):
    """一次备份聚合完成，状态与退出码已计算。"""

    status: RunStatus     # SUCCESS / PARTIAL_SUCCESS / FAILED。
    exit_code: ExitCode   # 调度器可感知退出码。


# 校验失败事件，供通知和可用性审计使用。
@dataclass(frozen=True)
class VerificationFailed(DomainEvent):
    """备份产物校验失败，产物将进入 Unavailable 状态。"""

    db_name: DbName            # 失败产物数据库。
    file_name: FileName        # 失败产物文件名。
    level: VerificationLevel   # 失败时使用的校验级别。
    reason: str                # 已脱敏失败原因。


# 产物删除事件，用于记录保留策略执行结果。
@dataclass(frozen=True)
class ArtifactDeleted(DomainEvent):
    """备份产物被保留策略清理。"""

    db_name: DbName      # 被删除产物数据库。
    file_name: FileName  # 被删除产物文件名。
    relative_path: str   # 相对 dest_dir 的路径。


# 磁盘空间不足事件，属于运行前预检或执行中发现的风险事实。
@dataclass(frozen=True)
class DiskSpaceLow(DomainEvent):
    """磁盘可用空间低于运行前检查阈值。"""

    path: str             # 检查的目标路径。
    free_bytes: int       # 剩余字节数。
    required_bytes: int   # 所需字节数。
