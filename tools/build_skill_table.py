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
import re
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


#: 描述文字裡的顏色碼（`^993300主動^000000`）。介面要不要上色自己決定，
#: 但抽欄位之前一定要先拿掉，否則 `系列 : ^993300主動` 比對不到。
_COLOR = re.compile(r"\^[0-9a-fA-F]{6}")

#: 「類型 : 近距離物理」這種欄位。冒號有全形也有半形。
_FIELD = re.compile(r"^([^:：]{1,10})\s*[:：]\s*(.*)$")

#: 把「類型」歸成打怪型還是補助型。**順序有意義**：
#: `SM_MAGNUM`（怒爆）的類型是「近距離物理，Buff」—— 它是攻擊技能，
#: 先比對攻擊字樣才不會被後面的 buff 搶走。
_ACTIVE_WORDS = ("物理", "魔法", "攻擊", "debuff", "陷阱", "安裝", "召喚", "製作", "恢復")
_BUFF_WORDS = ("buff", "輔助")


def _descriptions(folder: Path) -> dict[str, list[str]]:
    """skilldescript.lub：`SKILL_DESCRIPT[SKID.X] = {"第一行", "第二行", …}`。

    **原始行原樣留著**（含 `^RRGGBB` 顏色碼）——介面要照遊戲那樣上色才有得用，
    清乾淨就回不去了。要抽欄位的地方自己先過 `_COLOR`。
    """
    _, pairs = simulate(load(str(folder / "skilldescript.lub")).main)
    out: dict[str, list[str]] = {}
    for _table, key, value in pairs:
        if not isinstance(key, Index) or not isinstance(key.key, str):
            continue
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            out[key.key] = [tw(x) for x in value]
    return out


def _fields(lines: list[str]) -> dict[str, str]:
    """從描述行抽出「系列 / 類型 / 對象」這些欄位。`[Lv 1] : …` 不算欄位。"""
    found: dict[str, str] = {}
    for line in lines:
        plain = _COLOR.sub("", line).strip()
        if plain.startswith("["):
            continue
        matched = _FIELD.match(plain)
        if matched:
            found.setdefault(matched.group(1).strip(), matched.group(2).strip())
    return found


def _kind_hint(fields: dict[str, str]) -> str | None:
    """從描述判斷是打怪型還是補助型。判斷不出來回 None —— 留給 `inf` 決定。

    只吃「類型」欄位：「系列」欄位實測有 **100 多種寫法**（主動／主動/buff／
    BUFF/特殊／海鮮類(輔助)…），拿它分類就是在猜。
    """
    text = (fields.get("類型") or "").lower()
    if not text:
        return None
    if any(word in text for word in _ACTIVE_WORDS):
        return "active"
    if any(word in text for word in _BUFF_WORDS):
        return "buff"
    return None


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

    descriptions = _descriptions(folder)

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

        lines = descriptions.get(code)
        if lines:
            entry["desc"] = lines
            labels = _fields(lines)
            for label, out_key in (("類型", "type"), ("對象", "target"), ("系列", "series")):
                if labels.get(label):
                    entry[out_key] = labels[label]
            hint = _kind_hint(labels)
            if hint:
                entry["kind"] = hint
        skills[skill_id] = entry

    kinds = {"active": 0, "buff": 0, "none": 0}
    for entry in skills.values():
        kinds[entry.get("kind") or "none"] += 1
    meta = {
        "source": "luafiles514/skillinfoz/{skillid,skillinfolist,skilldescript}.lub",
        "counts": {
            "skid": len(skid),
            "info_rows": len(outer),
            "resolved": len(skills),
            "unresolved": len(unresolved),
            "nameless": len(nameless),
            "described": sum(1 for e in skills.values() if e.get("desc")),
            "kind": kinds,
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
    print(f"有描述的 {counts['described']} 個；"
          f"從「類型」分得出來的：打怪型 {counts['kind']['active']}、"
          f"補助型 {counts['kind']['buff']}、判不出來 {counts['kind']['none']}（交給 inf）")
    for skill_id in (5, 7, 8, 60, 142):
        entry = table.get(str(skill_id))
        if entry:
            print(f"  {skill_id:>5}  {entry['key']:<20} {entry['name']:<12} "
                  f"類型={entry.get('type')!r} 對象={entry.get('target')!r} "
                  f"→ {entry.get('kind')}")
    size = OUT.stat().st_size
    print(f"\n-> {OUT}（{size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
