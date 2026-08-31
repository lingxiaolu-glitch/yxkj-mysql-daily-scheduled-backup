"""备份领域的值对象与基础领域异常。

本模块保持纯领域语义：只使用标准库类型和规则校验，
不引入文件 IO、进程、网络或基础设施依赖。
"""

# 启用延迟类型注解，让字段注解以字符串形式保存，避免运行期解析开销。
from __future__ import annotations

# 正则模块：校验库名、SHA256 这类固定格式。
import re
# dataclass 用于定义不可变值对象；field 支持声明非初始化派生字段。
from dataclasses import dataclass, field
# datetime 是 BackupTime 的底层值；领域层不直接读取系统时钟。
from datetime import datetime
# Enum 表示有限业务枚举；IntEnum 表示退出码需要与进程返回码兼容。
from enum import Enum, IntEnum

# 领域规则违反统一使用 DomainError，便于上层转换为用户可读错误。
class DomainError(ValueError):
    """领域规则违反时抛出的基础异常。"""


# 压缩方式是有限集合，使用 str Enum 可直接序列化为 manifest 中的小写字符串。
class Compression(str, Enum):
    """备份输出压缩方式。"""

    GZIP = "gzip"       # 默认方案，标准库和系统工具支持最广。
    ZSTD = "zstd"       # 可选方案，预留给更大备份量的压缩需求。
    NONE = "none"       # 不压缩；用于排查压缩层问题或特殊恢复场景。


# 备份范围与配置中的 databases=["all"] / 指定库 / 指定表语义对应。
class BackupScope(str, Enum):
    """备份范围：全部业务库、指定库列表、库表组合。"""

    ALL = "all"         # 枚举全部业务库，并排除系统库。
    LIST = "list"       # 指定数据库列表。
    TABLES = "tables"   # 指定数据库和表组合。


# 保留档位供后续 RetentionService 决定哪些文件受额外保护。
class RetentionTier(str, Enum):
    """保留档位：日常、周备、月备。"""

    DAILY = "daily"     # 按保留天数管理的日常备份。
    WEEKLY = "weekly"   # 指定周备份，默认关闭。
    MONTHLY = "monthly" # 指定月备份，默认关闭。


# L0/L1/L2 与 PRD 的文件级、结构级、恢复级校验对应。
class VerificationLevel(str, Enum):
    """完整性校验级别。"""

    L0 = "L0"           # 文件非空、压缩合法、包含 Dump completed。
    L1 = "L1"           # 建表数量与源库比对。
    L2 = "L2"           # 影子库恢复与抽样比对。


# 只有 Available 产物可作为恢复源。
class Availability(str, Enum):
    """备份产物的恢复可用性。"""

    AVAILABLE = "available"           # 校验通过，可恢复。
    UNAVAILABLE = "unavailable"       # 校验失败或已被删除。
    PENDING_VERIFY = "pending_verify" # 尚未校验，不能直接恢复。


# 调度器只能感知进程退出码，这里固定 PRD 约定。
class ExitCode(IntEnum):
    """调度器可感知的进程退出码约定。"""

    SUCCESS = 0            # 全部成功。
    PARTIAL_SUCCESS = 1    # 部分失败。
    FAILED = 2             # 全部失败或系统异常。


# 任务状态覆盖 pending -> running -> retrying/success/failed。
class TaskStatus(str, Enum):
    """单个数据库备份任务状态。"""

    PENDING = "pending"     # 已加入 run，尚未执行。
    RUNNING = "running"     # 正在执行转储。
    RETRYING = "retrying"   # 最近一次失败但仍允许重试。
    SUCCESS = "success"     # 最终成功。
    FAILED = "failed"       # 最终失败。


# RunStatus 是聚合根根据全部任务终态计算出的整体状态。
class RunStatus(str, Enum):
    """一次备份聚合的整体状态。"""

    RUNNING = "running"               # 聚合尚未 finish。
    SUCCESS = "success"               # 所有任务成功。
    PARTIAL_SUCCESS = "partial_success" # 至少一个成功且至少一个失败。
    FAILED = "failed"                 # 所有任务失败，或没有任务。


