"""自動寄信的設定：**依角色名**存在使用者本機，下次開程式自動帶回來。

存什麼：要寄給誰、哪些**道具編號**各湊到幾個就寄、有沒有啟用。

⚠ **存道具編號不存格號**（CLAUDE.md：存身分，不存位置）。格號會挪動 ——
丟東西、賣東西、用完一整疊，後面的都會往前遞補（[MEM-028]）。
存格號的那一刻沒有錯，錯的是三分鐘後，而且它會**安靜地寄錯東西**。

⚠ **鍵是角色名不是 PID**：PID 每次開遊戲都不一樣，存了等於沒存。

檔案放使用者資料夾（`%APPDATA%\\RO-Online-toolbox\\mail_settings.json`），
壞掉／讀不到一律當成「沒有設定」—— 絕不讓一個壞檔案擋住整頁。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ro_toolbox.config.paths import user_data_dir

log = logging.getLogger(__name__)

_FILE_NAME = "mail_settings.json"
#: 一次最多寄幾個。RO 的信件附件是一格，數量受道具本身的堆疊上限管；
#: 這裡只擋明顯不合理的值（手改檔案）。
_MAX_AMOUNT = 30000
#: 角色名的長度上限（封包欄位是 24 bytes）。
_MAX_NAME = 24


@dataclass(frozen=True, slots=True)
class MailRule:
    """一條規則：這個道具湊到 `amount` 個就寄一次。"""

    item_id: int
    amount: int


@dataclass(frozen=True, slots=True)
class MailSaved:
    """一隻角色記住的寄信設定。"""

    #: 寄給誰（角色名）。
    receiver: str = ""
    #: 每一樣道具各自的門檻。
    rules: tuple[MailRule, ...] = field(default_factory=tuple)
    #: 有沒有啟用（使用者指定「寄信設定要有個啟用」）。
    enabled: bool = False

    @property
    def usable(self) -> bool:
        """設定完整到可以真的開始跑嗎。"""
        return bool(self.enabled and self.receiver and self.rules)


def _path():
    return user_data_dir() / _FILE_NAME


def _load_all() -> dict[str, dict]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("寄信設定讀不到（當成沒有設定）：%s", exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _clean(data: dict) -> MailSaved:
    """把檔案裡的一筆洗成可信的設定。任何一欄不合理就丟掉那一條。

    不信任檔案內容：使用者可能手改過、也可能是舊版寫的。
    ⚠ 寧可少一條規則，也不要留一條會寄錯東西的。
    """
    receiver = data.get("receiver")
    receiver = receiver.strip() if isinstance(receiver, str) else ""
    if len(receiver.encode("cp950", "ignore")) > _MAX_NAME:
        receiver = ""          # 封包欄位塞不下 —— 當成沒設定

    rules = []
    seen: set[int] = set()
    for row in data.get("rules") or ():
        if not isinstance(row, dict):
            continue
        item_id = row.get("item_id")
        amount = row.get("amount")
        if not isinstance(item_id, int) or item_id <= 0:
            continue
        if not isinstance(amount, int) or not 0 < amount <= _MAX_AMOUNT:
            continue
        if item_id in seen:
            continue           # 同一個道具兩條規則 —— 只留第一條
        seen.add(item_id)
        rules.append(MailRule(item_id, amount))

    return MailSaved(
        receiver=receiver,
        rules=tuple(rules),
        enabled=bool(data.get("enabled")),
    )


def get(character: str) -> MailSaved:
    """這隻角色記住的設定。沒有記過（或名字是空的）回一份空的。"""
    if not character.strip():
        return MailSaved()
    entry = _load_all().get(character)
    return _clean(entry) if isinstance(entry, dict) else MailSaved()


def save(character: str, config: MailSaved) -> None:
    """記住這隻角色的設定。寫不進去只記錄，不要害整頁掛掉。"""
    if not character.strip():
        return
    data = _load_all()
    data[character] = {
        "receiver": config.receiver,
        "rules": [{"item_id": r.item_id, "amount": r.amount} for r in config.rules],
        "enabled": bool(config.enabled),
    }
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("寄信設定存不進去：%s", exc)


def due(config: MailSaved, counts: dict[int, int]) -> MailRule | None:
    """現在有哪一樣達到門檻了嗎？回第一條達標的規則，沒有回 None。

    `counts` 是 `{道具編號: 背包裡有幾個}`。

    ⚠ 使用者指定：「**只要那樣物品數量達到我選擇的就會寄信，不需要全部湊齊**」
    —— 所以是「任一條達標就回」，不是「全部達標才回」。
    """
    if not config.usable:
        return None
    for rule in config.rules:
        if counts.get(rule.item_id, 0) >= rule.amount:
            return rule
    return None
