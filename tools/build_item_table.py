r"""從 RODATA 抽出合併的物品表。

    .\.venv\Scripts\python.exe tools\build_item_table.py

讀 RODATA/data 下的各張表，合併成 assets/items.json.gz：

    {"501": {"name": "紅色藥水", "desc": "...", "equip": false, ...}, ...}

依專案 CLAUDE.md：資料一律從資源檔**自動抽**，不准手打、不准從編號規律推。
改版重新解包後重跑這支腳本即可。

各表的編碼與格式差異見 RODATA-INDEX.md：
- 顯示名／說明是 cp950，資源檔名是 euc-kr。
- itemparamtable / itemslottable 是「兩行一組」（ID 一行、值一行），
  其餘是單行 `ID#值#`。
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lub_convert import convert as lub_to64  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "items.json.gz"


def _find_data() -> Path:
    """解包工具有的會多包一層 `data/`，兩種版面都吃。找不到就回第一個候選讓上層報錯。"""
    probe = "idnum2itemdisplaynametable.txt"
    candidates = [ROOT / "RODATA" / "data" / "data", ROOT / "RODATA" / "data"]
    for path in candidates:
        if (path / probe).is_file():
            return path
    return candidates[0]


DATA = _find_data()
# 客戶端自己的道具表，比解包的 txt 新（見 GAMEDATA [DAT-020]）。找不到就只用 txt。
GAME_ITEMINFO = Path("D:/ro/RagnarokOnline/System/iteminfo_new.lub")

_COLOR = re.compile(r"\^[0-9a-fA-F]{6}")
_HEAL_WORD = re.compile(r"恢復|回復")
_NUM_AFTER = re.compile(r"(?:恢復|回復)\s*(?:約)?\s*(\d+)")
_NUM_BEFORE = re.compile(r"(\d+)\s*(?:點|點的)?\s*(?:HP|SP)")
#: 出現這些詞代表那一行描述的是**別的東西**（箱子的內容、對別的道具的加成、
#: 強化石之類），不是這個道具自己會補多少。
#: ⚠ 不能放「增加」「提升」這種泛用詞：蜂膠寫「可恢復所有狀態且增加HP和SP」，
#: 那是真的補品，會被誤殺。只留指名道姓在講別的東西的詞。
_NOT_ABOUT_ITSELF = ("箱子", "裝有", "恢復量", "恢復率", "恢復速度",
                     "恢復石", "機率", "卡片")

# 裝備部位 bitmask。由 itemslottable 的值分佈反推並交叉驗證：
# 34 = 32|2（雙手武器佔武器+盾）、513 = 512|1（面具佔中段+下段）、
# 769 = 512|256|1（殭屍帽佔上中下三段）三個組合值都自洽。
EQUIP_SLOTS = {
    1: "下段",
    2: "武器",
    4: "披風",
    8: "飾品1",
    16: "鎧甲",
    32: "盾",
    64: "鞋",
    128: "飾品2",
    256: "上段",
    512: "中段",
}


def _body(path: Path, encoding: str) -> list[str]:
    text = path.read_bytes().decode(encoding, errors="replace")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    ]


def parse_single(path: Path, encoding: str = "cp950") -> dict[int, str]:
    """單行 `ID#值#` 格式。"""
    table: dict[int, str] = {}
    for line in _body(path, encoding):
        parts = line.split("#")
        if len(parts) >= 2 and parts[0].isdigit():
            table[int(parts[0])] = parts[1]
    return table


def parse_double(path: Path, encoding: str = "cp950") -> dict[int, str]:
    """兩行一組：ID 一行、值一行。"""
    lines = _body(path, encoding)
    table: dict[int, str] = {}
    for i in range(0, len(lines) - 1, 2):
        head = lines[i].split("#")[0]
        if head.isdigit():
            table[int(head)] = lines[i + 1].rstrip("#")
    return table


def parse_desc(path: Path) -> dict[int, str]:
    """說明表：一行 ID#，後面接連續多行描述，直到下一個 ID。"""
    table: dict[int, list[str]] = {}
    current: int | None = None
    for line in _body(path, "cp950"):
        head = line.split("#")[0]
        if line.endswith("#") and head.isdigit() and line.count("#") == 1:
            current = int(head)
            table[current] = []
        elif current is not None:
            table[current].append(line.rstrip("#"))
    return {k: "\n".join(v).strip() for k, v in table.items()}


