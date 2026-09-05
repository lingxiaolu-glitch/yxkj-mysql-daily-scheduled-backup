"""步骤 02：配置加载与校验（infrastructure/config_loader.py）。

职责
----
- 读取单个 TOML 配置文件 + 环境变量，输出强类型、不可变的 ``AppConfig``。
- 密码不落日志、不进错误信息：
  - ``MysqlConfig.password`` 标记 ``repr=False``，repr/日志不会出现密码值；
  - 任何 ``ConfigError`` 只含键名与原因，不含密码值；
  - ``AppConfig.safe_summary()`` 提供脱敏摘要供日志使用。
- 配置错误统一抛 ``ConfigError``，消息携带「配置文件路径 + 键名（点分路径）」。

约定
----
- 仅使用 Python 3.13 标准库（tomllib / dataclasses / enum）。
- 多实例 = 每实例一份配置文件，本 loader 单次只加载一份。
- 未知键/未知区块忽略（为 v2 预留键保留前向兼容）。
- 枚举值解析大小写不敏感（gzip/GZIP 均可），存储为规范化枚举。

键名唯一性与防模糊
------------------
- ``backup.databases``：库名不允许重复；特殊值 ``all`` 只能单独出现；
- ``backup.exclude_databases``：不允许重复；
- 同一数据库不能同时出现在 databases 与 exclude_databases 中；
- ``verify.sample_tables``：条目必须是 ``db.table`` 形式且不允许重复。
"""

# 类型注解延迟求值：注解以字符串形式保存，dataclass 字段可安全使用 str|Path|None 等新语法，
# 且避免运行期求值开销/自引用类型报错。
from __future__ import annotations

import enum      # 枚举基类：定义合法值白名单（CompressionType/VerifyLevel/NotifyType/LogLevel），非法枚举值直接报 ConfigError
import os        # 读取环境变量：缺省用 os.environ 解析密码（_resolve_password 通过 password_env 指定的变量名取值）
import re        # 正则校验：_IDENTIFIER_RE 校验库名/表名标识符，_TIME_RE 校验 HH:MM 调度时间格式
import tomllib   # Python 3.11+ 标准库 TOML 解析器：tomllib.load() 读取配置文件，并捕获 TOMLDecodeError 转为 ConfigError
from dataclasses import dataclass, field  # @dataclass(frozen=True) 定义不可变强类型配置对象；field(repr=False) 让密码不出现在 repr/日志
from pathlib import Path   # 路径类型：dest_dir/log_dir/source_path 等用 Path 表达，path.open("rb") 读取文件
from typing import Any, Mapping  # 类型标注：TOML 原始字典（dict[str, Any]）与环境变量映射（Mapping[str, str]）的通用类型

# 模块对外公开符号：`from infrastructure.config_loader import *` 只会导出这些名字
__all__ = [
    # 配置对象（强类型、不可变）
    "AppConfig",             # 总配置：聚合以下所有子配置
    "MysqlConfig",           # [mysql]    连接配置（password 已脱敏）
    "BackupConfig",          # [backup]   备份任务配置
    "RetentionConfig",       # [retention] 保留策略
    "ScheduleConfig",        # [schedule] 调度参考时间
    "VerifyConfig",          # [verify]   校验策略（L0/L1/L2）
    "NotifyConfig",          # [notify]   通知配置
    "LogConfig",             # [log]      日志配置
    # 枚举（合法值白名单）
    "CompressionType",       # 压缩方式：gzip | zstd | none
    "VerifyLevel",           # 校验级别：L0 | L1 | L2
    "NotifyType",            # 通知类型：log | smtp | webhook
    "LogLevel",              # 日志级别：DEBUG | INFO | WARNING | ERROR
    # 异常与入口
    "ConfigError",           # 配置错误异常（携带配置文件路径 + 键名）
    "load_config",           # 入口函数：加载配置文件 → AppConfig
]


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

