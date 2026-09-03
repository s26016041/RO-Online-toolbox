r"""自動掛機的「撿取黑名單」：**所有角色共用一份**，存在使用者本機。

存什麼：**不撿**的道具編號。

⚠ 使用者指定（2026-09-04）：「這個是**永遠開啟**的，所以不會有開關」——
所以刻意**沒有 enabled 欄位**。名單裡有東西就一定生效，
不會有「設定還在但忘了打開」這種安靜失效的狀態。

⚠ 使用者再指定（同日）：「黑名單做成**全部角色共用**，不區分角色，
大家都讀同一個」。所以這裡**沒有角色這個維度** ——
補水（`potion_store`）、寄信（`mail_store`）那兩份是依角色存的，這份不是。
不要照抄它們的形狀。

⚠ **存道具編號不存名字**（CLAUDE.md：存身分，不存位置）。
名字會隨改版翻譯調整，編號才是穩定的身分；地上的掉落物封包給的也是編號
（`world.GroundItem.name_id`），兩邊對得上不用轉換。

檔案放使用者資料夾（`%APPDATA%\RO-Online-toolbox\loot_blacklist.json`），
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
#: 現在的檔案長這樣：`{"items": [編號…]}`。
_KEY = "items"
#: 道具編號的合法上限。封包的 `name_id` 欄位是 2 bytes（見 `world._take_drop`），
#: 超出範圍的一律當成手改壞的檔案丟掉。
_MAX_ITEM_ID = 0xFFFF


def _path():
    return user_data_dir() / _FILE_NAME


def _raw() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("撿取黑名單讀不到（當成沒有黑名單）：%s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _clean(rows) -> set[int]:
    """把檔案裡的一串洗成可信的編號。不合理的那一項丟掉，其他照留。

    ⚠ 寧可少擋一樣東西（頂多多撿到一個），也不要因為一個壞值就整份放棄。
    """
    out = set()
    for value in rows or ():
        if isinstance(value, bool):
            continue           # `True` 在 Python 裡也是 int，會變成道具 1
        if isinstance(value, int) and 0 < value <= _MAX_ITEM_ID:
            out.add(value)
    return out


def get() -> frozenset[int]:
    """不撿的道具編號。沒設定過回空的集合。

    ⚠ **舊的「依角色存」格式會自動接過來**：把每一隻角色記過的名單**聯集**
    起來當成共用的那一份。直接丟掉的話，使用者會發現「設定不見了」，
    而且他不會知道是改版造成的 —— 只會覺得工具把東西弄丟了。
    """
    data = _raw()
    if _KEY in data:
        return frozenset(_clean(data.get(_KEY)))
    merged: set[int] = set()
    for rows in data.values():            # 舊格式：{角色名: [編號…]}
        if isinstance(rows, list):
            merged |= _clean(rows)
    return frozenset(merged)


def save(item_ids) -> None:
    """記住這份名單（所有角色共用）。寫不進去只記錄，不要害整頁掛掉。"""
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({_KEY: sorted(_clean(item_ids))}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("撿取黑名單存不進去：%s", exc)
