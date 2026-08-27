r"""從 RODATA 抽出傳點表（哪張地圖的哪一格會傳到哪裡）。

    .\.venv\Scripts\python.exe tools\build_warp_table.py

來源：`luafiles514/lua files/navigation/navi_link_tw.lub`
（Lua 5.1 bytecode，用 tools/lub_convert.py 轉成 64 位後由真的 Lua 直譯器載入）。

欄位是實測反推的，用 abbey01 與 prt_fild00 交叉驗證：

    [1] 地圖名   [2] 連結編號  [3] 型別  [4] NPC 編號
    [5] 連結代號 [6] （空字串）[7] x  [8] y  [9] 目的地圖  [10] 目的 x  [11] 目的 y

    ['prt_fild00', 17738, 200, 99999, '00b-04c', '', 165, 18, 'prt_fild04', 158, 384]

## ⚠ 分兩種：走過去會傳送的，跟要跟 NPC 講話的

判準是 **NPC 編號**，不是型別（實測 2026-08-27，4514 列）：

| 型別 | 筆數 | NPC=99999 | 例子 |
|---|---|---|---|
| 200 | 3630 | 全部 | 一般傳點 |
| 201 | 695 | 0 | 告示牌、分流移動器 |
| 204 | 143 | 0 | 船夫、船員 |
| 205 | 45 | 21 | 飛空艇內部通道（21 條）／其餘要對話 |

**`npc == 99999` 就是走過去自動傳送，其他都要對話。** 用型別當判準會漏掉
型別 205 裡那 21 條真的能走的。

舊版只收型別 200，於是**丟掉 883 條連結**（約 20%）—— 症狀是從島嶼／地城
這種只靠船進出的地圖算不出任何路線，使用者回報「遊戲裡正常，你卻說找不到」。
izlu2dun（拜倫島）就是這樣：只有一條往地城的傳點，回 izlude 要搭船。

輸出 assets/warps.json.gz：

    {"version": 2,
     "walk": {"prt_fild00": [[165, 18, "prt_fild04", 158, 384], ...]},
     "npc":  {"izlu2dun":   [[108, 27, "izlude", 195, 210, "船員"], ...]}}

`walk` 給自動尋路走；`npc` 只用來**講清楚為什麼過不去**（我們不會跟 NPC 對話）。

用途：伺服器只在**登入**和**換地圖**時推整份背包清單，而背包的
「格號 → 道具編號」記憶體裡沒有（[MEM-020]）。有了這張表就能自己走去傳點
把清單換出來，不必等玩家剛好過圖。
"""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lub_convert import convert as lub_to64  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "warps.json.gz"

#: NPC 編號等於這個 = 沒有 NPC，走過去就會傳送。其他都要跟 NPC 講話。
_NO_NPC = 99999
#: 這一版的輸出格式。讀取端拿它分辨新舊。
_VERSION = 2


def _find_navi() -> Path:
    tail = Path("luafiles514") / "lua files" / "navigation" / "navi_link_tw.lub"
    for root in ("data/data", "data", "data0/data"):
        path = ROOT / "RODATA" / root / tail
        if path.is_file():
            return path
    return ROOT / "RODATA" / "data" / tail


def load_links(path: Path) -> list[list]:
    from lupa import lua51

    runtime = lua51.LuaRuntime(unpack_returned_tuples=True, encoding=None)
    with tempfile.NamedTemporaryFile(suffix=".lub", delete=False) as handle:
        handle.write(lub_to64(path.read_bytes()))
        temp = Path(handle.name)
    try:
        runtime.eval("function(s) return assert(loadstring(s))() end")(temp.read_bytes())
    finally:
        temp.unlink(missing_ok=True)

    table = runtime.globals()[b"Navi_Link"]
    rows = []
    for key in sorted(table.keys()):
        row = table[key]
        if row is None:
            continue
        cells = [row[k] for k in sorted(row.keys())]
        rows.append([c.decode("cp950", "replace") if isinstance(c, bytes) else c for c in cells])
    return rows


def main() -> int:
    path = _find_navi()
    if not path.is_file():
        print(f"找不到 {path}", file=sys.stderr)
        return 1
    rows = load_links(path)
    print(f"navi_link 共 {len(rows):,} 列")

    walk: dict[str, list] = {}
    npc: dict[str, list] = {}
    skipped = 0
    for row in rows:
        # 欄位不到 11 個就跳過 —— 版面變了要看得出來，不要默默算錯
        if len(row) < 11:
            skipped += 1
            continue
        source, x, y, dest, dx, dy = row[0], row[6], row[7], row[8], row[9], row[10]
        if not isinstance(source, str) or not isinstance(dest, str):
            skipped += 1
            continue
        if not all(isinstance(v, (int, float)) for v in (x, y, dx, dy)):
            skipped += 1
            continue
        if source == "NULL" or not dest:
            skipped += 1          # 資料尾端有一列 NULL 佔位
            continue
        entry = [int(x), int(y), dest, int(dx), int(dy)]
        if row[3] == _NO_NPC:
            walk.setdefault(source, []).append(entry)
        else:
            # NPC 名字給使用者看（「去找 izlu2dun 的船員」），不是拿來自動化的
            name = row[4] if isinstance(row[4], str) else ""
            npc.setdefault(source, []).append([*entry, name])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        json.dump({"version": _VERSION, "walk": walk, "npc": npc},
                  handle, ensure_ascii=False, separators=(",", ":"))

    walk_n = sum(len(v) for v in walk.values())
    npc_n = sum(len(v) for v in npc.values())
    print(f"走過去就傳送 {walk_n:,} 條（{len(walk):,} 張圖）")
    print(f"要跟 NPC 講話 {npc_n:,} 條（{len(npc):,} 張圖）—— 只用來解釋為什麼過不去")
    print(f"跳過 {skipped:,} 列（欄位不合或佔位列）")
    for name in ("prt_fild00", "prontera", "izlu2dun"):
        if name in walk:
            print(f"  {name} 走的：{len(walk[name])} 條，例 {walk[name][0]}")
        if name in npc:
            print(f"  {name} 要對話：{len(npc[name])} 條，例 {npc[name][0]}")
    print(f"輸出：{OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