def parse_moveinfo(path: Path) -> dict[int, str]:
    """itemmoveinfov5.txt：Tab 分隔，行尾 `//` 註解常是英文代號。"""
    table: dict[int, str] = {}
    text = path.read_bytes().decode("cp950", errors="replace")
    for line in text.splitlines():
        if not line.strip() or line.startswith("//"):
            continue
        head = line.split("\t")[0].strip()
        if not head.isdigit() or "//" not in line:
            continue
        note = line.split("//", 1)[1].strip()
        # 韓文用 cp950 讀會是亂碼，只留看起來像英文代號的
        if note and all(c.isascii() for c in note):
            table[int(head)] = note
    return table


def _lua_str(raw) -> str:  # noqa: ANN001
    """iteminfo_new.lub 是 UTF-8，舊的 iteminfo.lub 是 cp950（見 [DAT-020]）。"""
    if not isinstance(raw, bytes):
        return str(raw or "")
    for enc in ("utf-8", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp950", errors="replace")


def parse_iteminfo_lub(path: Path) -> dict[int, dict]:
    """讀客戶端的 iteminfo(.lub)：Lua 5.1 bytecode，執行後拿全域表 `tbl`。

    要先把 32 位客戶端寫的 `size_t=4` 改成 64 位主機的 8，否則直譯器拒收。
    出處與欄位清單見 GAMEDATA [DAT-020]。
    """
    from lupa import lua51  # 只有這支腳本需要，執行期不裝也沒差

    runtime = lua51.LuaRuntime(unpack_returned_tuples=True, encoding=None)
    runtime.eval("function(s) return assert(loadstring(s))() end")(lub_to64(path.read_bytes()))
    tbl = runtime.globals()[b"tbl"]

    out: dict[int, dict] = {}
    for key in tbl.keys():
        entry = tbl[key]
        if entry is None:
            continue
        name = _lua_str(entry[b"identifiedDisplayName"])
        if not name:
            continue
        desc_tbl = entry[b"identifiedDescriptionName"]
        lines = []
        if desc_tbl is not None:
            lines = [_lua_str(desc_tbl[i]) for i in sorted(desc_tbl.keys())]
        got: dict = {"name": name, "desc": _COLOR.sub("", "\n".join(lines)).strip()}
        res = _lua_str(entry[b"identifiedResourceName"])
        if res:
            got["res"] = res
        slots = entry[b"slotCount"]
        if slots is not None:
            got["slots"] = int(slots)
        out[int(key)] = got
    return out


def restore_info(desc: str) -> dict:
    """從描述判斷這個道具補 HP 還是 SP，以及補多少（描述有寫才給）。

    ⚠ 描述**不保證有數字**：紅色藥水寫「HP恢復45」，但初學者專用藥水只寫
    「可少量的恢復HP」（[DAT-020] 實測）。所以數量欄可能缺，缺就留空，
    絕不用「同系列大概多少」去推 —— 那是猜的。
    「恢復力」是裝備的加成敘述，不算補品。
    """
    got: dict = {}
    if not desc:
        return got
    for match in _HEAL_WORD.finditer(desc):
        i = match.start()
        if desc[match.end() : match.end() + 1] == "力":
            continue
        # 那一行講的如果是**別的東西**就不算：箱子裡裝什麼、某道具的恢復量加成…
        # 實測抓到的假陽性：「裝有超大包子10個的箱子, 吃掉大包子HP可全部恢復」、
        # 「紅色藥水恢復量提升2015%」、「HP恢復石(披肩)」。
        line = desc[desc.rfind("\n", 0, i) + 1 : (desc.find("\n", i) + 1 or len(desc) + 1) - 1]
        if any(word in line for word in _NOT_ABOUT_ITSELF):
            continue
        window = desc[max(0, i - 12) : i + 14]
        for tag in ("HP", "SP"):
            if tag not in window:
                continue
            got[f"heal_{tag.lower()}"] = True
            amount = _NUM_AFTER.search(window) or _NUM_BEFORE.search(window)
            if amount and ("HP" in window) != ("SP" in window):
                got[f"heal_{tag.lower()}_amount"] = int(amount.group(1))
    if got:
        got["heal_src"] = "desc"
    return got


def slot_names(mask: int) -> list[str]:
    return [label for bit, label in EQUIP_SLOTS.items() if mask & bit]


def main() -> int:
    if not DATA.is_dir():
        print(f"找不到 {DATA}", file=sys.stderr)
        return 1

    names = parse_single(DATA / "idnum2itemdisplaynametable.txt")
    descs = parse_desc(DATA / "idnum2itemdesctable.txt")
    resources = parse_single(DATA / "idnum2itemresnametable.txt", "euc-kr")
    slot_count = parse_single(DATA / "itemslotcounttable.txt")
    slot_mask = parse_double(DATA / "itemslottable.txt")
    english = parse_moveinfo(DATA / "itemmoveinfov5.txt")

    # 客戶端自己的表比解包的 txt 新，名字／描述優先用它（[DAT-020]）。
    client: dict[int, dict] = {}
    if GAME_ITEMINFO.is_file():
        client = parse_iteminfo_lub(GAME_ITEMINFO)
        print(f"客戶端 {GAME_ITEMINFO.name}：{len(client):,} 筆（優先採用）")
    else:
        print(f"找不到 {GAME_ITEMINFO}，只用 RODATA 的 txt 表")

    items: dict[str, dict] = {}
    for item_id in sorted(set(names) | set(client)):
        mask_text = slot_mask.get(item_id)
        mask = int(mask_text) if mask_text and mask_text.isdigit() else None
        from_client = client.get(item_id, {})

        name = from_client.get("name") or names.get(item_id)
        if not name:
            continue
        entry: dict = {"name": name}
        desc = from_client.get("desc") or descs.get(item_id)
        if desc:
            entry["desc"] = desc
        res = from_client.get("res") or resources.get(item_id)
        if res:
            entry["res"] = res
        if item_id in english:
            entry["en"] = english[item_id]
        if from_client.get("slots") is not None:
            entry["slots"] = int(from_client["slots"])
        elif item_id in slot_count:
            entry["slots"] = int(slot_count[item_id])
        if mask is not None:
            entry["equip_mask"] = mask
            if mask:
                entry["equip_at"] = slot_names(mask)

        # 是否為裝備：itemslotcounttable 有列，或 equip_mask 非 0。
        # 兩者交叉檢定一致率 99.4%（矛盾的 3 筆是造型鬍鬚類）。
        entry["equip"] = item_id in slot_count or bool(mask)
        # 補品分類只看非裝備，避免把「HP恢復力上升」的裝備算進來。
        if not entry["equip"]:
            entry.update(restore_info(entry.get("desc", "")))
        items[str(item_id)] = entry

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        json.dump(items, handle, ensure_ascii=False, separators=(",", ":"))

    equips = sum(1 for e in items.values() if e["equip"])
    print(f"物品 {len(items):,} 筆（裝備 {equips:,} / 非裝備 {len(items) - equips:,}）")
    print(f"  有說明 {sum(1 for e in items.values() if 'desc' in e):,}")
    print(f"  有資源名 {sum(1 for e in items.values() if 'res' in e):,}")
    print(f"  有英文代號 {sum(1 for e in items.values() if 'en' in e):,}")
    hp = [e for e in items.values() if e.get("heal_hp")]
    sp = [e for e in items.values() if e.get("heal_sp")]
    print(f"  補 HP {len(hp):,} 筆（其中 {sum(1 for e in hp if 'heal_hp_amount' in e):,} 筆"
          f"描述有寫數字）")
    print(f"  補 SP {len(sp):,} 筆（其中 {sum(1 for e in sp if 'heal_sp_amount' in e):,} 筆"
          f"描述有寫數字）")
    print(f"  有裝備部位 {sum(1 for e in items.values() if 'equip_at' in e):,}")
    print(f"輸出：{OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