# 自定义异常类：抛出的错误自带「哪个文件 + 哪个配置键 + 原因」，方便快速定位
class ConfigError(Exception):
    """配置错误：附带来源文件路径与出错键名（点分路径，如 mysql.host）。"""

    # __init__ 是"构造方法"：创建 ConfigError 对象时自动调用，用来初始化对象
    # 参数类型注解（不是强制，只是给人和 IDE 看）：
    #   path: str | Path | None  —— 配置来源：字符串、Path 对象，或 None（无来源时）
    #   key: str                 —— 出错键名，点分路径，如 "mysql.host"
    #   message: str             —— 具体错误原因
    # -> None                    —— 返回注解：构造方法不返回值
    def __init__(self, path: str | Path | None, key: str, message: str) -> None:
        # self 就是"当前这个异常对象"，self.xxx = 存成它的属性，之后随时可取用
        # str(path) if path else "<memory>" 是三目运算：
        #   path 有值 → 转成字符串；path 为空/None → 用 "<memory>" 占位
        self.file_path: str = str(path) if path else "<memory>"
        self.key: str = key
        self.message: str = message
        # 调用父类 Exception 的构造方法，把最终报错文案交给内置异常机制
        # f"..." 是格式化字符串，{...} 里写变量会被替换成实际值
        super().__init__(f"{self.file_path}: [{self.key}] {self.message}")


# ---------------------------------------------------------------------------
# 枚举（str 枚举，值即配置文件中书写的字符串）
# ---------------------------------------------------------------------------

class CompressionType(str, enum.Enum):
    GZIP = "gzip"
    ZSTD = "zstd"
    NONE = "none"


