r"""即時看 RO 送出了什麼封包，用來對照「動作 → 封包」。

    .venv\Scripts\python.exe tools\watch_packets.py            # 自動選第一個 RO
    .venv\Scripts\python.exe tools\watch_packets.py 27992      # 指定 PID

用法：跑起來後，在遊戲裡做一個動作（走一步、攻擊一次、撿東西…），
看終端機跳出什麼 opcode，然後告訴 AI「走路 = 0xXXXX」。

心跳封包（CZ_REQUEST_TIME 0x0360）會一直送，預設隱藏，用 --all 顯示。
需要系統管理員。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ro_toolbox.core.ro_packet import RoPacket  # noqa: E402
from ro_toolbox.services import (
    npcap_capture,  # noqa: E402
    window_list,  # noqa: E402
)
from ro_toolbox.services.process_monitor import is_admin  # noqa: E402
from ro_toolbox.services.ro_capture import RoPacketCapture, find_server  # noqa: E402

# 已知會一直自動送的封包，對照動作時是雜訊，預設隱藏
NOISE_OPCODES = {0x0360, 0x007D, 0x0187}


def pick_pid(arg: str | None) -> int | None:
    ro = [w for w in window_list.enumerate_windows() if w.process_name.lower() == "ragexe.exe"]
    if arg:
        pid = int(arg)
        return pid if any(w.pid == pid for w in ro) else pid
    if not ro:
        return None
    if len(ro) > 1:
        print("有多個 RO，請指定 PID：")
        for w in ro:
            print(f"   {w.pid}  {w.title}")
        return None
    return ro[0].pid


def main() -> int:
    parser = argparse.ArgumentParser(description="即時看 RO 送出的封包")
    parser.add_argument("pid", nargs="?", help="RO 行程 PID")
    parser.add_argument("--all", action="store_true", help="連心跳等雜訊封包也顯示")
    parser.add_argument("--seconds", type=int, default=0, help="幾秒後自動停止（0=一直跑）")
    args = parser.parse_args()

    if not is_admin():
        print("⚠ 不是系統管理員，raw socket 會失敗。請用管理員身分執行。", file=sys.stderr)

    pid = pick_pid(args.pid)
    if pid is None:
        print("找不到（或沒指定）RO 行程。")
        return 1

    server = find_server(pid)
    print(f"目標 PID {pid}，伺服器 {server}")
    print("在遊戲裡做動作，看下面跳出什麼 opcode。Ctrl+C 結束。")
    print("（心跳等雜訊預設隱藏，加 --all 顯示）\n")
    print(f"{'時間':<13}{'opcode':<9}{'長度':<6}內容")

    seen: dict[int, int] = {}

    def on_packet(pkt: RoPacket) -> None:
        if not args.all and pkt.opcode in NOISE_OPCODES:
            return
        seen[pkt.opcode] = seen.get(pkt.opcode, 0) + 1
        first = "  ★新" if seen[pkt.opcode] == 1 else ""
        print(
            f"{pkt.time_text():<13}{pkt.opcode_hex:<9}"
            f"{pkt.length:<6}{pkt.payload_hex()}{first}"
        )

    def on_error(message: str) -> None:
        print(f"錯誤：{message}", file=sys.stderr)

    if npcap_capture.available()[0]:
        print("（Npcap 可用：雙向擷取，含伺服器推送）")
        capture = npcap_capture.NpcapCapture(pid, on_packet, on_error=on_error)
    else:
        print("（無 Npcap：只抓送出方向。裝 Npcap 後可看伺服器推送）")
        capture = RoPacketCapture(pid, on_packet, on_error=on_error)
    if not capture.start():
        return 1

    try:
        if args.seconds:
            time.sleep(args.seconds)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()

    print(f"\n這次看到的 opcode（共 {len(seen)} 種）：")
    for opcode, count in sorted(seen.items(), key=lambda kv: -kv[1]):
        print(f"   0x{opcode:04X}  {count} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
