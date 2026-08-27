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
from dataclasses import asdict, dataclass

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
    )


def get(character: str) -> PotionSaved | None:
    """這隻角色記住的設定。沒有記過（或名字是空的）回 None。"""
    if not character.strip():
        return None
    entry = _load_all().get(character)
    return _clean(entry) if isinstance(entry, dict) else None


def save(character: str, config: PotionSaved) -> None:
    """記住這隻角色的設定。寫不進去只記一行 —— 設定存不了不該擋住功能。"""
    if not character.strip():
        return
    everything = _load_all()
    everything[character] = asdict(_clean(asdict(config)))
    try:
        _path().write_text(
            json.dumps(everything, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("補水設定存不進去：%s", exc)


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
