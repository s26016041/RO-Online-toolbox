"""登入期間把遊戲鎖在最前面，並擋掉使用者的實體輸入。

## 為什麼要鎖

登入的前半段（合約書、帳密、OTP）只能用**前景輸入** —— 客戶端那幾個畫面
不吃 `PostMessage`。前景輸入有兩個致命的副作用：

1. 我們搶走焦點的瞬間，使用者正在打的字會**跑進遊戲裡**（實際發生過：
   使用者打的一串字直接餵進遊戲視窗）。
2. 使用者中途點別的視窗，遊戲失去焦點，後面的按鍵**全部打到別的地方**，
   自動登入從中間開始壞掉，而且看不出是哪一步壞的。

所以整段登入期間：把遊戲拉到最前面、把實體鍵鼠擋掉、結束時**一定**還原。

## ⚠ 不准用 `BlockInput`（踩過）

第一版用 `BlockInput(TRUE)` 去擋使用者的實體輸入。結果**連我們自己送的
`SendInput` 也被擋掉** —— 合約書連點 39 次完全沒反應，跟先前那個
「點不動」的症狀一模一樣，白白多繞一圈。

所以這支**只做焦點**：把遊戲拉到最前面、被搶走就搶回來、結束把焦點還回去。
使用者這幾秒不要點別的視窗就好。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

log = logging.getLogger(__name__)

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None

#: 看門狗的上限（秒）。整段登入正常是十幾秒；超過這個數一定是卡住了，
#: 那就寧可放開輸入讓使用者拿回控制權，也不要把鍵鼠鎖著不放。
_MAX_LOCK_SECONDS = 120.0

#: 重新確認前景的間隔（秒）。不是「等」，是**盯著**：發現被搶走就立刻搶回來。
_REASSERT_INTERVAL = 0.25


class LoginLock:
    """登入期間的前景鎖。用 `with` 包住整段需要前景輸入的流程。

    ⚠ 只包**需要前景**的那一段（合約書→帳密→OTP）。
    走封包的部分不需要鎖，也不該鎖。
    """

    def __init__(self, hwnd: int, on_note=None) -> None:
        self._hwnd = hwnd
        self._note = on_note or (lambda _text: None)
        self._previous = 0
        self._stop = threading.Event()
        self._guard: threading.Thread | None = None
        #: 看門狗的到期時間。`wait_for_user()` 會把它往後推 ——
        #: 「在等人動手」不該被算成「程式卡住」。
        self._deadline = 0.0
        self._deadline_lock = threading.Lock()

    # ---- 對外 -------------------------------------------------------

    def __enter__(self) -> LoginLock:
        if _user32 is None:
            return self
        self._previous = _user32.GetForegroundWindow()
        self.reassert()
        self._note("已把遊戲拉到最前面（登入這幾秒請先不要點別的視窗）")
        self._guard = threading.Thread(target=self._watch, daemon=True)
        self._guard.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def reassert(self) -> bool:
        """確認遊戲還在最前面；不在就搶回來。回傳現在是不是前景。

        每個輸入步驟之前都要呼叫 —— 焦點跑掉還照打，字就餵到別人的視窗去了。
        """
        if _user32 is None:
            return False
        if _user32.GetForegroundWindow() == self._hwnd:
            return True
        from ro_toolbox.services import input as game_input

        return game_input.focus_window(self._hwnd, 2.0)

    def release(self) -> None:
        """解除鎖定並把焦點還給使用者原本的視窗。呼叫幾次都安全。"""
        self._stop.set()
        if _user32 is None:
            return
        if self._previous and _user32.IsWindow(self._previous):
            # 把焦點還回去 —— 使用者原本在做的事不該被我們吃掉。
            _user32.SetForegroundWindow(self._previous)
        self._previous = 0

    # ---- 內部 -------------------------------------------------------

    def wait_for_user(self, seconds: float) -> None:
        """接下來這幾秒是**在等人動手**，不要算進看門狗的預算。

        ⚠ 這不是「延長鎖」的萬用後門，只給「請你手動按一次同意」那一段用：
        那段時間程式什麼都沒做、就是在等人，把它算成「卡住」是錯的。
        實機踩過：等人按合約等了 61 秒 → 回來重試 17 次 → 120 秒到了，
        看門狗把前景放掉 → 接著每一次輸入都 `PostMessage` 失敗，
        整個自動登入就死在「送不進視窗訊息」（使用者實測回報）。
        """
        with self._deadline_lock:
            self._deadline += max(0.0, seconds)

    def _watch(self) -> None:
        """看門狗：盯著前景，並在逾時後強制解除。

        ⚠ 這條執行緒的存在理由只有一個：**不准把使用者的鍵鼠鎖著不放。**
        主流程當掉、例外沒接到、忘了 release —— 都由它兜底。
        """
        with self._deadline_lock:
            self._deadline = time.monotonic() + _MAX_LOCK_SECONDS
        while not self._stop.wait(_REASSERT_INTERVAL):
            with self._deadline_lock:
                deadline = self._deadline
            if time.monotonic() >= deadline:
                log.warning(
                    "登入鎖超過 %.0f 秒還沒解除，強制放開輸入", _MAX_LOCK_SECONDS
                )
                self.release()
                return
            # ⚠ **不要在這裡搶前景。** 早期版本每 0.25 秒就 AttachThreadInput +
            # SetForegroundWindow 一次，結果跟負責點擊的子行程互相打架，
            # 點下去的那一下常常沒生效。焦點只在**每個輸入步驟之前**確認一次
            # （`reassert()` 由呼叫端主動叫），這裡只負責逾時兜底。
