"""校验编排服务：执行注入的校验器并把结果映射到产物可用性。"""

# 延迟类型注解。
from __future__ import annotations

# Callable/Mapping 描述校验器注册表。
from collections.abc import Callable, Mapping
# dataclass 定义不可变的校验结果。
from dataclasses import dataclass

# 导入产物实体与校验级别、领域异常。
from domain.model.entities.backup_artifact import BackupArtifact
from domain.model.value_objects import DomainError, VerificationLevel
# Clock 端口为校验时间注入，领域不读系统时钟。
from domain.repositories import Clock


@dataclass(frozen=True)
class VerificationResult:
    """一次完整性校验的领域结果。"""

    level: VerificationLevel  # 本次校验级别。
    success: bool             # 是否通过。
    reason: str = ""          # 失败原因（已脱敏、截断）。


class VerificationService:
    """编排 L0/L1/L2 校验。

    具体校验动作（gzip 合法性、建表数比对、影子库演练）由基础设施实现
    以校验器形式注入；本服务只负责编排，并把结果映射到产物可用性。
    """

    def __init__(
        self,
        verifiers: Mapping[VerificationLevel, Callable[[BackupArtifact], VerificationResult]],
        clock: Clock,
    ) -> None:
        # 复制注册表，避免外部后续修改影响本服务。
        self._verifiers = dict(verifiers)
        self._clock = clock

    def verify(self, artifact: BackupArtifact, level: VerificationLevel) -> VerificationResult:
        """对产物执行指定级别校验，并更新其可用性。"""
        # 未注册的级别不能静默跳过，必须显式失败。
        verifier = self._verifiers.get(level)
        if verifier is None:
            raise DomainError(f"未注册校验器：{level.value}")

        # 执行注入的具体校验动作。
        result = verifier(artifact)

        # 把校验结果映射到 Availability（Available / Unavailable），并记录校验元数据。
        artifact.verify(level, result.success, self._clock.now(), result.reason)
        return result
