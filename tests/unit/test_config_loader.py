"""步骤 02 配置加载器的单元测试。

测试目标：
- 合法配置能加载为强类型不可变对象；
- 必填项、枚举值和备份范围语义有清晰错误；
- 密码只从指定环境变量读取，且不会进入 repr 或脱敏摘要；
- 同一个 loader 可分别加载多个实例配置。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from infrastructure.config_loader import (
    CompressionType,
    ConfigError,
    LogLevel,
    NotifyType,
    VerifyLevel,
    load_config,
)

    # 统一使用假密码，验证“能解析但不泄露”。
SECRET = "unit-test-password-do-not-log"


    # 全量显式配置：覆盖所有配置区块与非默认值。
BASE_CONFIG = """
[mysql]
host = "db.example.test"
port = 3307
user = "backup_user"
password_env = "TEST_BACKUP_PASSWORD"

[backup]
dest_dir = "/tmp/mysql-backups"
databases = ["all"]
exclude_databases = ["information_schema", "performance_schema", "sys", "mysql"]
mysqldump_path = "/usr/bin/mysqldump"
compress = "zstd"
schema_only = false
extra_args = ["--hex-blob", "--quick"]
retry_times = 2
lock_wait_timeout = 30

[retention]
enabled = true
days = 3
weekly = 1
monthly = 1

[schedule]
time = "02:30"
timezone = "Asia/Shanghai"

[verify]
level = "L2"
shadow_db_prefix = "check_"
sample_tables = ["shop.orders", "crm.users"]

[notify]
enabled = true
on_success = true
on_failure = true
type = "log"

[log]
level = "WARNING"
dir = "/tmp/mysql-backups/logs"
max_bytes = 2048
backup_count = 4
"""


    # 仅保留必填项，用于验证文档化的默认值。
MINIMAL_CONFIG = """
[mysql]
host = "minimal.example.test"
user = "backup_user"
password_env = "TEST_BACKUP_PASSWORD"

[backup]
dest_dir = "/tmp/minimal-backups"
databases = ["all"]

[retention]

[schedule]

[verify]

[notify]

