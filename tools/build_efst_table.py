r"""抽出「狀態圖示表」（EFST）：編號 → 中文名稱。

    .\.venv\Scripts\python.exe tools\build_efst_table.py

來源（RODATA，只有開發機有；產出 assets/efst.json.gz 隨程式打包）：

- `luafiles514/lua files/stateicon/efstids.lub`
  `EFST_IDs` 的常數表：`EFST_TWOHANDQUICKEN = 2`。這是**編號的權威來源**，
  記憶體裡的狀態清單存的就是這個編號。

- `luafiles514/lua files/stateicon/stateiconinfo.lub`
  `StateIconList[EFST_IDs.EFST_XXX] = { haveTimeLimit=…, descript={ {標題}, … } }`
  —— 中文名稱與「有沒有倒數」都在這裡。只有 740 個編號有，其餘 700 多個是
  客戶端內部用、畫面上不會出現的狀態，抽出來也只有代號。

⚠ 為什麼要抽成 assets：使用者的電腦沒有 `RODATA/`（CLAUDE.md）。
改版後重跑這支腳本即可。
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lub_parse as L  # noqa: E402, N812

ROOT = Path(__file__).resolve().parents[1]
STATEICON = ROOT / "RODATA" / "data" / "data" / "luafiles514" / "lua files" / "stateicon"
OUT = ROOT / "assets" / "efst.json.gz"

#: 標題長這樣：`雙手劍攻擊速度增加(Two Hand Quicken)`、`凶砍最大值 (Overthrust Max)`。
#: 拆成中文與英文兩半，介面顯示中文那半就夠（英文留著給查表／回報用）。
_TITLE = re.compile(r"^(.*?)\s*[（(]\s*([^（()）]*?)\s*[）)]\s*$")


def _ids() -> dict[str, int]:
    """`EFST_XXX` → 編號。

    ⚠ 檔尾有一段 `CLOSURE`（一個函式定義，不是資料），直線碼 VM 不支援也
    **不該**支援 —— 這裡在第一個 CLOSURE 切斷，只跑前面那串常數賦值。
    切斷點是算出來的，不是寫死的行號：改版多幾個編號也不會錯位。
    """
    lub = L.load(str(STATEICON / "efstids.lub"))
    proto = lub.main
    cut = next(
        (i for i, ins in enumerate(proto.code) if ins.op == "CLOSURE"),
        len(proto.code),
    )
    proto.code = proto.code[:cut]
    out: dict[str, int] = {}
    for _table, key, value in L.simulate(proto)[1]:
        if isinstance(key, str) and isinstance(value, float):
            out[key] = int(value)
    return out


def _info() -> dict[str, dict]:
    """`EFST_XXX` → `{name, en, timed}`。

    `StateIconList` 的鍵是 `EFST_IDs.EFST_XXX`（GETTABLE 產生的 Index 佔位物件），
    所以要先把「哪個 table 物件屬於哪個 EFST」對起來，再收它底下的欄位。
    """
    lub = L.load(str(STATEICON / "stateiconinfo.lub"))
    _globals, pairs = L.simulate(lub.main)

    owner: dict[int, str] = {}
    for _table, key, value in pairs:
        if isinstance(key, L.Index) and getattr(key.table, "name", "") == "EFST_IDs":
            owner[id(value)] = key.key

    fields: dict[str, dict] = {}
    for table, key, value in pairs:
        name = owner.get(id(table))
        if name is not None and isinstance(key, str):
            fields.setdefault(name, {})[key] = value

    out: dict[str, dict] = {}
    for name, entry in fields.items():
        descript = entry.get("descript")
        title = ""
        if isinstance(descript, list) and descript:
            head = descript[0]
            if isinstance(head, list) and head and isinstance(head[0], str):
                title = L.tw(head[0])
        if not title:
            continue
        matched = _TITLE.match(title)
        row = {"name": matched.group(1) if matched else title}
        if matched and matched.group(2):
            row["en"] = matched.group(2)
        if entry.get("haveTimeLimit"):
            row["timed"] = True
        out[name] = row
    return out


def build() -> dict:
    ids = _ids()
    info = _info()
    table: dict[str, dict] = {}
    for key, efst in ids.items():
        # 同一個編號有兩個代號時（別名）以先出現的為準，不要讓後面的把名字蓋掉。
        if str(efst) in table:
            continue
        row = {"key": key, **info.get(key, {})}
        table[str(efst)] = row
    meta = {
        "source": "luafiles514/lua files/stateicon/{efstids,stateiconinfo}.lub",
        "ids": len(ids),
        "named": sum(1 for row in table.values() if row.get("name")),
    }
    return {"_meta": meta, **{k: table[k] for k in sorted(table, key=int)}}


def main() -> None:
    if not STATEICON.is_dir():
        print(f"找不到 {STATEICON}（這支腳本只能在有 RODATA 的開發機上跑）")
        raise SystemExit(1)
    table = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        json.dump(table, handle, ensure_ascii=False)
    meta = table["_meta"]
    print(f"EFST 編號 {meta['ids']} 個，其中 {meta['named']} 個有中文名稱")
    for efst in (2, 10, 12, 23, 28, 673):
        row = table.get(str(efst), {})
        print(f"  {efst:>5}  {row.get('key', '?'):<28} {row.get('name', '（無名稱）')}")
    print(f"-> {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