class VerifyLevel(str, enum.Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class NotifyType(str, enum.Enum):
    LOG = "log"
    SMTP = "smtp"
    WEBHOOK = "webhook"


class LogLevel(str, enum.Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ---------------------------------------------------------------------------
# 强类型配置对象（frozen：不可变，便于测试与并发复用）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MysqlConfig:
    """MySQL 连接配置。password 由 load_config 从 password_env 指定的环境变量解析。"""

    host: str
    port: int
    user: str
    password_env: str           # 环境变量名（不含密码值）
    password: str = field(repr=False, compare=False)  # 密码值：禁止打印/不参与相等比较


@dataclass(frozen=True)
class BackupConfig:
    """备份任务配置。"""

    dest_dir: Path
    databases: tuple[str, ...]           # ("all",) 或 ("db1", "db1:t1,t2", ...)
    exclude_databases: tuple[str, ...]
    mysqldump_path: str
    mysql_path: str
    compress: CompressionType
    schema_only: bool
    extra_args: tuple[str, ...]
    retry_times: int
    lock_wait_timeout: int
    min_free_bytes: int

    @property
    def is_all_databases(self) -> bool:
        """是否为「全部业务库」模式（databases == ["all"]）。"""
        return self.databases == ("all",)


@dataclass(frozen=True)
class RetentionConfig:
    """保留策略：日/周/月分级保留。"""

    enabled: bool
    days: int
    weekly: int
    monthly: int


@dataclass(frozen=True)
class ScheduleConfig:
    """调度参考时间（供部署脚本生成 cron/systemd/计划任务）。"""

    time: str        # 24 小时制 HH:MM，如 02:00
    timezone: str    # IANA 时区名，如 Asia/Shanghai


@dataclass(frozen=True)
class VerifyConfig:
    """校验策略（L0/L1/L2）。"""

    level: VerifyLevel                      # L0=文件级，L1=结构级，L2=影子库恢复比对
    shadow_db_prefix: str                   # L2 影子库前缀；实际库名为 {prefix}{db}
    sample_tables: tuple[str, ...]          # L2 行数抽样比对表，每项为 db.table；空表示仅比对表数量


@dataclass(frozen=True)
class NotifyConfig:
    """通知策略（v1 默认 log，smtp/webhook 为 v2 预留枚举值）。"""

    enabled: bool
    on_success: bool
    on_failure: bool
    type: NotifyType


@dataclass(frozen=True)
class LogConfig:
    """日志配置。"""

    level: LogLevel                 # 日志级别
    dir: Path                       # 日志目录
    max_bytes: int                  # 单个日志文件最大字节数，超过后轮转
    backup_count: int               # 保留的历史日志文件数量


@dataclass(frozen=True)
class AppConfig:
    """总配置：一个实例一份（source_path 记录来源文件路径）。"""

    source_path: Path
    mysql: MysqlConfig
    backup: BackupConfig
    retention: RetentionConfig
    schedule: ScheduleConfig
    verify: VerifyConfig
    notify: NotifyConfig
    log: LogConfig

    def safe_summary(self) -> dict[str, Any]:
        """供日志使用的脱敏摘要：不含密码值与任何敏感信息。"""
        return {
            "source_path": str(self.source_path),
            "mysql_host": self.mysql.host,
            "mysql_port": self.mysql.port,
            "mysql_user": self.mysql.user,
            "password_env": self.mysql.password_env,
            "dest_dir": str(self.backup.dest_dir),
            "databases": list(self.backup.databases),
            "exclude_databases": list(self.backup.exclude_databases),
            "compress": self.backup.compress.value,
            "schema_only": self.backup.schema_only,
            "mysql_path": self.backup.mysql_path,
            "min_free_bytes": self.backup.min_free_bytes,
            "retention": {
                "enabled": self.retention.enabled,
                "days": self.retention.days,
                "weekly": self.retention.weekly,
                "monthly": self.retention.monthly,
            },
            "schedule_time": self.schedule.time,
            "schedule_timezone": self.schedule.timezone,
            "verify_level": self.verify.level.value,
            "notify_type": self.notify.type.value,
            "log_level": self.log.level.value,
        }


# ---------------------------------------------------------------------------
# 校验常量
# ---------------------------------------------------------------------------

# 校验 MySQL 标识符（库名/表名）：仅允许字母、数字、下划线和 $
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$]+$")

# 校验调度时间格式：24 小时制 HH:MM，例如 02:00、23:59
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# MySQL 系统库列表：备份「全部业务库」时默认排除这些库
_SYSTEM_DATABASES = ("information_schema", "performance_schema", "sys", "mysql")


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> AppConfig:
    """加载并校验单个配置文件。

    参数
    ----
    path: TOML 配置文件路径。
    env: 环境变量映射，缺省使用 os.environ；密码由 mysql.password_env
         指定的键从该映射读取。

    返回
    ----
    AppConfig 强类型不可变配置对象。

    异常
    ----
    ConfigError：文件不存在 / TOML 语法错误 / 缺必填项 / 类型、枚举、唯一性冲突。
    """
    source_path = Path(path)
    raw = _read_toml(source_path)
    env_map = env if env is not None else os.environ
    return _build_config(source_path, raw, env_map)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

def _read_toml(path: Path) -> dict[str, Any]:
    """读取并解析 TOML 配置文件。

    文件不存在或 TOML 语法非法时，转换为统一的 ``ConfigError``。
    """
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(path, "<file>", "配置文件不存在") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, "<file>", f"TOML 语法错误：{exc}") from exc


