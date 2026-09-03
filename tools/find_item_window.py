r"""找出「道具說明視窗現在顯示的是哪個道具」存在記憶體的哪裡。

使用者提的方向（2026-09-04）：「右鍵物品會出現物品介紹小視窗，所以應該可以
看記憶體」—— 對，而且這比抓圖比對好：它是**遊戲自己載進記憶體的資料**
（CLAUDE.md 資料來源優先序第 1 條），不受 DPI、佈景、視窗被蓋住影響。

## 怎麼用（兩步，各跑一次）

    # 1) 在遊戲裡對「紅色藥水」按右鍵，讓說明視窗開著，然後：
    .\.venv\Scripts\python.exe tools\find_item_window.py 501

    # 2) 換成對「弄不壞的東西」按右鍵（說明視窗換一個道具），然後：
    .\.venv\Scripts\python.exe tools\find_item_window.py 909 --filter

第 2 步只留下「第 1 步是 501、現在是 909」的位址 —— 那就是說明視窗記著的
道具編號。還太多就換第三個道具再 `--filter` 一次。

⚠ 這是**調查工具**，不是功能。找到位址之後**不准把位址寫進程式**
（CLAUDE.md 最高原則）：要拿它當錨，反查是哪一段程式碼寫進去的，
做成 `CodeSignature` 收進 `services/signatures.py`。

⚠ 為什麼是 int32 又是 int16：封包裡的 `name_id` 是 2 bytes，但客戶端
內部通常存成 int。兩種都掃，哪一種留得下來就是哪一種。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ro_toolbox.services import window_list  # noqa: E402
from ro_toolbox.services.gamedata import item_name  # noqa: E402
from ro_toolbox.services.memory_scan import (  # noqa: E402
    VALUE_TYPES,
    MemoryScanner,
)
from ro_toolbox.services.process_monitor import is_admin  # noqa: E402

PROCESS = "ragexe.exe"
#: 兩步之間要把第一步的結果放著。放暫存目錄，不進版控。
STATE = Path(tempfile.gettempdir()) / "ro-item-window-scan.json"
#: 候選超過這個數量就只印前幾筆（印幾萬行會把終端機拖垮）。
_SHOW = 40


def pick_pid(want: int | None) -> int | None:
    targets = [
        w for w in window_list.enumerate_windows()
        if w.process_name.lower() == PROCESS
    ]
    if want is not None:
        return want if any(w.pid == want for w in targets) else None
    if len(targets) == 1:
        return targets[0].pid
    if not targets:
        print("找不到執行中的 ragexe.exe。", file=sys.stderr)
        return None
    print("開著好幾個遊戲視窗，用 --pid 指定一個：", file=sys.stderr)
    for w in targets:
        print(f"  --pid {w.pid}  {w.title}", file=sys.stderr)
    return None


def scan(scanner: MemoryScanner, kind: str, value: int) -> list[int]:
    """整份掃一次，回符合的位址。"""
    scanner.first_scan(VALUE_TYPES[kind], "exact", value)
    return [int(addr) for addr, _v in scanner.results(limit=1 << 20)]


def survivors(scanner: MemoryScanner, kind: str, addrs: list[int],
              value: int) -> list[int]:
    """上一輪留下來的位址裡，**現在**等於 `value` 的那些。

    ⚠ 這裡自己重讀，不用 `next_scan()` —— 那要接著上一次的掃描狀態，
    而我們是**兩次獨立執行**（中間使用者去遊戲裡點了東西）。
    """
    vt = VALUE_TYPES[kind]
    out = []
    for addr in addrs:
        got = scanner.read_value(addr, vt)
        if got is not None and int(got) == value:
            out.append(addr)
    return out


def report(scanner: MemoryScanner, kind: str, addrs: list[int]) -> None:
    print(f"  {kind}: 剩 {len(addrs)} 個")
    for addr in addrs[:_SHOW]:
        where = scanner.module_for_address(addr)
        tag = f"  {where[0].name}+0x{where[1]:X}" if where else "  （不在模組裡）"
        print(f"    0x{addr:08X}{tag}")
    if len(addrs) > _SHOW:
        print(f"    …還有 {len(addrs) - _SHOW} 個（沒印出來）")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="找『說明視窗顯示的道具編號』在記憶體哪裡")
    parser.add_argument("item_id", type=int, help="現在說明視窗開著的那個道具編號")
    parser.add_argument("--filter", action="store_true",
                        help="接著上一次的結果篩選（第二步以後都要加）")
    parser.add_argument("--pid", type=int, help="指定遊戲行程")
    args = parser.parse_args()

    if not is_admin():
        print("提醒：不是系統管理員，可能讀不到遊戲記憶體。", file=sys.stderr)
    pid = pick_pid(args.pid)
    if pid is None:
        return 1

    scanner = MemoryScanner()
    scanner.open(pid)
    try:
        name = item_name(args.item_id)
        print(f"PID {pid}：找現在等於 {args.item_id}（{name}）的位址")
        if not args.filter:
            found = {k: scan(scanner, k, args.item_id) for k in ("int32", "int16")}
            print("第一輪：")
            for kind, addrs in found.items():
                print(f"  {kind}: {len(addrs)} 個")
            STATE.write_text(json.dumps({
                "pid": pid, "item_id": args.item_id,
                "hits": {k: v for k, v in found.items()},
            }), encoding="utf-8")
            print(f"\n結果存到 {STATE}")
            print("→ 現在去遊戲裡對**另一個**道具按右鍵，再跑一次："
                  "\n   tools\\find_item_window.py <那個道具編號> --filter")
            return 0

        if not STATE.exists():
            print("沒有上一輪的結果 —— 先跑一次不加 --filter 的。", file=sys.stderr)
            return 1
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if state.get("pid") != pid:
            # ⚠ 位址只在同一個行程裡有意義。換了行程（重開遊戲）就得重來，
            #   拿舊的去讀只會得到一堆看似合法的垃圾。
            print(f"上一輪是 PID {state.get('pid')}，跟現在的 {pid} 不同 —— "
                  "重開過遊戲就要從第一步重來。", file=sys.stderr)
            return 1
        if state.get("item_id") == args.item_id:
            print("跟上一輪同一個道具 —— 這樣篩不掉任何東西，"
                  "請換一個道具再試。", file=sys.stderr)
            return 1

        print(f"接著上一輪（{state['item_id']} {item_name(state['item_id'])}）篩選：")
        left = {}
        for kind, addrs in state["hits"].items():
            left[kind] = survivors(scanner, kind, addrs, args.item_id)
            report(scanner, kind, left[kind])
        state.update(item_id=args.item_id, hits=left)
        STATE.write_text(json.dumps(state), encoding="utf-8")

        total = sum(len(v) for v in left.values())
        if total == 0:
            print("\n⚠ 一個都不剩 —— 說明視窗可能沒把編號原樣存著，"
                  "或存的是別的東西（索引、指標）。")
        elif total > 5:
            print("\n還太多 —— 換第三個道具再 --filter 一次。")
        else:
            print("\n✔ 剩沒幾個了。下一步：**不要把位址寫進程式**，"
                  "反查是哪段程式碼寫進去的，做成 CodeSignature。")
        return 0
    finally:
        scanner.close()


if __name__ == "__main__":
    raise SystemExit(main())
