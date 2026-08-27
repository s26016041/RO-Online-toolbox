"""列出目前可見的視窗，用來挑注入目標。

用視窗標題挑遊戲比用 PID 直覺，尤其開多開分身的時候。
pywin32 屬於選用相依，沒裝時回傳空清單而不是讓程式炸掉。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

try:
    import win32gui
    import win32process
except ImportError:  # pragma: no cover - 取決於安裝方式
    win32gui = None
    win32process = None

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

log = logging.getLogger(__name__)

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    process_name: str

    @property
    def label(self) -> str:
        return f"{self.title}　—　{self.process_name} (PID {self.pid})"


def available() -> bool:
    return win32gui is not None and win32process is not None


def window_pid(hwnd: int) -> int | None:
    """這個視窗屬於哪個行程。**讀不到回 None，不要回 0 假裝是答案。**

    這是全專案唯一一份「視窗 → 行程」的實作，其他地方一律呼叫它
    （CLAUDE.md：同一件事不准在第二個地方再寫一次）。

    一律走 ctypes 直接打 Win32，**不准用 `win32process.GetWindowThreadProcessId`**。
    實測 2026-08-25（GAMEDATA [INP-007]）：在「自己剛剛啟動遊戲」的那個行程裡，
    遊戲視窗剛畫出來的那幾秒，pywin32 版本回傳的 pid 是 **0**，而 ctypes 版本
    在**同一瞬間**回傳正確的 49356；換一個沒啟動過遊戲的行程再問，pywin32 又對了。
    pid 讀成 0 就永遠比對不中 → `find_window` 一直回 None → 自動登入卡在
    「等遊戲視窗」直到逾時（實際卡滿 300 秒，而視窗 9.7 秒就出現了），
    而且是**安靜地卡**，看起來像遊戲開很慢。
    """
    if _user32 is None:
        return None
    owner = ctypes.c_ulong(0)
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner))
    return owner.value or None


def enumerate_windows(title_contains: str = "") -> list[WindowInfo]:
    """列出有標題的可見視窗，依行程名稱與標題排序。"""
    if not available():
        log.warning("pywin32 未安裝，無法列舉視窗")
        return []

    keyword = title_contains.strip().lower()
    found: list[WindowInfo] = []

    def collect(hwnd: int, _param) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if keyword and keyword not in title.lower():
            return True

        pid = window_pid(hwnd)
        if pid is None:
            return True

        found.append(
            WindowInfo(
                hwnd=hwnd,
                pid=pid,
                title=title,
                process_name=_process_name(pid),
            )
        )
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception as exc:  # noqa: BLE001
        log.error("列舉視窗失敗：%s", exc)
        return []

    found.sort(key=lambda w: (w.process_name.lower(), w.title.lower()))
    return found


def _process_name(pid: int) -> str:
    if psutil is None:
        return f"pid-{pid}"
    try:
        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001 - 行程可能已結束或無權限
        return f"pid-{pid}"