def _build_config(path: Path, raw: Mapping[str, Any], env: Mapping[str, str]) -> AppConfig:
    """逐区块校验原始 TOML 数据，并组装为强类型不可变配置对象。

    参数
    ----
    path: 配置文件路径，主要用于错误信息定位。
    raw: 已解析的 TOML 原始数据。
    env: 环境变量映射，用于读取并解析 MySQL 密码。

    返回
    ----
    全部字段完成默认值填充、类型转换和语义校验后的 AppConfig。

    异常
    ----
    ConfigError：缺少区块/必填项、类型非法、枚举非法或业务约束冲突。
    """
    # 取出必填的 [mysql] 区块；后续连接字段都从这里读取。
    mysql_raw = _section(raw, path, "mysql")
    # MySQL 主机名或 IP 必填且不能是空白字符串。
    host = _nonempty(_required(mysql_raw, path, "mysql", "host", str), path, "mysql.host")
    # MySQL 端口可选，未配置时使用默认端口 3306。
    port = _optional(mysql_raw, path, "mysql", "port", int, 3306)
    # 校验端口必须落在合法 TCP 端口范围内。
    if not 1 <= port <= 65535:
        raise ConfigError(path, "mysql.port", f"端口超出范围 1-65535，实际为 {port}")
    # 登录用户名必填且不能为空。
    user = _nonempty(_required(mysql_raw, path, "mysql", "user", str), path, "mysql.user")
    # 配置文件只保存“密码所在环境变量的名字”。
    password_env_name = _nonempty(
        _required(mysql_raw, path, "mysql", "password_env", str), path, "mysql.password_env"
    )
    # 从真实系统环境变量或测试注入的 env 中解析密码。
    password = _resolve_password(env, path, password_env_name)

    # 取出必填的 [backup] 区块。
    backup_raw = _section(raw, path, "backup")
    # 解析备份输出根目录；目录字符串必填且非空。
    dest_dir = Path(
        _nonempty(_required(backup_raw, path, "backup", "dest_dir", str), path, "backup.dest_dir")
    )
    # 解析备份范围：all、数据库列表或 db:table 组合。
    databases = _parse_databases(backup_raw, path, "backup", "databases")
    # 排除库名列表；未配置时默认排除四个 MySQL 系统库。
    exclude_databases = _string_list(
        backup_raw, path, "backup", "exclude_databases", required=False, default=_SYSTEM_DATABASES
    )
    # 每个排除库名都必须符合安全标识符规则。
    for db_name in exclude_databases:
        _validate_identifier(path, "backup.exclude_databases", db_name)
    # 同一个排除库名不允许重复配置。
    _check_unique(path, "backup.exclude_databases", exclude_databases)
    # 一个库既被备份又被排除属于语义冲突。
    _check_no_overlap(path, databases, exclude_databases)
    # mysqldump 可执行文件名或路径，默认从 PATH 查找。
    mysqldump_path = _nonempty(
        _optional(backup_raw, path, "backup", "mysqldump_path", str, "mysqldump"),
        path,
        "backup.mysqldump_path",
    )
    # mysql CLI 可执行文件名或路径，默认从 PATH 查找。
    mysql_path = _nonempty(
        _optional(backup_raw, path, "backup", "mysql_path", str, "mysql"),
        path,
        "backup.mysql_path",
    )
    # 压缩格式必须命中枚举白名单，字符串转小写后匹配。
    compress = _enum(backup_raw, path, "backup", "compress", CompressionType, str.lower, default=CompressionType.GZIP)
    # 是否只导出表结构；默认开启。
    schema_only = _optional(backup_raw, path, "backup", "schema_only", bool, True)
    # 额外 mysqldump 参数；保留为列表，避免拼接不可控 shell 文本。
    extra_args = _string_list(backup_raw, path, "backup", "extra_args", required=False, default=())
    # 备份失败重试次数，默认为 1 次。
    retry_times = _optional(backup_raw, path, "backup", "retry_times", int, 1)
    # mysqldump 锁等待超时秒数，默认 3600 秒。
    lock_wait_timeout = _optional(backup_raw, path, "backup", "lock_wait_timeout", int, 3600)
    # 磁盘预检最低可用空间，默认 5 GiB（目标盘约 5GB，风险前置）。
    min_free_bytes = _optional(
        backup_raw, path, "backup", "min_free_bytes", int, 5 * 1024 * 1024 * 1024
    )
    # 重试次数不允许为负数。
    _check_non_negative(path, "backup.retry_times", retry_times)
    # 锁等待时间不允许为负数。
    _check_non_negative(path, "backup.lock_wait_timeout", lock_wait_timeout)
    # 磁盘最低空间不允许为负数。
    _check_non_negative(path, "backup.min_free_bytes", min_free_bytes)

    # 取出 [retention] 区块。
    retention_raw = _section(raw, path, "retention")
    # 日/周/月分级保留策略总开关，默认启用。
    retention_enabled = _optional(retention_raw, path, "retention", "enabled", bool, True)
    # 日备份数量；超过数量后清理旧文件。
    retention_days = _optional(retention_raw, path, "retention", "days", int, 1)
    # 周备份数量；0 表示不单独保留周层级。
    retention_weekly = _optional(retention_raw, path, "retention", "weekly", int, 0)
    # 月备份数量；0 表示不单独保留月层级。
    retention_monthly = _optional(retention_raw, path, "retention", "monthly", int, 0)
    # 日保留份数不允许为负数。
    _check_non_negative(path, "retention.days", retention_days)
    # 周保留份数不允许为负数。
    _check_non_negative(path, "retention.weekly", retention_weekly)
    # 月保留份数不允许为负数。
    _check_non_negative(path, "retention.monthly", retention_monthly)

    # 取出 [schedule] 区块，供部署脚本生成计划任务。
    schedule_raw = _section(raw, path, "schedule")
    # 调度时间可选，默认每天 02:00。
    schedule_time = _optional(schedule_raw, path, "schedule", "time", str, "02:00")
    # 强制校验 24 小时制 HH:MM，防止生成非法计划任务。
    if not _TIME_RE.match(schedule_time):
        raise ConfigError(
            path, "schedule.time", f"必须为 24 小时制 HH:MM（如 02:00），实际为「{schedule_time}」"
        )
    # IANA 时区名称不能为空；默认 Asia/Shanghai。
    schedule_timezone = _nonempty(
        _optional(schedule_raw, path, "schedule", "timezone", str, "Asia/Shanghai"),
        path,
        "schedule.timezone",
    )

    # 取出 [verify] 区块并准备完整性校验策略。
    verify_raw = _section(raw, path, "verify")
    # 校验级别只允许 L0/L1/L2；默认执行 L1 结构校验。
    verify_level = _enum(verify_raw, path, "verify", "level", VerifyLevel, str.upper, default=VerifyLevel.L1)
    # L2 影子库名会生成为 {影子库前缀}{源数据库名}。
    shadow_db_prefix = _nonempty(
        _optional(verify_raw, path, "verify", "shadow_db_prefix", str, "restore_check_"),
        path,
        "verify.shadow_db_prefix",
    )
    # L2 行数抽样表列表；空表示仅比对表数量。
    sample_tables = _string_list(verify_raw, path, "verify", "sample_tables", required=False, default=())
    # 每个抽样项都必须严格符合 db.table 格式。
    for table_ref in sample_tables:
        _validate_table_ref(path, "verify.sample_tables", table_ref)
    # 抽样表禁止重复，避免重复比对同一张表。
    _check_unique(path, "verify.sample_tables", sample_tables)

    # 取出 [notify] 区块。
    notify_raw = _section(raw, path, "notify")
    # 通知模块总开关，默认启用。
    notify_enabled = _optional(notify_raw, path, "notify", "enabled", bool, True)
    # 成功事件默认不通知，减少日常噪音。
    notify_on_success = _optional(notify_raw, path, "notify", "on_success", bool, False)
    # 失败事件默认必须通知。
    notify_on_failure = _optional(notify_raw, path, "notify", "on_failure", bool, True)
    # 通知方式使用枚举白名单；v1 默认 log。
    notify_type = _enum(notify_raw, path, "notify", "type", NotifyType, str.lower, default=NotifyType.LOG)

    # 取出 [log] 区块。
    log_raw = _section(raw, path, "log")
    # 运行日志级别使用枚举白名单，默认 INFO。
    log_level = _enum(log_raw, path, "log", "level", LogLevel, str.upper, default=LogLevel.INFO)
    # 运行日志目录，未配置时写入 logs 目录。
    log_dir = Path(_nonempty(_optional(log_raw, path, "log", "dir", str, "logs"), path, "log.dir"))
    # 单个日志文件最大字节数，默认 10 MiB。
    max_bytes = _optional(log_raw, path, "log", "max_bytes", int, 10 * 1024 * 1024)
    # 轮转阈值必须是正整数。
    if max_bytes <= 0:
        raise ConfigError(path, "log.max_bytes", f"必须为正整数，实际为 {max_bytes}")
    # 日志轮转后保留的历史日志数量，默认 7 份。
    backup_count = _optional(log_raw, path, "log", "backup_count", int, 7)
    # 历史日志份数不允许为负数。
    _check_non_negative(path, "log.backup_count", backup_count)

    # 所有区块均通过校验后，组装成 frozen dataclass 总配置。
    return AppConfig(
        source_path=path,
        mysql=MysqlConfig(
            host=host, port=port, user=user, password_env=password_env_name, password=password
        ),
        backup=BackupConfig(
            dest_dir=dest_dir,
            databases=databases,
            exclude_databases=exclude_databases,
            mysqldump_path=mysqldump_path,
            mysql_path=mysql_path,
            compress=compress,
            schema_only=schema_only,
            extra_args=extra_args,
            retry_times=retry_times,
            lock_wait_timeout=lock_wait_timeout,
            min_free_bytes=min_free_bytes,
        ),
        retention=RetentionConfig(
            enabled=retention_enabled,
            days=retention_days,
            weekly=retention_weekly,
            monthly=retention_monthly,
        ),
        schedule=ScheduleConfig(time=schedule_time, timezone=schedule_timezone),
        verify=VerifyConfig(
            level=verify_level,
            shadow_db_prefix=shadow_db_prefix,
            sample_tables=sample_tables,
        ),
        notify=NotifyConfig(
            enabled=notify_enabled,
            on_success=notify_on_success,
            on_failure=notify_on_failure,
            type=notify_type,
        ),
        log=LogConfig(level=log_level, dir=log_dir, max_bytes=max_bytes, backup_count=backup_count),
    )


