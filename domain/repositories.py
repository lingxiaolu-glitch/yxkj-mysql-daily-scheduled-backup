"""领域层端口定义。

Protocol 只描述调用方需要的最小能力；基础设施层负责实现。
领域层不 import 具体存储、进程、网络或配置实现。
"""

# 延迟类型注解。
from __future__ import annotations

# dataclass 用于描述端口间传递的数据结果。
from dataclasses import dataclass
# Iterable 表示流式字节块；Protocol 定义结构化接口；runtime_checkable 支持 isinstance 检查。
from typing import Iterable, Protocol, runtime_checkable

# 导入事件、聚合、任务和值对象，端口签名使用领域语言。
from domain.events import DomainEvent
from domain.model.aggregates.backup_run import BackupRun
from domain.model.entities.database_backup_task import DatabaseBackupTask
from domain.model.value_objects import (
    BackupTime,
    DbName,
    DumpResult,
    FileName,
)


# 备份运行清单的持久化契约。
@runtime_checkable
class BackupRunRepository(Protocol):
    """备份运行清单仓库端口，由 JSON manifest 仓库实现。"""

    def save(self, run: BackupRun) -> None:
        """保存或覆盖一次备份运行投影。"""

    def find(self, run_id: str) -> BackupRun | None:
        """按 run_id 读取备份运行；不存在时返回 None。"""

    def find_by_date(self, date_key: str) -> tuple[BackupRun, ...]:
        """读取某个 YYYYMMDD 日期的全部备份运行。"""


# mysqldump 防腐层的最小调用契约。
@runtime_checkable
class DumpExecutor(Protocol):
    """mysqldump 防腐层需要实现的最小端口。"""

    def dump(self, task: DatabaseBackupTask) -> DumpResult:
        """执行数据库转储，并把外部结果翻译为 DumpResult。"""


# 表数量统计结果，用于 L1 结构校验。
@dataclass(frozen=True)
class DatabaseTableCount:
    """源库表数量统计结果。"""

    database: DbName # 被统计数据库。
    count: int       # 表数量。


# MySQL CLI 网关端口；后续 MysqlCliClient 实现。
@runtime_checkable
class MySqlGateway(Protocol):
    """MySQL CLI 网关端口：枚举、结构比对和影子库演练能力。"""

    def list_databases(self) -> tuple[DbName, ...]:
        """枚举业务数据库，实现层应过滤系统库。"""

    def count_tables(self, database: DbName) -> int:
        """统计源数据库表数量，用于 L1 结构校验。"""

    def create_shadow_database(self, source: DbName, shadow: DbName) -> None:
        """将备份恢复到影子库。"""

    def drop_database(self, database: DbName) -> None:
        """删除影子库。"""

    def count_table_rows(self, database: DbName, table: str) -> int:
        """统计表行数，用于 L2 抽样比对。"""


# 存储层写入结果的领域描述。
@dataclass(frozen=True)
class StoredArtifact:
    """存储层写入结果：文件相对路径、大小和摘要。"""

    relative_path: str # 相对 dest_dir 路径。
    size_bytes: int    # 实际写入字节数。
    sha256: str        # 实际内容摘要。


# 本地或远端备份存储的抽象端口。
@runtime_checkable
class ArtifactStorage(Protocol):
    """备份产物存储端口，由本地文件存储实现。"""

    def write_chunks(
        self,
        file_name: FileName,
        chunks: Iterable[bytes],
    ) -> StoredArtifact:
        """写入备份字节流，并返回大小与 SHA256。"""

    def exists(self, relative_path: str) -> bool:
        """判断产物是否仍存在于 dest_dir 内。"""

    def read_bytes(self, relative_path: str) -> bytes:
        """读取产物字节；具体校验由领域服务编排。"""

    def delete(self, relative_path: str) -> None:
        """在路径防越界校验后删除产物。"""


# 压缩策略端口。
@runtime_checkable
class Compressor(Protocol):
    """压缩策略端口；实现层负责流式处理。"""

    @property
    def suffix(self) -> str:
        """压缩产物后缀，例如 .gz、.zst 或空字符串。"""

    def compress(self, chunks: Iterable[bytes]) -> Iterable[bytes]:
        """把输入字节流压缩为输出字节流。"""


# 通知通道端口；日志/Webhook/SMTP 实现同一契约。
@runtime_checkable
class Notifier(Protocol):
    """通知端口；日志、Webhook、SMTP 都实现同一接口。"""

    def notify(self, event: DomainEvent) -> None:
        """发送一个领域事件对应的通知。"""


# 时间端口，让领域测试可注入 FakeClock。
@runtime_checkable
class Clock(Protocol):
    """时间端口；领域和测试不直接依赖系统时钟。"""

    def now(self) -> BackupTime:
        """返回带时区的当前备份时间。"""
