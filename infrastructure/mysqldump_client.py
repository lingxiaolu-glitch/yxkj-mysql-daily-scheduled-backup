"""mysqldump 防腐层（ACL）：把外部 mysqldump 封装成领域 DumpExecutor 端口。

职责（PRD 7.3.6）：
- 按 MySQL 8.0 参数组装命令（--set-gtid-purged=OFF 等）；
- 流式执行 mysqldump：stdout 经 Compressor 压缩后写入 ArtifactStorage，
  不产生未压缩的中间大文件；
- 捕获 stderr 并脱敏、映射退出码为 DumpResult；
- 进程无法启动等致命错误抛 DumpFailed（区别于转储失败）。
- 配置 schema_only=true 表示“额外输出仅表结构文件”：dump() 先生成完整
  数据+结构备份，再生成 schema 文件；只有两个产物都成功才算任务成功。
"""

# 延迟类型注解，保持模块导入轻量。
from __future__ import annotations

# subprocess 执行外部 mysqldump；time 统计转储耗时。
import subprocess
import time
# Iterable/Iterator 描述字节流；Mapping 描述 db:tables 组合。
from collections.abc import Iterable, Iterator, Mapping

# 领域实体、值对象与端口。
from domain.model.entities.database_backup_task import DatabaseBackupTask
from domain.model.value_objects import (
    Compression,       # 领域压缩方式枚举。
    DomainError,       # 领域规则异常基类。
    DumpResult,        # 防腐层翻译后的转储结果。
    FileName,          # 领域生成的文件名。
)
from domain.repositories import (
    ArtifactStorage,   # 存储端口：写入压缩产物。
    Clock,             # 时间端口：提供产物创建时间。
    Compressor,        # 压缩端口：流式压缩 stdout。
    StoredArtifact,    # 存储写入结果。
)
# 配置对象与脱敏工具。
from infrastructure.config_loader import BackupConfig, CompressionType, MysqlConfig
from infrastructure.logging_utils import redact


class DumpFailed(DomainError):
    """mysqldump 无法启动等致命执行错误（不是转储结果，而是执行层失败）。"""