# ---------------------------------------------------------------------------
# 字段级工具
# ---------------------------------------------------------------------------

def _section(raw: Mapping[str, Any], path: Path, name: str) -> dict[str, Any]:
    # 从已解析的 TOML 顶层映射中，按名称查找配置区块，例如 [mysql]、[backup]。
    value = raw.get(name)

    # 如果这个键完全不存在，说明缺少必需的顶层配置区块。
    if value is None:
        # 抛出统一配置错误；key 使用区块名，便于提示用户补齐 [name]。
        raise ConfigError(path, name, "缺少必需配置区块")

    # tomllib 会把合法的 TOML 表解析成 Python dict；
    # 如果不是 dict，说明该字段虽然存在，但内容和预期结构不符。
    if not isinstance(value, dict):
        # 提示它必须是 [name] 表形式，并附上实际类型方便定位错误。
        raise ConfigError(path, name, f"必须是表（[{name}]）形式，实际为 {type(value).__name__}")

    # 类型和存在性都检查通过后，把这个字典形式的区块返回给调用方。
    return value


def _required(
    section: Mapping[str, Any], path: Path, section_name: str, key: str, expected: type[Any]
) -> Any:
    full_key = f"{section_name}.{key}"
    if key not in section:
        raise ConfigError(path, full_key, "缺少必填项")
    return _typed(section[key], path, full_key, expected)


