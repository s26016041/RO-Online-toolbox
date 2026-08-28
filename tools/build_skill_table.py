r"""從 RODATA 抽出技能表（技能 ID → 英文代號／中文名／最大等級／各級 SP 與射程）。

    .\.venv\Scripts\python.exe tools\build_skill_table.py

資料來源（全部是客戶端自己載的 Lua bytecode，不是外站抄的）：

| 檔案 | 給了什麼 |
|---|---|
| `skillinfoz/skillid.lub`       | `SKID.SM_BASH = 5` —— 英文代號 → 技能 ID |
| `skillinfoz/skillinfolist.lub` | `SKILL_INFO_LIST[SKID.X] = {...}` —— 中文名、MaxLv、SP、射程 |

兩張表的接點是 `SKID.XXX`：skillinfolist 的鍵是 `Index(Global('SKID'), 'SM_BASH')`
這個佔位物件（`lub_parse.simulate` 不會去解全域），所以要拿 skillid 的
「英文代號 → ID」把它翻回數字。**翻不出來的一律丟掉**，不猜、不推編號規律
（CLAUDE.md：ID 不照系列連號）。

輸出 assets/skills.json.gz：

    {"_meta": {...}, "5": {"key": "SM_BASH", "name": "狂擊", "maxlv": 10,
                           "sp": [8,8,8,8,8,15,15,15,15,15], "range": [1,...]}}

`sp`／`range` 是**每一級一個值**（索引 0 = Lv1）。留著的理由：它是「記憶體讀到的
技能結構是不是真的」最後一道交叉驗證 —— 實機量到 KN_TWOHANDQUICKEN Lv7 的
SP 是 38，而這張表的 `sp[6]` 正好是 38（見 GAMEDATA [MEM-050]）。

依專案 CLAUDE.md：資料一律從資源檔自動抽，改版重新解包後重跑這支。
"""

from __future__ import annotations

import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lub_parse import Index, load, simulate, tw  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "skills.json.gz"


def _find_lua() -> Path:
    """找解包出來的 `lua files/skillinfoz`。只有開發機有 RODATA。"""
    for candidate in (
        ROOT / "RODATA/data/data/luafiles514/lua files/skillinfoz",
        ROOT / "RODATA/data/luafiles514/lua files/skillinfoz",
    ):
        if candidate.is_dir():
            return candidate
    raise SystemExit(f"找不到 skillinfoz（需要解包好的 RODATA）：{ROOT / 'RODATA'}")


def _skill_ids(folder: Path) -> dict[str, int]:
    """skillid.lub：`SKID.SM_BASH = 5`。純直線碼，SETTABLE 全是常數對。"""
    _, pairs = simulate(load(str(folder / "skillid.lub")).main)
    return {
        key: int(value)
        for _table, key, value in pairs
        if isinstance(key, str) and isinstance(value, (int, float))
    }


def _numbers(value) -> list[int] | None:
    """把 Lua 的數字陣列轉成 int list；不是純數字陣列就回 None（安全退化）。"""
    if not isinstance(value, list) or not value:
        return None
    out = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            return None
        out.append(int(item))
    return out


def build() -> dict:
    folder = _find_lua()
    skid = _skill_ids(folder)

    _, pairs = simulate(load(str(folder / "skillinfolist.lub")).main)
    # simulate 回的是 (表物件, 鍵, 值) 的原始順序。巢狀表要靠 id() 認回去：
    #   外層 `SKILL_INFO_LIST[SKID.X] = <內層表>` 的鍵是 Index 佔位物件，
    #   內層 `<表>.SkillName = "..."` 的鍵是字串。
    inner: dict[int, dict] = defaultdict(dict)
    outer: list[tuple[Index, object]] = []
    for table, key, value in pairs:
        if isinstance(key, Index):
            outer.append((key, value))
        elif isinstance(key, str):
            inner[id(table)][key] = value

    skills: dict[int, dict] = {}
    unresolved: list[str] = []
    nameless: list[str] = []
    for key, value in outer:
        code = key.key if isinstance(key.key, str) else None
        fields = inner.get(id(value), {})
        name = fields.get("SkillName")
        if not code or not isinstance(name, str):
            # 沒有中文名的列（多半是只設了 _NeedSkillList 的前置關係表）。
            # 分開計數而不是安靜跳過 —— 數字對不上時要看得出來少在哪。
            nameless.append(str(code))
            continue
        skill_id = skid.get(code)
        if skill_id is None:          # skillid.lub 沒有這個代號 → 留空，不猜
            unresolved.append(code)
            continue
        maxlv = fields.get("MaxLv")
        entry = {
            "key": code,
            "name": tw(name),
            "maxlv": int(maxlv) if isinstance(maxlv, (int, float)) else None,
        }
        for field, out_key in (("SpAmount", "sp"), ("AttackRange", "range")):
            numbers = _numbers(fields.get(field))
            if numbers:
                entry[out_key] = numbers
        skills[skill_id] = entry

    meta = {
        "source": "luafiles514/skillinfoz/{skillid,skillinfolist}.lub",
        "counts": {
            "skid": len(skid),
            "info_rows": len(outer),
            "resolved": len(skills),
            "unresolved": len(unresolved),
            "nameless": len(nameless),
        },
    }
    table = {"_meta": meta, **{str(k): v for k, v in sorted(skills.items())}}
    return table, unresolved, nameless


def main() -> None:
    table, unresolved, nameless = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        json.dump(table, handle, ensure_ascii=False)

    counts = table["_meta"]["counts"]
    print(f"skillid {counts['skid']} 筆 / skillinfolist {counts['info_rows']} 列")
    print(f"對上 ID 的 {counts['resolved']} 個技能；"
          f"skillid.lub 查不到代號的 {counts['unresolved']} 個、"
          f"沒有中文名的 {counts['nameless']} 個（都丟掉，不猜）")
    if unresolved:
        print(f"  （查不到代號的前 10 個：{unresolved[:10]}）")
    if nameless:
        print(f"  （沒有中文名的前 10 個：{nameless[:10]}）")
    for skill_id in (1, 5, 28, 60):
        entry = table.get(str(skill_id))
        if entry:
            print(f"  {skill_id:>5}  {entry['key']:<20} {entry['name']}  "
                  f"MaxLv {entry['maxlv']}  SP {entry.get('sp')}")
    size = OUT.stat().st_size
    print(f"\n-> {OUT}（{size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
