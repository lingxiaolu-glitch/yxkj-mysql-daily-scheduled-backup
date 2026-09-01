"""领域服务（备份执行编排、保留策略、校验编排）纯逻辑单元测试。"""

# 延迟类型注解，与领域模块保持一致。
from __future__ import annotations

# unittest 提供测试类、断言和子测试。
import unittest
# datetime/timedelta/timezone 用于构造固定的带时区测试时间。
from datetime import datetime, timedelta, timezone

# 导入被测聚合根、实体与值对象。
from domain.model.aggregates.backup_run import BackupRun
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.entities.database_backup_task import DatabaseBackupTask
from domain.model.value_objects import (
    Availability,          # 产物可用性。
    BackupTime,            # 带时区时间。
    Compression,           # 压缩方式。
    DbName,                # 数据库名。
    DomainError,           # 领域规则异常。
    DumpResult,            # 外部转储结果。
    FileName,              # 领域生成文件名。
    RunStatus,             # 聚合整体状态。
    Sha256,                # 内容摘要。
    SizeBytes,             # 字节数。
    TaskStatus,            # 单任务状态。
    VerificationLevel,     # 校验级别。
)

# 导入被测领域服务。
from domain.services.backup_execution import BackupExecutionService
from domain.services.retention import RetentionService
from domain.services.verification import VerificationResult, VerificationService


class FakeClock:
    """固定时间端口，测试中完全可控。"""

    def __init__(self, now: BackupTime) -> None:
        # 保存固定时间。
        self._now = now

    def now(self) -> BackupTime:
        # 每次返回同一个时间。
        return self._now


class FakeDumpExecutor:
    """按调用顺序弹出预设 DumpResult 的假防腐层。"""

    def __init__(self, results: list[DumpResult]) -> None:
        # 保存剩余结果队列。
        self.results = list(results)
        # 记录每次 dump 的数据库名，便于断言调用顺序与次数。
        self.calls: list[str] = []

    def dump(self, task: DatabaseBackupTask) -> DumpResult:
        # 记录本次调用目标。
        self.calls.append(str(task.db_name))

        # 结果队列耗尽属于测试夹具配置错误。
        if not self.results:
            raise AssertionError("FakeDumpExecutor 预设结果已用尽")

        # 弹出队首结果返回。
        return self.results.pop(0)


class DomainServicesTestCase(unittest.TestCase):
    """领域服务公共测试夹具。"""

    def setUp(self) -> None:
        # 固定 UTC+8，避免测试结果受系统时区影响。
        self.zone = timezone(timedelta(hours=8))

        # 固定"当前时间"，供执行完成、校验和保留计算使用。
        self.now = BackupTime(datetime(2026, 8, 10, 2, 30, 0, tzinfo=self.zone))

        # 固定时钟端口。
        self.clock = FakeClock(self.now)

    def database(self, name: str) -> DbName:
        # 构造 DbName 的小工具。
        return DbName(name)

    def file_name(self, name: str, created_at: BackupTime) -> FileName:
        # 按领域规则生成文件名。
        return FileName(
            db_name=self.database(name),
            backup_time=created_at,
            compression=Compression.GZIP,
        )

    def artifact(
        self,
        name: str,
        created_at: BackupTime,
        availability: Availability = Availability.PENDING_VERIFY,
    ) -> BackupArtifact:
        # 组装一个备份产物实体。
        file_name = self.file_name(name, created_at)
        return BackupArtifact(
            db_name=self.database(name),
            file_name=file_name,
            relative_path=f"{created_at.date_key}/{file_name.value}",
            size_bytes=SizeBytes(128),
            sha256=Sha256("a" * 64),
            created_at=created_at,
            availability=availability,
        )

    def task(self, name: str, retry_times: int = 1) -> DatabaseBackupTask:
        # 构造一个待备份数据库任务。
        return DatabaseBackupTask(db_name=self.database(name), retry_times=retry_times)

    def result(self, success: bool, elapsed: float = 1.0, error: str = "") -> DumpResult:
        # 构造成功或失败 DumpResult。
        return DumpResult(
            success=success,
            return_code=0 if success else 2,
            elapsed_seconds=elapsed,
            error_digest=error,
        )

    def artifact_factory(self):
        """返回一个把成功任务组装为产物的工厂。"""

        def factory(task: DatabaseBackupTask, now: BackupTime) -> BackupArtifact:
            # 用任务库名和当前时间生成产物。
            return self.artifact(str(task.db_name), now)

        return factory