def _optional(
    section: Mapping[str, Any],
    path: Path,
    section_name: str,
    key: str,
    expected: type[Any],
    default: Any,
) -> Any:
    full_key = f"{section_name}.{key}"
    if key not in section:
        return default
    return _typed(section[key], path, full_key, expected)


def _typed(value: Any, path: Path, key: str, expected: type[Any]) -> Any:
    """校验配置值是否符合期望的基础类型；校验通过时原样返回。"""

    # Python 里 bool 是 int 的子类（True/False 也满足 isinstance(x, int)）。
    # 因此当期望类型是 int 时，需要先显式排除布尔值，避免 enabled = "yes" 或端口 = true 被误当成整数。
    if expected is int and isinstance(value, bool):
        # 用统一异常标明是哪个配置项把布尔值传给了整数参数。
        raise ConfigError(path, key, "类型错误：期望整数，实际为布尔值")

    # 对其他情况执行常规类型检查；expected 必须是可用于 isinstance 的具体类型。
    if not isinstance(value, expected):
        # 错误信息中同时给出期望类型名和实际类型名，方便定位 TOML 配置错误。
        raise ConfigError(
            path, key, f"类型错误：期望 {expected.__name__}，实际为 {type(value).__name__}"
        )

    # 类型检查通过后不做转换、不复制对象，保留 TOML 解析出的原始值。
    return value


