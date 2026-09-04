"""補水設定：**依角色名**存在使用者本機，下次開程式自動帶回來。

存什麼：補血／補魔各挑了哪個**道具編號**、百分比門檻、有沒有開啟，
以及「水用完回程」的開關與它要用哪個道具。

⚠ **自動打怪的開關刻意不存**（使用者指定）。開著程式回來就繼續打怪
太容易變成意外掛機；其他設定存回來只是「填好表單」，不會自己動作。

⚠ **存道具編號不存格號**（CLAUDE.md：存身分，不存位置）。
格號會挪動 —— 丟東西、賣東西、用完一整疊，後面的都會往前遞補（[MEM-028]）。
存格號的那一刻沒有錯，錯的是三分鐘後，而且它會**安靜地喝錯東西**。

⚠ **鍵是角色名不是 PID**。PID 每次開遊戲都不一樣，存了等於沒存。

檔案放使用者資料夾（`%APPDATA%\\RO-Online-toolbox\\potion_settings.json`），
不放專案目錄 —— 那是使用者自己的設定，換一版程式不該被蓋掉。
壞掉／讀不到一律當成「沒有設定」，絕不讓一個壞檔案擋住整頁。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace

from ro_toolbox.config.paths import user_data_dir

log = logging.getLogger(__name__)

_FILE_NAME = "potion_settings.json"
#: 門檻的合法範圍。100 以上會在滿血時照喝（實測 12 秒灌掉 58 瓶，見 [MEM-021]）。
_MAX_PERCENT = 99


@dataclass(frozen=True, slots=True)
class PotionSaved:
    """一隻角色記住的補水設定。"""

    hp_item: int | None = None
    hp_percent: int = 0
    sp_item: int | None = None
    sp_percent: int = 0
    enabled: bool = False
    #: 水用完回程：有沒有勾、用哪個道具回程（一樣存**編號**不存格號）
    go_home: bool = False
    home_item: int | None = None
    #: 自動尋路的目的地（地圖代碼）。None = 讀遊戲自己的尋路目標。
    travel_dest: str | None = None


def _path():
    return user_data_dir() / _FILE_NAME


def _load_all() -> dict[str, dict]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("補水設定讀不到（當成沒有設定）：%s", exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _clean(data: dict) -> PotionSaved:
    """把檔案裡的一筆洗成可信的設定。任何一欄不合理就退回安全值。

    不信任檔案內容：使用者可能手改過、也可能是舊版寫的。
    百分比夾在 0~99 —— 100 以上會在滿血時照喝（[MEM-021]，實際灌光過一整袋）。
    """

    def item(value) -> int | None:
        return int(value) if isinstance(value, int) and value > 0 else None

    def where(value) -> str | None:
        # 只收看起來像地圖代碼的（檔案可能被手改過）。不像就當沒設定 ——
        # 亂猜一個地圖名會把人送去完全不相干的地方。
        if not isinstance(value, str):
            return None
        text = value.strip().lower()
        return text if 0 < len(text) <= 20 and text.replace("_", "").replace(
            "@", "").isalnum() else None

    def percent(value) -> int:
        if not isinstance(value, int):
            return 0
        return max(0, min(_MAX_PERCENT, value))

    return PotionSaved(
        hp_item=item(data.get("hp_item")),
        hp_percent=percent(data.get("hp_percent")),
        sp_item=item(data.get("sp_item")),
        sp_percent=percent(data.get("sp_percent")),
        enabled=bool(data.get("enabled")),
        go_home=bool(data.get("go_home")),
        home_item=item(data.get("home_item")),
        travel_dest=where(data.get("travel_dest")),
    )


def get(character: str) -> PotionSaved | None:
    """這隻角色記住的設定。沒有記過（或名字是空的）回 None。"""
    if not character.strip():
        return None
    entry = _load_all().get(character)
    return _clean(entry) if isinstance(entry, dict) else None


#: 「有值 → 空值」要保護的欄位。空值＝ None／0／False —— 而那正好也是
#: **一張還沒還原的空卡片**長的樣子（見 `_keep_remembered`）。
_CLEARABLE = (
    "hp_item", "sp_item", "home_item",
    "hp_percent", "sp_percent", "go_home", "enabled", "travel_dest",
)


def _keep_remembered(
    who: str, before: PotionSaved, after: PotionSaved, cleared: frozenset[str]
) -> PotionSaved:
    """**只有使用者能把設定清掉。** 別人送來的空值一律當「還沒畫好」擋下來。

    這是同一個回報的第五次（[DAT-052]／[DAT-057]／[DAT-071]／[DAT-073]）。
    前四次都修在介面那一層：加 `_want_item`、去看 `self._bags`、改成
    `self._settings` 當本尊、加 `quiet` 閘門…… **每補一層就多一條繞得過去
    的路**，所以它每次都換一個面貌回來。

    實機證據（2026-09-04 17:15:15，`app.log`）：分頁一接上就喊
    「⚠ 還沒選道具或百分比是 0」，也就是存檔讀回來是
    `PotionSaved(enabled=True)` —— 每一欄都空、只有勾勾是開的。
    那正是**一張還沒還原的卡片**的形狀，它把使用者 15:55 存進去的
    502／50%／23455 整組蓋掉了，而且一行日誌都沒有。

    所以這次修在**存檔這一層**：介面那邊不管怎麼繞，
    「有值變成沒值」都要帶著使用者的授權才過得去。`cleared` 是使用者
    **真的動過**的欄位 —— 來源是 Qt 的 `activated`／`clicked`，
    那兩個訊號只有真人操作才會發，程式改值不會（跟自己維護的 `quiet`
    旗標不同，這一點是 Qt 保證的，繞不過去）。
    """
    keep = {
        field: getattr(before, field)
        for field in _CLEARABLE
        if field not in cleared
        and getattr(before, field) and not getattr(after, field)
    }
    if not keep:
        return after
    log.warning(
        "「%s」有設定被空值蓋掉，已擋下並保留原值：%s"
        "（使用者沒動過這幾欄 —— 多半是分頁還沒讀到背包）",
        who, "、".join(f"{k}={v}" for k, v in keep.items()),
    )
    return replace(after, **keep)


def save(
    character: str,
    config: PotionSaved,
    *,
    cleared: frozenset[str] = frozenset(),
) -> PotionSaved:
    """記住這隻角色的設定，**回傳真正存進去的那一份**。

    `cleared` = 使用者**自己**清掉的欄位名。沒列進來的欄位不准從「有值」
    變成「空值」（見 `_keep_remembered`）。擋下來的時候存進去的就跟送來的
    不一樣了 —— 所以要回傳，呼叫端才有辦法把畫面接回正確的那一份
    （不然畫面停在「未選擇」，使用者看到的還是「沒紀錄」）。

    寫不進去只記一行 —— 設定存不了不該擋住功能。
    """
    if not character.strip():
        return config
    everything = _load_all()
    before = everything.get(character)
    if isinstance(before, dict):
        config = _keep_remembered(
            character, _clean(before), _clean(asdict(config)), cleared
        )
    config = _clean(asdict(config))
    everything[character] = asdict(config)
    try:
        _path().write_text(
            json.dumps(everything, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("補水設定存不進去：%s", exc)
    return config


def forget(character: str) -> None:
    """把一隻角色的設定刪掉（測試與「重設」用）。"""
    everything = _load_all()
    if everything.pop(character, None) is None:
        return
    try:
        _path().write_text(
            json.dumps(everything, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("補水設定存不進去：%s", exc)
