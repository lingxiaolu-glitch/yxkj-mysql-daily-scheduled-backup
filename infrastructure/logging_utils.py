"""步骤 03：统一日志配置与敏感信息脱敏工具。

本模块只依赖 Python 标准库，供后续触发层和应用层共同使用：

- setup_logging：为每次备份创建/重建轮转文件日志；
- redact：把已知密码、Token 等替换成 ***；
- redact_command：生成可回显的命令行文本并遮蔽常见凭据参数。

注意：本模块不逐行打印运行期 debug 日志，避免把待脱敏文本提前写入其他 handler。
"""

# 启用延迟类型注解：兼容新语法并降低模块导入时的注解求值开销。
from __future__ import annotations

# 标准库日志：提供 logger、级别、格式器和过滤器协议。
import logging
# 正则模块：识别 URL userinfo、query token 和命令行密码选项。
import re
# shell 词法模块：把参数列表还原成带引号、适合人工审计的命令行。
import shlex
# 按大小轮转文件处理器：满足单文件上限和保留份数要求。
from logging.handlers import RotatingFileHandler
# 路径对象：统一处理 Windows/Linux 日志目录。
from pathlib import Path
# 只声明只读字符串序列；调用方可传 list 或 tuple。
from typing import Sequence

# 显式声明模块对外接口；from xxx import * 时只导出这些名字。
__all__ = [
    "LOGGER_NAME",
    "REDACTED",
    "setup_logging",
    "redact",
    "redact_command",
]

# 固定 logger 名称，保证全项目共用同一套日志配置。
LOGGER_NAME = "mysql_backup"
# 所有敏感值统一替换成该占位符。
REDACTED = "***"
# PRD 8.7 约定的日志时间格式：YYYY-MM-DD HH:MM:SS。
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 识别 URL query 中 token/api_key/password/signature 等敏感参数。
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|access[_-]?token|refresh[_-]?token|id[_-]?token"
    r"|api[_-]?key|apikey|secret|password|passwd|pwd|signature)=)[^&#\s]+"
)
# 识别 scheme://user:password@host 中的密码部分。
_URL_USERINFO_RE = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s:?#]+):([^@\s/?#]+)@"
)
# 识别 --password/--passwd/--pwd 及其带后缀或带等号值的形态。
_LONG_PASSWORD_OPTION_RE = re.compile(
    r"^--(?:password|passwd|pwd)(?:[-_][a-z0-9_-]+)*(?:=.+)?$", re.I
)


def _resolve_level(level: int | str) -> int:
    """把整数级别或 INFO/WARNING 这类名称转换成标准库数值级别。"""
    if isinstance(level, int):
        return level
    # 把 INFO/WARNING 这类名称统一转大写后交给标准库解析。
    resolved = logging.getLevelName(str(level).upper())
    if not isinstance(resolved, int):
        raise ValueError(f"非法日志级别：{level}")
    return resolved


def _prepare_log_directory(log_dir: str | Path) -> Path:
    """确保日志目录存在，并尽力设置成仅所有者可进入（0700）。"""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


class _RunIdFilter(logging.Filter):
    """给当前 logger 的每条记录注入同一个 run_id。"""

    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def setup_logging(level, log_dir, max_bytes, backup_count, run_id):
    """初始化 MySQL 备份使用的轮转文件日志。"""
    # 校验 run_id：它是贯穿一次备份所有日志的主标识，不允许为空。
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id 不能为空")

    # 将 "INFO"/"WARNING" 或数字统一解析成标准库可用的数字级别。
    resolved_level = _resolve_level(level)

    # maxBytes=0 会让按大小轮转失效，因此必须为正整数。
    if max_bytes <= 0:
        raise ValueError(f"max_bytes 必须为正整数，实际为 {max_bytes}")

    # backup_count=0 表示不保留历史文件，负数无意义。
    if backup_count < 0:
        raise ValueError(f"backup_count 不能为负数，实际为 {backup_count}")

    # 创建实例专属日志目录，并尽力收紧 POSIX 权限。
    directory = _prepare_log_directory(log_dir)

    # 使用固定名称获取进程级 logger，方便各模块共享同一套配置。
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(resolved_level)

    # 禁止向 root logger 冒泡，避免备份日志被父级 handler 重复输出。
    logger.propagate = False

    # 重复初始化时必须清空旧 handler，否则同一条日志会写入多个位置。
    for old_handler in list(logger.handlers):
        logger.removeHandler(old_handler)
        old_handler.close()

    # 创建按大小轮转的文件处理器：delay=True 表示第一条日志写入前不打开文件。
    handler = RotatingFileHandler(
        directory / "backup.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )

    # Handler 和 Logger 使用同一级别，保证过滤行为一致。
    handler.setLevel(resolved_level)

    # 每条 LogRecord 都注入同一个 run_id，供格式化器输出。
    handler.addFilter(_RunIdFilter(run_id))

    # 统一格式：时间 + 级别 + run_id + 业务上下文/消息。
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [run_id=%(run_id)s] %(message)s",
            datefmt=_TIME_FORMAT,
        )
    )

    # 将最终处理器挂载到备份 logger 并返回。
    logger.addHandler(handler)
    return logger


def redact(text, secrets):
    """把文本中的已知敏感值和常见 URL 凭据替换为 ***。"""
    if not text:
        return ""

    # result 是独立工作副本；字符串不可变，不会影响调用方原对象。
    result = text

    # 去重、剔除空白值，并按长度倒序：优先替换最长敏感值，防止短值截断长值。
    unique_secrets = sorted(
        {item for item in secrets if isinstance(item, str) and item.strip()},
        key=len,
        reverse=True,
    )

    # 先替换调用方明确给出的完整密码/Token。
    for secret in unique_secrets:
        result = result.replace(secret, REDACTED)

    # mysql://user:password@host/db => mysql://user:***@host/db
    result = _URL_USERINFO_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}:{REDACTED}@", result
    )

    # https://host/hook?token=secret&x=1 => https://host/hook?token=***&x=1
    result = _QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", result)

    return result


def redact_command(argv, secrets=None):
    """把命令参数列表转换成可安全写入日志的命令行文本。"""
    raw_args = [str(item) for item in argv]
    sanitized = []
    index = 0

    while index < len(raw_args):
        argument = raw_args[index]

        # 命中 --password、--pwd、--password-file 等长密码类选项。
        if _LONG_PASSWORD_OPTION_RE.match(argument):
            if "=" in argument:
                # --password=xxx => --password=***，等号后整个值不可回显。
                option_name = argument.split("=", 1)[0]
                sanitized.append(f"{option_name}={REDACTED}")
            else:
                # 先保留选项名；下一个独立参数是凭据值时再遮蔽。
                sanitized.append(argument)
                if index + 1 < len(raw_args):
                    sanitized.append(REDACTED)
                    index += 1

        # mysqldump/mysql 的紧凑短选项：-pSecret => -p***。
        elif argument.startswith("-p") and len(argument) > 2:
            sanitized.append(f"-p{REDACTED}")

        # 普通参数先原样保留，最后由 redact 对 URL 凭据和显式 secrets 兜底。
        else:
            sanitized.append(argument)

        index += 1

    # shlex.join 生成带必要引号的命令行文本，再执行最终脱敏。
    return redact(shlex.join(sanitized), secrets or [])