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


def _load(path) -> dict[str, dict]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            table = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("載入 %s 失敗：%s", path.name, exc)
        return {}
    table.pop("_meta", None)
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
def _equip_ids() -> frozenset[int]:
    """是裝備的道具 ID。來源見 tools/build_item_table.py 的 `equip` 欄位。"""
    return frozenset(
        int(k) for k, v in _load(_ITEM_TABLE).items() if v.get("equip")
    )


def is_equip(item_id: int | None) -> bool:
    return item_id is not None and item_id in _equip_ids()


@lru_cache(maxsize=1)
def _warp_table() -> dict[str, list]:
    try:
        with gzip.open(_WARP_TABLE, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("載入 %s 失敗：%s", _WARP_TABLE.name, exc)
        return {}


def warps_on_map(map_name: str) -> list[tuple[int, int, str, int, int]]:
    """這張地圖上的傳點：(x, y, 目的地圖, 目的x, 目的y)。

    來源是客戶端導航資料 `navi_link_tw.lub`（見 `tools/build_warp_table.py`）。
    查不到回空清單 —— 呼叫端要自己安全退化，不要亂走。
    """
    return [
        (int(r[0]), int(r[1]), str(r[2]), int(r[3]), int(r[4]))
        for r in _warp_table().get(map_name, [])
        if len(r) >= 5
    ]


@lru_cache(maxsize=1)
def _map_name_table() -> dict[str, str]:
    try:
        with gzip.open(_MAP_NAME_TABLE, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("載入 %s 失敗：%s", _MAP_NAME_TABLE.name, exc)
        return {}


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
