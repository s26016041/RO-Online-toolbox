"""RO Online Toolbox 啟動腳本。

    py main.py

若不是用專案 venv 的 Python 執行，會自動改用它重跑一次，
不必自己指定 .venv 的路徑。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = Path(__file__).resolve()
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def _running_in_venv() -> bool:
    try:
        return Path(sys.executable).resolve() == VENV_PYTHON.resolve()
    except OSError:
        return False


def _relaunch_in_venv() -> int:
    """用 venv 的 Python 重跑一次自己。

    直接 py main.py 走的是系統 Python，選用套件（pymem、pywin32…）都不在
    那裡。與其印一行提醒叫使用者改指令，不如直接切過去。
    """
    result = subprocess.run([str(VENV_PYTHON), str(SCRIPT), *sys.argv[1:]])
    return result.returncode


def _run() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from ro_toolbox.app import run
    except ImportError as exc:
        print(f"啟動失敗：{exc}", file=sys.stderr)
        print("請先建立環境：", file=sys.stderr)
        print("    py -3.12 -m venv .venv", file=sys.stderr)
        print(r"    .\.venv\Scripts\python.exe -m pip install -e .[dev,packet,memory]",
              file=sys.stderr)
        return 1
    return run(sys.argv)


def main() -> int:
    if VENV_PYTHON.exists() and not _running_in_venv():
        return _relaunch_in_venv()
    return _run()


if __name__ == "__main__":
    sys.exit(main())