class BackupExecutionServiceTests(DomainServicesTestCase):
    """备份执行编排：部分失败隔离、重试次数、聚合完成。"""

    def test_all_successful_maps_to_success_and_attaches_artifacts(self) -> None:
        """全部成功：SUCCESS / exit 0，且成功任务关联产物。"""

        # 两个不允许重试的任务。
        tasks = (self.task("shop", retry_times=0), self.task("crm", retry_times=0))

        # 创建聚合并执行。
        run = BackupRun.start("run-ok", self.now, tasks)
        executor = FakeDumpExecutor([self.result(True), self.result(True)])
        service = BackupExecutionService(executor, self.clock)
        result_run = service.execute(run, artifact_factory=self.artifact_factory())

        # 返回同一个聚合实例。
        self.assertIs(result_run, run)

        # 整体成功、退出码 0。
        self.assertIs(RunStatus.SUCCESS, run.status)
        self.assertEqual(0, run.exit_code)

        # 两个任务都被执行且成功。
        self.assertEqual(["shop", "crm"], executor.calls)
        for task in tasks:
            self.assertIs(TaskStatus.SUCCESS, task.status)
            self.assertIsNotNone(task.artifact)

    def test_partial_failure_isolated_and_other_tasks_continue(self) -> None:
        """部分失败隔离：单库失败其余继续，整体 PARTIAL_SUCCESS / exit 1。"""

        # 第一个成功、第二个失败（均不允许重试）。
        tasks = (self.task("shop", retry_times=0), self.task("crm", retry_times=0))
        run = BackupRun.start("run-partial", self.now, tasks)
        executor = FakeDumpExecutor([self.result(True), self.result(False, error="dump failed")])
        service = BackupExecutionService(executor, self.clock)
        service.execute(run, artifact_factory=self.artifact_factory())

        # 整体部分成功、退出码 1。
        self.assertIs(RunStatus.PARTIAL_SUCCESS, run.status)
        self.assertEqual(1, run.exit_code)

        # 成功任务关联产物，失败任务保持终态失败。
        self.assertIs(TaskStatus.SUCCESS, tasks[0].status)
        self.assertIsNotNone(tasks[0].artifact)
        self.assertIs(TaskStatus.FAILED, tasks[1].status)

        # 失败任务没有阻断后续任务执行。
        self.assertEqual(["shop", "crm"], executor.calls)

    def test_retry_until_success(self) -> None:
        """重试：首次失败进入 RETRYING，第二次成功。"""

        # 允许 1 次额外重试。
        task = self.task("shop", retry_times=1)
        run = BackupRun.start("run-retry", self.now, (task,))
        executor = FakeDumpExecutor([self.result(False, error="first fail"), self.result(True)])
        service = BackupExecutionService(executor, self.clock)
        service.execute(run, artifact_factory=self.artifact_factory())

        # 共尝试 2 次后成功。
        self.assertEqual(2, task.attempts)
        self.assertIs(TaskStatus.SUCCESS, task.status)
        self.assertIsNotNone(task.artifact)
        self.assertIs(RunStatus.SUCCESS, run.status)
        self.assertEqual(["shop", "shop"], executor.calls)

    def test_retry_exhausted_becomes_failed(self) -> None:
        """重试耗尽：用完所有机会后进入最终失败。"""

        # 允许 1 次额外重试，但两次都失败。
        task = self.task("shop", retry_times=1)
        run = BackupRun.start("run-fail", self.now, (task,))
        executor = FakeDumpExecutor([self.result(False, error="a"), self.result(False, error="b")])
        service = BackupExecutionService(executor, self.clock)
        service.execute(run)

        # 共尝试 2 次后最终失败，整体 FAILED / exit 2。
        self.assertEqual(2, task.attempts)
        self.assertIs(TaskStatus.FAILED, task.status)
        self.assertIs(RunStatus.FAILED, run.status)
        self.assertEqual(2, run.exit_code)

    def test_no_retry_when_retry_times_zero(self) -> None:
        """retry_times=0：失败后不重试，只尝试一次。"""

        # 不允许重试。
        task = self.task("shop", retry_times=0)
        run = BackupRun.start("run-no-retry", self.now, (task,))
        executor = FakeDumpExecutor([self.result(False, error="fail")])
        service = BackupExecutionService(executor, self.clock)
        service.execute(run)

        # 只尝试一次即最终失败。
        self.assertEqual(1, task.attempts)
        self.assertIs(TaskStatus.FAILED, task.status)
        self.assertIs(RunStatus.FAILED, run.status)

    def test_empty_run_is_failed(self) -> None:
        """没有任务：完成时映射为 FAILED / exit 2。"""

        # 创建空聚合并执行。
        run = BackupRun.start("run-empty", self.now, ())
        service = BackupExecutionService(FakeDumpExecutor([]), self.clock)
        service.execute(run)

        # 空运行不能算成功。
        self.assertIs(RunStatus.FAILED, run.status)
        self.assertEqual(2, run.exit_code)


