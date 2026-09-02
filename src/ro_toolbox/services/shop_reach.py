r"""「這家店走得到嗎」—— 補水踩過的坑記下來，下次不要再踩。

## 為什麼需要

補水挑店是「離我最近的那張有藥水商人的圖」。從妙勒尼山脈回城，最近的永遠是
**普隆德拉內部（prt_in）**，而那家店**永遠走不到**：室內圖是一張地圖裡好幾間
互不相連的房間，NPC 傳送把人放在 (79,110)、商人在 (126,76)，兩間房之間沒有路。

`restock_bot` 本來就會「走不到就換一家」，所以最後還是買得到 —— 但那個 `skip`
只活在**這一趟**裡。實機 2026-09-01 的四次補水（07:44／08:46／17:54／18:31）
**每一次都先去 prt_in、每一次都失敗**，每次白白走 1.5~2 分鐘。
使用者看到的就是「一直找不到商店買水」。

## 形狀：**降級，不是刪除**

記下來的店只是**排到最後**，不是永遠不去：

- 落地點會變（NPC 傳送的落點、走哪一道門），今天走不到不代表明天走不到。
- 萬一每一家都被記成走不到，還是要有東西可以試 —— 否則「記憶」本身
  就變成「一瓶水都買不到」的原因。那是 CLAUDE.md 說的「安靜地做錯事」。

所以 `usable()` 在**全部被排除時會退回原本那份清單**，只是順序不同。

⚠ 存的是**身分**（地圖 ＋ 商人站的格），不是清單第幾家：商人資料表改版之後
格子會變，那時舊紀錄自然失效（比對不上就當沒記過），不會安靜地跳過一家好店。

檔案放使用者資料夾（`%APPDATA%\RO-Online-toolbox\shop_reach.json`）。
壞掉／讀不到一律當成「沒記過」—— 一個壞檔案不可以擋住補水。
"""

from __future__ import annotations

import json
import logging
import time

from ro_toolbox.config.paths import user_data_dir

log = logging.getLogger(__name__)

_FILE_NAME = "shop_reach.json"
#: 記過走不到的店，這麼久之後重新給它一次機會（秒）。
#:
#: ⚠ 不設永久：落地點可能因為改版、或走了別條路線而不同。一週試一次的代價
#: 是一趟白走，換來的是「地圖真的改好了我們會發現」。
_RETRY_AFTER = 7 * 24 * 3600.0


def _path():
    return user_data_dir() / _FILE_NAME


def _key(map_name: str, cell: tuple[int, int]) -> str:
    return f"{map_name}:{cell[0]},{cell[1]}"


def _load() -> dict[str, float]:
    try:
        with _path().open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("走不到的商店紀錄讀不到（當成沒記過）：%s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, int | float)}


def _save(data: dict[str, float]) -> None:
    try:
        with _path().open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.warning("走不到的商店紀錄存不起來：%s", exc)


def note_bad(map_name: str, cell: tuple[int, int], now: float | None = None) -> None:
    """這家店這一趟走不到 —— 記下來，下次排最後。"""
    data = _load()
    data[_key(map_name, cell)] = time.time() if now is None else now
    _save(data)
    log.info("記下來：%s 的商店 %s 走不到，下次先試別家", map_name, cell)


def note_good(map_name: str, cell: tuple[int, int]) -> None:
    """走到了 —— 把舊紀錄清掉（它已經被推翻了）。"""
    data = _load()
    if data.pop(_key(map_name, cell), None) is not None:
        _save(data)
        log.info("%s 的商店 %s 這次走到了，清掉舊紀錄", map_name, cell)


def is_bad(map_name: str, cell: tuple[int, int], now: float | None = None) -> bool:
    """這家店最近被記成走不到嗎（過了 `_RETRY_AFTER` 就當沒記過）。"""
    at = _load().get(_key(map_name, cell))
    if at is None:
        return False
    return (time.time() if now is None else now) - at < _RETRY_AFTER


def forget_all() -> None:
    """測試／使用者要重來時用。"""
    _save({})
