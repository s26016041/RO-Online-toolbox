"""本地編譯 + 冒煙測試（不上傳 GitHub，純本機驗證）。

為什麼需要它
------------
`--windowed` 的 exe 出問題**不會有任何訊息**：漏收資料檔就是選單空白、
漏收模組就是開一個怪視窗，你只會覺得「怪怪的」但不知道哪裡怪。
這支工具讓你在本機：

  1. 用 `RO-Online-toolbox.spec` 編出 exe（與 release.py 同一份設定）。
  2. 立刻跑 `exe --selftest`，確認分頁、道具表、怪物表、圖示、樣式都在。

出問題時用 `--debug` 編「帶主控台的除錯版」，直接看 traceback。

用法
----
    .\\.venv\\Scripts\\python.exe build_local.py            # 編正式版 ＋ 冒煙測試
    .\\.venv\\Scripts\\python.exe build_local.py --debug    # 帶主控台，看得到 traceback
    .\\.venv\\Scripts\\python.exe build_local.py --run      # 通過後把 GUI 開起來眼睛確認
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 主控台是 cp950，勾勾叉叉編不進去會拋 UnicodeEncodeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
SPEC = "RO-Online-toolbox.spec"
EXE_NAME = "RO-Online-toolbox"


def sh(cmd: list[str], env: dict | None = None) -> int:
    print("$", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode


def ensure_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401, PLC0415
    except ImportError:
        print("PyInstaller 未安裝，安裝中…")
        if sh([sys.executable, "-m", "pip", "install", "pyinstaller"]) != 0:
            print("✗ 安裝 PyInstaller 失敗。")
            return False
    return True


def build(debug: bool = False) -> Path | None:
    """編 exe。回傳 exe 路徑，失敗回 None。"""
    if not ensure_pyinstaller():
        return None

    for folder in ("build", "dist"):
        shutil.rmtree(ROOT / folder, ignore_errors=True)

    env = dict(os.environ)
    if debug:
        env["ROT_CONSOLE"] = "1"
        name = f"{EXE_NAME}-debug.exe"
        print("\n=== 編譯除錯版（帶主控台，看得到 traceback）===")
    else:
        env.pop("ROT_CONSOLE", None)
        name = f"{EXE_NAME}.exe"
        print("\n=== 編譯正式版（無主控台，GUI）===")

    started = time.monotonic()
    if sh([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", SPEC], env=env) != 0:
        print("✗ 編譯失敗，請看上面 PyInstaller 的訊息。")
        return None

    exe = ROOT / "dist" / name
    if not exe.exists():
        print(f"✗ 找不到編出的 exe：{exe}")
        return None
    print(f"\n✓ 編譯完成：{exe}"
          f"（{exe.stat().st_size / 1048576:.0f} MB，花了 {time.monotonic() - started:.0f} 秒）")
    return exe


def smoke(exe: Path, debug: bool = False) -> bool:
    """跑打包好的 exe 的 --selftest。這是唯一算數的驗收。"""
    print("\n=== 冒煙測試：exe --selftest ===")
    started = time.monotonic()
    result = subprocess.run([str(exe), "--selftest"], cwd=ROOT,
                            capture_output=True, text=True, check=False)
    elapsed = time.monotonic() - started
    print((result.stdout or "").strip() or "(沒有輸出)")
    if result.stderr.strip():
        print(result.stderr.strip())
    if result.returncode != 0:
        print(f"\n✗ 冒煙測試失敗（return code {result.returncode}）。")
        if not debug:
            print("  → 用 `build_local.py --debug` 重編除錯版，直接看 traceback。")
        return False
    print(f"\n✅ 冒煙測試通過（啟動到自檢完成 {elapsed:.1f} 秒）。")
    return True


def main() -> int:
    debug = "--debug" in sys.argv
    exe = build(debug)
    if exe is None:
        return 1
    if not smoke(exe, debug):
        return 1
    if "--run" in sys.argv:
        print("\n=== 啟動 GUI（關掉視窗即結束）===")
        subprocess.run([str(exe)], cwd=ROOT, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
