"""领域模型、值对象、状态机和事件的纯逻辑单元测试。"""

# 延迟类型注解，与领域模块保持一致。
from __future__ import annotations

# unittest 提供测试类、断言和子测试。
import unittest
# datetime/timezone/timedelta 用于构造固定的带时区测试时间。
from datetime import datetime, timedelta, timezone

# 导入需要验证的领域事件。
from domain.events import (
    BackupRunCompleted,       # 聚合完成事件。
    BackupRunStarted,         # 聚合开始事件。
    DatabaseBackupFailed,     # 单库失败事件。
    DatabaseBackupSucceeded,  # 单库成功事件。
)

# 导入领域实体和聚合根。
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.aggregates.backup_run import BackupRun
from domain.model.entities.database_backup_task import DatabaseBackupTask

# 导入被测值对象和领域异常。
from domain.model.value_objects import (
    Availability,          # 产物可用性。
    BackupScope,           # 备份范围。
    BackupTime,            # 带时区时间。
    Compression,           # 压缩方式。
    DbName,                # 数据库名。
    DomainError,           # 领域规则异常。
    DumpResult,            # 外部转储结果。
    ExitCode,              # 进程退出码。
    FileName,              # 领域生成文件名。
    RunStatus,             # 聚合状态。
    Sha256,                # 内容摘要。
    SizeBytes,             # 字节数。
    TaskStatus,            # 任务状态。
    VerificationLevel,     # 校验级别。
)


class DomainModelTestCase(unittest.TestCase):
    """领域模型公共测试夹具。"""

    def setUp(self) -> None:
        """为每个测试准备固定时间。"""

        # 固定 UTC+8，避免测试结果受系统时区影响。
        self.zone = timezone(timedelta(hours=8))

        # 构造带时区的开始时间。
        self.started_at = BackupTime(datetime(2026, 8, 10, 2, 0, 0, tzinfo=self.zone))

        # 构造晚于开始时间的完成时间。
        self.finished_at = BackupTime(datetime(2026, 8, 10, 2, 15, 0, tzinfo=self.zone))

    def database(self, name: str) -> DbName:
        """构造 DbName 的小工具。"""

        # 值对象内部会校验名称格式。
        return DbName(name)

    def file_name(
        self,
        name: str,
        *,
        schema_only: bool = False,
        compression: Compression = Compression.GZIP,
    ) -> FileName:
        """构造符合命名规则的 FileName。"""

        # 由领域规则自动生成完整文件名。
        return FileName(
            db_name=self.database(name),
            backup_time=self.started_at,
            compression=compression,
            schema_only=schema_only,
        )

    def artifact(
        self,
        name: str,
        *,
        schema_only: bool = False,
        availability: Availability = Availability.PENDING_VERIFY,
    ) -> BackupArtifact:
        """构造一个未校验状态的备份产物。"""

        # 生成与数据库和时间匹配的文件名。
        file_name = self.file_name(name, schema_only=schema_only)

        # 组装产物实体。
        return BackupArtifact(
            db_name=self.database(name),
            file_name=file_name,
            relative_path=f"20260810/{file_name.value}",
            size_bytes=SizeBytes(128),
            sha256=Sha256("a" * 64),
            created_at=self.started_at,
            schema_only=schema_only,
            availability=availability,
        )

    def task(self, name: str, retry_times: int = 1) -> DatabaseBackupTask:
        """构造一个待备份数据库任务。"""

        # 默认允许一次额外重试。
        return DatabaseBackupTask(
            db_name=self.database(name),
            retry_times=retry_times,
        )

    def success_result(self, elapsed: float = 1.5) -> DumpResult:
        """构造成功 DumpResult。"""

        # 成功对应退出码 0。
        return DumpResult(
            success=True,
            return_code=0,
            elapsed_seconds=elapsed,
        )

    def failure_result(self, elapsed: float = 2.0, error: str = "dump failed") -> DumpResult:
        """构造失败 DumpResult。"""

        # 错误摘要已由防腐层脱敏，测试中只是领域文本。
        return DumpResult(
            success=False,
            return_code=2,
            elapsed_seconds=elapsed,
            error_digest=error,
        )


