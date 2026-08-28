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

## 觀察期在防什麼（**不是換地圖**）

⚠ 舊註解寫著「換地圖時連線會短暫消失，所以要等」。**那是錯的**，
而且錯了很久 —— [PKT-063] 實機量過換圖那一刻的連線表：

    22:33:50  prt_fild05     只有 219.84.200.102:10022
    22:34:03  換到 prontera  .101:10010（新）＋ .102:10022（舊，還在）
    22:46:00  舊的過了 11 分鐘才收掉

**換圖是兩條並存，不是零條**（新的先接起來，舊的很久才收）。
所以換地圖根本不會讓 `find_server()` 變 None，`find_server()` 甚至還得
靠建立時間去挑哪一條才是新的。同一張圖內的傳送更不會動到 socket。

那觀察期還留著幹嘛？只為了一件事：**重連會關掉使用者正在玩的遊戲，
不可逆，不能只憑一拍的讀數就做。** `find_server()` 讀的是 Windows TCP 表的
快照，任何單一次取樣都可能因為讀取失敗或時序落差給出假的 None。
所以要求**連續幾拍都是 None**——那是取樣穩健性，不是換圖保護。

也因此觀察期只需要「幾拍」，不需要幾十秒：整條回連（關遊戲→重開→
重新登入）本來就要三十秒級，前面等 5 秒在體感上等於即時。

## ⛔ 走過但拿掉了：從畫面認「與伺服器斷線」對話框

2026-08-28 做過一版：斷線時遊戲會跳一個自己畫的訊息框，用樣板比對認出來
就能當場確認、跳過觀察期（做法、門檻與兩條死路都記在 GAMEDATA [INP-012]）。
**觀察期從 20 秒縮到 5 秒之後它就不划算了** —— 省下的時間沒了，
只剩下每 3 秒 160 ms 的畫面比對與一整套樣板要維護。已移除。
要是哪天需要第二個獨立來源（例如終於要分辨伺服器維修），[INP-012] 有全部細節。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: 連線消失多久才算真的斷線。
#:
#: ⚠ 這**不是**在等換地圖（換圖不會讓連線表變空，見檔頭與 [PKT-063]），
#: 是在避免「只憑一拍的 TCP 讀數就把遊戲關掉重開」。呼叫端每秒取樣一次，
#: 所以 5 秒 ≈ 連續 5 拍都沒有連線才動手。
GRACE_SEC = 5.0
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
            self.note = "偵測到連線消失，再確認幾拍"
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
