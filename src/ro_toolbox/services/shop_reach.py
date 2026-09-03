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

## ⚠⚠ 只准記「真的走不到」與「真的沒賣」

這份記憶會**改變下一趟挑誰**，所以寫錯一筆的代價不是白走一趟，是**接下來
一整個星期都挑錯人**。實機 2026-09-03：`_walk()` 對任何失敗都回同一個
「沒走到」，於是斷線那一拍把三家好店一起寫進來 ——

    10:48:56 ⚠ 遊戲連線已中斷 → 記下來：izlude_in  的商店 (57,110) 走不到
    10:48:59 找不到伺服器連線 → 記下來：lasagna    的商店 (165,125) 走不到
    10:49:02 找不到伺服器連線 → 記下來：cmd_fild07 的商店 (257,126) 走不到

三秒鐘記掉三家，而且全是**斷線**不是走不到。等到每一張圖的「道具商人」都被
寫掉，挑店就只剩同一張圖上的**高級藥水商人** —— 那家沒有紅色藥水也沒有回程
道具，於是每一趟都走到底才發現「這家店沒有你設定的藥水」（使用者 2026-09-03
回報：「自動補水 藥水商人都找錯」）。

所以：**只有走路那一段自己判定「到不了」（`TravelStats.unreachable`）才准寫**。
斷線、還沒登入、使用者取消、逾時 —— 一律不寫，那些跟「走不走得到」無關。

## 第二種記憶：**這家店沒賣我要的東西**

走得到不代表買得到。開店那一包（`0x00C6`）會把貨架整個送過來，所以
「這家沒有我設定的藥水」是**當場量到的事實**，不是推論 —— 記下來，
下一趟就不會再走一次同樣的路去撲空。

⚠ 這一種要**連道具編號一起記**：同一家店對紅色藥水沒貨，不代表對白色藥水
也沒貨。使用者換一種藥水，舊紀錄就自然不適用（比對不上＝沒記過）。

檔案放使用者資料夾（`%APPDATA%\RO-Online-toolbox\shop_reach.json`）。
壞掉／讀不到一律當成「沒記過」—— 一個壞檔案不可以擋住補水。

⚠ 檔案有 `version`：上面那個 bug 已經在使用者的檔案裡寫了一堆假紀錄，
版本對不上就整份當「沒記過」（少記一週 ≪ 挑錯人一週）。
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

#: 檔案格式版本。**對不上就整份當「沒記過」。**
#:
#: 1 → 2（2026-09-03）：v1 是 `_walk()` 對任何失敗都記一筆的年代，使用者的檔案
#: 裡混著「斷線」「還沒登入」寫進去的假紀錄，而且分不出哪筆是真的
#: （只存了時間）。留著會讓每一台已經裝過的機器繼續挑錯商人一整週，
#: 所以整份丟掉重學 —— 少記一週只是多走一趟，挑錯人是一週都買不到水。
_VERSION = 2


def _path():
    return user_data_dir() / _FILE_NAME


def _key(map_name: str, cell: tuple[int, int]) -> str:
    """「走不到」的鍵：地圖 ＋ 商人站的那一格（**身分**，不是清單第幾家）。"""
    return f"{map_name}:{cell[0]},{cell[1]}"


def _stock_key(map_name: str, cell: tuple[int, int], item_id: int) -> str:
    """「沒賣這個」的鍵：**連道具編號一起記**。

    同一家店對紅色藥水沒貨，不代表對白色藥水也沒貨 —— 使用者換一種藥水，
    舊紀錄比對不上就當沒記過（安全退化：最多多走一趟，不會少一家店可挑）。
    """
    return f"{_key(map_name, cell)}#{int(item_id)}"


