"""自動回連：斷線 → 關遊戲 → 重開 → 重新登入 → **把斷線前在跑的東西接回去**。

判斷「該不該重連」在 `services/reconnect.py`（純邏輯）；這裡是**執行**那一層。

## 為什麼需要它

使用者實測回報：**這遊戲很容易斷線，而且常常斷在傳送／換地圖那一瞬間**。
掛機掛到一半斷線，人就停在那裡到你回來看為止 —— 而自動尋路正在跨圖時斷線，
回來還得自己把路重走一遍。

## ⚠ 三種「沒有連線」處理方式完全相反

1. **你自己的網路斷了** → **什麼都不做**。關遊戲重開是幫倒忙：重開照樣連不上，
   而且原本還在線上的角色被登出了。
2. **換地圖的過渡** → 等。換圖時伺服器會把連線移到另一台 map server
   （[PKT-038]），那一瞬間就是「沒有連線」。看到一次就重開＝每次換圖都自砍。
3. **真的斷線** → 才重連。

這三條由 `ReconnectDecider` 判定，這裡只負責照著做。

## 接回去的是「身分」，不是「位置」

重新登入之後角色多半在存檔點，**不會在斷線的地方**。所以快照裡存的是
**目的地地圖**（身分）而不是路線走到第幾段（位置）—— 回來之後路線從當下位置
重算（CLAUDE.md：存身分，不存位置）。自動打怪、自動補水同理，存的是設定。

## 全部靠注入，所以測得起來

關遊戲、開遊戲、登入、快照、還原都是傳進來的函式。這支不 import 任何會動到
遊戲的東西，測試餵假的進去就能把整條流程跑完。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ro_toolbox.services.reconnect import (
    BACKOFF,
    NO_NETWORK,
    OK,
    RECONNECT,
    WATCHING,
    ReconnectDecider,
)

log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """斷線前在跑什麼。**存身分不存位置。**"""

    #: 自動打怪有沒有在跑
    farming: bool = False
    #: 自動補水的設定（`PotionConfig`），None = 沒開
    potion: Any = None
    #: 自動尋路的**目的地地圖**（不是走到第幾段）。None = 沒在趕路
    destination: str | None = None
    #: 給人看的說明
    labels: list[str] = field(default_factory=list)

    @property
    def anything(self) -> bool:
        return bool(self.farming or self.potion is not None or self.destination)


class ReconnectSupervisor:
    """一個帳號一個。每拍呼叫 `tick()`，回傳目前的狀態字串。"""

    def __init__(
        self,
        name: str,
        *,
        find_pid: Callable[[], int | None],
        connected: Callable[[int], bool],
        network_up: Callable[[], bool],
        close_game: Callable[[int], None],
        relaunch: Callable[[], int | None],
        login: Callable[[int], bool],
        snapshot: Callable[[int], Snapshot],
        restore: Callable[[int, Snapshot], None],
        decider: ReconnectDecider | None = None,
        see_dialog: Callable[[int], bool | None] = lambda _pid: None,
    ) -> None:
        self.name = name
        self._find_pid = find_pid
        self._connected = connected
        self._network_up = network_up
        self._close_game = close_game
        self._relaunch = relaunch
        self._login = login
        self._snapshot = snapshot
        self._restore = restore
        #: 畫面上有沒有「與伺服器斷線」。回 None＝看不了 —— 那就走觀察期。
        #: 預設「永遠看不了」，所以沒接畫面判定的呼叫端行為完全不變。
        self._see_dialog = see_dialog
        self._decider = decider or ReconnectDecider()
        #: 最後一次「連線正常」時在跑什麼。斷線之後就是靠它接回去。
        self.snap = Snapshot()
        self.note = ""
        self.reconnects = 0

    def tick(self, now: float) -> str:
        pid = self._find_pid()
        alive = pid is not None and self._connected(pid)
        if alive:
            # ⚠ 只在**連線正常**時更新快照。斷線當下的狀態是「什麼都停了」，
            # 那時候拍下來等於把要接回去的東西全部忘掉。
            self.snap = self._snapshot(pid)
        # ⚠ 只在**沒有連線**的時候才去看畫面：抓圖＋比對要一百多毫秒，
        # 連線正常時每拍都做是白花的。
        dialog = None if alive or pid is None else self._see_dialog(pid)
        state = self._decider.decide(alive, self._network_up(), now, dialog)
        self.note = self._decider.note
        if state != RECONNECT:
            return state
        return self._reconnect(now)

    def _reconnect(self, now: float) -> str:
        keep = self.snap
        log.warning("「%s」判定斷線，開始自動回連%s", self.name,
                    f"（回來要接回：{'、'.join(keep.labels)}）" if keep.labels else "")
        pid = self._find_pid()
        if pid is not None:
            # 接續一個斷在半途的客戶端是賭博，關掉重開是確定的
            # （批次登入用的是同一條規則）。
            self._close_game(pid)

        fresh = self._relaunch()
        if fresh is None:
            return self._failed(now, "開遊戲失敗")
        if not self._login(fresh):
            return self._failed(now, "重新登入失敗")

        self.reconnects += 1
        self._decider.reset()
        if keep.anything:
            self._restore(fresh, keep)
            self.note = f"已回連，接回：{'、'.join(keep.labels) or '（無）'}"
        else:
            self.note = "已回連"
        log.warning("「%s」%s", self.name, self.note)
        return OK

    def _failed(self, now: float, why: str) -> str:
        # ⚠ 失敗要**退避**，不能無腦一直重開。伺服器維修時我們分不出來
        # （見 services/reconnect.py 的檔頭），退避是那種情況唯一的保護。
        self._decider.note_attempt_failed(now)
        self.note = f"⚠ {why}：{self._decider.note}"
        log.warning("「%s」%s", self.name, self.note)
        return BACKOFF


__all__ = [
    "BACKOFF",
    "NO_NETWORK",
    "OK",
    "RECONNECT",
    "WATCHING",
    "ReconnectSupervisor",
    "Snapshot",
]