class RetentionServiceTests(DomainServicesTestCase):
    """保留策略纯函数：日备过期、周/月保护、负数拒绝。"""

    def setUp(self) -> None:
        # 复用公共夹具。
        super().setUp()
        # 被测保留服务。
        self.service = RetentionService()

    def artifact_on(self, name: str, day: int) -> BackupArtifact:
        """构造 2026-08 某一天 02:00 的产物。"""

        # 固定当天 02:00 创建。
        return self.artifact(name, BackupTime(datetime(2026, 8, day, 2, 0, 0, tzinfo=self.zone)))

    def test_daily_keeps_recent_and_deletes_expired(self) -> None:
        """日备：保留最近 1 天，更早的进入删除。"""

        # 08-08 早于截止日（08-09）应删除；08-09、08-10 应保留。
        old = self.artifact_on("shop", 8)
        recent = self.artifact_on("crm", 9)
        today = self.artifact_on("user", 10)
        plan = self.service.plan(1, 0, 0, [old, recent, today], self.now)

        # 只删除过期产物。
        self.assertEqual((old,), plan.to_delete)
        self.assertEqual((), plan.to_keep)

    def test_weekly_marked_protected_from_daily_cleanup(self) -> None:
        """周备保护：过期但属于周标记的产物不删除。"""

        # 上一周的周一（必然过期）作为周备标记产物。
        monday = BackupTime(self.now.value - timedelta(days=self.now.value.weekday() + 7))
        monday_artifact = self.artifact("shop", monday)

        # 普通过期产物。
        expired_plain = self.artifact_on("crm", 8)

        plan = self.service.plan(1, 1, 0, [monday_artifact, expired_plain], self.now)

        # 普通过期产物删除，周备标记产物保留。
        self.assertEqual((expired_plain,), plan.to_delete)
        self.assertEqual((monday_artifact,), plan.to_keep)

    def test_monthly_marked_protected_from_daily_cleanup(self) -> None:
        """月备保护：过期但属于月标记（每月 1 日）的产物不删除。"""

        # 08-01 为月备标记产物（已过期）。
        first = self.artifact_on("shop", 1)

        # 普通过期产物。
        expired_plain = self.artifact_on("crm", 8)

        plan = self.service.plan(1, 0, 1, [first, expired_plain], self.now)

        # 普通过期产物删除，月备标记产物保留。
        self.assertEqual((expired_plain,), plan.to_delete)
        self.assertEqual((first,), plan.to_keep)

    def test_weekly_monthly_off_means_no_protection(self) -> None:
        """周/月备关闭（0）时，过期产物一律删除。"""

        # 周一的过期产物，但 weekly=0 不保护。
        monday = BackupTime(self.now.value - timedelta(days=self.now.value.weekday() + 7))
        monday_artifact = self.artifact("shop", monday)

        plan = self.service.plan(1, 0, 0, [monday_artifact], self.now)

        # 照常删除。
        self.assertEqual((monday_artifact,), plan.to_delete)
        self.assertEqual((), plan.to_keep)

    def test_negative_retention_rejected(self) -> None:
        """负数档位：拒绝，避免误删语义。"""

        # 负天数必须抛领域异常。
        with self.assertRaises(DomainError):
            self.service.plan(-1, 0, 0, [], self.now)

    def test_deleted_names_reports_file_names(self) -> None:
        """CleanupPlan.deleted_names 返回删除产物文件名。"""

        # 构造一个过期产物。
        old = self.artifact_on("shop", 8)
        plan = self.service.plan(1, 0, 0, [old], self.now)

        # manifest 记录用文件名。
        self.assertEqual((str(old.file_name),), plan.deleted_names)


