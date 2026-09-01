r"""量「自動打怪卡住 45 秒」到底卡在哪一層 —— **只讀記憶體，不碰遊戲**。

## 要回答的問題

`farm_bot` 的保護機制說「45 秒沒進展」，而那 45 秒裡日誌**一行都沒有**
（不是打怪、不是脫離傳點，就是漫遊）。可能性只有三種，三種的修法完全不同：

1. **角色真的站著，客戶端卻說「我正在走」**（`index>=0` 且路徑陣列有值）
   → `Walker` 永遠等不到「停住」，不重送也不判 blocked
   → 修 `walker.update()` 裡那道沒有上限的 `moving()` 防線。
2. **角色真的站著，客戶端也說站著，而移動終點一直在變**
   → 我們一直送、伺服器一直收，人就是不動
   → 送出去的目標本身有問題（或角色被定身）。
3. **角色其實有在動，只是我們讀到的座標凍住**
   → 讀取端的問題（移動元件挑錯／路徑索引解錯），修 `player_position`。

所以這支工具就盯著這幾個欄位取樣，把「座標凍住」的每一段攤開來看。

用法（遊戲照常掛機，不要停）：

    .\.venv\Scripts\python.exe tools\probe_walk_freeze.py --seconds 300

主控台只印每一段凍結的結論，逐筆取樣寫到 `reports/walk_freeze-<時間>.tsv`。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import struct
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ro_toolbox.services import actor  # noqa: E402
from ro_toolbox.services.character import CharacterReader  # noqa: E402
from ro_toolbox.services.memory_scan import MemoryScanner  # noqa: E402
from ro_toolbox.services.window_list import enumerate_windows  # noqa: E402

REPORTS = ROOT / "reports"
_PROCESS = "ragexe.exe"
#: 座標連續這麼久沒變就算一段「凍結」，值得攤開來看。
#: 走路速度約 1 格 0.15 秒，正常走路不可能到 2 秒。
FREEZE_SEC = 2.5
SPAN = actor.PATH_INDEX + 4


class Watch:
    """跟著一個遊戲行程取樣。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.reader = CharacterReader()
        self.ok = self.reader.attach(pid)
        self.scanner = MemoryScanner()
        self.scanner.open(pid)
        self.name = ""
        status = self.reader.read() if self.ok else None
        if status is not None:
            self.name = status.name
        self.freeze_from: float | None = None
        self.samples: list[tuple] = []
        self.freezes: list[dict] = []

    def raw(self) -> dict:
        """直接讀移動元件那一塊（`player_position` 用的同一份欄位）。"""
        addr = self.reader._position._addr  # noqa: SLF001 - 診斷工具
        out = {"addr": addr, "state": None, "index": None, "begin": None,
               "end": None, "dest": None}
        if addr is None:
            return out
        buf = self.scanner.read_region(addr, SPAN)
        if buf is None or len(buf) < SPAN:
            return out
        buf = bytes(buf)
        out["state"] = struct.unpack_from("<I", buf, actor.STATE)[0]
        out["dest"] = struct.unpack_from("<ii", buf, actor.DEST_X)
        out["begin"] = struct.unpack_from("<I", buf, actor.PATH_BEGIN)[0]
        out["end"] = struct.unpack_from("<I", buf, actor.PATH_END)[0]
        out["index"] = struct.unpack_from("<i", buf, actor.PATH_INDEX)[0]
        return out

    def tick(self, now: float) -> str | None:
        pos = self.reader.read_position()
        live = self.reader.position_live
        moving = self.reader.position_moving()
        raw = self.raw()
        row = (now, pos, live, moving, raw["state"], raw["index"],
               raw["begin"], raw["dest"], raw["addr"])
        self.samples.append(row)
        prev = self.samples[-2] if len(self.samples) > 1 else None
        if prev is None:
            return None
        if pos != prev[1] or pos is None:
            done = self._close(now)
            self.freeze_from = None
            return done
        if self.freeze_from is None:
            self.freeze_from = prev[0]
        return None

    def _close(self, now: float) -> str | None:
        """一段凍結結束了：把它的形狀總結成一行。"""
        if self.freeze_from is None:
            return None
        held = now - self.freeze_from
        if held < FREEZE_SEC:
            return None
        rows = [r for r in self.samples if r[0] >= self.freeze_from]
        moving = Counter(r[3] for r in rows)
        dests = {r[7] for r in rows}
        idx = {r[5] for r in rows}
        states = {r[4] for r in rows}
        addrs = {r[8] for r in rows}
        self.freezes.append({"held": held, "pos": rows[0][1], "moving": dict(moving),
                             "dests": len(dests), "index": sorted(idx)[:6],
                             "state": sorted(states)})
        share = moving[True] / max(1, len(rows)) * 100
        return (f"pid {self.pid} {self.name or '?'}：座標凍結 {held:5.1f} 秒 @{rows[0][1]}"
                f"｜客戶端說在走 {share:3.0f}%"
                f"｜移動終點換了 {len(dests) - 1} 次"
                f"｜index {sorted(idx)[:4]}｜state {sorted(states)}"
                f"｜元件 {len(addrs)} 個")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=180.0)
    ap.add_argument("--hz", type=float, default=8.0)
    args = ap.parse_args()

    pids = sorted({w.pid for w in enumerate_windows()
                   if w.process_name.lower() == _PROCESS})
    if not pids:
        sys.exit(f"找不到 {_PROCESS}，請先開遊戲並登入")
    watches = [Watch(pid) for pid in pids]
    for w in watches:
        print(f"pid {w.pid} {w.name or '(還沒讀到名字)'}："
              f"{'已接上' if w.ok else '角色定位失敗'}")
    watches = [w for w in watches if w.ok]
    if not watches:
        sys.exit("沒有一個行程接得上")

    period = 1.0 / args.hz
    end = time.monotonic() + args.seconds
    print(f"取樣 {args.seconds:.0f} 秒（{args.hz:.0f} Hz）——"
          f" 座標凍結超過 {FREEZE_SEC} 秒就印一行\n")
    while time.monotonic() < end:
        now = time.monotonic()
        for w in watches:
            line = w.tick(now)
            if line:
                print(line)
        time.sleep(max(0.0, period - (time.monotonic() - now)))
    for w in watches:
        line = w._close(time.monotonic())  # noqa: SLF001
        if line:
            print(line)

    REPORTS.mkdir(exist_ok=True)
    stamp = _dt.datetime.now(tz=_dt.UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    path = REPORTS / f"walk_freeze-{stamp}.tsv"
    with path.open("w", encoding="utf-8") as fh:
        fh.write("pid\tt\tpos\tlive\tmoving\tstate\tindex\tbegin\tdest\taddr\n")
        for w in watches:
            for r in w.samples:
                fh.write(f"{w.pid}\t" + "\t".join(str(x) for x in r) + "\n")
    print(f"\n逐筆取樣：{path}")
    for w in watches:
        long = [f for f in w.freezes if f["held"] >= 10]
        print(f"pid {w.pid} {w.name}：取樣 {len(w.samples)} 筆、"
              f"凍結 {len(w.freezes)} 段（其中 {len(long)} 段超過 10 秒）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
