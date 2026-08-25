r"""從 RODATA 抽出傳點表（哪張地圖的哪一格會傳到哪裡）。

    .\.venv\Scripts\python.exe tools\build_warp_table.py

來源：`luafiles514/lua files/navigation/navi_link_tw.lub`
（Lua 5.1 bytecode，用 tools/lub_convert.py 轉成 64 位後由真的 Lua 直譯器載入）。

欄位是實測反推的，用 abbey01 與 prt_fild00 交叉驗證：

    [1] 地圖名   [2] 連結編號  [3] 型別(200=傳點)  [4] NPC 編號(99999)
    [5] 連結代號 [6] （空字串）[7] x  [8] y  [9] 目的地圖  [10] 目的 x  [11] 目的 y

    ['prt_fild00', 17738, 200, 99999, '00b-04c', '', 165, 18, 'prt_fild04', 158, 384]

輸出 assets/warps.json.gz：{"prt_fild00": [[165, 18, "prt_fild04", 158, 384], ...]}

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

_WARP_TYPE = 200


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

    warps: dict[str, list] = {}
    skipped = 0
    for row in rows:
        # 欄位不到 11 個或型別不是傳點就跳過 —— 版面變了要看得出來，不要默默算錯
        if len(row) < 11 or row[2] != _WARP_TYPE:
            skipped += 1
            continue
        source, x, y, dest, dx, dy = row[0], row[6], row[7], row[8], row[9], row[10]
        if not isinstance(source, str) or not isinstance(dest, str):
            skipped += 1
            continue
        if not all(isinstance(v, (int, float)) for v in (x, y, dx, dy)):
            skipped += 1
            continue
        warps.setdefault(source, []).append([int(x), int(y), dest, int(dx), int(dy)])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        json.dump(warps, handle, ensure_ascii=False, separators=(",", ":"))

    total = sum(len(v) for v in warps.values())
    print(f"傳點 {total:,} 個，分佈在 {len(warps):,} 張地圖（跳過 {skipped:,} 列非傳點）")
    for name in ("prt_fild00", "prontera", "moc_fild01"):
        if name in warps:
            print(f"  {name}：{len(warps[name])} 個，例 {warps[name][0]}")
    print(f"輸出：{OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
