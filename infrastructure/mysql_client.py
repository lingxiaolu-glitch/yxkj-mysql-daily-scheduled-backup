"""mysql CLI 网关：把 mysql 命令行封装成领域 MySqlGateway 端口。

职责（PRD 7.3.7）：
- 枚举业务库（自动排除系统库）；
- 统计源库表数量（L1 结构校验用）；
- 创建/删除影子库、统计表行数（L2 演练用）；
- 恢复导入（全库/单库/仅结构），供恢复流程调用。
"""

# 延迟类型注解。
from __future__ import annotations

# subprocess 执行外部 mysql 命令。
import os
import re
import subprocess

# 领域值对象与端口。
from domain.model.value_objects import DbName, DomainError

# 配置与脱敏工具。
from infrastructure.config_loader import MysqlConfig
from infrastructure.logging_utils import redact


class MySqlCliError(DomainError):
    """mysql CLI 执行失败（非零退出码或无法启动）。"""


class MysqlCliClient:
    """mysql CLI 网关适配器：实现领域 MySqlGateway 端口。"""

    # 系统库：枚举业务库时默认排除。
    _SYSTEM_DATABASES = ("information_schema", "performance_schema", "sys", "mysql")

    def __init__(self, mysql: MysqlConfig, mysql_path: str = "mysql") -> None:
        # 保存连接配置。
        self._mysql = mysql
        # mysql 可执行文件路径（默认 PATH 中的 mysql）。
        self._mysql_path = mysql_path

    def _base_args(self) -> list[str]:
        """组装公共连接参数（批处理、无列名输出）。"""

        # 可执行文件 + 连接信息。
        return [
            self._mysql_path,
            f"--host={self._mysql.host}",
            f"--port={self._mysql.port}",
            f"--user={self._mysql.user}",
            "--batch",              # 制表符分隔，便于解析。
            "--skip-column-names",  # 去掉列头，只留数据行。
        ]

    def _mysql_env(self) -> dict[str, str]:
        """返回带 MYSQL_PWD 的环境变量，避免密码出现在命令行参数。"""
        env = os.environ.copy()
        env["MYSQL_PWD"] = self._mysql.password
        return env

    def _run(self, sql: str) -> str:
        """执行一条 SQL 并返回标准输出文本。"""

        # 同步执行 mysql -e <sql>，捕获输出与错误。
        try:
            proc = subprocess.run(
                [*self._base_args(), "-e", sql],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._mysql_env(),
            )
        except OSError as exc:
            # 二进制不存在/无权限属于致命错误。
            raise MySqlCliError(f"无法启动 mysql CLI：{exc}") from exc

        # 非零退出码：脱敏后抛出领域异常。
        if proc.returncode != 0:
            raise MySqlCliError(redact(proc.stderr.strip(), [self._mysql.password]))

        # 返回标准输出。
        return proc.stdout

    def list_databases(self) -> tuple[DbName, ...]:
        """枚举业务数据库，自动排除系统库。"""

        # 查询全部数据库名。
        out = self._run("SHOW DATABASES")

        # 逐行解析，过滤空行与系统库。
        databases: list[DbName] = []
        for line in out.splitlines():
            name = line.strip()
            if name and name not in self._SYSTEM_DATABASES:
                # DbName 构造时校验标识符合法性。
                databases.append(DbName(name))

        # 返回有序元组。
        return tuple(databases)

    def count_tables(self, database: DbName) -> int:
        """统计源库表数量（information_schema，L1 结构校验用）。"""

        # 库名已由 DbName 校验（安全标识符），可直接内插。
        sql = (
            "SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_schema = '{database}'"
        )
        return self._parse_int(self._run(sql))

    def create_shadow_database(self, source: DbName, shadow: DbName) -> None:
        """创建影子库（L2 演练第一步）。"""

        # 幂等创建；库名用反引号包裹防注入。
        self._run(f"CREATE DATABASE IF NOT EXISTS `{shadow}`")

    def drop_database(self, database: DbName) -> None:
        """删除影子库（L2 演练收尾）。"""

        # 幂等删除。
        self._run(f"DROP DATABASE IF EXISTS `{database}`")

    def count_table_rows(self, database: DbName, table: str) -> int:
        """统计某张表的行数（L2 抽样比对用）。"""

        # 库名/表名反引号包裹，表名来自配置且已校验。
        sql = f"SELECT COUNT(*) FROM `{database}`.`{table}`"
        return self._parse_int(self._run(sql))

    def restore(
        self,
        sql_file: str,
        database: DbName,
        one_database: bool = False,
        rewrite_to_database: DbName | None = None,
    ) -> None:
        """把 SQL 备份文件导入目标库（全库/单库）。

        rewrite_to_database 用于 L2 影子库演练：把 dump 中的
        CREATE DATABASE / USE 改写为目标影子库，避免恢复回源库。
        """

        # 目标库作为位置参数；one_database 时加 --one-database。
        argv = [*self._base_args(), str(database)]
        if one_database:
            argv.insert(-1, "--one-database")

        # 默认直接以文件作为 stdin 导入，避免把大文件读进内存。
        input_bytes = None
        if rewrite_to_database is not None:
            try:
                with open(sql_file, "r", encoding="utf-8", errors="replace") as handle:
                    sql_text = handle.read()
                # 改写 CREATE DATABASE 与 USE，保证恢复到指定影子库。
                target = str(rewrite_to_database)
                sql_text = re.sub(
                    r"(?im)^\s*CREATE\s+DATABASE\s+"
                    r"(?:/\*!\d+\s+IF\s+NOT\s+EXISTS\s*\*/\s+)?"
                    r"`[^`]+`",
                    f"CREATE DATABASE IF NOT EXISTS `{target}`",
                    sql_text,
                    count=1,
                )
                sql_text = re.sub(
                    r"(?im)^\s*USE\s+`[^`]+`",
                    f"USE `{target}`",
                    sql_text,
                    count=1,
                )
                input_bytes = sql_text.encode("utf-8")
            except OSError as exc:
                raise MySqlCliError(f"读取 SQL 文件失败：{exc}") from exc
            try:
                proc = subprocess.run(argv, input=input_bytes, capture_output=True, env=self._mysql_env())
            except OSError as exc:
                raise MySqlCliError(f"无法启动 mysql CLI：{exc}") from exc
        else:
            # 以文件作为 stdin 导入，避免把大文件读进内存。
            try:
                with open(sql_file, "rb") as handle:
                    proc = subprocess.run(argv, stdin=handle, capture_output=True, env=self._mysql_env())
            except OSError as exc:
                raise MySqlCliError(f"无法启动 mysql CLI：{exc}") from exc

        # 非零退出码：脱敏后抛出领域异常。
        if proc.returncode != 0:
            stderr_text = proc.stderr.decode("utf-8", errors="replace")
            raise MySqlCliError(redact(stderr_text.strip(), [self._mysql.password]))

    @staticmethod
    def _parse_int(out: str) -> int:
        """从 mysql 输出中解析单个整数。"""

        # 取第一行并去掉空白。
        return int(out.strip().splitlines()[0])
