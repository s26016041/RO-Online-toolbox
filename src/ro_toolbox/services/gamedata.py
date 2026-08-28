"""道具表／怪物表（assets/*.json.gz）的唯一入口。

兩張表都是「ID → 中文名」的靜態資料，由 `tools/build_item_table.py`、
`tools/build_mob_table.py` 從 RODATA 解包資料自動抽出（見 CLAUDE.md 的
資料來源優先序：確認過不在記憶體裡才抄資源包，而且一律用腳本抽、不手打）。

集中在這裡的理由：撿到道具要查名字、封包解出的 class ID 要驗證是不是怪物，
兩件事分散在 UI 與 service 各載一次會有兩份快取、也容易只改到一邊。

查不到一律回 None／False —— 呼叫端要自己決定安全退化（顯示編號、放棄動作），
絕不拿猜的值繼續算。
"""

from __future__ import annotations

import gzip
import json
import logging
from functools import lru_cache

from ro_toolbox.config.paths import ASSETS_DIR

log = logging.getLogger(__name__)

_ITEM_TABLE = ASSETS_DIR / "items.json.gz"
_MOB_TABLE = ASSETS_DIR / "mobs.json.gz"
_WARP_TABLE = ASSETS_DIR / "warps.json.gz"
_MAP_NAME_TABLE = ASSETS_DIR / "mapnames.json.gz"
_NPC_TABLE = ASSETS_DIR / "npcs.json.gz"
_SKILL_TABLE = ASSETS_DIR / "skills.json.gz"


#: 讀成功的表就留在記憶體裡，見 `_load()`。
_LOADED: dict[str, dict[str, dict]] = {}


def _load(path) -> dict[str, dict]:
    r"""讀一張表。**成功的結果留著，失敗的不留。**

    留著的理由有兩個，第二個才是重點：

    1. `is_boss()` 這種每一拍都會呼叫的函式，不該每次都去解壓 90 KB 的 gz。
    2. onefile 的解壓目錄**會在執行中被挖空**：另一支 PyInstaller 程式開場清理
       `%TEMP%\_MEI*` 時，會把我們正在用的目錄裡刪得掉的檔案全刪掉（鎖住的 DLL
       留著，所以我們不會當，只是資料表憑空消失 —— [ENV-007]）。開場讀進來的
       內容留在記憶體裡，那之後再怎麼被挖都不影響。

    失敗**不留**：一次暫時性的讀取失敗不該讓整張表空一輩子，下次呼叫要能重試。
    """
    cached = _LOADED.get(str(path))
    if cached is not None:
        return cached
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            table = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("載入 %s 失敗：%s", path.name, exc)
        return {}
    table.pop("_meta", None)
    _LOADED[str(path)] = table
    return table


@lru_cache(maxsize=1)
def item_names() -> dict[int, str]:
    """道具 ID → 繁中名稱。`name` 是繁中、`res` 才是韓文（見 GAMEDATA [PKT-028]）。"""
    return {int(k): v.get("name") or f"#{k}" for k, v in _load(_ITEM_TABLE).items()}


@lru_cache(maxsize=1)
def mob_names() -> dict[int, str]:
    """怪物 class ID → 繁中名稱。"""
    return {int(k): v.get("name") or f"#{k}" for k, v in _load(_MOB_TABLE).items()}


@lru_cache(maxsize=1)
def _skip_classes() -> frozenset[int]:
    """不該自動打的 class ID：MVP 與草。

    - `boss`：來自 navi_mob 第 2 欄 == 301，[DAT-016] 已驗證那 91 筆全是 MVP。
    - `kind == "plant"`：名稱結尾是草／菇且等級 1（紅草、藍草、綠草…共 8 種）。
      `plant?` 是「sprite 與草相同但沒有等級可證」的 9 筆 —— 不確定就別打，
      這是安全退化，打錯只是浪費時間、漏打只是少一個目標。
    - **菁英怪不在此列**（菁英摩卡 2741 的 boss 是 False），照打。
    """
    skip = set()
    for key, entry in _load(_MOB_TABLE).items():
        if entry.get("boss") or str(entry.get("kind", "")).startswith("plant"):
            skip.add(int(key))
    return frozenset(skip)


def is_boss(class_id: int | None) -> bool:
    return class_id is not None and bool(_load(_MOB_TABLE).get(str(class_id), {}).get("boss"))


def is_farmable(class_id: int | None) -> bool:
    """這隻該不該自動打。查不到的 class ID 一律當可打（只擋掉明確的 MVP 與草）。"""
    return class_id is None or class_id not in _skip_classes()