class ValueObjectTests(DomainModelTestCase):
    """基础值对象规则。"""

    def test_file_name_matches_prd_rules(self) -> None:
        """全量与仅结构备份应按 PRD 命名。"""

        # 生成普通全量备份文件名。
        full = self.file_name("shop")

        # 生成仅结构备份文件名。
        schema = self.file_name("shop", schema_only=True)

        # 全量命名应精确匹配 PRD 示例格式。
        self.assertEqual("shop_20260810_020000.sql.gz", full.value)

        # 仅结构命名应包含 schema 标记。
        self.assertEqual("shop_schema_20260810_020000.sql.gz", schema.value)

    def test_file_name_supports_compression_kinds(self) -> None:
        """不同压缩策略应生成不同后缀。"""

        # gzip 对应 .sql.gz。
        self.assertEqual(
            "shop_20260810_020000.sql.gz",
            self.file_name("shop", compression=Compression.GZIP).value,
        )

        # zstd 对应 .sql.zst。
        self.assertEqual(
            "shop_20260810_020000.sql.zst",
            self.file_name("shop", compression=Compression.ZSTD).value,
        )

        # none 只保留 .sql。
        self.assertEqual(
            "shop_20260810_020000.sql",
            self.file_name("shop", compression=Compression.NONE).value,
        )

    def test_value_objects_reject_invalid_data(self) -> None:
        """非法标识符、无时区时间、摘要和负数大小必须拒绝。"""

        # 库名不能包含连字符。
        with self.assertRaises(DomainError):
            self.database("bad-name")

        # 时间缺少 tzinfo 必须拒绝。
        with self.assertRaises(DomainError):
            BackupTime(datetime(2026, 8, 10, 2, 0, 0))

        # SHA256 必须是小写十六进制。
        with self.assertRaises(DomainError):
            Sha256("A" * 64)

        # 文件大小不能为负。
        with self.assertRaises(DomainError):
            SizeBytes(-1)

    def test_backup_time_exports_manifest_keys(self) -> None:
        """BackupTime 提供清单与文件名使用的日期、时间键。"""

        # manifest 文件名使用 8 位日期。
        self.assertEqual("20260810", self.started_at.date_key)

        # 备份文件名使用 6 位时间。
        self.assertEqual("020000", self.started_at.time_key)

        # ISO 字符串保留时区偏移。
        self.assertEqual("2026-08-10T02:00:00+08:00", str(self.started_at))


class DatabaseBackupTaskTests(DomainModelTestCase):
    """单库任务重试状态机。"""

    def test_retry_times_one_allows_one_extra_attempt(self) -> None:
        """retry_times=1：首次失败进入 RETRYING，第二次失败才终态。"""

        # 创建允许一次额外重试的任务。
        task = self.task("shop", retry_times=1)

        # 第一次失败只进入重试状态。
        task.apply_dump_result(self.failure_result())

        # 首次尝试后计数为 1。
        self.assertEqual(1, task.attempts)

        # 尚未最终失败，应等待重试。
        self.assertIs(TaskStatus.RETRYING, task.status)

        # 已标记进入过重试。
        self.assertTrue(task.retried)

        # 保留失败原因。
        self.assertEqual("dump failed", task.last_error)

        # 第二次失败用完重试机会。
        task.apply_dump_result(self.failure_result())

        # 尝试次数变成 2。
        self.assertEqual(2, task.attempts)

        # 进入最终失败。
        self.assertIs(TaskStatus.FAILED, task.status)

    def test_zero_retry_fails_on_first_error(self) -> None:
        """retry_times=0：首次失败即为最终失败。"""

        # 禁止额外重试。
        task = self.task("shop", retry_times=0)

        # 应用第一次失败。
        task.apply_dump_result(self.failure_result())

        # 只尝试一次。
        self.assertEqual(1, task.attempts)

        # 直接进入最终失败。
        self.assertIs(TaskStatus.FAILED, task.status)

        # 没有发生过重试。
        self.assertFalse(task.retried)

    def test_retry_success_attaches_matching_artifact(self) -> None:
        """第一次失败、重试成功后任务应关联产物。"""

        # 允许一次额外重试。
        task = self.task("shop", retry_times=1)

        # 准备与任务一致的产物。
        artifact = self.artifact("shop")

        # 第一次失败进入重试。
        task.apply_dump_result(self.failure_result())

        # 第二次成功并关联产物。
        task.apply_dump_result(self.success_result(), artifact)

        # 最终状态成功。
        self.assertIs(TaskStatus.SUCCESS, task.status)

        # 成功产物已经关联。
        self.assertIs(artifact, task.artifact)

        # 共尝试两次。
        self.assertEqual(2, task.attempts)

        # 确实发生过重试。
        self.assertTrue(task.retried)

    def test_success_artifact_database_must_match(self) -> None:
        """成功产物与任务数据库不一致时必须拒绝。"""

        # 创建 shop 任务。
        task = self.task("shop", retry_times=0)

        # 产物属于另一个数据库。
        artifact = self.artifact("crm")

        # 不允许错挂产物。
        with self.assertRaises(DomainError):
            task.apply_dump_result(self.success_result(), artifact)

    def test_terminal_task_cannot_accept_result(self) -> None:
        """已成功任务不允许继续应用 DumpResult。"""

        # 创建并立即成功。
        task = self.task("shop", retry_times=0)
        task.apply_dump_result(self.success_result(), self.artifact("shop"))

        # 终态任务不能再接收新结果。
        with self.assertRaises(DomainError):
            task.apply_dump_result(self.success_result())


