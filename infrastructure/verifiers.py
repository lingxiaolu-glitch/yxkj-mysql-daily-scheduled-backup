"""L0/L1 校验器：把校验动作翻译为 VerificationResult。

L0 校验文件完整性：
- 文件必须存在且非空；
- gzip 文件必须能完整解压；
- SQL 尾部必须包含 mysqldump 的 "Dump completed" 标记。

L1 校验结构一致性：
- 解压后统计 CREATE TABLE 数量；
- 与 MySQL 网关统计的源库表数量比对；
- 支持显式传入 db -> 期望表数量，用于指定表备份模式。
"""

# 延迟类型注解。
from __future__ import annotations

# gzip 解压与 zlib 错误处理；re 统计 CREATE TABLE；Mapping 描述期望表数。
import gzip
import re
import tempfile
import zlib
from pathlib import Path
from collections.abc import Mapping

# 产物实体。
from domain.model.entities.backup_artifact import BackupArtifact

# 值对象与领域异常。
from domain.model.value_objects import Compression, DbName, DomainError, VerificationLevel

# 领域端口与校验结果。
from domain.repositories import ArtifactStorage, MySqlGateway
from domain.services.verification import VerificationResult


class FileIntegrityVerifier:
    """L0 文件完整性校验器。"""

    # SQL 备份结束标记，mysqldump 默认会输出。
    DUMP_COMPLETED = "Dump completed"

    def __init__(self, storage: ArtifactStorage) -> None:
        # 注入存储端口，校验器不直接使用文件系统绝对路径。
        self._storage = storage

    def verify(self, artifact: BackupArtifact) -> VerificationResult:
        """执行 L0 校验并返回结果。"""
        try:
            decoded = self._decode_sql_checked(artifact)
        except Exception as exc:
            # 错误信息只保留脱敏后的摘要，不把密码等敏感值带出。
            return VerificationResult(VerificationLevel.L0, False, reason=str(exc))

        # 尾部最近 4KB 必须出现结束标记。
        if self.DUMP_COMPLETED.encode("utf-8") not in decoded[-4096:]:
            return VerificationResult(
                VerificationLevel.L0,
                False,
                reason=f"备份尾部缺少 {self.DUMP_COMPLETED} 标记",
            )

        return VerificationResult(VerificationLevel.L0, True)

    def __call__(self, artifact: BackupArtifact) -> VerificationResult:
        """让实例可直接作为校验器 callable 使用。"""
        return self.verify(artifact)

    def _decode_sql_checked(self, artifact: BackupArtifact) -> bytes:
        """读取并解压备份文件；同时完成存在性、非空与 gzip 合法性检查。"""
        # 必须先检查存在性，避免 read_bytes 抛出底层路径错误。
        if not self._storage.exists(artifact.relative_path):
            raise DomainError("备份文件不存在")

        raw = self._storage.read_bytes(artifact.relative_path)
        if not raw:
            raise DomainError("备份文件为空")

        compression = artifact.file_name.compression
        if compression is Compression.NONE:
            return raw

        if compression is Compression.GZIP:
            try:
                return gzip.decompress(raw)
            except (OSError, EOFError, zlib.error) as exc:
                raise DomainError(f"gzip 文件损坏：{exc}") from exc

        # zstd 当前由 ZstdCompressor 预留，不生产，因此校验器明确拒绝。
        raise DomainError(f"当前不支持校验压缩格式：{compression.value}")


def decode_artifact_sql(storage: ArtifactStorage, artifact: BackupArtifact) -> bytes:
    """读取并解压备份文件；供 L0/L1/L2 共用。"""

    # 复用 L0 的检查逻辑，避免三处重复实现。
    return FileIntegrityVerifier(storage)._decode_sql_checked(artifact)


