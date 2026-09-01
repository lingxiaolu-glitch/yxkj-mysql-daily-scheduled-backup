"""压缩适配器单元测试：gzip 可解压且非空、noop 透传、zstd 预留。"""

# 延迟类型注解。
from __future__ import annotations

# gzip 用于解压验证。
import gzip
# unittest 提供测试类与断言。
import unittest

# 导入被测压缩适配器。
from infrastructure.compressor import GzipCompressor, NoopCompressor, ZstdCompressor


class CompressorTests(unittest.TestCase):
    """Gzip/Noop/Zstd 三种压缩策略。"""

    def test_gzip_compress_decompress_roundtrip(self) -> None:
        """gzip 压缩产物非空且可解压回原文。"""

        # 压缩一大块数据。
        compressor = GzipCompressor()
        data = b"hello world " * 500
        compressed = b"".join(compressor.compress([data]))

        # 非空、可解压、内容一致。
        self.assertTrue(compressed)
        self.assertEqual(data, gzip.decompress(compressed))

    def test_gzip_streaming_chunks(self) -> None:
        """gzip 逐块压缩结果与一次性压缩一致。"""

        # 分三块喂入。
        compressor = GzipCompressor()
        data = b"streaming data " * 100
        parts = [data[:100], data[100:200], data[200:]]
        compressed = b"".join(compressor.compress(parts))

        # 结果等价且可解压。
        self.assertEqual(data, gzip.decompress(compressed))

    def test_gzip_suffix(self) -> None:
        """gzip 后缀为 .gz。"""

        # 后缀用于文件名生成。
        self.assertEqual(".gz", GzipCompressor().suffix)

    def test_noop_passthrough(self) -> None:
        """noop 原样透传且无后缀。"""

        # 透传字节块。
        compressor = NoopCompressor()
        chunks = [b"ab", b"cd", b"ef"]
        self.assertEqual(b"abcdef", b"".join(compressor.compress(chunks)))
        self.assertEqual("", compressor.suffix)

    def test_zstd_reserved_raises(self) -> None:
        """zstd 预留：suffix 为 .zst，但 compress() 明确报错。"""

        # 未接入第三方库前不允许静默使用。
        compressor = ZstdCompressor()
        self.assertEqual(".zst", compressor.suffix)
        with self.assertRaises(NotImplementedError):
            b"".join(compressor.compress([b"x"]))


# 支持直接运行。
if __name__ == "__main__":
    unittest.main()