class BackupArtifactTests(DomainModelTestCase):
    """备份产物校验与恢复门禁。"""

    def test_new_artifact_starts_pending(self) -> None:
        """新产物默认 PendingVerify，不能作为恢复源。"""

        # 创建未校验产物。
        artifact = self.artifact("shop")

        # 默认可用性是 pending_verify。
        self.assertIs(Availability.PENDING_VERIFY, artifact.availability)

        # 尚不可恢复。
        self.assertFalse(artifact.is_available)

        # 恢复门禁必须拒绝。
        with self.assertRaises(DomainError):
            artifact.ensure_available()

    def test_successful_verification_enables_restore(self) -> None:
        """L1 校验成功后产物 Available 并可返回恢复路径。"""

        # 创建待校验产物。
        artifact = self.artifact("shop")

        # 执行成功的 L1 校验。
        artifact.verify(VerificationLevel.L1, True, self.finished_at)

        # 可用性变为 available。
        self.assertIs(Availability.AVAILABLE, artifact.availability)

        # 门禁返回相对恢复路径。
        self.assertEqual("20260810/shop_20260810_020000.sql.gz", artifact.ensure_available())

    def test_failed_verification_blocks_restore(self) -> None:
        """校验失败产物进入 Unavailable 并拒绝恢复。"""

        # 创建产物。
        artifact = self.artifact("shop")

        # 执行失败的 L0 校验。
        artifact.verify(
            VerificationLevel.L0,
            False,
            self.finished_at,
            "gzip tail missing",
        )

        # 可用性必须变为 unavailable。
        self.assertIs(Availability.UNAVAILABLE, artifact.availability)

        # 保存失败原因供日志和恢复错误提示。
        self.assertEqual("gzip tail missing", artifact.verification_error)

        # 恢复门禁拒绝不可用产物。
        with self.assertRaises(DomainError):
            artifact.ensure_available()

    def test_deleted_artifact_cannot_be_restored_or_verified(self) -> None:
        """已删除产物必须永远离开恢复候选集。"""

        # 先创建并校验成功。
        artifact = self.artifact("shop")
        artifact.verify(VerificationLevel.L1, True, self.finished_at)

        # 再标记删除。
        artifact.mark_deleted()

        # 删除终态生效。
        self.assertTrue(artifact.deleted)

        # 恢复门禁拒绝。
        with self.assertRaises(DomainError):
            artifact.ensure_available()

        # 已删除产物不允许重新校验。
        with self.assertRaises(DomainError):
            artifact.verify(VerificationLevel.L2, True, self.finished_at)

    def test_artifact_rejects_path_escape(self) -> None:
        """产物相对路径禁止绝对路径或父目录跳转。"""

        # 尝试使用 .. 跳出 dest_dir。
        with self.assertRaises(DomainError):
            BackupArtifact(
                db_name=self.database("shop"),
                file_name=self.file_name("shop"),
                relative_path="../shop/escape.sql.gz",
                size_bytes=SizeBytes(1),
                sha256=Sha256("b" * 64),
                created_at=self.started_at,
            )


