"""应用层入口：python application/main.py backup|restore|cleanup。

直接执行脚本时自动把仓库根目录加入 sys.path，
因此既可以 `python application/main.py ...` 运行，
也可以 `python -m application.main ...` 运行。
"""

# 标准库先导入，之后再加载项目模块。
from __future__ import annotations

import sys
from pathlib import Path

# 仓库根目录 = application 的上一级。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 延迟导入应用 CLI，保证路径修正先生效。
from application.cli import main as cli_main  # noqa: E402


def main() -> int:
    """返回进程退出码。"""
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())