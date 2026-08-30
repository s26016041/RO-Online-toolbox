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


#: 送輸入的小 exe。**它的重點就是小**（[INP-023]）。
WORKER_SPEC = "ro-input.spec"
WORKER_NAME = "ro-input.exe"
#: 超過這個大小就是打包設定寫錯了（Qt 或 numpy 被收進來）。
#:
#: ⚠ 這個上限不是美感問題：實測 83 MB 的主 exe 送輸入會被 GameGuard
#: 隨機整批擋掉（PostMessage 5/10、SendInput 4/10 失敗），7 MB 的小 exe
#: 20/20 全過。這顆一旦胖起來，自動登入就會安靜地退回「打 22 次才成功」。
WORKER_MAX_MB = 20.0


def build_input_worker() -> bool:
    """編出送輸入的小 exe，並**把大小釘住**。編不出來或太肥就回 False。"""
    print("\n=== 編譯送輸入的小 exe（ro-input.exe）===")
    if sh([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
           WORKER_SPEC]) != 0:
        print("✗ 小 exe 編譯失敗。")
        return False
    exe = ROOT / "dist" / WORKER_NAME
    if not exe.exists():
        print(f"✗ 找不到小 exe：{exe}")
        return False
    size = exe.stat().st_size / 1048576
    if size > WORKER_MAX_MB:
        print(f"✗ 小 exe 太肥了：{size:.1f} MB（上限 {WORKER_MAX_MB:.0f} MB）"
              " —— 八成是 Qt 或 numpy 被收進來了，看 ro-input.spec 的 excludes。")
        return False
    print(f"✓ 小 exe 完成：{exe}（{size:.1f} MB）")
    return True


def build(debug: bool = False) -> Path | None:
    """編 exe。回傳 exe 路徑，失敗回 None。"""
    if not ensure_pyinstaller():
        return None

    # ⚠ 不用 ignore_errors=True：刪不掉幾乎都是「舊的 exe 還在跑」，
    # 吞掉的話 PyInstaller 會在很後面才用 PermissionError 失敗，訊息完全看不懂。
    # 而且 onefile 的 exe 會生**子行程**，關掉視窗不代表行程沒了。
    for folder in ("build", "dist"):
        target = ROOT / folder
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"✗ 清不掉舊的 {folder}/：{exc}")
            print("  → 多半是上一顆 exe 還在跑。用工作管理員關掉 "
                  f"{EXE_NAME}.exe（可能不只一個行程）再試。")
            return None

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
    # ★ **先編送輸入的小 exe**，主 exe 才收得到它（它是主 exe 的資料檔）。
    if not build_input_worker():
        return None
    if sh([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", SPEC], env=env) != 0:
        print("✗ 編譯失敗，請看上面 PyInstaller 的訊息。")
        return None

    exe = ROOT / "dist" / name
    if not exe.exists():
        print(f"✗ 找不到編出的 exe：{exe}")
        return None
    print(f"\n✓ 編譯完成：{exe}"
          f"（{exe.stat().st_size / 1048576:.0f} MB，花了 {time.monotonic() - started:.0f} 秒）")

    # ⚠ **一定要簽。** 未簽章的 exe 會被 GameGuard 擋掉大量記憶體讀取
    # （錯誤碼 5，見 GAMEDATA [ENV-006]）—— 程式看起來會動，但讀不到角色、
    # 讀不到背包，而那個症狀完全不像「忘了簽」。簽不成就不要交出去。
    sys.path.insert(0, str(ROOT / "tools"))
    from sign_exe import sign  # noqa: PLC0415

    if not sign(exe):
        print("✗ 簽章失敗 —— 這顆 exe 讀不到遊戲，不要用它。")
        return None
    return exe


def smoke(exe: Path, debug: bool = False) -> bool:
    """跑打包好的 exe 的 --selftest。這是唯一算數的驗收。"""
    print("\n=== 冒煙測試：exe --selftest ===")
    started = time.monotonic()
    # encoding 要指定：預設會用 cp950 解，exe 印出中文以外的字元就會炸在
    # 讀取執行緒裡（實際踩過）。errors="replace" 保證量測工具自己不會失敗。
    result = subprocess.run([str(exe), "--selftest"], cwd=ROOT,
                            capture_output=True, text=True, check=False,
                            encoding="utf-8", errors="replace")
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
