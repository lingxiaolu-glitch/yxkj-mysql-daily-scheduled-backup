"""备份产物实体：表示一个可审计、可校验、可清理的备份文件。"""

# 延迟类型注解；本文件使用 Python 3.10+ 的联合类型语法。
from __future__ import annotations

# dataclass 表示备份产物实体；产物生命周期内状态会变化。
from dataclasses import dataclass

# 导入产物生命周期所需的值对象和领域异常。
from domain.model.value_objects import (
    Availability,        # 产物当前可用性。
    BackupTime,          # 创建和校验时间。
    DbName,              # 产物所属数据库。
    DomainError,         # 领域规则异常。
    FileName,            # 领域生成的文件名。
    Sha256,              # 文件内容摘要。
    SizeBytes,           # 文件字节数。
    VerificationLevel,   # 最近一次校验级别。
)


# 专门区分“不可恢复”错误，恢复处理器可以精确捕获。
class ArtifactUnavailableError(DomainError):
    """非 Available 产物不允许作为恢复源。"""


# 普通 dataclass：产物创建后仍会因校验、清理而改变状态。
@dataclass
class BackupArtifact:
    """一个备份文件的生命周期实体。

    relative_path 必须是相对 dest_dir 的 POSIX 风格路径，
    具体磁盘读写与路径防越界由基础设施 ArtifactStorage 负责。
    """

    db_name: DbName                            # 产物所属业务库。
    file_name: FileName                        # 领域规则生成的文件名。
    relative_path: str                         # 相对 dest_dir 的存储路径。
    size_bytes: SizeBytes                      # 文件字节数。
    sha256: Sha256                             # 文件内容摘要。
    created_at: BackupTime                     # 产物创建时间。
    availability: Availability = Availability.PENDING_VERIFY # 新产物默认未校验。
    schema_only: bool = False                  # 是否仅表结构备份。
    deleted: bool = False                      # 是否已被保留策略清理。
    verification_error: str = ""               # 最近一次校验失败原因。
    verified_at: BackupTime | None = None      # 最近一次校验时间。
    verification_level: VerificationLevel | None = None # 最近一次校验级别。

    def __post_init__(self) -> None:
        # 产物、文件名、任务数据库必须三方一致，避免清单错挂。
        if self.db_name != self.file_name.db_name:
            raise DomainError("产物库名与文件名中的库名不一致")

        # 统一反斜杠为 POSIX 分隔符，再移除首尾斜杠。
        normalized = self.relative_path.replace("\\\\", "/").strip("/")

        # 拒绝空路径、绝对路径和 .. 路径段，防止存储层误处理越界路径。
        if (
            not normalized
            or normalized.startswith("/")
            or ".." in normalized.split("/")
        ):
            raise DomainError("产物路径必须是 dest_dir 内的相对路径")

        # 保存规范化后的相对路径。
        self.relative_path = normalized

    @property
    def is_available(self) -> bool:
        """当前是否可作为恢复源。"""

        # 删除或未通过校验都不可恢复。
        return not self.deleted and self.availability is Availability.AVAILABLE

    def verify(
        self,
        level: VerificationLevel,
        success: bool,
        verified_at: BackupTime,
        error: str = "",
    ) -> None:
        """记录校验结果并更新可用性。"""

        # 已删除文件不属于活动备份集。
        if self.deleted:
            raise DomainError("已删除产物不能重新校验")

        # 无论成功失败，都记录本次使用的校验级别。
        self.verification_level = level

        # 记录校验发生时间。
        self.verified_at = verified_at

        # 成功时清空旧错误；失败时保存可审计原因。
        self.verification_error = "" if success else error

        # 根据校验结果切换恢复可用性。
        if success:
            self.availability = Availability.AVAILABLE
        else:
            self.availability = Availability.UNAVAILABLE

    def ensure_available(self) -> str:
        """恢复门禁：只有可用产物才返回相对路径。"""

        # 已删除产物永远离开恢复候选集。
        if self.deleted:
            raise ArtifactUnavailableError("备份产物已删除，不能恢复")

        # PendingVerify 和 Unavailable 都必须拒绝。
        if self.availability is not Availability.AVAILABLE:
            reason = self.verification_error or self.availability.value
            raise ArtifactUnavailableError(f"备份产物不可用：{reason}")

        # 通过门禁后返回存储层可解析的相对路径。
        return self.relative_path

    def mark_deleted(self) -> None:
        """清理流程确认删除后更新实体状态。"""

        # 防止重复标记导致事件重复发布。
        if self.deleted:
            raise DomainError("备份产物已处于删除状态")

        # 进入删除终态，并立即取消恢复可用性。
        self.deleted = True
        self.availability = Availability.UNAVAILABLE
