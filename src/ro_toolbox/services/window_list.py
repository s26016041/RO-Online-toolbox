"""列出目前可見的視窗，用來挑注入目標。

用視窗標題挑遊戲比用 PID 直覺，尤其開多開分身的時候。
pywin32 屬於選用相依，沒裝時回傳空清單而不是讓程式炸掉。
"""

from __future__ import annotations

import logging
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

        try:
            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001 - 視窗可能剛好關掉
            return True
        if not pid:
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