# DbName 是不可变值对象；相同字符串自动相等。
@dataclass(frozen=True)
class DbName:
    """合法 MySQL 业务库名。"""

    value: str # 保存原始合法库名。

    def __post_init__(self) -> None:
        # 构造时立即校验，避免非法库名进入命令、路径或 manifest。
        if not re.fullmatch(r"[A-Za-z0-9_$]+", self.value):
            raise DomainError(f"非法数据库名：{self.value!r}")

    def __str__(self) -> str:
        # 输出原始库名，便于日志和命令构造。
        return self.value


# BackupTime 强制携带时区，防止服务器本地时区不同导致清单时间不可比较。
@dataclass(frozen=True)
class BackupTime:
    """带时区的备份时间，禁止使用无时区 datetime。"""

    value: datetime # 保存 timezone-aware datetime。

    def __post_init__(self) -> None:
        # tzinfo 存在但 utcoffset 为 None 也应拒绝。
        if self.value.tzinfo is None or self.value.utcoffset() is None:
            raise DomainError("备份时间必须携带时区")

    @property
    def date_key(self) -> str:
        """manifest 文件名使用的 YYYYMMDD。"""

        # 格式化成 8 位日期，例如 20260810。
        return f"{self.value:%Y%m%d}"

    @property
    def time_key(self) -> str:
        """备份文件名使用的 HHMMSS。"""

        # 格式化成 6 位时间，例如 020000。
        return f"{self.value:%H%M%S}"

    def __str__(self) -> str:
        # 使用 ISO 8601，保留时区偏移，便于审计。
        return f"{self.value.isoformat()}"


# FileName 不接受外部传入文件名；由 DbName + BackupTime + Compression 规则生成。
@dataclass(frozen=True)
class FileName:
    """由领域规则生成的备份文件名，杜绝调用方手工拼接。"""

    db_name: DbName                    # 备份目标数据库。
    backup_time: BackupTime            # 带时区的备份时间。
    compression: Compression           # 决定文件后缀。
    schema_only: bool = False          # 是否仅导出表结构。
    value: str = field(init=False)     # 由 __post_init__ 生成的完整文件名。

    def __post_init__(self) -> None:
        # 仅结构备份使用 schema 前缀，普通备份直接以库名开头。
        prefix = f"{self.db_name}_schema_" if self.schema_only else f"{self.db_name}_"

        # SQL 基础文件名先固定为 .sql。
        stem = f"{prefix}{self.backup_time.date_key}_{self.backup_time.time_key}.sql"

        # frozen dataclass 中派生字段必须通过 object.__setattr__ 写入。
        if self.compression is Compression.GZIP:
            object.__setattr__(self, "value", f"{stem}.gz")
        elif self.compression is Compression.ZSTD:
            object.__setattr__(self, "value", f"{stem}.zst")
        else:
            object.__setattr__(self, "value", stem)

    def __str__(self) -> str:
        # 返回完整文件名，供存储层和 manifest 使用。
        return self.value


# Sha256 只接受 64 位小写十六进制，保证 manifest 中格式一致。
@dataclass(frozen=True)
class Sha256:
    """SHA256 校验值，统一保存为小写十六进制。"""

    value: str # 64 位小写摘要。

    def __post_init__(self) -> None:
        # 白名单校验大小写和长度，避免大写或截断值进入清单。
        if not re.fullmatch(r"[0-9a-f]{64}", self.value):
            raise DomainError("SHA256 必须是 64 位小写十六进制字符")

    def __str__(self) -> str:
        # 返回摘要字符串。
        return self.value


# SizeBytes 明确表示字节数，避免后续把 KB/MB 误存到同一字段。
@dataclass(frozen=True)
class SizeBytes:
    """备份文件大小；0 允许由后续校验判定为不可用。"""

    value: int # 非负字节数。

    def __post_init__(self) -> None:
        # 文件大小不可能为负。
        if self.value < 0:
            raise DomainError("文件大小不能为负数")

    def __int__(self) -> int:
        # 支持显式转换回 int，便于计算空间和比较。
        return self.value


# DumpResult 是外部 mysqldump 结果经防腐层翻译后的领域事实。
@dataclass(frozen=True)
class DumpResult:
    """mysqldump 执行结果经防腐层翻译后的领域值对象。"""

    success: bool             # 外部进程是否成功。
    return_code: int          # 外部进程退出码。
    elapsed_seconds: float    # 转储耗时（秒）。
    error_digest: str = ""    # 已脱敏、截断后的错误摘要。
