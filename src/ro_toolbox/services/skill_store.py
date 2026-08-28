r"""技能面板的設定：**依角色名**存在使用者本機，下次開程式帶回來。

存什麼：

- `buffs` —— 勾起來要自動補的補助技能：**技能編號 → 要用第幾級**。
- `levels` —— 打怪型技能欄選的等級（目前只記著，還沒有動作）。
- `learned` —— 「這個技能會上哪個狀態」**學到的對應**（技能編號 → EFST）。
  那是當場學出來的知識不是使用者的設定（見 `services/buffs.py`），
  但一樣要留著 —— 不然每次重開程式都得再放一次才知道要檢查什麼。

⚠ **鍵一律是技能編號，不是清單第幾列**（CLAUDE.md：存身分，不存位置）。
技能列表會因為學了新技能而重排，存第幾列的那一刻沒有錯，錯的是下次升級之後。

⚠ **鍵是角色名不是 PID** —— PID 每次開遊戲都不一樣，存了等於沒存。

檔案放使用者資料夾（`%APPDATA%\RO-Online-toolbox\skill_settings.json`）。
壞掉／讀不到一律當成「沒有設定」，絕不讓一個壞檔案擋住整頁。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ro_toolbox.config.paths import user_data_dir

log = logging.getLogger(__name__)

_FILE_NAME = "skill_settings.json"
#: 技能等級的合法範圍。超出就是檔案被改壞了，退回安全值。
_MAX_LEVEL = 20
#: EFST 編號是 uint16（`status_effects` 讀到超過就判定不合理）。
_MAX_EFST = 0xFFFF


@dataclass(frozen=True, slots=True)
class SkillSaved:
    """一隻角色記住的技能面板設定。"""

    buffs: dict[int, int] = field(default_factory=dict)
    levels: dict[int, int] = field(default_factory=dict)
    learned: dict[int, int] = field(default_factory=dict)


def _path():
    return user_data_dir() / _FILE_NAME


def _load_all() -> dict[str, dict]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("技能設定讀不到（當成沒有設定）：%s", exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _int_map(value, limit: int) -> dict[int, int]:
    """把 JSON 的 `{"60": 7}` 洗成 `{60: 7}`。任何一筆不合理就丟掉那一筆。

    不信任檔案內容：可能被手改過，也可能是舊版寫的。
    """
    out: dict[int, int] = {}
    if not isinstance(value, dict):
        return out
    for key, level in value.items():
        try:
            skill_id = int(key)
        except (TypeError, ValueError):
            continue
        if skill_id <= 0 or not isinstance(level, int) or isinstance(level, bool):
            continue
        if 1 <= level <= limit:
            out[skill_id] = level
    return out


def _clean(data: dict) -> SkillSaved:
    return SkillSaved(
        buffs=_int_map(data.get("buffs"), _MAX_LEVEL),
        levels=_int_map(data.get("levels"), _MAX_LEVEL),
        learned=_int_map(data.get("learned"), _MAX_EFST),
    )


def get(character: str) -> SkillSaved:
    """這隻角色記住的設定。沒記過就回一份空的（不是 None —— 呼叫端不必分兩種）。"""
    if not character.strip():
        return SkillSaved()
    entry = _load_all().get(character)
    return _clean(entry) if isinstance(entry, dict) else SkillSaved()


def save(character: str, saved: SkillSaved) -> None:
    """寫回檔案。寫不進去只記一行 —— 設定存不了不該讓程式停下來。"""
    if not character.strip():
        return
    everything = _load_all()
    everything[character] = {
        "buffs": {str(k): v for k, v in sorted(saved.buffs.items())},
        "levels": {str(k): v for k, v in sorted(saved.levels.items())},
        "learned": {str(k): v for k, v in sorted(saved.learned.items())},
    }
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(everything, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("技能設定存不進去：%s", exc)