class StructureVerifier:
    """L1 结构校验器：CREATE TABLE 数量与源库比对。"""

    # 匹配 mysqldump 输出的建表语句，不区分大小写。
    _CREATE_TABLE_RE = re.compile(
        r"(?im)^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    )

    def __init__(
        self,
        storage: ArtifactStorage,
        gateway: MySqlGateway,
        expected_counts: Mapping[str, int] | None = None,
    ) -> None:
        # 保存存储与 MySQL 网关端口。
        self._storage = storage
        self._gateway = gateway
        # 复制期望表数映射，避免调用方后续修改影响校验。
        self._expected_counts = dict(expected_counts or {})

    def __call__(self, artifact: BackupArtifact) -> VerificationResult:
        """让实例可直接作为校验器 callable 使用。"""
        return self.verify(artifact)

    def verify(self, artifact: BackupArtifact) -> VerificationResult:
        """执行 L1 校验并返回结果。"""
        try:
            decoded = FileIntegrityVerifier(self._storage)._decode_sql_checked(artifact)
        except Exception as exc:
            return VerificationResult(VerificationLevel.L1, False, reason=str(exc))

        # 使用 decode 后的 UTF-8 文本统计 CREATE TABLE。
        content = decoded.decode("utf-8", errors="replace")
        actual = len(self._CREATE_TABLE_RE.findall(content))

        # 指定表备份时使用配置里的期望数量；整库模式直接查询源库。
        expected = self._expected_counts.get(str(artifact.db_name))
        if expected is None:
            expected = self._gateway.count_tables(artifact.db_name)

        if actual != expected:
            return VerificationResult(
                VerificationLevel.L1,
                False,
                reason=f"建表数量不一致：产物 {actual}，期望 {expected}",
            )

        return VerificationResult(VerificationLevel.L1, True)

class RestoreVerifier:
    """L2 恢复级校验器：恢复到影子库并比对表数量/抽样行数。"""

    def __init__(
        self,
        storage: ArtifactStorage,
        gateway: MySqlGateway,
        shadow_db_prefix: str,
        sample_tables: tuple[str, ...] = (),
        expected_counts: Mapping[str, int] | None = None,
        temp_dir: str | None = None,
    ) -> None:
        # 保存存储与 MySQL 网关。
        self._storage = storage
        self._gateway = gateway
        self._shadow_db_prefix = shadow_db_prefix
        self._sample_tables = tuple(sample_tables)
        self._expected_counts = dict(expected_counts or {})
        self._temp_dir = temp_dir

    def __call__(self, artifact: BackupArtifact) -> VerificationResult:
        """让实例可直接作为校验器 callable 使用。"""
        return self.verify(artifact)

    def verify(self, artifact: BackupArtifact) -> VerificationResult:
        """执行影子库恢复比对，失败时清理影子库。"""
        source = artifact.db_name
        shadow = DbName(f"{self._shadow_db_prefix}{source}")

        # 不校验 schema-only 文件：L2 必须对完整数据产物执行。
        if artifact.file_name.schema_only:
            return VerificationResult(
                VerificationLevel.L2,
                False,
                reason="L2 不校验仅表结构文件",
            )

        try:
            # 先解码备份内容（gzip 损坏/缺失直接失败）。
            decoded = decode_artifact_sql(self._storage, artifact)

            # 写入临时 SQL 文件，再交给 mysql CLI 导入。
            temp = tempfile.NamedTemporaryFile(
                mode="wb", suffix=".sql", delete=False, dir=self._temp_dir
            )
            temp.write(decoded)
            temp.close()

            # 从干净状态开始，避免上一次演练残留。
            self._gateway.drop_database(shadow)
            self._gateway.create_shadow_database(source, shadow)

            try:
                # 导入到影子库；one_database 保证只进入本次目标库。
                self._gateway.restore(
                    temp.name,
                    shadow,
                    one_database=True,
                    rewrite_to_database=shadow,
                )

                # 表数量比对；支持指定表模式提供期望数量。
                actual_tables = self._gateway.count_tables(shadow)
                expected_count = self._expected_counts.get(str(source))
                if expected_count is None:
                    expected_count = self._gateway.count_tables(source)

                if actual_tables != expected_count:
                    return VerificationResult(
                        VerificationLevel.L2,
                        False,
                        reason=f"影子库表数量不一致：{actual_tables}/{expected_count}",
                    )

                # 抽样行数比对：sample_tables 为 db.table 形式。
                for table_ref in self._sample_tables:
                    if "." not in table_ref:
                        continue
                    ref_db, table = table_ref.split(".", 1)
                    if ref_db != str(source):
                        continue
                    source_rows = self._gateway.count_table_rows(source, table)
                    shadow_rows = self._gateway.count_table_rows(shadow, table)
                    if source_rows != shadow_rows:
                        return VerificationResult(
                            VerificationLevel.L2,
                            False,
                            reason=f"行数不一致：{table} {source_rows}/{shadow_rows}",
                        )

                return VerificationResult(VerificationLevel.L2, True)
            finally:
                # 无论成功失败，都清理临时文件和影子库。
                self._gateway.drop_database(shadow)
                try:
                    Path(temp.name).unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:
            # 恢复演练失败按校验失败处理。
            return VerificationResult(VerificationLevel.L2, False, reason=str(exc))
