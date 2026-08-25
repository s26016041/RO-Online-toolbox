r"""從 RODATA 抽出怪物表（class ID → 名稱／等級／種族／出沒地圖）。

    .\.venv\Scripts\python.exe tools\build_mob_table.py

資料來源（全部是客戶端自己載的 Lua bytecode，不是外站抄的）：

| 檔案 | 給了什麼 |
|---|---|
| `datainfo/npcidentity.lub` | `jobtbl["JT_XXX"] = id` —— 全部 class ID |
| `datainfo/jobname.lub`     | `JobNameTable[jobtbl.JT_XXX] = "資源名"` —— sprite 檔名 |
| `navigation/navi_mob_tw.lub` | 每張地圖的出怪表，台版中文名 + 等級 + 種族 + 體型 + 屬性 + MVP |

navi_mob 每列 8 欄，欄位意義是實測反推並交叉驗證出來的（見 GAMEDATA [DAT-015]）：

    [0] 地圖檔名        'abbey01'
    [1] 導航流水號       24620          （不是 class ID）
    [2] 300=一般 / 301=MVP
    [3] (數量 << 16) | class ID          低 16 bits 2907/2908 命中 jobtbl
    [4] 中文名（cp950）  '食人妖'
    [5] sprite 資源名    'GHOUL'
    [6] 等級            61
    [7] (屬性 << 16) | (體型 << 8) | 種族   屬性 = 20*屬性等級 + 屬性種類

輸出 assets/mobs.json.gz：

    {"_meta": {...}, "1080": {"name": "綠草", "en": "JT_GREEN_PLANT", ...}}

依專案 CLAUDE.md：資料一律從資源檔自動抽，改版重新解包後重跑這支。
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lub_parse import Global, Index, kr, load, simulate, tw  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _find_lua() -> Path:
    """解包工具有的會多包一層 `data/`，兩種版面都吃（跟 build_item_table 同理）。"""
    tail = Path("luafiles514") / "lua files"
    probe = Path("navigation") / "navi_mob_tw.lub"
    candidates = [
        ROOT / "RODATA" / "data" / "data" / tail,
        ROOT / "RODATA" / "data" / tail,
        ROOT / "RODATA" / "data0" / "data" / tail,
    ]
    for path in candidates:
        if (path / probe).is_file():
            return path
    return candidates[0]


LUA = _find_lua()
OUT = ROOT / "assets" / "mobs.json.gz"

RACE = {0: "無形", 1: "不死", 2: "動物", 3: "植物", 4: "昆蟲",
        5: "魚貝", 6: "惡魔", 7: "人形", 8: "天使", 9: "龍族"}
SIZE = {0: "小", 1: "中", 2: "大"}
ELEMENT = {0: "無", 1: "水", 2: "地", 3: "火", 4: "風",
           5: "毒", 6: "聖", 7: "暗", 8: "念", 9: "不死"}

# 「這是草不是怪」的判準。刻意只用客戶端自己的兩個欄位，不靠 ID 連號、
# 不靠背下來的清單：台版名稱結尾是「草」或「菇」，而且等級 = 1。
# 等級條件不能拿掉：魔菇(SPORE, lv18)、變形魔菇(DR_SPORE, lv18) 都會走動會打人。
# 結果集會在腳本結尾整份印出來，對不對一眼看得到。
PLANT_NAME_SUFFIX = ("草", "菇")
PLANT_MAX_LEVEL = 1


def _int(x) -> int:
    return int(x) if isinstance(x, float) else x


def read_jobtbl() -> dict[str, int]:
    """npcidentity.lub → {'JT_PORING': 1002, ...}"""
    proto = load(str(LUA / "datainfo" / "npcidentity.lub")).main
    _, pairs = simulate(proto)
    table: dict[str, int] = {}
    for _, key, value in pairs:
        if isinstance(key, str) and isinstance(value, float) and value.is_integer():
            table[key] = int(value)
        else:
            raise ValueError(f"npcidentity 出現非常數項: {key!r} = {value!r}")
    return table


def read_jobname() -> dict[str, str]:
    """jobname.lub → {'JT_PORING': 'PORING', ...}（值是 euc-kr 資源檔名）"""
    proto = load(str(LUA / "datainfo" / "jobname.lub")).main
    _, pairs = simulate(proto)
    table: dict[str, str] = {}
    for _, key, value in pairs:
        # 原碼是 JobNameTable[jobtbl.JT_XXX] = "資源名"
        if (isinstance(key, Index) and isinstance(key.table, Global)
                and key.table.name == "jobtbl" and isinstance(key.key, str)
                and isinstance(value, str)):
            table[key.key] = kr(value)
        else:
            raise ValueError(f"jobname 出現非預期項: {key!r} = {value!r}")
    return table


def read_navi_mob() -> list[dict]:
    """navi_mob_tw.lub → 每張地圖的出怪列。"""
    proto = load(str(LUA / "navigation" / "navi_mob_tw.lub")).main
    globs, _ = simulate(proto)
    rows = globs.get("Navi_Mob")
    if not rows:
        raise ValueError("navi_mob_tw.lub 沒有 Navi_Mob 全域")
    out = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 8:
            raise ValueError(f"navi_mob 列數不是 8 欄: {row!r}")
        packed_id = _int(row[3])
        mob_id = packed_id & 0xFFFF
        if mob_id == 0:
            continue  # 檔尾有一列 'NULL' 佔位
        flags = _int(row[7])
        out.append({
            "map": row[0],
            "id": mob_id,
            "count": packed_id >> 16,
            "name": tw(row[4]).strip(),
            "res": kr(row[5]),
            "level": _int(row[6]),
            "ele": (flags >> 16) % 20,
            "ele_lv": (flags >> 16) // 20,
            "size": (flags >> 8) & 0xFF,
            "race": flags & 0xFF,
            "boss": _int(row[2]) == 301,
        })
    return out


def build() -> dict:
    jobtbl = read_jobtbl()
    jobname = read_jobname()
    navi = read_navi_mob()

    by_id: dict[int, list[str]] = {}
    for jt, mid in jobtbl.items():
        by_id.setdefault(mid, []).append(jt)

    # 客戶端自己定的怪物 ID 區間；不准手寫 1000/3999 這些數字。
    ranges = [
        [jobtbl["JT_MON_BEGIN"], jobtbl["JT_MONSTER_LAST"]],
        [jobtbl["JT_MONSTER_2ND_BEGIN"], jobtbl["JT_MONSTER_2ND_END"]],
    ]

    mobs: dict[int, dict] = {}
    for row in navi:
        entry = mobs.setdefault(row["id"], {
            "name": row["name"],
            "res": row["res"],
            "level": row["level"],
            "race": row["race"],
            "size": row["size"],
            "ele": row["ele"],
            "ele_lv": row["ele_lv"],
            "boss": row["boss"],
            "maps": {},
        })
        entry["maps"][row["map"]] = row["count"]

    # 判「草」：名稱結尾 + 等級，兩個都來自客戶端資料
    plant_res: set[str] = set()
    for entry in mobs.values():
        is_plant = (entry["name"].endswith(PLANT_NAME_SUFFIX)
                    and entry["level"] <= PLANT_MAX_LEVEL)
        entry["kind"] = "plant" if is_plant else "mob"
        if is_plant:
            plant_res.add(entry["res"].upper())

    # 補上 navi_mob 沒收錄的 ID（活動怪、副本怪、改版新怪）：
    # 只有 ID / JT 名 / sprite 名，沒有等級與種族就不准判 kind。
    for mid, jts in by_id.items():
        if mid in mobs:
            continue
        if not any(lo <= mid <= hi for lo, hi in ranges):
            continue  # NPC / 玩家職業 / 傭兵，不是怪
        res = ""
        for jt in jts:
            res = jobname.get(jt) or res
        # sprite 跟已確認的草同一張圖 → 幾乎確定也是草，但沒有等級可證，
        # 標成 plant? 讓呼叫端自己決定，不要安靜地當成怪。
        kind = "plant?" if res.upper() in plant_res else None
        mobs[mid] = {"name": "", "res": res, "kind": kind, "maps": {}}

    for mid, entry in mobs.items():
        names = by_id.get(mid)
        entry["en"] = sorted(names)[0] if names else ""

    meta = {
        "source": "npcidentity.lub + jobname.lub + navi_mob_tw.lub",
        "monster_id_ranges": ranges,
        "race": RACE, "size": SIZE, "element": ELEMENT,
        "plant_rule": f"台版名稱結尾為 {'/'.join(PLANT_NAME_SUFFIX)} 且 level<={PLANT_MAX_LEVEL}",
        "counts": {
            "jobtbl": len(jobtbl),
            "jobname": len(jobname),
            "navi_rows": len(navi),
            "mobs": len(mobs),
        },
    }
    return {"_meta": meta, **{str(k): v for k, v in sorted(mobs.items())}}


def main() -> None:
    table = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        json.dump(table, handle, ensure_ascii=False)

    meta = table["_meta"]
    mobs = {k: v for k, v in table.items() if k != "_meta"}
    kinds: dict[str, int] = {}
    for entry in mobs.values():
        key = str(entry.get("kind"))
        kinds[key] = kinds.get(key, 0) + 1
    print(f"jobtbl {meta['counts']['jobtbl']} 筆 / jobname {meta['counts']['jobname']} 筆 "
          f"/ navi_mob {meta['counts']['navi_rows']} 列")
    print(f"怪物 ID 區間（客戶端自定）：{meta['monster_id_ranges']}")
    print(f"合併後 {len(mobs)} 隻，分類：{kinds}")
    print(f"\n判為「草」的（規則：{meta['plant_rule']}）：")
    for mid, entry in mobs.items():
        if entry.get("kind") == "plant":
            print(f"  {mid:>6}  {entry['name']:<8} {entry['res']:<16} "
                  f"lv{entry['level']} {SIZE[entry['size']]}{RACE[entry['race']]} "
                  f"出沒 {len(entry['maps'])} 張圖")
    unsure = [(m, e) for m, e in mobs.items() if e.get("kind") == "plant?"]
    print(f"\nsprite 與草相同、但無等級資料可證（kind='plant?'，{len(unsure)} 隻）：")
    for mid, entry in unsure:
        print(f"  {mid:>6}  {entry['en']:<26} {entry['res']}")
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
