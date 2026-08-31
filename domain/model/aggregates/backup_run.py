"""备份运行聚合根：维护单次备份的状态机与退出码。"""

# 延迟类型注解，避免导入模型时求值复杂类型。
from __future__ import annotations

# dataclass 定义聚合根；field 用于保存不参与 repr 的事件列表。
from dataclasses import dataclass, field

# 聚合生命周期会产生开始、任务结果和完成事件。
from domain.events import (
    BackupRunCompleted,        # 聚合完成事件。
    BackupRunStarted,          # 聚合开始事件。
    DatabaseBackupFailed,      # 单库失败或重试事件。
    DatabaseBackupSucceeded,   # 单库成功事件。
    DomainEvent,               # 领域事件基类。
)

# 聚合根组合多个数据库任务实体。
from domain.model.entities.database_backup_task import DatabaseBackupTask

# 导入状态机、事件和任务建模所需值对象。
from domain.model.value_objects import (
    BackupScope,   # 备份范围。
    BackupTime,    # 带时区时间。
    DbName,        # 数据库标识。
    DomainError,   # 领域规则异常。
    DumpResult,    # 外部转储结果。
    ExitCode,      # 退出码。
    RunStatus,     # 聚合整体状态。
    TaskStatus,    # 单任务状态。
)


# BackupRun 是聚合根；任务结果和完成事件都通过它维护。
@dataclass
class BackupRun:
    """一次备份运行的一致性边界。

    调用方通过 mark_task_result 应用 DumpResult，
    聚合内部维护任务状态、事件和整体退出码。
    """

    run_id: str                                  # 本次运行唯一标识。
    started_at: BackupTime                       # 带时区开始时间。
    tasks: tuple[DatabaseBackupTask, ...]        # 初始化后固定任务集合。
    scope: BackupScope = BackupScope.LIST        # 备份范围。
    status: RunStatus = RunStatus.RUNNING        # 聚合当前状态。
    exit_code: ExitCode | None = None            # finish 后才计算。
    finished_at: BackupTime | None = None        # finish 后才有值。
    events: list[DomainEvent] = field(default_factory=list, repr=False) # 领域事件顺序记录。

    def __post_init__(self) -> None:
        # run_id 为空会让日志、manifest 和事件无法关联。
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise DomainError("run_id 不能为空")

        # 同一 run 中每个数据库只能有一个任务。
        names = [str(task.db_name) for task in self.tasks]
        if len(names) != len(set(names)):
            raise DomainError(f"备份任务数据库重复：{sorted(names)}")

    @classmethod
    def start(
        cls,
        run_id: str,
        started_at: BackupTime,
        tasks: tuple[DatabaseBackupTask, ...],
        scope: BackupScope = BackupScope.LIST,
    ) -> "BackupRun":
        """创建并发布运行开始事件的聚合实例。"""

        # 先构造聚合，让基础不变量先完成校验。
        run = cls(
            run_id=run_id,
            started_at=started_at,
            tasks=tasks,
            scope=scope,
        )

        # 发布“运行已开始”这一领域事实。
        run.record_event(
            BackupRunStarted(
                run_id=run.run_id,
                occurred_at=started_at,
                scope=scope.value,
            )
        )

        # 返回带开始事件的聚合。
        return run

    @property
    def database_names(self) -> tuple[str, ...]:
        """返回当前聚合包含的数据库顺序。"""

        # 顺序与创建任务顺序一致，方便 manifest 展示。
        return tuple(str(task.db_name) for task in self.tasks)

    def record_event(self, event: DomainEvent) -> None:
        """追加领域事件；事件是事实，不允许外部修改。"""

        # 事件按发生顺序保存，后续通知和投影依赖该顺序。
        self.events.append(event)

    def mark_task_result(
        self,
        database: DbName | str,
        result: DumpResult,
        artifact=None,
    ) -> None:
        """应用某个数据库任务的一次 DumpResult。"""

        # 聚合完成后任务状态必须冻结。
        if self.finished_at is not None:
            raise DomainError("备份运行已完成，不能继续更新任务")

        # 用字符串比较兼容 DbName 对象和普通字符串。
        key = str(database)

        # 查找目标数据库任务。
        for task in self.tasks:
            if str(task.db_name) == key:
                # 交给任务实体维护 attempts、retry 和终态。
                task.apply_dump_result(result, artifact)

                # 成功和失败分别发布不同领域事件。
                if task.status is TaskStatus.SUCCESS:
                    self.record_event(
                        DatabaseBackupSucceeded(
                            run_id=self.run_id,
                            occurred_at=self.started_at,
                            db_name=task.db_name,
                            file_name=task.artifact.file_name,
                            size_bytes=task.artifact.size_bytes,
                            elapsed_seconds=task.elapsed_seconds or 0.0,
                        )
                    )
                else:
                    self.record_event(
                        DatabaseBackupFailed(
                            run_id=self.run_id,
                            occurred_at=self.started_at,
                            db_name=task.db_name,
                            attempts=task.attempts,
                            will_retry=task.status is TaskStatus.RETRYING,
                            error_digest=task.last_error,
                            elapsed_seconds=task.elapsed_seconds or 0.0,
                        )
                    )

                # 只处理一个匹配任务后返回。
                return

        # 未注册的数据库不能临时加入聚合。
        raise DomainError(f"备份运行不包含数据库任务：{key}")

    def _computed_status(self) -> RunStatus:
        """根据全部终态任务计算整体状态。"""

        # 空任务不能伪装成成功。
        if not self.tasks:
            return RunStatus.FAILED

        # 提取所有任务当前状态。
        statuses = [task.status for task in self.tasks]

        # 全部成功映射为 SUCCESS。
        if all(status is TaskStatus.SUCCESS for status in statuses):
            return RunStatus.SUCCESS

        # 全部最终失败映射为 FAILED。
        if all(status is TaskStatus.FAILED for status in statuses):
            return RunStatus.FAILED

        # 其余终态组合属于部分成功。
        return RunStatus.PARTIAL_SUCCESS

    def finish(self, finished_at: BackupTime) -> None:
        """完成备份运行并生成整体状态、退出码与完成事件。"""

        # 聚合只能完成一次。
        if self.finished_at is not None:
            raise DomainError("备份运行已完成，不能重复完成")

        # 完成时间必须不早于开始时间。
        if finished_at.value < self.started_at.value:
            raise DomainError("完成时间不能早于开始时间")

        # pending/running/retrying 任务都无法得出最终整体状态。
        if any(
            task.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING)
            for task in self.tasks
        ):
            raise DomainError("仍有未终态的备份任务，不能完成运行")

        # 记录完成时间。
        self.finished_at = finished_at

        # 根据所有任务终态计算聚合状态。
        self.status = self._computed_status()

        # 聚合状态同步映射为 PRD 约定退出码。
        if self.status is RunStatus.SUCCESS:
            self.exit_code = ExitCode.SUCCESS
        elif self.status is RunStatus.PARTIAL_SUCCESS:
            self.exit_code = ExitCode.PARTIAL_SUCCESS
        else:
            self.exit_code = ExitCode.FAILED

        # 发布 BackupRunCompleted 事实。
        self.record_event(
            BackupRunCompleted(
                run_id=self.run_id,
                occurred_at=finished_at,
                status=self.status,
                exit_code=self.exit_code,
            )
        )
