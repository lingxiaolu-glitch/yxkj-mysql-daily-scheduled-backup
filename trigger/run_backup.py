"""触发层：备份命令处理器，串起预检、执行、校验、清理与通知。"""

# 延迟类型注解。
from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

# 领域事件、聚合与实体。
from domain.events import (
    BackupRunCompleted,
    BackupRunSkipped,
    DatabaseBackupFailed,
    DiskSpaceLow,
    DomainEvent,
    VerificationFailed,
)
from domain.model.aggregates.backup_run import BackupRun
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.entities.database_backup_task import DatabaseBackupTask
from domain.model.value_objects import (
    BackupScope,
    BackupTime,
    Compression,
    DbName,
    DomainError,
    ExitCode,
    FileName,
    RunStatus,
    Sha256,
    SizeBytes,
    VerificationLevel,
)
from domain.services.backup_execution import BackupExecutionService
from domain.services.verification import VerificationResult, VerificationService

# 配置与运行时。
from infrastructure.config_loader import (
    AppConfig,
    BackupConfig,
    CompressionType,
    VerifyLevel,
    load_config,
)
from infrastructure.logging_utils import LOGGER_NAME, setup_logging
from infrastructure.run_lock import RunLock
from trigger.runtime import Runtime, build_runtime

# 校验器需要的 StoredArtifact 只是端口类型，运行时已注入。
from domain.repositories import StoredArtifact