@lru_cache(maxsize=8)
def mobs_on_map(map_name: str) -> frozenset[int]:
    """這張地圖會出現的怪 class ID（來自 navi_mob 出沒資料，見 [DAT-016]）。

    拿來當記憶體掃描的過濾條件：沙漠的 MVP 不可能出現在草原，
    這一條就把大部分假陽性擋掉了（見 [MEM-014]）。查不到回空集合，
    呼叫端要自己決定退化行為（改用全表，並知道誤判會變多）。
    """
    table = _load(_MOB_TABLE)
    return frozenset(
        int(k) for k, v in table.items() if map_name in (v.get("maps") or {})
    )


def item_name(item_id: int) -> str:
    """查不到就回 `#編號` —— 安全退化，不會假裝知道。"""
    return item_names().get(item_id, f"#{item_id}")


def mob_name(class_id: int | None) -> str:
    if class_id is None:
        return "未知"
    return mob_names().get(class_id, f"#{class_id}")


@lru_cache(maxsize=1)
def skill_table() -> dict[int, dict]:
    """技能 ID → `{key, name, maxlv, sp[], range[]}`。來源 tools/build_skill_table.py。

    `sp`／`range` 是每一級一個值（索引 0 = Lv1），拿來跟記憶體讀到的欄位
    交叉比對（見 `services/skills.py`）。
    """
    return {int(k): v for k, v in _load(_SKILL_TABLE).items()}


@lru_cache(maxsize=1)
def skill_codes() -> dict[str, int]:
    """技能英文代號（`SM_BASH`）→ ID。

    記憶體裡的技能結構帶著英文代號的字串指標，這張表是「字串與 ID 對不對得上」
    那一道交叉驗證的另一半 —— 沒有它就只能靠數值範圍猜，那會誤中一堆堆積垃圾。
    """
    return {v["key"]: k for k, v in skill_table().items() if v.get("key")}


def skill_name(skill_id: int | None) -> str:
    """查不到就回 `#編號` —— 安全退化，不會假裝知道。"""
    if skill_id is None:
        return "未知"
    entry = skill_table().get(skill_id)
    return (entry or {}).get("name") or f"#{skill_id}"


@lru_cache(maxsize=1)
def _equip_ids() -> frozenset[int]:
    """是裝備的道具 ID。來源見 tools/build_item_table.py 的 `equip` 欄位。"""
    return frozenset(
        int(k) for k, v in _load(_ITEM_TABLE).items() if v.get("equip")
    )


def is_equip(item_id: int | None) -> bool:
    return item_id is not None and item_id in _equip_ids()