[log]
"""


class LoadConfigTestCase(unittest.TestCase):
    # 测试夹具：每个用例都有独立临时目录和注入的凭据环境。
    def setUp(self) -> None:
        # 每个用例使用独立临时目录，避免互相影响，也不依赖仓库中的实例配置。
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.root = Path(self._temp_dir.name)
        self.env = {"TEST_BACKUP_PASSWORD": SECRET}

    # 工具方法：把 TOML 文本写入临时目录并返回路径。
    def write_config(self, filename: str = "config.toml", content: str = BASE_CONFIG) -> Path:
        path = self.root / filename
        path.write_text(content, encoding="utf-8")
        return path

    # 工具方法：删除以指定前缀开始的必填项，构造缺失字段场景。
    def remove_line(self, marker: str, content: str = BASE_CONFIG) -> str:
        lines = [line for line in content.splitlines() if not line.startswith(marker)]
        if len(lines) == len(content.splitlines()):
            self.fail(f"测试数据中找不到要移除的行：{marker}")
        return "\n".join(lines) + "\n"

    # 验证合法全量配置会被映射到各强类型值对象。
    def test_valid_config_maps_all_sections_to_immutable_objects(self) -> None:
        path = self.write_config()
        config = load_config(path, env=self.env)

        self.assertEqual(path, config.source_path)
        self.assertEqual("db.example.test", config.mysql.host)
        self.assertEqual(3307, config.mysql.port)
        self.assertEqual("backup_user", config.mysql.user)
        self.assertEqual("TEST_BACKUP_PASSWORD", config.mysql.password_env)
        self.assertEqual(SECRET, config.mysql.password)

        self.assertTrue(config.backup.is_all_databases)
        self.assertEqual(Path("/tmp/mysql-backups"), config.backup.dest_dir)
        self.assertEqual(("--hex-blob", "--quick"), config.backup.extra_args)
        self.assertIs(CompressionType.ZSTD, config.backup.compress)
        self.assertFalse(config.backup.schema_only)
        self.assertEqual(2, config.backup.retry_times)
        self.assertEqual(30, config.backup.lock_wait_timeout)

        self.assertTrue(config.retention.enabled)
        self.assertEqual(3, config.retention.days)
        self.assertEqual(1, config.retention.weekly)
        self.assertEqual(1, config.retention.monthly)

        self.assertEqual("02:30", config.schedule.time)
        self.assertEqual("Asia/Shanghai", config.schedule.timezone)

        self.assertIs(VerifyLevel.L2, config.verify.level)
        self.assertEqual("check_", config.verify.shadow_db_prefix)
        self.assertEqual(("shop.orders", "crm.users"), config.verify.sample_tables)

        self.assertTrue(config.notify.enabled)
        self.assertTrue(config.notify.on_success)
        self.assertTrue(config.notify.on_failure)
        self.assertIs(NotifyType.LOG, config.notify.type)

        self.assertIs(LogLevel.WARNING, config.log.level)
        self.assertEqual(Path("/tmp/mysql-backups/logs"), config.log.dir)
        self.assertEqual(2048, config.log.max_bytes)
        self.assertEqual(4, config.log.backup_count)

        # frozen dataclass 不允许写入，确保配置进程内不会被意外修改。
        with self.assertRaises(AttributeError):
            config.mysql.port = 3306  # type: ignore[misc]

    # 验证省略可选键时，loader 按 PRD 约定填充默认值。
    def test_minimal_config_fills_documented_defaults(self) -> None:
        config = load_config(self.write_config("minimal.toml", MINIMAL_CONFIG), env=self.env)

        self.assertEqual(3306, config.mysql.port)
        self.assertEqual(Path("/tmp/minimal-backups"), config.backup.dest_dir)
        self.assertEqual(("information_schema", "performance_schema", "sys", "mysql"), config.backup.exclude_databases)
        self.assertEqual("mysqldump", config.backup.mysqldump_path)
        self.assertIs(CompressionType.GZIP, config.backup.compress)
        self.assertTrue(config.backup.schema_only)
        self.assertEqual((), config.backup.extra_args)
        self.assertEqual(1, config.backup.retry_times)
        self.assertEqual(3600, config.backup.lock_wait_timeout)

        self.assertTrue(config.retention.enabled)
        self.assertEqual(1, config.retention.days)
        self.assertEqual(0, config.retention.weekly)
        self.assertEqual(0, config.retention.monthly)
        self.assertEqual("02:00", config.schedule.time)
        self.assertEqual("Asia/Shanghai", config.schedule.timezone)
        self.assertIs(VerifyLevel.L1, config.verify.level)
        self.assertEqual("restore_check_", config.verify.shadow_db_prefix)
        self.assertEqual((), config.verify.sample_tables)
        self.assertTrue(config.notify.enabled)
        self.assertFalse(config.notify.on_success)
        self.assertTrue(config.notify.on_failure)
        self.assertIs(NotifyType.LOG, config.notify.type)
        self.assertIs(LogLevel.INFO, config.log.level)
        self.assertEqual(Path("logs"), config.log.dir)
        self.assertEqual(10 * 1024 * 1024, config.log.max_bytes)
        self.assertEqual(7, config.log.backup_count)

    # 缺少不同区块中的必填项时，错误都必须定位到完整键名。
    def test_required_fields_report_file_and_key(self) -> None:
        cases = {
            "host =": "mysql.host",
            "user =": "mysql.user",
            "password_env =": "mysql.password_env",
            "dest_dir =": "backup.dest_dir",
            "databases =": "backup.databases",
        }
        # 逐个测试“删除某个必填项后必须报错”的场景；subTest 让每个字段单独展示结果。
        for marker, expected_key in cases.items():
            with self.subTest(field=expected_key):

                # 按当前 marker 删除一行，构造缺少该必填项的 TOML 配置文件。
                path = self.write_config("invalid.toml", self.remove_line(marker))

                # load_config 必须抛出统一配置异常，并把异常对象保存到 ctx 中供后续检查。
                with self.assertRaises(ConfigError) as ctx:
                    load_config(path, env=self.env)

                # 把异常转为文本后，分别验证错误信息能定位到配置文件和具体键名。
                message = str(ctx.exception)

                # 错误信息中必须包含出错配置文件的完整路径。
                self.assertIn(str(path), message)

                # 错误信息中必须包含点分键名（如 mysql.host），避免只给模糊错误。
                self.assertIn(f"[{expected_key}]", message)

    # 白名单外的枚举值必须拒绝，避免后续流程拿到未知状态。
    def test_invalid_enum_values_are_rejected(self) -> None:
        cases = {
            'compress = "zstd"': ('compress = "rar"', "backup.compress"),
            'level = "L2"': ('level = "L9"', "verify.level"),
            'type = "log"': ('type = "sms"', "notify.type"),
            'level = "WARNING"': ('level = "VERBOSE"', "log.level"),
        }
        for marker, (replacement, expected_key) in cases.items():
            with self.subTest(key=expected_key):
                content = BASE_CONFIG.replace(marker, replacement, 1)
                path = self.write_config("invalid_enum.toml", content)
                with self.assertRaises(ConfigError) as ctx:
                    load_config(path, env=self.env)
                self.assertIn(f"[{expected_key}]", str(ctx.exception))

    # TCP 端口必须落在 1..65535；0 和 65536 都非法。
    def test_mysql_port_bounds_are_enforced(self) -> None:
        for port in (0, 65536):
            with self.subTest(port=port):
                content = BASE_CONFIG.replace("port = 3307", f"port = {port}", 1)
                path = self.write_config("bad-port.toml", content)
                with self.assertRaises(ConfigError) as ctx:
                    load_config(path, env=self.env)
                self.assertIn("[mysql.port]", str(ctx.exception))

    # 公开 API 兼容 str 与 Path 两种路径类型。
    def test_accepts_string_path_as_well_as_path_object(self) -> None:
        path = self.write_config()
        from_str = load_config(str(path), env=self.env)
        from_path = load_config(path, env=self.env)
        self.assertEqual(from_path, from_str)

    # 显式库表条目应保留顺序和冒号语法，空白仅用于规范化。
    def test_explicit_database_list_and_table_scope_are_preserved(self) -> None:
        content = BASE_CONFIG.replace(
            'databases = ["all"]',
            'databases = ["shop", "blog: posts , orders"]',
            1,
        )
        config = load_config(self.write_config("list.toml", content), env=self.env)
        self.assertFalse(config.backup.is_all_databases)
        self.assertEqual(("shop", "blog: posts , orders"), config.backup.databases)

    # 空 databases 无法确定备份范围，必须在加载阶段失败。
    def test_empty_database_list_is_rejected(self) -> None:
        content = BASE_CONFIG.replace('databases = ["all"]', "databases = []", 1)
        path = self.write_config("empty-databases.toml", content)
        with self.assertRaises(ConfigError) as ctx:
            load_config(path, env=self.env)
        self.assertIn("数据库列表不能为空", str(ctx.exception))

    # all 是保留模式，不能与具体库名混用造成歧义。
    def test_all_may_not_be_mixed_with_named_entries(self) -> None:
        content = BASE_CONFIG.replace('databases = ["all"]', 'databases = ["all", "shop"]', 1)
        path = self.write_config("mixed-all.toml", content)
        with self.assertRaises(ConfigError) as ctx:
            load_config(path, env=self.env)
        self.assertIn("只能单独出现", str(ctx.exception))

    # 普通库名与 db:tables 条目指向同一库时也视为重复。
    def test_duplicate_database_entries_are_rejected(self) -> None:
        content = BASE_CONFIG.replace(
            'databases = ["all"]',
            'databases = ["shop", "shop:orders"]',
            1,
        )
        path = self.write_config("duplicate.toml", content)
        with self.assertRaises(ConfigError) as ctx:
            load_config(path, env=self.env)
        self.assertIn("重复出现", str(ctx.exception))

    # 同一个数据库既被点名备份又被排除属于语义冲突。
    def test_named_database_may_not_overlap_exclude_list(self) -> None:
        content = BASE_CONFIG.replace('databases = ["all"]', 'databases = ["shop"]', 1)
        content = content.replace(
            'exclude_databases = ["information_schema", "performance_schema", "sys", "mysql"]',
            'exclude_databases = ["shop"]',
            1,
        )
        path = self.write_config("overlap.toml", content)
        with self.assertRaises(ConfigError) as ctx:
            load_config(path, env=self.env)
        self.assertIn("与 backup.databases 重复：shop", str(ctx.exception))

    # L2 抽样表必须是 db.table 形式。
    def test_sample_table_refs_must_use_db_table_form(self) -> None:
        content = BASE_CONFIG.replace(
            'sample_tables = ["shop.orders", "crm.users"]',
            'sample_tables = ["just-table"]',
            1,
        )
        path = self.write_config("bad-table-ref.toml", content)
        with self.assertRaises(ConfigError) as ctx:
            load_config(path, env=self.env)
        self.assertIn("必须是 db.table 形式", str(ctx.exception))

    # 抽样表重复会导致重复比对结果，因此禁止。
    def test_verify_sample_tables_must_be_unique(self) -> None:
        content = BASE_CONFIG.replace(
            'sample_tables = ["shop.orders", "crm.users"]',
            'sample_tables = ["shop.orders", "shop.orders"]',
            1,
        )
        path = self.write_config("duplicate-sample.toml", content)
        with self.assertRaises(ConfigError) as ctx:
            load_config(path, env=self.env)
        self.assertIn("重复出现", str(ctx.exception))

    # 密码要能传给后续流程，但不能被 repr、str 或安全摘要输出。
    def test_secret_is_loaded_but_never_exposed_by_repr_or_safe_summary(self) -> None:
        config = load_config(self.write_config(), env=self.env)

        self.assertEqual(SECRET, config.mysql.password)
        summary = config.safe_summary()
        exposed_text = json.dumps(
            [repr(config), str(config), json.dumps(summary, ensure_ascii=False)],
            ensure_ascii=False,
        )

        # 环境变量名允许出现在摘要中，但密码明文绝不允许。
        self.assertNotIn(SECRET, exposed_text)
        self.assertEqual("TEST_BACKUP_PASSWORD", summary["password_env"])

    # 多实例部署依赖各自文件与环境变量互不污染。
    def test_two_instance_files_remain_independent(self) -> None:
        content_b = BASE_CONFIG.replace("db.example.test", "db-b.example.test", 1)
        content_b = content_b.replace("backup_user", "backup_user_b", 1)
        path_a = self.write_config("instance-a.toml")
        path_b = self.write_config("instance-b.toml", content_b)

        config_a = load_config(path_a, env={"TEST_BACKUP_PASSWORD": "password-a"})
        config_b = load_config(path_b, env={"TEST_BACKUP_PASSWORD": "password-b"})

        self.assertEqual(path_a, config_a.source_path)
        self.assertEqual(path_b, config_b.source_path)
        self.assertEqual("db.example.test", config_a.mysql.host)
        self.assertEqual("db-b.example.test", config_b.mysql.host)
        self.assertEqual("password-a", config_a.mysql.password)
        self.assertEqual("password-b", config_b.mysql.password)


if __name__ == "__main__":
    unittest.main()
