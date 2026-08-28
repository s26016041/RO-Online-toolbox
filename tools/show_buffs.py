r"""列出每個執行中 RO 角色**身上現在有什麼狀態**（buff／debuff）。

    .venv\Scripts\python.exe tools\show_buffs.py
    .venv\Scripts\python.exe tools\show_buffs.py --watch

位址一律用 AOB 特徵定位（見 CLAUDE.md），不記位址。
資料來源說明見 `services/status_effects.py`。
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


def show(pid: int, reader: CharacterReader) -> None:
    status = reader.read()
    name = status.name if status else "（讀不到角色）"
    rows = reader.status_effects()
    if rows is None:
        print(f"  PID {pid:<6} {name}：狀態清單讀不到（定位失敗或內容不可信）")
        return
    if not rows:
        print(f"  PID {pid:<6} {name}：身上沒有任何狀態")
        return
    print(f"  PID {pid:<6} {name}：{len(rows)} 個")
    for row in rows:
        left = "無時限" if row.remaining_ms is None else f"剩 {row.remaining_ms / 1000:6.1f} 秒"
        total = "" if row.total_ms is None else f"／共 {row.total_ms / 1000:.0f} 秒"
        print(f"      [{row.efst:>4}] {row.name:<20} {left}{total}"
              f"   val={row.val1},{row.val2},{row.val3}")


def main() -> int:
    parser = argparse.ArgumentParser(description="顯示 RO 角色身上的狀態")
    parser.add_argument("--watch", action="store_true", help="持續刷新（Ctrl+C 結束）")
    parser.add_argument("--interval", type=float, default=1.0, help="刷新秒數")
    args = parser.parse_args()

    if not is_admin():
        print("提醒：不是系統管理員，可能讀不到遊戲記憶體。", file=sys.stderr)

    pids = sorted({
        w.pid for w in window_list.enumerate_windows()
        if w.process_name.lower() == PROCESS
    })
    if not pids:
        print(f"找不到執行中的 {PROCESS}")
        return 1

    readers: list[tuple[int, CharacterReader]] = []
    for pid in pids:
        reader = CharacterReader()
        if reader.attach(pid):
            readers.append((pid, reader))
        else:
            print(f"  PID {pid}：角色定位失敗（可能還在登入畫面）")
    if not readers:
        return 1

    try:
        while True:
            print(f"--- {time.strftime('%H:%M:%S')}")
            for pid, reader in readers:
                show(pid, reader)
            if not args.watch:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
