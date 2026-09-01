"""压缩适配器：把领域 Compressor 端口落地为流式压缩实现。"""

# 延迟类型注解。
from __future__ import annotations

# zlib 以流式方式生成 gzip 格式；Iterable/Iterator 描述字节流。
import zlib
from collections.abc import Iterable, Iterator


class GzipCompressor:
    """gzip 流式压缩（默认方案，纯标准库，产出合法 .gz）。"""

    @property
    def suffix(self) -> str:
        # 压缩产物后缀。
        return ".gz"

    def compress(self, chunks: Iterable[bytes]) -> Iterator[bytes]:
        """把输入字节流压缩为 gzip 字节流。"""

        # wbits=31（MAX_WBITS|16）让 zlib 输出 gzip 格式（含头部与尾部校验和）。
        compressor = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
        for chunk in chunks:
            if chunk:
                # 逐块压缩，内存里始终只有一小块。
                yield compressor.compress(chunk)
        # flush 输出剩余数据与 gzip 尾部，结束压缩流。
        yield compressor.flush()


class NoopCompressor:
    """不压缩：原样透传字节流，用于排查压缩层问题或特殊场景。"""

    @property
    def suffix(self) -> str:
        # 无压缩后缀。
        return ""

    def compress(self, chunks: Iterable[bytes]) -> Iterator[bytes]:
        # 原样透传每一块，不做任何处理。
        for chunk in chunks:
            yield chunk


class ZstdCompressor:
    """zstd 压缩（v1 预留）。

    标准库不含 zstd；需第三方 zstandard 库或系统 zstd 命令才可启用。
    compress() 明确报错，避免静默产出错误产物。
    """

    @property
    def suffix(self) -> str:
        # zstd 产物后缀。
        return ".zst"

    def compress(self, chunks: Iterable[bytes]) -> Iterator[bytes]:
        # 预留实现：未接入第三方库前不允许使用。
        raise NotImplementedError("zstd 压缩需第三方 zstandard 库或系统 zstd 命令，v1 仅预留接口")
