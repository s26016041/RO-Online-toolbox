r"""從 RODATA 抽出 NPC 表（誰、在哪張圖的哪一格、外觀編號）。

    .\.venv\Scripts\python.exe tools\build_npc_table.py

來源：`luafiles514/lua files/navigation/navi_npc_tw.lub`
（Lua 5.1 bytecode，用 tools/lub_convert.py 轉成 64 位後由真的 Lua 直譯器載入）。

欄位是實測反推的（9,662 列，全部 8 欄）：

    [1] 地圖名  [2] 編號  [3] 型別  [4] 外觀編號  [5] 名字  [6] 代號  [7] x  [8] y
    ['payon_in01', 12345, 101, 86, '道具商人', '', 12, 132]

## ⚠ 型別欄**不是**商店旗標

實測只有三個值：101（9,423）、102（238）、0（1）。**沒有「這個是商店」的欄位**，
所以「誰是道具商人」只能靠**名字**。名字是遊戲自己的顯示名（不是我們推的），
但這是唯一線索 —— 名字換了就抓不到，抓不到就**不買**（安全退化）。

## ⚠ 客戶端**沒有**商店賣什麼、賣多少錢

RO 的商店內容在伺服器上，開店那一刻才用 `0x00C6` 送過來（GAMEDATA [PKT-074]）。
所以這張表只回答「人在哪」，「賣什麼」一定要走過去開店才知道。

輸出 assets/npcs.json.gz：

    {"version": 1,
     "npc": {"payon_in01": [[12, 132, "道具商人", 86], ...]}}
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
OUT = ROOT / "assets" / "npcs.json.gz"
_VERSION = 1


def _find_navi() -> Path:
    tail = Path("luafiles514") / "lua files" / "navigation" / "navi_npc_tw.lub"
    for root in ("data/data", "data", "data0/data"):
        path = ROOT / "RODATA" / root / tail
        if path.is_file():
            return path
    return ROOT / "RODATA" / "data" / tail


def load_npcs(path: Path) -> list[list]:
    from lupa import lua51

    runtime = lua51.LuaRuntime(unpack_returned_tuples=True, encoding=None)
    with tempfile.NamedTemporaryFile(suffix=".lub", delete=False) as handle:
        handle.write(lub_to64(path.read_bytes()))
        temp = Path(handle.name)
    try:
        runtime.eval("function(s) return assert(loadstring(s))() end")(temp.read_bytes())
    finally:
        temp.unlink(missing_ok=True)

    table = runtime.globals()[b"Navi_Npc"]
    rows = []
    for key in sorted(table.keys()):
        row = table[key]
        if row is None:
            continue
        cells = [row[k] for k in sorted(row.keys())]
        rows.append(
            [c.decode("cp950", "replace") if isinstance(c, bytes) else c for c in cells]
        )
    return rows


def main() -> int:
    path = _find_navi()
    if not path.is_file():
        print(f"找不到 {path}", file=sys.stderr)
        return 1
    rows = load_npcs(path)
    print(f"navi_npc 共 {len(rows):,} 列")

    npc: dict[str, list] = {}
    skipped = 0
    for row in rows:
        if len(row) < 8:
            skipped += 1
            continue
        where, look, name, x, y = row[0], row[3], row[4], row[6], row[7]
        if not isinstance(where, str) or not isinstance(name, str) or not name:
            skipped += 1
            continue
        if not all(isinstance(v, (int, float)) for v in (x, y, look)):
            skipped += 1
            continue
        if where == "NULL":
            skipped += 1
            continue
        npc.setdefault(where, []).append([int(x), int(y), name, int(look)])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        json.dump({"version": _VERSION, "npc": npc}, handle,
                  ensure_ascii=False, separators=(",", ":"))

    total = sum(len(v) for v in npc.values())
    print(f"收下 {total:,} 個 NPC（{len(npc):,} 張圖），跳過 {skipped:,} 列")
    print(f"輸出：{OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