class BackupRunTests(DomainModelTestCase):
    """备份聚合整体状态机。"""

    def test_start_records_run_started_event(self) -> None:
        """创建聚合时应发布 BackupRunStarted。"""

        # 使用 start 工厂创建两个任务。
        run = BackupRun.start(
            "20260810-020000-aaaa",
            self.started_at,
            (self.task("shop"), self.task("crm")),
            BackupScope.ALL,
        )

        # 初始状态是 running。
        self.assertIs(RunStatus.RUNNING, run.status)

        # 数据库顺序与任务顺序一致。
        self.assertEqual(("shop", "crm"), run.database_names)

        # 只有开始事件。
        self.assertEqual(1, len(run.events))

        # 事件类型是 BackupRunStarted。
        self.assertIsInstance(run.events[0], BackupRunStarted)

    def test_rejects_duplicate_database_tasks(self) -> None:
        """同一 run 中数据库任务必须唯一。"""

        # 同名任务会造成结果无法归属，必须拒绝。
        with self.assertRaises(DomainError):
            BackupRun(
                "run-id",
                self.started_at,
                (self.task("shop"), self.task("shop")),
            )

    def test_all_success_maps_to_exit_zero(self) -> None:
        """全部成功：SUCCESS / exit 0。"""

        # 创建两个待备份数据库。
        tasks = (self.task("shop"), self.task("crm"))

        # 创建聚合。
        run = BackupRun.start("run-ok", self.started_at, tasks)

        # 逐个标记成功并关联产物。
        for task in tasks:
            run.mark_task_result(
                task.db_name,
                self.success_result(),
                self.artifact(str(task.db_name)),
            )

        # 完成聚合。
        run.finish(self.finished_at)

        # 全部成功映射为 SUCCESS。
        self.assertIs(RunStatus.SUCCESS, run.status)

        # 退出码必须是 0。
        self.assertEqual(ExitCode.SUCCESS, run.exit_code)

        # 找出完成事件。
        completed_events = [event for event in run.events if isinstance(event, BackupRunCompleted)]

        # 找出单库成功事件。
        success_events = [event for event in run.events if isinstance(event, DatabaseBackupSucceeded)]

        # 只应有一个完成事件。
        self.assertEqual(1, len(completed_events))

        # 每个成功任务都应有一条成功事件。
        self.assertEqual(2, len(success_events))

    def test_partial_failure_maps_to_exit_one(self) -> None:
        """部分失败：PARTIAL_SUCCESS / exit 1。"""

        # 一个任务成功，一个任务禁止重试。
        success_task = self.task("shop")
        failed_task = self.task("crm", retry_times=0)

        # 创建聚合。
        run = BackupRun.start("run-partial", self.started_at, (success_task, failed_task))

        # 第一个任务成功。
        run.mark_task_result(
            success_task.db_name,
            self.success_result(),
            self.artifact("shop"),
        )

        # 第二个任务最终失败。
        run.mark_task_result(failed_task.db_name, self.failure_result())

        # 完成聚合。
        run.finish(self.finished_at)

        # 部分成功状态。
        self.assertIs(RunStatus.PARTIAL_SUCCESS, run.status)

        # 退出码为 1。
        self.assertEqual(ExitCode.PARTIAL_SUCCESS, run.exit_code)

        # 找出单库失败事件。
        failed_events = [event for event in run.events if isinstance(event, DatabaseBackupFailed)]

        # 只有 crm 失败一次。
        self.assertEqual(1, len(failed_events))

        # 该失败没有重试机会。
        self.assertFalse(failed_events[0].will_retry)

    def test_all_failed_maps_to_exit_two(self) -> None:
        """全部失败：FAILED / exit 2。"""

        # 两个任务都禁止重试。
        tasks = (self.task("shop", retry_times=0), self.task("crm", retry_times=0))

        # 创建聚合。
        run = BackupRun.start("run-failed", self.started_at, tasks)

        # 全部标记最终失败。
        for task in tasks:
            run.mark_task_result(task.db_name, self.failure_result())

        # 完成聚合。
        run.finish(self.finished_at)

        # 聚合状态为 FAILED。
        self.assertIs(RunStatus.FAILED, run.status)

        # 退出码为 2。
        self.assertEqual(ExitCode.FAILED, run.exit_code)

    def test_empty_run_is_failed_when_finished(self) -> None:
        """没有任务视为无有效备份，完成时映射为 FAILED。"""

        # 创建空聚合。
        run = BackupRun.start("run-empty", self.started_at, ())

        # 完成空运行。
        run.finish(self.finished_at)

        # 空运行不能算成功。
        self.assertIs(RunStatus.FAILED, run.status)

        # 退出码为 2。
        self.assertEqual(ExitCode.FAILED, run.exit_code)

    def test_cannot_finish_while_task_is_retrying(self) -> None:
        """仍有 RETRYING 任务时不能生成最终退出码。"""

        # 创建一个允许重试的任务。
        run = BackupRun.start("run-retrying", self.started_at, (self.task("shop"),))

        # 第一次失败进入重试。
        run.mark_task_result("shop", self.failure_result())

        # 存在重试任务时不能 finish。
        with self.assertRaises(DomainError):
            run.finish(self.finished_at)

    def test_unknown_task_and_duplicate_finish_are_rejected(self) -> None:
        """聚合应拒绝未知任务和重复完成。"""

        # 创建一个立即失败的任务。
        run = BackupRun.start("run-guard", self.started_at, (self.task("shop", retry_times=0),))

        # 标记失败进入终态。
        run.mark_task_result("shop", self.failure_result())

        # 正常完成聚合。
        run.finish(self.finished_at)

        # 完成后不能重复 finish。
        with self.assertRaises(DomainError):
            run.finish(self.finished_at)

        # 完成后也不能再更新任何任务。
        with self.assertRaises(DomainError):
            run.mark_task_result("unknown", self.success_result())


# 支持 python tests/unit/domain/test_models.py 直接运行。
if __name__ == "__main__":
    unittest.main()
