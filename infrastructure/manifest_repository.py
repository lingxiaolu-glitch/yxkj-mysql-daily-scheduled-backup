"""manifest 仓库：把备份运行聚合序列化为可审计 JSON 清单。

设计说明：
- 每个备份日期一个 manifest 文件，文件内保存当日全部运行（支持多实例/多次运行）；
- status 文件保存轻量摘要，便于人工检查，不参与领域加载；
- 所有 JSON 读写使用临时文件 + os.replace，避免半文件；
- 文件权限 0600，目录 0700（POSIX 尽力而为）。
"""

# 延迟类型注解，保持导入轻量。
from __future__ import annotations

# json 序列化/反序列化；os 提供原子替换与权限设置。
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

# 领域聚合与实体。
from domain.model.aggregates.backup_run import BackupRun
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.entities.database_backup_task import DatabaseBackupTask

# 领域值对象与领域异常。
from domain.model.value_objects import (
    Availability,
    BackupScope,
    BackupTime,
    Compression,
    DbName,
    DomainError,
    DumpResult,
    ExitCode,
    FileName,
    RunStatus,
    Sha256,
    SizeBytes,
    TaskStatus,
    VerificationLevel,
)


class ManifestError(DomainError):
    """manifest JSON 损坏、字段缺失或类型不合法时抛出。"""


