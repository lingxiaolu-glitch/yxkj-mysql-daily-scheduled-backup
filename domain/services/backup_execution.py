"""备份执行编排服务：逐任务调用 DumpExecutor 端口并维护聚合状态。"""

# 延迟类型注解，保持领域层导入轻量。
from __future__ import annotations

# Callable 用于注入"成功产物组装器"。
from collections.abc import Callable

# 导入聚合根、实体与值对象。
from domain.model.aggregates.backup_run import BackupRun
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.entities.database_backup_task import DatabaseBackupTask
from domain.model.value_objects import BackupTime, TaskStatus

# 领域端口：DumpExecutor 执行转储，Clock 提供完成时间。
from domain.repositories import Clock, DumpExecutor


class BackupExecutionService:
    """编排一次备份运行。

    遍历运行内全部任务 -> 通过 DumpExecutor 端口转储 -> 按 DumpResult
    更新任务状态（含失败重试）-> 全部任务终态后完成聚合并计算退出码。
    单个任务失败不会中断其他任务（部分失败隔离）。
    """

    def __init__(self, dump_executor: DumpExecutor, clock: Clock) -> None:
        # 依赖注入领域端口；服务本身不触碰 subprocess / 文件 IO / 系统时钟。
        self._dump_executor = dump_executor
        self._clock = clock

    def execute(
        self,
        run: BackupRun,
        artifact_factory: Callable[[DatabaseBackupTask, BackupTime], BackupArtifact | None] | None = None,
    ) -> BackupRun:
        """执行一次备份运行，并返回同一个（已完成的）聚合实例。

        artifact_factory 在任务成功后调用，用于把存储层写入结果组装为
        BackupArtifact；未提供时成功任务不关联产物，由上层决定何时注入。
        """
        # 逐个任务执行，任务间失败互不影响（部分失败隔离）。
        for task in run.tasks:
            self._execute_task(run, task, artifact_factory)

        # 全部任务进入终态后完成聚合：计算整体状态与退出码。
        run.finish(self._clock.now())
        return run

    def _execute_task(
        self,
        run: BackupRun,
        task: DatabaseBackupTask,
        artifact_factory: Callable[[DatabaseBackupTask, BackupTime], BackupArtifact | None] | None,
    ) -> None:
        # PENDING/RETRYING 表示仍有尝试机会；SUCCESS/FAILED 终态后停止。
        while task.status in (TaskStatus.PENDING, TaskStatus.RETRYING):
            # 通过端口执行外部转储，外部细节被防腐层隔离。
            result = self._dump_executor.dump(task)

            # 成功时若注入了产物组装器，则生成并关联产物。
            artifact = None
            if result.success and artifact_factory is not None:
                artifact = artifact_factory(task, self._clock.now())

            # 交给聚合根推进任务状态机，并发布对应领域事件。
            run.mark_task_result(task.db_name, result, artifact)
