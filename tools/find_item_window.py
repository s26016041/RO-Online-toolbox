r"""找出「道具說明小視窗現在顯示的是哪個道具」存在記憶體的哪裡。

使用者堅持走這條（2026-09-04）：「不准用圖片辨識」「讀記憶體」「絕對有記憶體」。
方向是對的：那是遊戲自己載進記憶體的資料（CLAUDE.md 資料來源優先序第 1 條），
不受 DPI、佈景、視窗被蓋住影響。

## ⛔ 走過的冤枉路（同一天，全部落空，不要再走一次）

全部都建立在「它存的是**道具編號**」這個假設上 —— 而這個假設**是錯的**：

1. 「值 = 背包裡任一編號」當第一道濾網 → uint16 留 **130 萬**個候選，
   每輪重讀 9 秒，使用者按的第 2、3、4 次全被吃掉。最後剩的兩個是
   **主執行緒堆疊上的垃圾**（值每秒亂跳二十幾次，剛好瞬間等於 2112）。
2. 「先掃 905、再篩 939」→ **0 個**活下來。
3. 關掉視窗看誰消失 → **只有 4 個**，而且全是雜訊。
   （被釋放的記憶體會**留著舊值**，關視窗根本不是好訊號。）
4. 開視窗看誰**新增** → 955 只多 1 個，是浮點頂點資料。
5. 全記憶體快照比對 `int32 / uint16 / float32 / float64`
   「舊 = 955、新 = 757」→ **四種全部 0 個**。
6. 找「指到 955 附近的指標變成指到 757 附近」→ 6 個，全是**音訊 PCM 波形**。
7. 找「指向道具名字串的指標」→ 0 個。
   而且 `昆蟲外殼` 23 處、`鋁原石` 6 處，換道具前後**一個字都沒變** ——
   所以視窗**沒有複製名字**，是從共用的表直接畫。

**結論：它存的不是編號，也不是名字。** 可能是索引、控制代碼、指標鏈…
所以下面這一版**不假設任何表示法**。

## 現在的做法：讓它在 A、B 兩個道具之間來回

    # 1) 視窗顯示道具 A：
    .\.venv\Scripts\python.exe tools\find_item_window.py --reset --phase a
    # 2) 換成道具 B：
    .\.venv\Scripts\python.exe tools\find_item_window.py --phase b
    # 3) 換回 A：      4) 換回 B：      5) 換回 A…（每次都會再篩一輪）
    .\.venv\Scripts\python.exe tools\find_item_window.py --phase a

一個位址要活下來，必須**每一次都回到那一相對應的值**。
不管它存的是編號、索引、指標還是雜湊，都躲不掉；
而「剛好跟著我們來回切」的雜訊活不過三、四輪。

⚠ 這是**調查工具**，不是功能。找到位址之後**不准把位址寫進程式**
（CLAUDE.md 最高原則：位址一律 AOB 特徵掃描）——
要拿它當錨反查是哪一段程式碼寫進去的，做成 `CodeSignature`。
"""
from __future__ import annotations

import argparse
import pickle
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ro_toolbox.services import window_list  # noqa: E402
from ro_toolbox.services.memory_scan import MemoryScanner  # noqa: E402
from ro_toolbox.services.process_monitor import is_admin  # noqa: E402

PROCESS = "ragexe.exe"
STATE = Path(tempfile.gettempdir()) / "ro-item-window.pkl"
#: 印出來的上限（全域規定：大量輸出會把終端機拖垮）。
_SHOW = 40
#: 剩這麼少就算收斂了。
_DONE = 12


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


def read_all(scanner: MemoryScanner) -> dict[int, np.ndarray]:
    """每個可寫區段現在的內容（dword）。"""
    out = {}
    for base, size in scanner.regions(writable_only=True):
        raw = scanner.read_region(base, size)
        if raw is None:
            continue
        n = len(raw) // 4
        if n:
            out[base] = np.frombuffer(raw, dtype="<u4", count=n).copy()
    return out