class JsonManifestRepository:
    """本地 JSON manifest 仓库，实现 BackupRunRepository 端口。

    默认目录为 dest_dir/manifests；也允许调用方显式指定一个
    dest_dir 内的子目录，便于测试和未来扩展。
    """

    # 文档格式版本，后续兼容性迁移依据。
    VERSION = 1

    # 日期目录名必须是 8 位数字，避免路径注入。
    _DATE_RE = re.compile(r"^\d{8}$")

    def __init__(self, dest_dir: Path, manifest_dir: Path | None = None) -> None:
        # 备份根目录，所有清单都限定在其内部。
        self._dest_dir = Path(dest_dir).resolve()

        # 默认使用 manifests 子目录；显式目录也必须落在备份根目录内。
        if manifest_dir is None:
            self._manifest_dir = self._dest_dir / "manifests"
        else:
            self._manifest_dir = Path(manifest_dir).resolve()
            if not self._manifest_dir.is_relative_to(self._dest_dir):
                raise DomainError("manifest 目录必须位于 dest_dir 内")

    @property
    def manifest_dir(self) -> Path:
        """返回 manifest 根目录。"""
        return self._manifest_dir

    def manifest_path(self, date_key: str) -> Path:
        """返回指定日期的 manifest 文件路径。"""
        return self._manifest_dir / f"manifest_{self._date_key(date_key)}.json"

    def status_path(self, date_key: str) -> Path:
        """返回指定日期的 status 摘要文件路径。"""
        return self._manifest_dir / f"status_{self._date_key(date_key)}.json"

    # ------------------------------------------------------------------
    # BackupRunRepository 端口
    # ------------------------------------------------------------------

    def save(self, run: BackupRun) -> None:
        """保存或更新一次备份运行；同 run_id 会覆盖旧记录。"""
        date_key = run.started_at.date_key

        # 读取当日已有清单，保留其他运行并替换同 run_id。
        current = self._read_document(self.manifest_path(date_key)) or []
        runs = [item for item in current if item.get("run_id") != run.run_id]
        runs.append(self._run_to_dict(run))

        # 写入 manifest 和轻量 status 摘要（status 同样保留当日全部运行）。
        current_status = self._read_document(self.status_path(date_key)) or []
        statuses = [item for item in current_status if item.get("run_id") != run.run_id]
        statuses.append(self._status_to_dict(run))
        self._write_document(
            self.status_path(date_key),
            {"version": self.VERSION, "date": date_key, "runs": statuses},
        )
        self._write_document(self.manifest_path(date_key), {"version": self.VERSION, "runs": runs})

    def find(self, run_id: str) -> BackupRun | None:
        """按 run_id 查找全部日期清单，不存在返回 None。"""
        if not isinstance(run_id, str) or not run_id.strip():
            raise ManifestError("run_id 不能为空")

        # 扫描全部 manifest_*.json，避免调用方按日期猜测路径。
        for path in sorted(self._manifest_dir.glob("manifest_*.json")):
            for item in self._read_document(path) or []:
                if item.get("run_id") == run_id:
                    return self._run_from_dict(item)
        return None

    def find_by_date(self, date_key: str) -> tuple[BackupRun, ...]:
        """读取某日全部备份运行；文件不存在时返回空元组。"""
        path = self.manifest_path(date_key)
        items = self._read_document(path) or []
        return tuple(self._run_from_dict(item) for item in items)

    # ------------------------------------------------------------------
    # JSON I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _date_key(value: str) -> str:
        """校验并返回 YYYYMMDD 日期键。"""
        if not JsonManifestRepository._DATE_RE.fullmatch(value):
            raise ManifestError(f"日期键必须是 YYYYMMDD：{value!r}")
        return value

    @staticmethod
    def _read_document(path: Path) -> list[dict[str, Any]] | None:
        """读取一份 JSON 文档；不存在返回 None，损坏则抛 ManifestError。"""
        if not path.exists():
            return None

        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"manifest 读取失败：{path}：{exc}") from exc

        # 顶层必须是 version + runs，且 runs 为对象数组。
        runs = data.get("runs") if isinstance(data, dict) else None
        if not isinstance(runs, list) or not all(isinstance(item, dict) for item in runs):
            raise ManifestError(f"manifest 格式错误：{path}")
        return runs

    @staticmethod
    def _write_document(path: Path, data: dict[str, Any]) -> None:
        """原子写入 JSON 文件并收紧权限。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass

        # 先写临时文件，再原子替换，避免崩溃后留下截断 JSON。
        temp = path.with_name(path.name + ".tmp")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, path)
        except OSError as exc:
            # 失败时清理临时文件。
            try:
                temp.unlink()
            except OSError:
                pass
            raise ManifestError(f"manifest 写入失败：{path}：{exc}") from exc

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    @classmethod
    def _run_to_dict(cls, run: BackupRun) -> dict[str, Any]:
        """备份运行聚合 -> JSON 兼容字典。"""
        return {
            "version": cls.VERSION,
            "run_id": run.run_id,
            "scope": run.scope.value,
            "status": run.status.value,
            "exit_code": int(run.exit_code) if run.exit_code is not None else None,
            "started_at": cls._time_to_str(run.started_at),
            "finished_at": cls._time_to_str(run.finished_at) if run.finished_at else None,
            "tasks": [cls._task_to_dict(task) for task in run.tasks],
        }

    @classmethod
    def _task_to_dict(cls, task: DatabaseBackupTask) -> dict[str, Any]:
        """数据库任务 -> JSON 兼容字典。"""
        return {
            "db_name": str(task.db_name),
            "retry_times": task.retry_times,
            "attempts": task.attempts,
            "status": task.status.value,
            "retried": task.retried,
            "elapsed_seconds": task.elapsed_seconds,
            "last_error": task.last_error,
            "history": [
                {
                    "success": item.success,
                    "return_code": item.return_code,
                    "elapsed_seconds": item.elapsed_seconds,
                    "error_digest": item.error_digest,
                }
                for item in task.history
            ],
            "artifact": cls._artifact_to_dict(task.artifact) if task.artifact is not None else None,
        }

    @classmethod
    def _artifact_to_dict(cls, artifact: BackupArtifact) -> dict[str, Any]:
        """备份产物 -> JSON 兼容字典。"""
        return {
            "db_name": str(artifact.db_name),
            "file_name": artifact.file_name.value,
            "file_compression": artifact.file_name.compression.value,
            "file_backup_time": cls._time_to_str(artifact.file_name.backup_time),
            "file_schema_only": artifact.file_name.schema_only,
            "relative_path": artifact.relative_path,
            "size_bytes": int(artifact.size_bytes),
            "sha256": str(artifact.sha256),
            "created_at": cls._time_to_str(artifact.created_at),
            "availability": artifact.availability.value,
            "schema_only": artifact.schema_only,
            "deleted": artifact.deleted,
            "verification_error": artifact.verification_error,
            "verified_at": cls._time_to_str(artifact.verified_at) if artifact.verified_at else None,
            "verification_level": artifact.verification_level.value if artifact.verification_level else None,
        }

    @staticmethod
    def _status_to_dict(run: BackupRun) -> dict[str, Any]:
        """生成轻量状态摘要（不包含完整任务历史）。"""
        success = sum(1 for task in run.tasks if task.status is TaskStatus.SUCCESS)
        failed = sum(1 for task in run.tasks if task.status is TaskStatus.FAILED)
        return {
            "run_id": run.run_id,
            "scope": run.scope.value,
            "status": run.status.value,
            "exit_code": int(run.exit_code) if run.exit_code is not None else None,
            "started_at": BackupTime.__str__(run.started_at),
            "finished_at": BackupTime.__str__(run.finished_at) if run.finished_at else None,
            "database_count": len(run.tasks),
            "success_count": success,
            "failed_count": failed,
            "artifact_count": sum(1 for task in run.tasks if task.artifact is not None),
        }

    # ------------------------------------------------------------------
    # 反序列化
    # ------------------------------------------------------------------

    @classmethod
    def _run_from_dict(cls, data: Any) -> BackupRun:
        """JSON 兼容字典 -> 备份运行聚合。"""
        if not isinstance(data, dict):
            raise ManifestError("run 记录必须是对象")

        run_id = cls._require_str(data, "run_id")
        started_at = cls._time_from_str(cls._require_str(data, "started_at"))
        tasks_data = data.get("tasks")
        if not isinstance(tasks_data, list):
            raise ManifestError("run.tasks 必须是数组")

        # 用直接构造而非 BackupRun.start：加载历史时不再重放开始事件。
        return BackupRun(
            run_id=run_id,
            started_at=started_at,
            tasks=tuple(cls._task_from_dict(item) for item in tasks_data),
            scope=BackupScope(cls._require_str(data, "scope")),
            status=RunStatus(cls._require_str(data, "status")),
            exit_code=ExitCode(data["exit_code"]) if data.get("exit_code") is not None else None,
            finished_at=cls._optional_time(data.get("finished_at")),
        )

    @classmethod
    def _task_from_dict(cls, data: Any) -> DatabaseBackupTask:
        """JSON 兼容字典 -> 数据库任务实体。"""
        if not isinstance(data, dict):
            raise ManifestError("task 记录必须是对象")

        history = []
        for item in data.get("history") or []:
            if not isinstance(item, dict):
                raise ManifestError("task.history 项必须是对象")
            history.append(
                DumpResult(
                    success=bool(item.get("success", False)),
                    return_code=int(item.get("return_code", -1)),
                    elapsed_seconds=float(item.get("elapsed_seconds", 0.0)),
                    error_digest=str(item.get("error_digest", "")),
                )
            )

        artifact_data = data.get("artifact")
        artifact = cls._artifact_from_dict(artifact_data) if artifact_data is not None else None
        task = DatabaseBackupTask(
            db_name=DbName(cls._require_str(data, "db_name")),
            retry_times=int(data.get("retry_times", 0)),
            attempts=int(data.get("attempts", 0)),
            status=TaskStatus(cls._require_str(data, "status")),
            artifact=artifact,
            retried=bool(data.get("retried", False)),
            elapsed_seconds=(
                float(data["elapsed_seconds"]) if data.get("elapsed_seconds") is not None else None
            ),
            last_error=str(data.get("last_error", "")),
            history=history,
        )
        return task

    @classmethod
    def _artifact_from_dict(cls, data: Any) -> BackupArtifact:
        """JSON 兼容字典 -> 备份产物实体。"""
        if not isinstance(data, dict):
            raise ManifestError("artifact 记录必须是对象")

        db_name = DbName(cls._require_str(data, "db_name"))
        backup_time = cls._time_from_str(cls._require_str(data, "file_backup_time"))
        compression = Compression(cls._require_str(data, "file_compression"))
        schema_only = bool(data.get("file_schema_only", False))
        file_name = FileName(
            db_name=db_name,
            backup_time=backup_time,
            compression=compression,
            schema_only=schema_only,
        )

        # 文件名字段与领域规则生成值必须一致，防止缓存/复制出错。
        if file_name.value != data.get("file_name"):
            raise ManifestError("manifest 中的 file_name 与领域规则不一致")

        verified_at_data = data.get("verified_at")
        level_data = data.get("verification_level")
        return BackupArtifact(
            db_name=db_name,
            file_name=file_name,
            relative_path=cls._require_str(data, "relative_path"),
            size_bytes=SizeBytes(int(data.get("size_bytes", 0))),
            sha256=Sha256(cls._require_str(data, "sha256")),
            created_at=cls._time_from_str(cls._require_str(data, "created_at")),
            availability=Availability(cls._require_str(data, "availability")),
            schema_only=schema_only,
            deleted=bool(data.get("deleted", False)),
            verification_error=str(data.get("verification_error", "")),
            verified_at=cls._optional_time(verified_at_data),
            verification_level=VerificationLevel(level_data) if level_data else None,
        )
    # ------------------------------------------------------------------
    # 类型工具
    # ------------------------------------------------------------------

    @staticmethod
    def _require_str(data: dict[str, Any], key: str) -> str:
        """读取必填字符串字段。"""
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(f"manifest 缺少合法字段：{key}")
        return value

    @staticmethod
    def _time_to_str(value: BackupTime) -> str:
        """BackupTime -> ISO 字符串。"""
        return value.value.isoformat()

    @staticmethod
    def _time_from_str(value: str) -> BackupTime:
        """ISO 字符串 -> BackupTime。"""
        try:
            return BackupTime(datetime.fromisoformat(value))
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"非法时间：{value!r}") from exc

    @staticmethod
    def _optional_time(value: Any) -> BackupTime | None:
        """可空时间字段。"""
        return None if value is None else JsonManifestRepository._time_from_str(str(value))