def _nonempty(value: str, path: Path, key: str) -> str:
    """校验字符串不能为空或只包含空白字符。"""

    # 去掉首尾空白后再判断；空字符串、只含空格/制表符/换行符都视为无效。
    if not value.strip():
        # 抛出统一配置错误，key 用于精确提示是哪个配置项非法。
        raise ConfigError(path, key, "不能为空字符串")

    # 校验通过后返回原始字符串；这里不会主动修改用户配置中的首尾空白。
    return value


def _string_list(
    section: Mapping[str, Any],
    path: Path,
    section_name: str,
    key: str,
    required: bool,
    default: tuple[Any, ...],
) -> tuple[str, ...]:
    """读取并校验字符串列表；每一项都不能为空，最后返回不可变元组。"""

    # 拼出完整键名（例如 backup.exclude_databases），用于精确报错。
    full_key = f"{section_name}.{key}"

    # 该键未配置时：必填项直接报错，可选项返回调用方提供的默认值。
    if key not in section:
        if required:
            raise ConfigError(path, full_key, "缺少必填项")
        return default

    # 先检查外层值本身必须是 TOML 数组 / Python list；
    # 不做字符串到列表的隐式转换，避免用户误写成单个字符串。
    value = _typed(section[key], path, full_key, list)

    # 收集通过校验后的列表项。
    result: list[str] = []

    # 从 1 开始编号，让错误提示里的“第 N 项”更符合人类阅读习惯。
    for index, item in enumerate(value, start=1):
        # 列表中的每一项都必须本身就是字符串，不自动转换数字/布尔值等类型。
        if not isinstance(item, str):
            raise ConfigError(path, full_key, f"第 {index} 项必须是字符串，实际为 {type(item).__name__}")

        # 空字符串或只有空白字符的项没有业务含义，视为非法。
        if not item.strip():
            raise ConfigError(path, full_key, f"第 {index} 项为空字符串")

        # 去掉每一项首尾空白后保存，便于后续库名、表名和路径校验保持一致。
        result.append(item.strip())

    # 返回 tuple 保证本模块中已解析配置不可被外部意外修改。
    return tuple(result)


def _enum(
    section: Mapping[str, Any],
    path: Path,
    section_name: str,
    key: str,
    enum_cls: type[enum.Enum],
    normalize: Any,
    default: enum.Enum,
) -> enum.Enum:
    full_key = f"{section_name}.{key}"
    if key not in section:
        return default
    value = _typed(section[key], path, full_key, str)
    try:
        return enum_cls(normalize(value))
    except ValueError:
        allowed = "、".join(member.value for member in enum_cls)
        raise ConfigError(path, full_key, f"非法枚举值「{value}」，允许：{allowed}")


# ---------------------------------------------------------------------------
# 语义级校验（唯一性 / 防模糊）
# ---------------------------------------------------------------------------

def _validate_identifier(path: Path, key: str, name: str) -> None:
    if not _IDENTIFIER_RE.match(name):
        raise ConfigError(
            path,
            key,
            f"「{name}」不是合法标识符：仅允许字母、数字、下划线、$，不含空格/连字符/点",
        )


def _validate_table_ref(path: Path, key: str, table_ref: str) -> None:
    db_name, sep, table_name = table_ref.partition(".")
    if not sep or not db_name or not table_name:
        raise ConfigError(path, key, f"「{table_ref}」必须是 db.table 形式")
    _validate_identifier(path, key, db_name)
    _validate_identifier(path, key, table_name)


