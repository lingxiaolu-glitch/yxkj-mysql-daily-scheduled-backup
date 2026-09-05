"""触发层：恢复命令处理器，按可用性门禁选择备份并导入。"""

# 延迟类型注解。
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Mapping

# 领域实体、值对象与异常。
from domain.model.entities.backup_artifact import BackupArtifact, ArtifactUnavailableError
from domain.model.value_objects import DbName, DomainError, ExitCode

# 配置/运行时/校验解码工具。
from infrastructure.config_loader import load_config
from infrastructure.logging_utils import LOGGER_NAME, setup_logging
from infrastructure.verifiers import decode_artifact_sql
from trigger.runtime import Runtime, build_runtime


class RestoreBackupCommandHandler:
    """把备份产物恢复到目标库。"""

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        runtime: Runtime | None = None,
        runtime_factory=None,
    ) -> None:
        # 保存入口配置与可选运行时。
        self._config_path = Path(config_path) if config_path is not None else None
        self._env = env
        self._runtime = runtime
        self._runtime_factory = runtime_factory

    def execute(
        self,
        config_path: str | Path | None = None,
        db: str | DbName = "",
        file: str | None = None,
        mode: str = "full",
        to_host: str | None = None,
    ) -> int:
        """执行恢复并返回退出码。"""
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

            now = runtime.clock.now()
            setup_logging(
                config.log.level.value,
                config.log.dir,
                config.log.max_bytes,
                config.log.backup_count,
                f"restore-{now.date_key}-{now.time_key}",
            )
            logger = logging.getLogger(LOGGER_NAME)

            # 远程目标位于 v2 规划，v1 只支持本地恢复。
            if to_host:
                logger.error("v1 暂不支持恢复到其他主机：%s", to_host)
                return int(ExitCode.FAILED)

            target = DbName(str(db))
            artifact = self._find_artifact(runtime, target, file, mode)
            return self._restore_artifact(runtime, artifact, target, mode, logger)
        except Exception as exc:
            logging.getLogger(LOGGER_NAME).error("恢复失败：%s", exc, exc_info=True)
            return int(ExitCode.FAILED)

    # ------------------------------------------------------------------
    # 选择产物
    # ------------------------------------------------------------------

    @staticmethod
    def _find_artifact(runtime: Runtime, target: DbName, file: str | None, mode: str) -> BackupArtifact:
        """按 db/file/mode 选择最近可用产物。"""
        candidates: list[BackupArtifact] = []
        for run in runtime.manifest_repository.find_all():
            for task in run.tasks:
                if task.db_name != target:
                    continue
                for artifact in task.all_artifacts:
                    if artifact.deleted:
                        continue
                    # 指定文件时精确匹配相对路径或文件名。
                    if file and artifact.relative_path != file and artifact.file_name.value != file:
                        continue
                    # schema 模式只选仅结构文件，full/db 只选完整文件。
                    if mode == "schema" and not artifact.file_name.schema_only:
                        continue
                    if mode in ("full", "db") and artifact.file_name.schema_only:
                        continue
                    candidates.append(artifact)

        if not candidates:
            raise ArtifactUnavailableError(f"未找到 {target} 的恢复产物：mode={mode} file={file}")

        # 按创建时间倒序取最新。
        best = max(candidates, key=lambda item: item.created_at.value)
        best.ensure_available()
        return best

    # ------------------------------------------------------------------
    # 执行恢复
    # ------------------------------------------------------------------

    @staticmethod
    def _restore_artifact(
        runtime: Runtime,
        artifact: BackupArtifact,
        target: DbName,
        mode: str,
        logger: logging.Logger,
    ) -> int:
        """解压并把 SQL 导入 MySQL。"""
        # Availability 门禁再次校验。
        try:
            artifact.ensure_available()
        except ArtifactUnavailableError as exc:
            logger.error("%s", exc)
            return int(ExitCode.FAILED)

        # 解压到临时 SQL 文件，随后交给 mysql CLI 流式导入。
        try:
            decoded = decode_artifact_sql(runtime.storage, artifact)
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".sql", delete=False
            ) as handle:
                handle.write(decoded)
                sql_file = handle.name
        except Exception as exc:
            logger.error("读取备份产物失败：%s", exc)
            return int(ExitCode.FAILED)

        try:
            # one_database 确保多库 dump 只恢复目标库。
            runtime.gateway.restore(
                sql_file,
                target,
                one_database=True,
                rewrite_to_database=target,
            )
            logger.info("恢复成功：db=%s file=%s mode=%s", target, artifact.file_name.value, mode)
            return int(ExitCode.SUCCESS)
        except Exception as exc:
            logger.error("恢复导入失败：%s", exc)
            return int(ExitCode.FAILED)
        finally:
            try:
                Path(sql_file).unlink(missing_ok=True)
            except OSError:
                pass