@lru_cache(maxsize=1)
def _warp_file() -> dict:
    try:
        with gzip.open(_WARP_TABLE, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("載入 %s 失敗：%s", _WARP_TABLE.name, exc)
        return {}


def _warp_table() -> dict[str, list]:
    """走過去就會傳送的那些。

    v2 之後檔案分成 `walk`／`npc` 兩份；v1 是一個扁平的 {地圖: [...]}，
    那時候只收得到「走得過去」的，所以整份就等於 walk。
    """
    data = _warp_file()
    return data.get("walk", data) if "walk" in data else data


def _npc_table() -> dict[str, list]:
    return _warp_file().get("npc", {})


def warps_on_map(map_name: str) -> list[tuple[int, int, str, int, int]]:
    """這張地圖上**走過去就會傳送**的傳點：(x, y, 目的地圖, 目的x, 目的y)。

    來源是客戶端導航資料 `navi_link_tw.lub`（見 `tools/build_warp_table.py`）。
    查不到回空清單 —— 呼叫端要自己安全退化，不要亂走。
    """
    return [
        (int(r[0]), int(r[1]), str(r[2]), int(r[3]), int(r[4]))
        for r in _warp_table().get(map_name, [])
        if len(r) >= 5
    ]


def npc_links_on_map(
    map_name: str,
) -> list[tuple[int, int, str, int, int, str, int]]:
    """這張地圖上**要跟 NPC 講話**才過得去的連結，最後一欄是 NPC 名字。

    ⚠ 這些**不能拿去自動走** —— 走到那一格不會發生任何事，要對話（船夫、
    傳送師、告示牌），有的還要付錢。它只有一個用途：算不出路線時**說清楚
    為什麼**（「izlu2dun 的船員可以送你回 izlude」），而不是丟一句「找不到」。
    """
    return [
        (int(r[0]), int(r[1]), str(r[2]), int(r[3]), int(r[4]),
         str(r[5]) if len(r) > 5 else "", int(r[6]) if len(r) > 6 else 0)
        for r in _npc_table().get(map_name, [])
        if len(r) >= 5
    ]


@lru_cache(maxsize=32)
def warp_landings_on(map_name: str) -> tuple[tuple[int, int], ...]:
    """從**別的地圖**進到 `map_name` 時會落在哪些格（含要跟 NPC 講話的那種）。

    用途只有一個：判斷「這張圖是不是分成好幾個各自獨立進出的區域」。
    主城的室內圖是一張圖裡好幾間店 —— `prt_in` 實測 26 個互不相連的區塊、
    20 道各自獨立通往 prontera 的門。遊戲的尋路目標只給**地圖名**，
    走進去的是不是你要的那一間，我們沒有資料可以判斷，只能講清楚。
    """
    out: list[tuple[int, int]] = []
    for table in (_warp_table(), _npc_table()):
        for rows in table.values():
            for row in rows:
                if len(row) >= 5 and str(row[2]) == map_name:
                    out.append((int(row[3]), int(row[4])))
    return tuple(dict.fromkeys(out))


@lru_cache(maxsize=1)
def _map_name_table() -> dict[str, str]:
    try:
        with gzip.open(_MAP_NAME_TABLE, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("載入 %s 失敗：%s", _MAP_NAME_TABLE.name, exc)
        return {}


def map_name_table() -> dict[str, str]:
    """{地圖代碼: 中文名}。給目的地選單用（中文與代碼都要能搜）。"""
    return dict(_map_name_table())


def map_display_name(map_name: str) -> str:
    """地圖的中文名，查不到就回內部名（安全退化：顯示編號好過顯示錯的名字）。

    來源是客戶端導航視窗自己用的 `navi_map_tw.lub`
    （見 `tools/build_map_names.py`），所以顯示出來的字跟遊戲裡一致。
    """
    return _map_name_table().get(map_name.lower(), map_name)


@lru_cache(maxsize=1)
def _heal_table() -> dict[int, dict]:
    """道具 ID → 補品資訊。只有描述寫得出來的才進表（見 [DAT-020]）。"""
    out: dict[int, dict] = {}
    for key, entry in _load(_ITEM_TABLE).items():
        if entry.get("heal_hp") or entry.get("heal_sp"):
            out[int(key)] = entry
    return out


def heals_hp(item_id: int | None) -> bool:
    """這個道具補不補 HP。

    ⚠ 依據是客戶端描述文字（`heal_src == "desc"`）—— 客戶端**沒有**回血量的
    數值表，那是伺服器端 item_db 的資料（[DAT-020] 已窮舉確認）。
    所以這只夠用來「分類」，不夠拿來算「喝幾瓶會滿」。
    """
    return item_id is not None and bool(_heal_table().get(item_id, {}).get("heal_hp"))


def heals_sp(item_id: int | None) -> bool:
    return item_id is not None and bool(_heal_table().get(item_id, {}).get("heal_sp"))


def heal_amount(item_id: int | None, kind: str = "hp") -> int | None:
    """描述有寫數字才回，沒寫回 None —— 不從同系列去推（那是猜的）。"""
    if item_id is None:
        return None
    return _heal_table().get(item_id, {}).get(f"heal_{kind}_amount")


def is_mob(class_id: int) -> bool:
    """這個 class ID 是不是怪物表裡的怪。

    封包解出來的 class ID 拿來過濾「這隻實體是不是怪」：NPC／傳送點／其他玩家
    的 job 編號不在怪物表裡，就不會被當成怪去打（見 GAMEDATA [PKT-029]）。
    """
    return class_id in mob_names()


# ---- NPC：誰在哪張圖的哪一格（`assets/npcs.json.gz`，來源 navi_npc_tw.lub）----
#
# ⚠ 這張表**只回答「人在哪」**。RO 的商店賣什麼、賣多少錢在**伺服器**上，
# 開店那一刻才用 0x00C6 送過來（[PKT-074]）—— 客戶端資料裡沒有。
#
# ⚠ 也**沒有**「這個是商店」的欄位：型別欄實測只有 101/102/0 三個值，
# 跟商不商店無關。所以「誰是道具商人」只能靠**名字**（那是遊戲自己的顯示名，
# 不是我們推的）。名字換了就抓不到 —— 抓不到就不買，那是安全退化。


@lru_cache(maxsize=1)
def _npc_file() -> dict:
    try:
        with gzip.open(_NPC_TABLE, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("載入 %s 失敗：%s", _NPC_TABLE.name, exc)
        return {}


def npcs_on_map(map_name: str) -> list[tuple[int, int, str, int]]:
    """這張圖上的 NPC：(x, y, 名字, 外觀編號)。查不到回空的。"""
    rows = _npc_file().get("npc", {}).get(map_name, [])
    return [(int(r[0]), int(r[1]), str(r[2]), int(r[3])) for r in rows if len(r) >= 4]


#: 賣消耗品的商人，名字裡會有這幾個詞之一。
#:
#: ⚠ 這是**遊戲自己的顯示名**，不是我們替它分類。實測 9,585 個 NPC 裡有 122 個
#: 命中，最多的是「道具商人」48、「工具商人」26、「戰術道具商人」25、
#: 「高級藥水商人」10。命不中就是**不買**（安全退化），不准拿別的 NPC 去猜。
POTION_SELLER_WORDS = ("道具商人", "工具商人", "藥水商人", "雜貨商人")


def potion_sellers_on(map_name: str) -> list[tuple[int, int, str, int]]:
    """這張圖上**可能賣藥水**的商人。名字比對，命不中就回空的。"""
    return [
        npc for npc in npcs_on_map(map_name)
        if any(word in npc[2] for word in POTION_SELLER_WORDS)
    ]


def maps_with_potion_sellers() -> list[str]:
    """哪些地圖上有賣藥水的商人（給尋路挑目的地用）。"""
    table = _npc_file().get("npc", {})
    out = []
    for name, rows in table.items():
        if any(any(w in str(r[2]) for w in POTION_SELLER_WORDS) for r in rows if len(r) >= 4):
            out.append(name)
    return sorted(out)


# ---- 怪物出沒：哪張圖有這隻怪、大概幾隻 ------------------------------------
#
# 資料就在 `assets/mobs.json.gz` 的 `maps` 欄裡（`{地圖: 數量}`），來源是客戶端
# 自己的 `navi_mob_tw.lub` —— 遊戲的地圖資訊視窗用的是同一份（[DAT-016]）。
# 也就是說**數量不是我們估的**，是遊戲自己的數字。


#: 數量 → 給人看的粗略標籤。
#:
#: ⚠ **分界是我們自己切的**，不是客戶端給的字串 —— 客戶端資料裡只有數字。
#: 切法照實際分布走（2,907 筆出沒資料：最少 1、中位數 15、75% 是 35、
#: 90% 是 65、最多 230），所以四段大約各佔 30% / 30% / 25% / 15%。
#: 標籤只是**方便掃視**，真正的依據永遠是旁邊那個數字。
DENSITY_STEPS = ((5, "很少"), (20, "普通"), (50, "多"))
DENSITY_TOP = "超多"


def density_label(count: int) -> str:
    """把出沒數量講成一個詞。⚠ 分界見 `DENSITY_STEPS`，是我們切的不是遊戲給的。"""
    for limit, word in DENSITY_STEPS:
        if count <= limit:
            return word
    return DENSITY_TOP


def mob_maps(class_id: int) -> list[tuple[str, int]]:
    """這隻怪出現在哪些圖、各幾隻。**由多到少**排。查不到回空的。"""
    entry = _load(_MOB_TABLE).get(str(class_id)) or {}
    maps = entry.get("maps") or {}
    return sorted(((m, int(c)) for m, c in maps.items()), key=lambda kv: -kv[1])


def find_mobs(text: str) -> list[tuple[int, str]]:
    """名字含 `text` 的怪：[(class ID, 名字)]。空字串回空的（不要整份倒出來）。"""
    text = text.strip()
    if not text:
        return []
    out = [
        (int(k), v["name"])
        for k, v in _load(_MOB_TABLE).items()
        if v.get("name") and text in v["name"]
    ]
    return sorted(out, key=lambda kv: (len(kv[1]), kv[1]))


@lru_cache(maxsize=1)
def mob_spawn_rows() -> list[tuple[str, str, int]]:
    """全部「怪 → 圖」的出沒列：[(怪名, 地圖代碼, 數量)]，怪名相同時數量多的在前。

    給目的地選單用：使用者打怪物名字就找得到「哪張圖有牠、多不多」。
    ⚠ 只收**有中文名的怪**（沒名字沒得搜）與**我們有地形的圖**
    （走不到的地圖列出來只會讓人選到一個註定失敗的目的地）。
    """
    from ro_toolbox.services.mapdata import has_terrain

    rows: list[tuple[str, str, int]] = []
    for entry in _load(_MOB_TABLE).values():
        name = entry.get("name")
        if not name:
            continue
        for where, count in (entry.get("maps") or {}).items():
            if has_terrain(where):
                rows.append((name, where, int(count)))
    rows.sort(key=lambda row: (row[0], -row[2]))
    return rows