class RunBackupCommandHandler:
    """执行一次完整备份，并返回 PRD 约定的退出码。"""

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        runtime: Runtime | None = None,
        runtime_factory: Callable[[AppConfig], Runtime] | None = None,
        artifact_factory: Callable[[DatabaseBackupTask, BackupTime], BackupArtifact] | None = None,
        check_binaries: bool = True,
    ) -> None:
        # 保存入口配置与可选注入。
        self._config_path = Path(config_path) if config_path is not None else None
        self._env = env
        self._runtime = runtime
        self._runtime_factory = runtime_factory
        self._artifact_factory = artifact_factory
        self._check_binaries = check_binaries

    def execute(self, config_path: str | Path | None = None) -> int:
        """加载配置并执行备份；任何未预期异常都映射为退出码 2。"""
        try:
            path = Path(config_path or self._config_path)
            if path is None:
                raise ValueError("缺少配置文件路径")
            if self._runtime is not None:
                config = self._runtime.config
                runtime = self._runtime
            else:
                config = load_config(path, env=self._env)
                runtime = self._runtime_factory(config) if self._runtime_factory else build_runtime(config)
            return self._execute_runtime(runtime)
        except Exception as exc:
            logging.getLogger(LOGGER_NAME).error("备份执行异常：%s", exc, exc_info=True)
            return int(ExitCode.FAILED)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def _execute_runtime(self, runtime: Runtime) -> int:
        """使用已装配运行时执行备份。"""
        config = runtime.config
        now = runtime.clock.now()
        run_id = f"run-{now.date_key}-{now.time_key}-{uuid.uuid4().hex[:8]}"

        # 初始化每实例日志，run_id 贯穿全程。
        try:
            setup_logging(
                config.log.level.value,
                config.log.dir,
                config.log.max_bytes,
                config.log.backup_count,
                run_id,
            )
        except Exception:
            # 日志目录权限异常不阻断备份，但记录到标准 logger。
            logging.getLogger(LOGGER_NAME).exception("初始化日志失败")
        logger = logging.getLogger(LOGGER_NAME)
        logger.info("开始备份：%s", config.safe_summary())

        # 确保目标目录存在，锁文件放目标目录。
        dest_dir = Path(config.backup.dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("无法创建备份目录：%s", exc)
            return int(ExitCode.FAILED)

        # 并发保护：拿不到锁就跳过，避免两个备份互相覆盖。
        lock = RunLock(dest_dir / ".backup.lock", config.backup.lock_wait_timeout)
        if not lock.acquire():
            logger.warning("其他备份正在运行，本次跳过：run_id=%s", run_id)
            runtime.notifier.notify(
                BackupRunSkipped(
                    run_id=run_id,
                    occurred_at=runtime.clock.now(),
                    reason="运行锁冲突",
                )
            )
            return int(ExitCode.FAILED)
        try:
            return self._execute_locked(runtime, run_id, logger)
        finally:
            lock.release()

    def _execute_locked(self, runtime: Runtime, run_id: str, logger: logging.Logger) -> int:
        """获取锁后的主流程。"""
        config = runtime.config
        dest_dir = Path(config.backup.dest_dir)

        # 命令可用性预检（测试可关闭，生产默认开启）。
        if self._check_binaries:
            if not self._binary_available(config.backup.mysqldump_path):
                logger.error("mysqldump 不可用：%s", config.backup.mysqldump_path)
                return int(ExitCode.FAILED)
            mysql_path = getattr(runtime.gateway, "_mysql_path", "mysql")
            if not self._binary_available(mysql_path):
                logger.error("mysql CLI 不可用：%s", mysql_path)
                return int(ExitCode.FAILED)

        # 磁盘预检（FR-15）。
        if not runtime.disk_checker.has_enough_space(dest_dir):
            logger.error("目标盘空间不足，放弃本次备份")
            runtime.notifier.notify(
                DiskSpaceLow(
                    run_id=run_id,
                    occurred_at=runtime.clock.now(),
                    path=str(dest_dir),
                    free_bytes=runtime.disk_checker.free_bytes(dest_dir),
                    required_bytes=runtime.disk_checker.required_bytes,
                )
            )
            return int(ExitCode.FAILED)

        # 解析实际要备份的业务库。
        tasks = self._resolve_tasks(config, runtime.gateway)
        if not tasks:
            logger.error("没有可备份的业务库")
            return int(ExitCode.FAILED)

        logger.info("本次备份 %d 个数据库：%s", len(tasks), [str(t.db_name) for t in tasks])

        # 创建聚合与领域服务。
        scope = BackupScope.ALL if config.backup.is_all_databases else (
            BackupScope.TABLES if any(":" in entry for entry in config.backup.databases) else BackupScope.LIST
        )
        run = BackupRun.start(run_id, runtime.clock.now(), tuple(tasks), scope)
        schema_artifacts: dict[str, BackupArtifact] = {}

        if self._artifact_factory is not None:
            factory = self._artifact_factory
        else:
            factory = self._make_artifact_factory(runtime, config, schema_artifacts)

        try:
            service = BackupExecutionService(runtime.dump_executor, runtime.clock)
            service.execute(run, factory)

            # 完整任务成功后再关联额外 schema 文件。
            for task in run.tasks:
                schema_artifact = schema_artifacts.get(str(task.db_name))
                if schema_artifact is not None and task.status.value == "success":
                    task.attach_schema_artifact(schema_artifact)
        except Exception as exc:
            logger.warning("转储失败：%s", exc)
            return int(ExitCode.FAILED)

        # 校验产物并记录 VerificationFailed 事件。
        integrity_failed = self._verify_artifacts(runtime, run, logger)

        # 校验失败但任务本身成功时，按“部分成功”处理，避免恢复不可用产物被当作全成功。
        if integrity_failed and run.status is RunStatus.SUCCESS:
            run.status = RunStatus.PARTIAL_SUCCESS
            run.exit_code = ExitCode.PARTIAL_SUCCESS
        if run.status is RunStatus.FAILED:
            run.exit_code = ExitCode.FAILED

        # 落盘 manifest，随后执行保留清理并落盘清理结果。
        try:
            runtime.manifest_repository.save(run)
        except Exception as exc:
            logger.error("保存 manifest 失败：%s", exc)
            return int(ExitCode.FAILED)

        # 通知最终事件。
        self._notify_events(runtime, run)

        # 保留清理由同一 handler 调用，保证“备份后自动清理”。
        from trigger.cleanup import execute_cleanup
        try:
            execute_cleanup(runtime)
        except Exception as exc:
            # 清理失败不影响本次备份成功状态，只记错误。
            logger.error("保留清理失败：%s", exc)

        logger.info("备份结束：run_id=%s status=%s exit_code=%s", run.run_id, run.status.value, run.exit_code)
        return int(run.exit_code if run.exit_code is not None else ExitCode.FAILED)

    # ------------------------------------------------------------------
    # 任务解析
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_tasks(config: AppConfig, gateway) -> list[DatabaseBackupTask]:
        """按配置解析业务库，all 模式调用网关枚举。"""
        excluded = set(config.backup.exclude_databases)

        if config.backup.is_all_databases:
            names = [str(item) for item in gateway.list_databases()]
            return [
                DatabaseBackupTask(DbName(name), retry_times=config.backup.retry_times)
                for name in names
                if name not in excluded
            ]

        tasks: list[DatabaseBackupTask] = []
        seen: set[str] = set()
        for entry in config.backup.databases:
            db_name = entry.split(":", 1)[0]
            if db_name in excluded or db_name in seen:
                continue
            seen.add(db_name)
            tasks.append(DatabaseBackupTask(DbName(db_name), retry_times=config.backup.retry_times))
        return tasks

    # ------------------------------------------------------------------
    # 产物与校验
    # ------------------------------------------------------------------

    @staticmethod
    def _make_artifact_factory(runtime: Runtime, config: AppConfig, schema_artifacts: dict[str, BackupArtifact]):
        """构造默认成功产物工厂。"""

        def factory(task: DatabaseBackupTask, backup_time: BackupTime) -> BackupArtifact:
            # 完整产物信息来自 MysqldumpClient.last_stored / last_file_name。
            stored = getattr(runtime.dump_executor, "last_stored", None)
            file_name = getattr(runtime.dump_executor, "last_file_name", None)
            if stored is None:
                raise DomainError("转储成功但未返回存储产物")
            full_artifact = RunBackupCommandHandler._artifact_from_stored(
                task, stored, file_name, backup_time, config.backup.compress, schema_only=False
            )

            # 若配置要求额外 schema 文件，则在完整成功后缓存。
            schema_stored = getattr(runtime.dump_executor, "last_schema_stored", None)
            schema_file_name = getattr(runtime.dump_executor, "last_schema_file_name", None)
            if config.backup.schema_only and schema_stored is not None:
                schema_artifacts[str(task.db_name)] = RunBackupCommandHandler._artifact_from_stored(
                    task,
                    schema_stored,
                    schema_file_name,
                    backup_time,
                    config.backup.compress,
                    schema_only=True,
                )
            return full_artifact

        return factory

    @staticmethod
    def _artifact_from_stored(
        task: DatabaseBackupTask,
        stored: StoredArtifact,
        file_name: FileName | None,
        backup_time: BackupTime,
        compression_type: CompressionType,
        schema_only: bool,
    ) -> BackupArtifact:
        """StoredArtifact + 领域文件名 -> BackupArtifact。"""
        if file_name is None:
            # 兼容未暴露 file_name 的假实现：按时间重建。
            file_name = FileName(
                task.db_name,
                backup_time,
                RunBackupCommandHandler._domain_compression(compression_type),
                schema_only=schema_only,
            )
        return BackupArtifact(
            db_name=task.db_name,
            file_name=file_name,
            relative_path=stored.relative_path,
            size_bytes=SizeBytes(stored.size_bytes),
            sha256=Sha256(stored.sha256),
            created_at=backup_time,
            schema_only=schema_only,
        )

    @staticmethod
    def _domain_compression(compression_type: CompressionType) -> Compression:
        """配置压缩类型 -> 领域压缩枚举。"""
        return {
            CompressionType.GZIP: Compression.GZIP,
            CompressionType.ZSTD: Compression.ZSTD,
            CompressionType.NONE: Compression.NONE,
        }[compression_type]

    def _verify_artifacts(self, runtime: Runtime, run: BackupRun, logger: logging.Logger) -> bool:
        """按配置级别校验全部产物，返回是否存在校验失败。"""
        config = runtime.config
        levels = self._verification_levels(config.verify.level)
        service = VerificationService(runtime.verifiers, runtime.clock)
        failed = False

        for task in run.tasks:
            for artifact in task.all_artifacts:
                for level in levels:
                    # L2 恢复演练只对完整数据产物执行，schema-only 文件不参与。
                    if artifact.file_name.schema_only and level is VerificationLevel.L2:
                        continue
                    try:
                        result: VerificationResult = service.verify(artifact, level)
                    except Exception as exc:
                        result = VerificationResult(level, False, reason=str(exc))
                    if result.success:
                        continue
                    failed = True
                    event = VerificationFailed(
                        run_id=run.run_id,
                        occurred_at=runtime.clock.now(),
                        db_name=artifact.db_name,
                        file_name=artifact.file_name,
                        level=level,
                        reason=result.reason,
                    )
                    run.record_event(event)
                    logger.warning("校验失败：db=%s file=%s level=%s reason=%s",
                                   artifact.db_name, artifact.file_name, level.value, result.reason)
        return failed

    @staticmethod
    def _verification_levels(level: VerifyLevel) -> tuple[VerificationLevel, ...]:
        """把配置校验级别展开为从 L0 到目标级别的序列。"""
        mapping = {
            VerifyLevel.L0: (VerificationLevel.L0,),
            VerifyLevel.L1: (VerificationLevel.L0, VerificationLevel.L1),
            VerifyLevel.L2: (VerificationLevel.L0, VerificationLevel.L1, VerificationLevel.L2),
        }
        return mapping[level]

    # ------------------------------------------------------------------
    # 通知与工具
    # ------------------------------------------------------------------

    @staticmethod
    def _notify_events(runtime: Runtime, run: BackupRun) -> None:
        """按配置通知成功/失败事件。"""
        config = runtime.config
        for event in run.events:
            if isinstance(event, BackupRunCompleted):
                success = event.status.value == "success"
                if success and config.notify.on_success:
                    runtime.notifier.notify(event)
                if not success and config.notify.on_failure:
                    runtime.notifier.notify(event)
            elif isinstance(event, (DatabaseBackupFailed, VerificationFailed, DiskSpaceLow)):
                if config.notify.on_failure:
                    runtime.notifier.notify(event)

    @staticmethod
    def _binary_available(path: str) -> bool:
        """检查可执行文件是否存在/PATH 可解析。"""
        if not path:
            return False
        if Path(path).is_file():
            return True
        return shutil.which(path) is not None