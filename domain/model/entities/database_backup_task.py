"""单个数据库备份任务实体，包含重试与产物关联规则。"""

# 延迟类型注解，保证模型导入轻量。
from __future__ import annotations

# dataclass 表示任务实体；field 用于创建不参与 repr 的执行历史。
from dataclasses import dataclass, field

# 成功任务需要关联一个备份产物实体。
from domain.model.entities.backup_artifact import BackupArtifact

# 导入任务状态机和结果建模所需的值对象。
from domain.model.value_objects import (
    DbName,        # 任务目标数据库。
    DomainError,   # 领域规则异常。
    DumpResult,    # 外部转储结果的领域表示。
    TaskStatus,    # 任务生命周期状态。
)


# 任务状态会变化，因此使用普通 dataclass。
@dataclass
class DatabaseBackupTask:
    """一次数据库备份作业。

    retry_times 表示首次失败后的额外重试次数；
    总尝试次数因此最多为 retry_times + 1。
    """

    db_name: DbName                                  # 目标业务数据库。
    retry_times: int = 1                             # 首次失败后的额外重试次数。
    attempts: int = 0                                # 已执行尝试次数。
    status: TaskStatus = TaskStatus.PENDING          # 当前任务状态。
    artifact: BackupArtifact | None = None           # 成功后关联的产物。
    retried: bool = False                            # 是否已经进入过重试。
    elapsed_seconds: float | None = None             # 最近一次尝试耗时。
    last_error: str = ""                             # 最近一次失败的脱敏错误。
    history: list[DumpResult] = field(default_factory=list, repr=False) # 保留完整尝试历史。

    def __post_init__(self) -> None:
        # 配置错误必须在任务创建时暴露。
        if self.retry_times < 0:
            raise DomainError("重试次数不能为负数")

        # 历史尝试次数也不可能为负。
        if self.attempts < 0:
            raise DomainError("已尝试次数不能为负数")

    @property
    def max_attempts(self) -> int:
        """首次执行 + 额外重试次数。"""

        # retry_times=1 表示最多执行 2 次。
        return self.retry_times + 1

    @property
    def can_retry(self) -> bool:
        """最近一次失败后是否仍允许重试。"""

        # 只有 RETRYING 是非终态且明确等待下一次尝试。
        return self.status is TaskStatus.RETRYING

    def apply_dump_result(self, result: DumpResult, artifact: BackupArtifact | None = None) -> None:
        """把一次 DumpResult 应用到任务状态机。"""

        # SUCCESS/FAILED 是终态，不能被后续结果覆盖。
        if self.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
            raise DomainError("任务已处于终态，不能继续应用 DumpResult")

        # 本次尝试开始计数。
        self.attempts += 1

        # 保留每次 DumpResult，便于后续审计和重试原因分析。
        self.history.append(result)

        # 最近耗时总是覆盖为本次结果。
        self.elapsed_seconds = result.elapsed_seconds

        # 成功进入终态。
        if result.success:
            self.status = TaskStatus.SUCCESS
            self.last_error = ""

            # 只有调用方提供了产物才进行关联校验。
            if artifact is not None:
                self.attach_artifact(artifact)

            return

        # 保存最近一次失败的脱敏摘要。
        self.last_error = result.error_digest

        # 尝试次数小于最大次数说明仍有额外重试机会。
        if self.attempts < self.max_attempts:
            self.status = TaskStatus.RETRYING
            self.retried = True
        else:
            # 用完全部重试机会后才进入最终失败。
            self.status = TaskStatus.FAILED

    def attach_artifact(self, artifact: BackupArtifact) -> None:
        """成功产物必须与任务数据库一致。"""

        # 失败或未执行任务不能虚构产物。
        if self.status is not TaskStatus.SUCCESS:
            raise DomainError("只有成功任务可以关联备份产物")

        # 产物库名必须和任务库名一致，防止清单错挂。
        if artifact.db_name != self.db_name:
            raise DomainError("备份产物与任务数据库不一致")

        # 关联通过校验的产物。
        self.artifact = artifact
