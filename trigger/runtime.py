"""触发层运行装配：把配置组装成可测试的领域/基础设施依赖集合。"""

# 延迟类型注解。
from __future__ import annotations

# dataclass 描述运行时；Mapping 描述校验器注册表。
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any

# 端口与领域对象。
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.value_objects import VerificationLevel
from domain.repositories import (
    ArtifactStorage,
    Clock,
    Compressor,
    DumpExecutor,
    MySqlGateway,
    Notifier,
)
from domain.services.verification import VerificationResult

# 配置与基础设施实现。
from infrastructure.compressor import GzipCompressor, NoopCompressor, ZstdCompressor
from infrastructure.config_loader import AppConfig, CompressionType
from infrastructure.disk_space import DiskSpaceChecker
from infrastructure.file_storage import LocalFileStorage
from infrastructure.manifest_repository import JsonManifestRepository
from infrastructure.mysql_client import MysqlCliClient
from infrastructure.mysqldump_client import MysqldumpClient
from infrastructure.notifiers import create_notifier
from infrastructure.system_clock import SystemClock
from infrastructure.verifiers import FileIntegrityVerifier, RestoreVerifier, StructureVerifier


@dataclass
class Runtime:
    """一次备份/恢复运行所需的全部依赖。"""

    config: AppConfig
    clock: Clock
    storage: ArtifactStorage
    compressor: Compressor
    dump_executor: DumpExecutor
    gateway: MySqlGateway
    manifest_repository: JsonManifestRepository
    verifiers: Mapping[VerificationLevel, Callable[[BackupArtifact], VerificationResult]]
    notifier: Notifier
    disk_checker: DiskSpaceChecker


def _expected_table_counts(config: AppConfig) -> dict[str, int]:
    """从 db:tables 配置中提取指定表模式的期望表数。"""

    result: dict[str, int] = {}
    for entry in config.backup.databases:
        if ":" in entry:
            db, _, tables = entry.partition(":")
            result[db] = len([item for item in tables.split(",") if item])
    return result


def build_runtime(
    config: AppConfig,
    *,
    clock: Clock | None = None,
    storage: ArtifactStorage | None = None,
    compressor: Compressor | None = None,
    dump_executor: DumpExecutor | None = None,
    gateway: MySqlGateway | None = None,
    manifest_repository: JsonManifestRepository | None = None,
    verifiers: Mapping[VerificationLevel, Callable[[BackupArtifact], VerificationResult]] | None = None,
    notifier: Notifier | None = None,
    disk_checker: DiskSpaceChecker | None = None,
) -> Runtime:
    """根据配置创建运行时；测试可逐个注入 fake 覆盖默认实现。"""
    # 默认适配器。
    clock = clock or SystemClock()
    storage = storage or LocalFileStorage(config.backup.dest_dir)

    # 配置压缩方式 -> 流式压缩器。
    if compressor is None:
        if config.backup.compress is CompressionType.GZIP:
            compressor = GzipCompressor()
        elif config.backup.compress is CompressionType.NONE:
            compressor = NoopCompressor()
        else:
            compressor = ZstdCompressor()

    gateway = gateway or MysqlCliClient(config.mysql, config.backup.mysql_path)
    dump_executor = dump_executor or MysqldumpClient(
        config.mysql,
        config.backup,
        storage,
        compressor,
        clock,
    )
    manifest_repository = manifest_repository or JsonManifestRepository(config.backup.dest_dir)

    # 默认校验器：L0 始终执行，L1/L2 按 config.verify.level 由 handler 选择。
    expected_counts = _expected_table_counts(config)
    if verifiers is None:
        verifiers = {
            VerificationLevel.L0: FileIntegrityVerifier(storage),
            VerificationLevel.L1: StructureVerifier(
                storage,
                gateway,
                expected_counts=expected_counts,
            ),
            VerificationLevel.L2: RestoreVerifier(
                storage,
                gateway,
                shadow_db_prefix=config.verify.shadow_db_prefix,
                sample_tables=config.verify.sample_tables,
                expected_counts=expected_counts,
            ),
        }

    notifier = notifier or create_notifier(
        config.notify,
        secrets=[config.mysql.password],
    )
    disk_checker = disk_checker or DiskSpaceChecker(config.backup.min_free_bytes)

    return Runtime(
        config=config,
        clock=clock,
        storage=storage,
        compressor=compressor,
        dump_executor=dump_executor,
        gateway=gateway,
        manifest_repository=manifest_repository,
        verifiers=verifiers,
        notifier=notifier,
        disk_checker=disk_checker,
    )