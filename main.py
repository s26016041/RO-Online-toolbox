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
    # 打包之後 `src/` 不存在（程式碼已經在 exe 裡），插進去是無害的沒用功；
    # 沒打包時它是唯一能找到 ro_toolbox 的方法。
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


def _packaged() -> bool:
    """已經打包成 exe 了嗎？

    ⚠ 兩種打包工具的旗標不一樣，**兩個都要認**：
    PyInstaller 設 `sys.frozen`，Nuitka 設模組層級的 `__compiled__`。
    只認 `sys.frozen` 的話，Nuitka 編出來的 exe 會以為自己是原始碼，
    跑去找 `.venv` 重新啟動自己 —— 那個路徑在使用者的電腦上不存在。
    """
    return bool(getattr(sys, "frozen", False) or globals().get("__compiled__"))


def main() -> int:
    # 打包成 exe 之後沒有 venv 這回事：相依全都在 exe 裡面。
    if _packaged():
        return _run()
    if VENV_PYTHON.exists() and not _running_in_venv():
        return _relaunch_in_venv()
    return _run()


if __name__ == "__main__":
    sys.exit(main())
