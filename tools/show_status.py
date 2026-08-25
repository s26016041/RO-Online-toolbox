r"""列出所有執行中 RO 角色的即時狀態。

    .venv\Scripts\python.exe tools\show_status.py
    .venv\Scripts\python.exe tools\show_status.py --watch

位址一律用 AOB 特徵定位（見 CLAUDE.md），不記絕對位址。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ro_toolbox.services import window_list  # noqa: E402
from ro_toolbox.services.character import CharacterReader  # noqa: E402
from ro_toolbox.services.process_monitor import is_admin  # noqa: E402

PROCESS = "ragexe.exe"


def bar(percent: float, width: int = 20) -> str:
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def main() -> int:
    parser = argparse.ArgumentParser(description="顯示 RO 角色狀態")
    parser.add_argument("--watch", action="store_true", help="持續刷新（Ctrl+C 結束）")
    parser.add_argument("--interval", type=float, default=1.0, help="刷新秒數")
    args = parser.parse_args()

    if not is_admin():
        print("提醒：不是系統管理員，可能讀不到遊戲記憶體。", file=sys.stderr)

    targets = [
        w for w in window_list.enumerate_windows()
        if w.process_name.lower() == PROCESS
    ]
    if not targets:
        print(f"找不到執行中的 {PROCESS}")
        return 1

    readers: list[tuple[int, CharacterReader]] = []
    for win in targets:
        reader = CharacterReader()
        if reader.attach(win.pid):
            readers.append((win.pid, reader))
        else:
            print(f"PID {win.pid}：AOB 定位失敗（遊戲可能已改版）", file=sys.stderr)

    if not readers:
        return 1

    try:
        while True:
            for pid, reader in readers:
                status = reader.read()
                if status is None:
                    print(f"PID {pid:<6} 讀取失敗")
                    continue
                print(
                    f"{status.name:<10} Base {status.base_level:>3} "
                    f"Job {status.job_level:>3}  "
                    f"HP {bar(status.hp_percent)} {status.hp:>6}/{status.max_hp:<6} "
                    f"SP {bar(status.sp_percent)} {status.sp:>5}/{status.max_sp}"
                )
            if not args.watch:
                break
            time.sleep(args.interval)
            print("-" * 90)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        for _pid, reader in readers:
            reader.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