def read_at(scanner: MemoryScanner, addrs: np.ndarray) -> np.ndarray:
    """讀這些位址現在的 dword。讀不到的填 0xFFFFFFFF（一定篩不過）。"""
    out = np.empty(len(addrs), dtype=np.uint32)
    for i, a in enumerate(addrs):
        raw = scanner.read_region(int(a), 4)
        out[i] = (np.frombuffer(raw, dtype="<u4", count=1)[0]
                  if raw is not None and len(raw) >= 4 else 0xFFFFFFFF)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="找『說明小視窗顯示哪個道具』在記憶體哪裡（A/B 來回篩）")
    parser.add_argument("--phase", choices=("a", "b"), required=True,
                        help="視窗**現在**顯示的是 A 還是 B 那個道具")
    parser.add_argument("--reset", action="store_true", help="從頭開始（整份快照）")
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
        if args.reset:
            if args.phase != "a":
                print("--reset 要從 --phase a 開始。", file=sys.stderr)
                return 1
            snap = read_all(scanner)
            STATE.write_bytes(pickle.dumps({"pid": pid, "stage": "snap", "snap": snap}))
            print(f"整份快照好了：{len(snap)} 個區段，"
                  f"{sum(len(v) for v in snap.values())/1e6:.0f}M dword")
            print("→ 換成**另一個道具 B** 按右鍵，再跑 --phase b")
            return 0

        if not STATE.exists():
            print("還沒開始 —— 先跑 --reset --phase a。", file=sys.stderr)
            return 1
        state = pickle.loads(STATE.read_bytes())
        if state["pid"] != pid:
            # ⚠ 位址只在同一個行程裡有意義；重開過遊戲一定要 --reset 重來。
            print(f"上一輪是 PID {state['pid']}，跟現在的 {pid} 不同 —— 請 --reset 重來。",
                  file=sys.stderr)
            return 1

        if state["stage"] == "snap":
            if args.phase != "b":
                print("這一步要顯示**另一個**道具（--phase b）。", file=sys.stderr)
                return 1
            # 第一次收斂：留下「跟 A 那一刻不一樣」的位址，記住 A 值與 B 值。
            snap = state["snap"]
            addrs, va, vb = [], [], []
            for base, before in snap.items():
                raw = scanner.read_region(base, len(before) * 4)
                if raw is None:
                    continue
                n = len(raw) // 4
                if n != len(before):
                    continue
                now = np.frombuffer(raw, dtype="<u4", count=n)
                idx = np.nonzero(now != before)[0]
                if len(idx):
                    addrs.append(base + idx.astype(np.int64) * 4)
                    va.append(before[idx])
                    vb.append(now[idx])
            state = {
                "pid": pid, "stage": "pair", "round": 1,
                "addr": np.concatenate(addrs),
                "a": np.concatenate(va), "b": np.concatenate(vb),
            }
            print(f"第 1 輪（A→B 有變的）：{len(state['addr'])} 個")
        else:
            # 之後每一輪：值必須**回到**這一相該有的那個。
            want = state[args.phase]
            now = read_at(scanner, state["addr"])
            keep = now == want
            state["round"] += 1
            for key in ("addr", "a", "b"):
                state[key] = state[key][keep]
            print(f"第 {state['round']} 輪（回到 {args.phase.upper()} 的值）："
                  f"{len(state['addr'])} 個")

        STATE.write_bytes(pickle.dumps(state))
        left = len(state["addr"])
        if left == 0:
            print("\n⚠ 一個都不剩 —— 這一輪視窗可能沒真的換人（或換錯道具了）。"
                  "從 --reset --phase a 重來一次。")
        elif left <= _DONE:
            print("\n✔ 收斂了：")
            for a, x, y in zip(state["addr"][:_SHOW], state["a"][:_SHOW],
                               state["b"][:_SHOW], strict=False):
                print(f"    0x{int(a):08X}   A=0x{int(x):08X}  B=0x{int(y):08X}")
            print("下一步：**不要把位址寫進程式** —— 反查是哪段程式碼寫的，"
                  "做成 CodeSignature。")
        else:
            nxt = "a" if args.phase == "b" else "b"
            print(f"→ 換回**{nxt.upper()}** 那個道具按右鍵，再跑 --phase {nxt}")
        return 0
    finally:
        scanner.close()


if __name__ == "__main__":
    raise SystemExit(main())
