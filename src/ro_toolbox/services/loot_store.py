"""自動掛機的「撿取黑名單」：**依角色名**存在使用者本機，下次開程式帶回來。

存什麼：**不撿**的道具編號。

⚠ 使用者指定（2026-09-04）：「這個是**永遠開啟**的，所以不會有開關」——
所以這裡刻意**沒有 enabled 欄位**。名單裡有東西就一定生效，
不會有「設定還在但忘了打開」這種安靜失效的狀態。

⚠ **存道具編號不存格號、不存名字**（CLAUDE.md：存身分，不存位置）。
名字會隨改版翻譯調整，編號才是穩定的身分；地上的掉落物封包給的也是編號
（`world.GroundItem.name_id`），兩邊對得上不用轉換。

⚠ **鍵是角色名不是 PID**：PID 每次開遊戲都不一樣，存了等於沒存。

檔案放使用者資料夾（`%APPDATA%\\RO-Online-toolbox\\loot_blacklist.json`），
壞掉／讀不到一律當成「沒有黑名單」—— 一個壞檔案不該讓掛機整個停掉。
安全退化的方向也只有這一個：**讀不到就照撿**。反過來（讀不到就都不撿）
會讓角色打了一整晚什麼都沒帶回來，而且全程不報錯。
"""

from __future__ import annotations

import json
import logging

from ro_toolbox.config.paths import user_data_dir

log = logging.getLogger(__name__)

_FILE_NAME = "loot_blacklist.json"
#: 道具編號的合法上限。封包的 `name_id` 欄位是 2 bytes（見 `world._take_drop`），
#: 超出範圍的一律當成手改壞的檔案丟掉。
_MAX_ITEM_ID = 0xFFFF


def _path():
    return user_data_dir() / _FILE_NAME


def _load_all() -> dict[str, list]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("撿取黑名單讀不到（當成沒有黑名單）：%s", exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _clean(rows) -> frozenset[int]:
    """把檔案裡的一筆洗成可信的編號集合。不合理的那一項丟掉，其他照留。

    ⚠ 寧可少擋一樣東西（頂多多撿到一個），也不要因為一個壞值就整份放棄。
    """
    out = set()
    for value in rows or ():
        if isinstance(value, bool):
            continue           # `True` 在 Python 裡也是 int，會變成道具 1
        if isinstance(value, int) and 0 < value <= _MAX_ITEM_ID:
            out.add(value)
    return frozenset(out)


def get(character: str) -> frozenset[int]:
    """這隻角色不撿的道具編號。沒設定過（或名字是空的）回空的集合。"""
    if not character.strip():
        return frozenset()
    return _clean(_load_all().get(character))


def save(character: str, item_ids) -> None:
    """記住這隻角色的黑名單。寫不進去只記錄，不要害整頁掛掉。"""
    if not character.strip():
        return
    data = _load_all()
    data[character] = sorted(_clean(item_ids))
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("撿取黑名單存不進去：%s", exc)
