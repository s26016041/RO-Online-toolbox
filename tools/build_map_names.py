r"""從 RODATA 抽出地圖中文名表。

    .\.venv\Scripts\python.exe tools\build_map_names.py

來源：`luafiles514/lua files/navigation/navi_map_tw.lub`
（Lua 5.1 bytecode，用 tools/lub_convert.py 轉成 64 位後由真的 Lua 直譯器載入）。

欄位（實測，用 06guild_01 與 cmd_fild01 交叉核對）：

    [1] 內部地圖名  [2] 中文名  [3] 型別  [4] 寬  [5] 高
    ['cmd_fild01', '克魔島巴不其卡森林', 5001, 400, 400]

**為什麼用 `navi_map` 而不是 `data/mapnametable.txt`**：這份就是導航視窗自己用的
那張表，一列一張圖、鍵不帶 `.rsw`，還附了地圖尺寸。mapnametable 是給世界地圖／
標題列用的，格式是 `名稱.rsw#中文名#`，還要自己剝副檔名。

⚠ 兩份都有同一個怪：mjolnir 那批的名字是「妙勒妙勒尼山脈南區」，前綴重複。
那是**遊戲自己的資料就長這樣**，不是抽壞了 —— 照抄就是跟客戶端顯示一致，
不要自作聰明去「修正」它（CLAUDE.md：不准手打、不准用推理修資料）。

輸出 assets/mapnames.json.gz：{"prt_fild08": "普隆德拉原野", ...}

用途：自動尋路要把讀到的內部地圖名顯示成人看得懂的名字。
RODATA 不會打包進 exe，所以一定要落地成 assets/。
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
OUT = ROOT / "assets" / "mapnames.json.gz"


def _find_navi() -> Path:
    tail = Path("luafiles514") / "lua files" / "navigation" / "navi_map_tw.lub"
    for root in ("data/data", "data", "data0/data"):
        path = ROOT / "RODATA" / root / tail
        if path.is_file():
            return path
    return ROOT / "RODATA" / "data" / tail


def load_rows(path: Path) -> list[list]:
    from lupa import lua51

    runtime = lua51.LuaRuntime(unpack_returned_tuples=True, encoding=None)
    with tempfile.NamedTemporaryFile(suffix=".lub", delete=False) as handle:
        handle.write(lub_to64(path.read_bytes()))
        temp = Path(handle.name)
    try:
        runtime.eval("function(s) return assert(loadstring(s))() end")(temp.read_bytes())
    finally:
        temp.unlink(missing_ok=True)

    table = runtime.globals()[b"Navi_Map"]
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
    rows = load_rows(path)
    print(f"navi_map 共 {len(rows):,} 列")

    names: dict[str, str] = {}
    skipped = 0
    for row in rows:
        # 版面變了要看得出來，不要默默抽出半份表
        if len(row) < 2 or not isinstance(row[0], str) or not isinstance(row[1], str):
            skipped += 1
            continue
        stem, label = row[0].strip().lower(), row[1].strip()
        if stem and label:
            names[stem] = label
        else:
            skipped += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        json.dump(names, handle, ensure_ascii=False, separators=(",", ":"))

    print(f"地圖中文名 {len(names):,} 筆（跳過 {skipped:,} 列）")
    for name in ("prontera", "prt_fild08", "mjolnir_06", "payon"):
        if name in names:
            print(f"  {name}：{names[name]}")
    print(f"輸出：{OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
