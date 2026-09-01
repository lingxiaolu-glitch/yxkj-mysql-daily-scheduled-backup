"""本地文件存储适配器：把领域 ArtifactStorage 端口落地为本地目录读写。"""

# 延迟类型注解。
from __future__ import annotations

# hashlib 计算 SHA256；os/stat 负责权限；Iterable 描述字节流输入。
import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

# 领域值对象与端口结果。
from domain.model.value_objects import DomainError, FileName
from domain.repositories import StoredArtifact


class LocalFileStorage:
    """本地目录文件存储：写流、列目录、按名删除，路径一律防越界。

    所有相对路径必须落在 dest_dir 内；越界访问直接抛 DomainError。
    文件权限 0600、目录 0700（Windows 尽力而为）。
    """

    def __init__(self, dest_dir: Path) -> None:
        # 根目录解析为绝对路径，后续所有路径都以它为基准。
        self._dest_dir = Path(dest_dir).resolve()

    # ---- 路径安全：规范化 + 防越界（双重校验） ----

    def _resolve(self, relative_path: str) -> Path:
        """把相对路径规范化并解析为绝对路径。"""

        # 统一反斜杠为 POSIX 分隔符；先保留原始形态用于绝对路径判断。
        raw = relative_path.replace("\\", "/")
        # 移除首尾斜杠得到用于拼接的规范化路径。
        normalized = raw.strip("/")

        # 拒绝空路径、绝对路径与 .. 跳转段。
        # 注意：绝对路径必须在 strip 之前判断，否则 /abs 会被误当成相对路径。
        if not normalized or raw.startswith("/") or ".." in normalized.split("/"):
            raise DomainError("产物路径必须是 dest_dir 内的相对路径")

        # resolve() 处理符号链接后返回绝对路径。
        return (self._dest_dir / normalized).resolve()

    def _ensure_inside(self, path: Path) -> Path:
        """确认解析后的路径仍在 dest_dir 内，防止越界删除/读取。"""

        # Windows 的 PurePath 比较自带大小写不敏感处理。
        if not path.is_relative_to(self._dest_dir):
            raise DomainError("产物路径越界：不允许访问 dest_dir 之外")
        return path

    # ---- 权限（Windows 尽力而为） ----

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        # POSIX 上生效；Windows chmod 语义有限，失败时静默忽略。
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    # ---- 领域 ArtifactStorage 端口实现 ----

    def write_chunks(self, file_name: FileName, chunks: Iterable[bytes]) -> StoredArtifact:
        """按日期目录写入备份字节流，返回相对路径、大小与 SHA256。"""

        # 相对路径 = 日期目录 / 完整文件名（如 20260810/shop_..._020000.sql.gz）。
        relative = f"{file_name.backup_time.date_key}/{file_name.value}"
        target = self._ensure_inside(self._resolve(relative))

        # 创建日期目录（0700 尽力而为）。
        target.parent.mkdir(parents=True, exist_ok=True)
        self._chmod(target.parent, 0o700)

        # 边写边计算大小与摘要，避免二次读盘。
        hasher = hashlib.sha256()
        size = 0
        try:
            with target.open("wb") as fh:
                for chunk in chunks:
                    fh.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
        except BaseException:
            # 写入失败时清理残留文件，避免留下半成品。
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            raise

        # 产物文件权限 0600（尽力而为）。
        self._chmod(target, 0o600)
        return StoredArtifact(
            relative_path=relative,
            size_bytes=size,
            sha256=hasher.hexdigest(),
        )

    def exists(self, relative_path: str) -> bool:
        """判断产物是否仍存在于 dest_dir 内。"""

        # 先防越界，再判断文件存在性。
        target = self._ensure_inside(self._resolve(relative_path))
        return target.is_file()

    def read_bytes(self, relative_path: str) -> bytes:
        """读取产物字节；具体校验由领域服务编排。"""

        # 先防越界，再读取。
        target = self._ensure_inside(self._resolve(relative_path))
        return target.read_bytes()

    def delete(self, relative_path: str) -> None:
        """在路径防越界校验后删除产物。"""

        # 先防越界，再删除；文件不存在时静默忽略。
        target = self._ensure_inside(self._resolve(relative_path))
        try:
            target.unlink()
        except FileNotFoundError:
            pass

    # ---- 扩展：列目录（供保留清理枚举产物） ----

    def list_relative_paths(self) -> tuple[str, ...]:
        """列出 dest_dir 内全部产物文件的相对路径（POSIX 风格，排序稳定）。"""

        # rglob 递归枚举，仅保留文件并转成相对路径。
        return tuple(
            path.relative_to(self._dest_dir).as_posix()
            for path in sorted(self._dest_dir.rglob("*"))
            if path.is_file()
        )
