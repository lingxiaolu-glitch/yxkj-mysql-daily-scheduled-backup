"""触发层：保留策略清理命令处理器。"""

# 延迟类型注解。
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# 领域事件、聚合与产物实体。
from domain.events import ArtifactDeleted
from domain.model.aggregates.backup_run import BackupRun
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.value_objects import DomainError, ExitCode
from domain.services.retention import CleanupPlan, RetentionService

# 配置、日志与运行时。
from infrastructure.config_loader import AppConfig, load_config
from infrastructure.logging_utils import LOGGER_NAME, setup_logging
from trigger.runtime import Runtime, build_runtime


@dataclass(frozen=True)
class CleanupOutcome:
    """清理结果摘要。"""

    deleted: int = 0
    kept: int = 0


class CleanupCommandHandler:
    """只执行保留策略清理，不触发备份。"""

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        runtime: Runtime | None = None,
        runtime_factory=None,
    ) -> None:
        # 保存入口配置与注入运行时。
        self._config_path = Path(config_path) if config_path is not None else None
        self._env = env
        self._runtime = runtime
        self._runtime_factory = runtime_factory

    def execute(self, config_path: str | Path | None = None) -> int:
        """清理入口：返回 0 成功、2 失败。"""
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
            # 用当前时间初始化日志，便于人工查看清理结果。
            now = runtime.clock.now()
            setup_logging(
                config.log.level.value,
                config.log.dir,
                config.log.max_bytes,
                config.log.backup_count,
                f"cleanup-{now.date_key}-{now.time_key}",
            )
            outcome = execute_cleanup(runtime)
            logging.getLogger(LOGGER_NAME).info(
                "保留清理完成：delete=%d keep=%d", outcome.deleted, outcome.kept
            )
            return int(ExitCode.SUCCESS)
        except Exception as exc:
            logging.getLogger(LOGGER_NAME).error("保留清理失败：%s", exc, exc_info=True)
            return int(ExitCode.FAILED)


def execute_cleanup(runtime: Runtime) -> CleanupOutcome:
    """按 manifest 中的产物执行保留清理。

    该函数同时被 RunBackupCommandHandler 在备份完成后调用，
    确保清理失败不会被误报为备份失败。
    """
    config = runtime.config
    if not config.retention.enabled:
        return CleanupOutcome()

    # 从已落盘 manifest 收集全部产物，保留它们的审计元数据。
    all_runs = runtime.manifest_repository.find_all()
    artifacts: list[BackupArtifact] = [
        artifact
        for run in all_runs
        for task in run.tasks
        for artifact in task.all_artifacts
        if artifact is not None and not artifact.deleted
    ]
    if not artifacts:
        return CleanupOutcome()

    # 领域纯函数计算清理计划。
    plan: CleanupPlan = RetentionService().plan(
        days=config.retention.days,
        weekly=config.retention.weekly,
        monthly=config.retention.monthly,
        artifacts=artifacts,
        now=runtime.clock.now(),
    )

    # 删除过期文件，并按 run 更新 manifest。
    affected_runs: set[str] = set()
    deleted_events = []
    for artifact in plan.to_delete:
        # 文件已不存在则直接标记删除，避免清理幂等性问题。
        if runtime.storage.exists(artifact.relative_path):
            try:
                runtime.storage.delete(artifact.relative_path)
            except DomainError as exc:
                logging.getLogger(LOGGER_NAME).warning("清理文件失败：%s", exc)
                continue
        artifact.mark_deleted()
        affected_runs.add(artifact_file_owner(artifact, all_runs))
        deleted_events.append(artifact)

    # 保存受影响 run 的更新状态；不影响其他日期/实例的 manifest。
    for run in all_runs:
        if run.run_id in affected_runs:
            runtime.manifest_repository.save(run)

    # 删除事件只记录到日志，不参与成功/失败通知配置。
    for artifact in deleted_events:
        _emit_deleted_log(runtime, artifact)

    return CleanupOutcome(deleted=len(deleted_events), kept=len(plan.to_keep))


def artifact_file_owner(artifact: BackupArtifact, runs: tuple[BackupRun, ...]) -> str:
    """返回包含该产物对象的 run_id（简单对象比较）。"""
    for run in runs:
        for task in run.tasks:
            if any(item is artifact for item in task.all_artifacts):
                return run.run_id
    raise DomainError("找不到产物所属运行")


def _emit_deleted_log(runtime: Runtime, artifact: BackupArtifact) -> None:
    """输出结构化的删除日志。"""
    # 手工构造事件，便于未来接入通知时复用；这里当前只写日志。
    event = ArtifactDeleted(
        run_id="cleanup",
        occurred_at=runtime.clock.now(),
        db_name=artifact.db_name,
        file_name=artifact.file_name,
        relative_path=artifact.relative_path,
    )
    logger = logging.getLogger(LOGGER_NAME)
    # 直接调 execute_cleanup 时可能尚未配置 handler，避免输出调试噪声。
    if logger.handlers:
        logger.info("已清理产物：%s", event_to_text(event))


def event_to_text(event) -> str:
    """轻量事件文本，避免从基础设施反向依赖通知实现。"""
    return (
        f"{type(event).__name__}(db={event.db_name}, "
        f"file={event.file_name}, path={event.relative_path})"
    )
