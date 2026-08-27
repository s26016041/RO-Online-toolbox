"""自動回連的**判斷邏輯**：現在到底該不該重連？

這裡只做決定，不碰遊戲、不送封包、不開行程 —— 所以可以完整測試。
呼叫端每拍餵三件事實進來（有沒有連線、本機網路在不在、現在幾點），
看回傳的狀態決定下一步。

## 三種「沒有連線」要分開處理

1. **你自己的網路斷了** —— 這時候關掉遊戲重開是**幫倒忙**：重開之後照樣連不上，
   而且你原本的角色被登出了。正確做法是**什麼都不做，等網路回來**。
   本機網卡的狀態不必發任何封包就看得出來（`network_up`）。
2. **遊戲斷線**（你的網路正常，但遊戲沒有連線）—— 這才是要重連的情況。
3. **伺服器維修** —— **目前分不出來**，所以不猜。重連失敗就**退避重試**
   （間隔一次比一次長）並且大聲回報，不會無腦一直重開遊戲。
   等真的遇到維修、看到實際徵兆之後再補上判斷。

## 為什麼要有「觀察期」

連線短暫消失是正常的：換地圖時伺服器會把連線移到另一台 map server
（[PKT-038]），那個瞬間 `find_server()` 就是 None。看到一次就重開遊戲，
等於每次換地圖都把自己踢掉。所以要**連續一段時間都沒有**才算斷線。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: 連線消失多久才算真的斷線。換地圖的過渡遠短於這個。
GRACE_SEC = 20.0
#: 重連失敗後的退避間隔（秒）。用完就一直用最後一個 —— **不會無腦一直試**。
BACKOFF_SEC = (30.0, 60.0, 120.0, 300.0, 600.0)

#: 回傳的狀態
OK = "ok"                       # 連線正常
NO_NETWORK = "no_network"       # 你的網路斷了 —— 等它回來，不要動遊戲
WATCHING = "watching"           # 連線不見了，但還在觀察期（可能只是換地圖）
RECONNECT = "reconnect"         # 該重連了
BACKOFF = "backoff"             # 剛試過失敗，等退避時間到


class ReconnectDecider:
    """每拍餵事實進來，告訴你現在該做什麼。"""

    def __init__(self, grace: float = GRACE_SEC) -> None:
        self._grace = grace
        self._lost_at: float | None = None   # 連線是什麼時候不見的
        self._failures = 0
        self._next_try = 0.0
        #: 給人看的一句話
        self.note = ""

    def reset(self) -> None:
        """連線回來了：把觀察期與退避通通歸零。"""
        self._lost_at = None
        self._failures = 0
        self._next_try = 0.0

    def note_attempt_failed(self, now: float) -> None:
        """重連試過了但沒成功 —— 下一次要等更久。"""
        index = min(self._failures, len(BACKOFF_SEC) - 1)
        wait = BACKOFF_SEC[index]
        self._failures += 1
        self._next_try = now + wait
        self._lost_at = None      # 重新開始觀察，不要一失敗就立刻再試
        self.note = f"重連失敗第 {self._failures} 次，{wait:.0f} 秒後再試"
        log.warning("%s", self.note)

    def decide(self, has_server: bool, network_up: bool, now: float) -> str:
        """回傳目前該做什麼。**這是唯一的決策入口。**"""
        if has_server:
            if self._lost_at is not None or self._failures:
                self.note = "連線正常"
            self.reset()
            return OK

        if not network_up:
            # ⚠ 你自己的網路斷了。這時候關遊戲重開是幫倒忙 ——
            # 重開照樣連不上，而且原本還在線上的角色被登出了。
            self._lost_at = None      # 網路回來之後才開始算觀察期
            self.note = "你的網路斷線了，等它回來（不會動遊戲）"
            return NO_NETWORK

        if now < self._next_try:
            left = self._next_try - now
            self.note = f"上次重連失敗，還要等 {left:.0f} 秒"
            return BACKOFF

        if self._lost_at is None:
            self._lost_at = now
            self.note = "偵測到連線消失，觀察中（換地圖時也會這樣）"
            return WATCHING

        if now - self._lost_at < self._grace:
            left = self._grace - (now - self._lost_at)
            self.note = f"連線消失中，再觀察 {left:.0f} 秒"
            return WATCHING

        self.note = "確認斷線，準備重連"
        return RECONNECT

    @property
    def failures(self) -> int:
        return self._failures