def _load() -> dict[str, float]:
    try:
        with _path().open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("商店紀錄讀不到（當成沒記過）：%s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("version") != _VERSION:
        # 舊版（或被誰改壞了）→ 整份當沒記過。⚠ 這裡**不要**試著沿用舊資料：
        # v1 的每一筆都可能是斷線寫進來的假紀錄，而檔案裡分不出來。
        log.info("商店紀錄是舊格式（version=%r），整份重學", data.get("version"))
        return {}
    shops = data.get("shops")
    if not isinstance(shops, dict):
        return {}
    return {k: float(v) for k, v in shops.items() if isinstance(v, int | float)}


def _save(data: dict[str, float]) -> None:
    try:
        with _path().open("w", encoding="utf-8") as fh:
            json.dump({"version": _VERSION, "shops": data}, fh,
                      ensure_ascii=False, indent=2)
    except OSError as exc:
        log.warning("商店紀錄存不起來：%s", exc)


def note_bad(map_name: str, cell: tuple[int, int], now: float | None = None) -> None:
    """這家店這一趟**走不到** —— 記下來，下次排最後。

    ⛔ **只有走路那一段自己說「到不了」才准呼叫**（`TravelStats.unreachable`）。
    斷線、還沒登入、使用者取消、逾時都不算 —— 那些跟走不走得到無關，
    寫進來會讓好店被冷凍一週（見模組開頭 2026-09-03 那段實機日誌）。
    """
    data = _load()
    data[_key(map_name, cell)] = time.time() if now is None else now
    _save(data)
    log.info("記下來：%s 的商店 %s 走不到，下次先試別家", map_name, cell)


def note_no_stock(map_name: str, cell: tuple[int, int], items,
                  now: float | None = None) -> None:
    """開了店、貨架上**沒有**我們要的這幾樣 —— 記下來，下次排最後。

    這是**當場量到的事實**（開店那一包把整個貨架送過來），不是推論。
    """
    wanted = [int(i) for i in items if i]
    if not wanted:
        return
    data = _load()
    at = time.time() if now is None else now
    for item_id in wanted:
        data[_stock_key(map_name, cell, item_id)] = at
    _save(data)
    log.info("記下來：%s 的商店 %s 沒賣 %s，下次先試別家", map_name, cell, wanted)


def note_good(map_name: str, cell: tuple[int, int], items=()) -> None:
    """走到了（買到了）—— 把被推翻的舊紀錄清掉。

    `items` 給的是**這次真的買到的**道具編號：買到了就代表「沒賣」那筆是錯的。
    """
    data = _load()
    gone = data.pop(_key(map_name, cell), None) is not None
    for item_id in items:
        if item_id and data.pop(_stock_key(map_name, cell, int(item_id)), None):
            gone = True
    if gone:
        _save(data)
        log.info("%s 的商店 %s 這次成功了，清掉舊紀錄", map_name, cell)


class Memory:
    """「現在哪幾家被記成走不到／沒賣」的一份快照。

    ⚠ 為什麼要有這個：挑店會把**每一張有藥水商人的圖**問過一遍，一家一次
    `is_bad()` 就是一次開檔＋解析 JSON，而挑店一趟最多跑三次 ——
    幾十次多餘的磁碟讀取。快照讀一次就好，而且一趟裡的判斷也一致。
    """

    def __init__(self, data: dict[str, float], now: float) -> None:
        self._bad = frozenset(k for k, at in data.items() if now - at < _RETRY_AFTER)

    def is_bad(self, map_name: str, cell: tuple[int, int]) -> bool:
        """上次**走不到**這一家嗎。"""
        return _key(map_name, cell) in self._bad

    def lacks(self, map_name: str, cell: tuple[int, int], items) -> bool:
        """這家店對我們現在要買的東西**整份都沒貨**嗎。

        ⚠ 要 `all` 不要 `any`：只有一樣沒貨還是值得去（另一樣買得到）。
        紀錄比對不上（換了道具）就是 False —— 沒記過，照試。
        """
        wanted = [int(i) for i in items if i]
        if not wanted:
            return False
        return all(_stock_key(map_name, cell, i) in self._bad for i in wanted)

    def skip(self, map_name: str, cell: tuple[int, int], items=()) -> bool:
        """挑店時的總判斷：走不到、或已知沒賣，都排到後面去。"""
        return self.is_bad(map_name, cell) or self.lacks(map_name, cell, items)

    def __len__(self) -> int:
        return len(self._bad)


def snapshot(now: float | None = None) -> Memory:
    """讀一次檔，之後想問幾家就問幾家（見 `Memory`）。"""
    return Memory(_load(), time.time() if now is None else now)


def is_bad(map_name: str, cell: tuple[int, int], now: float | None = None) -> bool:
    """這家店最近被記成走不到嗎（過了 `_RETRY_AFTER` 就當沒記過）。

    ⚠ 一次一家。要問很多家請用 `snapshot()`，不然每問一家就開一次檔。
    """
    return snapshot(now).is_bad(map_name, cell)


def forget_all() -> None:
    """測試／使用者要重來時用。"""
    _save({})