class VerificationServiceTests(DomainServicesTestCase):
    """校验编排：结果到可用性映射、未注册级别拒绝。"""

    def test_successful_verification_maps_to_available(self) -> None:
        """L0 校验通过：产物变为 Available，并记录校验元数据。"""

        # 注入通过校验器。
        artifact = self.artifact("shop", self.now)

        def verifier(a: BackupArtifact) -> VerificationResult:
            # 恒通过的 L0 校验。
            return VerificationResult(level=VerificationLevel.L0, success=True)

        service = VerificationService({VerificationLevel.L0: verifier}, self.clock)
        result = service.verify(artifact, VerificationLevel.L0)

        # 结果与可用性映射正确。
        self.assertTrue(result.success)
        self.assertIs(Availability.AVAILABLE, artifact.availability)
        self.assertIs(VerificationLevel.L0, artifact.verification_level)
        self.assertEqual(self.now, artifact.verified_at)

    def test_failed_verification_maps_to_unavailable(self) -> None:
        """L1 校验失败：产物变为 Unavailable，并记录失败原因。"""

        # 注入失败校验器。
        artifact = self.artifact("shop", self.now)

        def verifier(a: BackupArtifact) -> VerificationResult:
            # 恒失败的 L1 校验。
            return VerificationResult(
                level=VerificationLevel.L1,
                success=False,
                reason="table count mismatch",
            )

        service = VerificationService({VerificationLevel.L1: verifier}, self.clock)
        result = service.verify(artifact, VerificationLevel.L1)

        # 结果与可用性映射正确。
        self.assertFalse(result.success)
        self.assertIs(Availability.UNAVAILABLE, artifact.availability)
        self.assertEqual("table count mismatch", artifact.verification_error)

    def test_unregistered_level_rejected(self) -> None:
        """未注册的校验级别：显式抛领域异常，不允许静默跳过。"""

        # 空注册表。
        artifact = self.artifact("shop", self.now)
        service = VerificationService({}, self.clock)

        # L2 未注册必须拒绝。
        with self.assertRaises(DomainError):
            service.verify(artifact, VerificationLevel.L2)

    def test_verifier_receives_the_artifact(self) -> None:
        """校验器接收被测产物实体。"""

        # 记录传入的产物。
        seen: list[BackupArtifact] = []

        def verifier(a: BackupArtifact) -> VerificationResult:
            # 记录并返回通过。
            seen.append(a)
            return VerificationResult(level=VerificationLevel.L0, success=True)

        artifact = self.artifact("shop", self.now)
        service = VerificationService({VerificationLevel.L0: verifier}, self.clock)
        service.verify(artifact, VerificationLevel.L0)

        # 校验器拿到的正是被测产物。
        self.assertEqual([artifact], seen)


# 支持 python tests/unit/domain/test_services.py 直接运行。
if __name__ == "__main__":
    unittest.main()