def _check_unique(path: Path, key: str, names: tuple[str, ...] | list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ConfigError(path, key, f"「{name}」重复出现")
        seen.add(name)


def _check_non_negative(path: Path, key: str, value: int) -> None:
    if value < 0:
        raise ConfigError(path, key, f"不能为负数，实际为 {value}")


def _check_no_overlap(
    path: Path, databases: tuple[str, ...], exclude_databases: tuple[str, ...]
) -> None:
    """校验显式备份列表和排除列表之间不能指向同一个数据库。"""

    # databases = ["all"] 是全量业务库模式；此时 exclude_databases 本来就用于过滤全量集合。
    # 因此这里不算冲突，直接返回，由后续备份逻辑执行排除规则。
    if databases == ("all",):
        return

    # 把每个备份条目还原成数据库维度：
    # "db1"     -> "db1"
    # "db1:t1"  -> "db1"
    # 使用集合便于和排除库做交集聚合比较。
    database_names = {entry.partition(":")[0] for entry in databases}

    # 取“显式要求备份的库名”与“要求排除的库名”的交集。
    # 若交集非空，说明同一个数据库既被点名备份又被排除，配置语义矛盾。
    overlap = database_names & set(exclude_databases)

    # 只要有冲突立即失败；排序后拼接库名，保证错误提示稳定且易于阅读。
    if overlap:
        names = "、".join(sorted(overlap))
        raise ConfigError(path, "backup.exclude_databases", f"与 backup.databases 重复：{names}")


def _parse_databases(
    section: Mapping[str, Any], path: Path, section_name: str, key: str
) -> tuple[str, ...]:
    # 拼出完整配置键名（例如 backup.databases），用于错误信息中准确定位。
    full_key = f"{section_name}.{key}"

    # 先统一按“字符串列表”读取；missing/类型不对时由 _string_list 报错。
    values = _string_list(section, path, section_name, key, required=True, default=())

    # databases 是本次任务的核心配置，完全没给任何条目时无法确定备份范围。
    if not values:
        raise ConfigError(path, full_key, "数据库列表不能为空")

    # 特殊值 ["all"] 表示备份「全部业务库」；系统库稍后由排除列表过滤。
    if values == ("all",):
        return values

    # all 是保留关键字，不能和具体库名/表级条目混在一起。
    if "all" in values:
        raise ConfigError(path, full_key, "「all」只能单独出现，不能与具体库名混用")

    # 从每个条目的冒号前部分提取数据库名并检查唯一性；
    # 这样可以避免 db1、db1:t1、db1:t1,t2 这类条目重复描述同一个库。
    _check_unique(path, full_key, [entry.partition(":")[0] for entry in values])

    # 逐个解析普通数据库条目或“数据库 + 表清单”条目。
    for entry in values:
        # 按“第一个冒号”切分：冒号前是库名，冒号后是逗号分隔的表清单。
        db_name, sep, table_part = entry.partition(":")

        # 无论是否指定表清单，数据库标识符本身都必须合法且不含空白/点等字符。
        _validate_identifier(path, full_key, db_name)

        # sep 非空表示这是 db:table1,table2 形式的表级备份条目。
        if sep:
            # 按逗号拆分表清单，去掉每项首尾空白，并丢弃空白项。
            tables = [item for item in (t.strip() for t in table_part.split(",")) if item]

            # “db:”“db:,,”这类写法等于没有提供任何表名，属于非法条目。
            if not tables:
                raise ConfigError(path, full_key, f"条目「{entry}」的表列表为空")

            # 每一个表名都必须满足安全标识符规则。
            for table_name in tables:
                _validate_identifier(path, full_key, table_name)

    # 全部语义校验通过后，保留原始条目顺序返回，供后续备份流程使用。
    return values


# ---------------------------------------------------------------------------
# 密码解析（只读环境变量，绝不回显）
# ---------------------------------------------------------------------------

def _resolve_password(env: Mapping[str, str], path: Path, env_name: str) -> str:
    if env_name not in env:
        raise ConfigError(path, "mysql.password_env", f"环境变量「{env_name}」未设置")
    value = env[env_name]
    if not isinstance(value, str) or not value:
        raise ConfigError(path, "mysql.password_env", f"环境变量「{env_name}」为空或类型错误")
    return value