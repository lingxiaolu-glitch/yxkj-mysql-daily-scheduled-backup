"""部署脚本静态检查：确保交付物存在且具备基本可执行结构。"""

# 延迟类型注解。
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SH_SCRIPTS = (
    "scripts/install_cron.sh",
    "scripts/install_systemd.sh",
    "scripts/restore.sh",
    "scripts/run_backup.sh",
    "scripts/verify_deployment.sh",
    "scripts/deploy_to_server.sh",
)


class DeploymentScriptsTests(unittest.TestCase):
    """部署交付物检查。"""

    def test_shell_scripts_exist_and_have_shebang(self) -> None:
        """每个 bash 脚本必须存在、非空并以 shebang 开头。"""
        for relative in SH_SCRIPTS:
            with self.subTest(script=relative):
                path = ROOT / relative
                self.assertTrue(path.is_file(), path)
                content = path.read_text(encoding="utf-8")
                self.assertTrue(content.startswith("#!/usr/bin/env bash"), path)
                self.assertGreater(len(content), 100, path)

    def test_windows_task_script_exists(self) -> None:
        """Windows 计划任务安装脚本必须存在。"""
        self.assertTrue((ROOT / "scripts" / "install_task.ps1").is_file())

    def test_production_configs_include_mysql_and_disk_paths(self) -> None:
        """实例配置包含 mysqldump_path、mysql_path、min_free_bytes。"""
        for relative in ("configs/instance-a.toml", "configs/instance-b.toml"):
            with self.subTest(config=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("mysqldump_path", content)
                self.assertIn("mysql_path", content)
                self.assertIn("min_free_bytes", content)


if __name__ == "__main__":
    unittest.main()