"""值对象包：不可变、按值相等、无独立生命周期的领域对象。

包含备份领域的全部值对象与基础领域异常 DomainError；
本包对外统一导出，调用方从 domain.model.value_objects 直接导入。
"""

# 值对象与领域异常集中在单个模块中，保持纯领域语义。
from domain.model.value_objects.value_objects import (
    Availability,          # 产物可用性。
    BackupScope,           # 备份范围。
    BackupTime,            # 带时区时间。
    Compression,           # 压缩方式。
    DbName,                # 数据库名。
    DomainError,           # 领域规则异常。
    DumpResult,            # 外部转储结果。
    ExitCode,              # 进程退出码。
    FileName,              # 领域生成文件名。
    RetentionTier,         # 保留档位。
    RunStatus,             # 聚合整体状态。
    Sha256,                # 内容摘要。
    SizeBytes,             # 字节数。
    TaskStatus,            # 单任务状态。
    VerificationLevel,     # 校验级别。
)

__all__ = [
    "Availability",
    "BackupScope",
    "BackupTime",
    "Compression",
    "DbName",
    "DomainError",
    "DumpResult",
    "ExitCode",
    "FileName",
    "RetentionTier",
    "RunStatus",
    "Sha256",
    "SizeBytes",
    "TaskStatus",
    "VerificationLevel",
]