def _parse_tables_by_db(databases: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """解析配置里的 db:tables 组合，返回「库名 -> 表名元组」映射。"""

    # 遍历每个 databases 条目。
    result: dict[str, tuple[str, ...]] = {}
    for entry in databases:
        # 仅处理含冒号的库表组合（如 "db1:t1,t2"）。
        if ":" in entry:
            # 冒号前是库名，冒号后是逗号分隔的表名列表。
            db, _, tables = entry.partition(":")
            # 过滤空表名后保存。
            result[db] = tuple(t for t in tables.split(",") if t)
    return result


class MysqldumpClient:
    """mysqldump 防腐层适配器：实现领域 DumpExecutor 端口。

    依赖注入：mysql 配置、backup 配置、存储、压缩器与时钟，
    领域层与调用方都不接触真实子进程细节。
    """

    def __init__(
        self,
        mysql: MysqlConfig,
        backup: BackupConfig,
        storage: ArtifactStorage,
        compressor: Compressor,
        clock: Clock,
    ) -> None:
        # 保存连接配置与备份配置。
        self._mysql = mysql
        self._backup = backup
        # 保存注入的存储/压缩/时钟端口。
        self._storage = storage
        self._compressor = compressor
        self._clock = clock
        # 配置压缩方式 -> 领域压缩方式（决定文件名后缀）。
        self._compression = {
            CompressionType.GZIP: Compression.GZIP,
            CompressionType.ZSTD: Compression.ZSTD,
            CompressionType.NONE: Compression.NONE,
        }[backup.compress]
        # 解析 db:tables 组合，供 --tables 参数使用。
        self._tables_by_db = _parse_tables_by_db(backup.databases)
        # 最近一次成功完整转储的存储结果与领域文件名。
        self._last_stored: StoredArtifact | None = None
        self._last_file_name: FileName | None = None
        # 最近一次成功 schema-only 转储的存储结果与领域文件名。
        self._last_schema_stored: StoredArtifact | None = None
        self._last_schema_file_name: FileName | None = None

    @property
    def last_stored(self) -> StoredArtifact | None:
        """最近一次成功完整转储写入存储的结果（失败为 None）。"""

        # 上层（步骤 10 装配）据此构造备份产物实体。
        return self._last_stored

    @property
    def last_schema_stored(self) -> StoredArtifact | None:
        """最近一次成功 schema-only 转储的存储结果。"""

        # 配置开启额外结构文件时由 dump() 内部自动生成。
        return self._last_schema_stored

    @property
    def last_file_name(self) -> FileName | None:
        """最近一次完整转储的领域文件名。"""
        return self._last_file_name

    @property
    def last_schema_file_name(self) -> FileName | None:
        """最近一次 schema-only 转储的领域文件名。"""
        return self._last_schema_file_name

    def dump(self, task: DatabaseBackupTask) -> DumpResult:
        """执行完整数据库转储；schema_only=true 时额外生成仅结构文件。

        返回结果表示完整产物与（可选的）schema 产物的联合结果：
        任一必需产物失败都会返回失败，使领域服务按重试规则重新执行。
        """
        # 每次调用必须清空上一次结果，避免跨任务串档。
        self._last_stored = None
        self._last_file_name = None
        self._last_schema_stored = None
        self._last_schema_file_name = None

        # 第一步：完整表结构 + 数据。
        full = self._dump_once(task, schema_only=False)
        if not full.success:
            return full

        # 未开启额外结构文件时，完整产物就是最终结果。
        if not self._backup.schema_only:
            return full

        # 第二步：额外仅结构文件（--no-data）。
        schema = self._dump_once(task, schema_only=True)
        if not schema.success:
            # 完整文件已经生成但 schema 文件失败，不能把不完整产物留作成功候选。
            if self._last_stored is not None:
                self._storage.delete(self._last_stored.relative_path)
            self._last_stored = None
            self._last_file_name = None
            self._last_schema_file_name = None
            return DumpResult(
                success=False,
                return_code=schema.return_code,
                elapsed_seconds=full.elapsed_seconds + schema.elapsed_seconds,
                error_digest=schema.error_digest,
            )

        # 两个产物都成功，返回联合结果；上层可读取 last_stored / last_schema_stored。
        return DumpResult(
            success=True,
            return_code=0,
            elapsed_seconds=full.elapsed_seconds + schema.elapsed_seconds,
            error_digest="",
        )

    def dump_schema(self, task: DatabaseBackupTask) -> DumpResult:
        """单独执行 schema-only 转储（测试或未来按需生成场景使用）。"""
        self._last_schema_stored = None
        self._last_schema_file_name = None
        return self._dump_once(task, schema_only=True)

    def _dump_once(self, task: DatabaseBackupTask, schema_only: bool) -> DumpResult:
        """执行一次 mysqldump 并翻译为 DumpResult。"""

        # 由任务 + 当前时间 + 配置生成领域文件名（schema_only 决定前缀/参数）。
        file_name = FileName(
            db_name=task.db_name,
            backup_time=self._clock.now(),
            compression=self._compression,
            schema_only=schema_only,
        )

        # 组装 mysqldump 命令参数。
        argv = self._build_argv(task, schema_only)

        # 记录开始时刻，用于计算耗时。
        started = time.monotonic()

        # 启动子进程；stdin 丢弃，stdout/stderr 走管道供流式读取。
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            # 二进制不存在/无权限属于致命错误，抛 DumpFailed。
            raise DumpFailed(f"无法启动 mysqldump：{exc}") from exc

        # 记录本次存储写入结果与错误摘要。
        stored: StoredArtifact | None = None
        try:
            # 流式管道：stdout 分块 -> 压缩 -> 写入存储（低内存、无中间文件）。
            compressed = self._compressor.compress(self._iter_stdout(proc))
            stored = self._storage.write_chunks(file_name, compressed)

            # 等待进程结束并取退出码。
            returncode = proc.wait()

            # 读取 stderr 原文并脱敏（密码等敏感信息替换为 ***）。
            stderr_text = proc.stderr.read().decode("utf-8", errors="replace")
            error_digest = redact(stderr_text.strip(), [self._mysql.password])
        except Exception as exc:
            # 管道/存储写入异常：先终止子进程避免残留。
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait()
            # 作为转储失败返回，让其他库继续（部分失败隔离）。
            return DumpResult(
                success=False,
                return_code=-1,
                elapsed_seconds=time.monotonic() - started,
                error_digest=f"流式写入失败：{exc}",
            )

        # 非零退出码说明转储失败：清理已写入的半成品文件。
        if returncode != 0 and stored is not None:
            self._storage.delete(stored.relative_path)
            stored = None

        # 记录本次存储结果（仅成功时有效）。
        if schema_only:
            self._last_schema_stored = stored
            self._last_schema_file_name = file_name if stored is not None else None
        else:
            self._last_stored = stored
            self._last_file_name = file_name if stored is not None else None

        # 翻译为领域结果。
        return DumpResult(
            success=returncode == 0,
            return_code=returncode,
            elapsed_seconds=time.monotonic() - started,
            error_digest=error_digest,
        )

    def _build_argv(self, task: DatabaseBackupTask, schema_only: bool = False) -> list[str]:
        """组装 mysqldump 命令参数（MySQL 8.0）。"""

        # 可执行文件路径（可用绝对路径或 PATH 中的名字）。
        argv: list[str] = [self._backup.mysqldump_path]

        # 连接参数。
        argv.append(f"--host={self._mysql.host}")
        argv.append(f"--port={self._mysql.port}")
        argv.append(f"--user={self._mysql.user}")
        # 密码走 --password=；日志层用 redact_command 遮蔽，不落盘。
        argv.append(f"--password={self._mysql.password}")

        # MySQL 8.0 一致性/完整性参数。
        argv.append("--set-gtid-purged=OFF")
        argv.append("--single-transaction")
        argv.append("--quick")
        argv.append("--routines")
        argv.append("--triggers")
        argv.append("--events")

        # 仅结构文件额外加 --no-data；完整文件不得加该参数。
        if schema_only:
            argv.append("--no-data")

        # 用户自定义额外参数（如 --hex-blob）。
        argv.extend(self._backup.extra_args)

        # 目标库：库表组合用「库名 + 表名列表」，否则用 --databases 库名。
        tables = self._tables_by_db.get(str(task.db_name), ())
        if tables:
            # 指定表时不用 --databases，直接「库名 表1 表2」。
            argv.append(str(task.db_name))
            argv.extend(tables)
        else:
            # 整库转储：--databases 保证 CREATE DATABASE 语句包含在输出中。
            argv.append("--databases")
            argv.append(str(task.db_name))

        return argv

    @staticmethod
    def _iter_stdout(proc) -> Iterator[bytes]:
        """分块读取子进程 stdout，供压缩器逐块消费。"""

        # 每块 8KB，内存占用恒定。
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            yield chunk