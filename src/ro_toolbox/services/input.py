"""把輸入送進遊戲。**兩種通道，用途完全不同，不要混用。**

## 為什麼有兩種

RO 客戶端的兩個畫面對輸入的處理不一樣（實測，[INP-001]）：

| 畫面 | `PostMessage` | 需要前景？ |
|---|---|---|
| 使用者合約書 | ❌ 完全無效（鍵盤、滑鼠都是） | ✅ 只能 `SendInput` |
| 登入畫面 | ✅ **有效** | ❌ **不需要** |

所以：

- `click_foreground()` —— **只給合約書用**。會搶前景、動到實體滑鼠游標，
  是整個自動登入唯一會打擾使用者的一步（約一秒）。
- `type_background()` / `press_background()` —— 給登入畫面用。
  遊戲視窗可以被別的視窗蓋住，**不占鍵盤滑鼠**。

## 兩個一定要記得的限制

1. **視窗不能最小化。** 最小化的視窗完全不處理 `PostMessage`（實測），
   而且 `GetWindowRect` 會回負座標的縮圖矩形。送之前先問 `game_screen.is_minimised`。
2. **DPI-aware 要在最前面宣告。** `click_foreground` 用的是螢幕絕對座標；
   行程若不是 DPI-aware，或中途才改，座標會前後不一致
   （踩過：要求 (1495,864) 實際落在 (667,1088)）。用 `ensure_dpi_aware()`，
   而且要在**匯入任何視窗相關模組之前**呼叫。
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

try:
    import win32api
    import win32con
    import win32gui
except ImportError:  # pragma: no cover - 取決於安裝方式
    win32gui = None

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None

#: 專門用來呼叫 SendInput 的 handle —— 要拿得到 GetLastError。
#: `ctypes.windll` 那份不會保存 last error，失敗時只能得到「不知道為什麼」。
_user32_err = (
    ctypes.WinDLL("user32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None
)

#: SendInput 失敗常見錯誤碼的人話。
_SEND_INPUT_ERRORS = {
    5: "存取被拒（ERROR_ACCESS_DENIED）—— 前景視窗的權限比本程式高，"
       "多半要以系統管理員身分執行工具箱",
    87: "參數錯誤（ERROR_INVALID_PARAMETER）",
}

# ---- SendInput 用的結構 -----------------------------------------------------

_INPUT_MOUSE = 0
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_ABSOLUTE = 0x8000


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


class InputError(RuntimeError):
    """送不進去。訊息是要直接給使用者看的。"""


def available() -> bool:
    return win32gui is not None and _user32 is not None


def ensure_dpi_aware() -> bool:
    """宣告本行程是 DPI-aware。**必須在碰任何視窗 API 之前呼叫。**

    不宣告的話 `GetWindowRect` 拿到的是縮放過的邏輯座標，而 `SendInput`
    用的是實體像素 —— 兩者不一致，點擊會落在別的地方。
    中途才改更糟：前後兩套座標系混用（踩過）。
    """
    if _user32 is None:
        return False
    try:
        # -4 = PER_MONITOR_AWARE_V2
        return bool(_user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except (AttributeError, OSError) as exc:
        log.warning("宣告 DPI-aware 失敗，座標可能不準：%s", exc)
        return False


# ---- 背景通道（登入畫面用） -------------------------------------------------


def _post(hwnd: int, message: int, wparam: int, lparam: int) -> None:
    """送一個視窗訊息，失敗就講清楚。

    `PostMessage` 回 FALSE（pywin32 會丟例外）通常代表：視窗已經不在了，
    或對方的訊息佇列滿了。原始例外訊息是「No error message is available」，
    對使用者完全沒有幫助 —— 包成看得懂的話。
    """
    if not win32gui.IsWindow(hwnd):
        raise InputError("遊戲視窗已經不在了（遊戲關掉了？）。")
    try:
        win32gui.PostMessage(hwnd, message, wparam, lparam)
    except Exception as exc:  # noqa: BLE001 - pywintypes.error 的型別不保證
        raise InputError(
            f"送不進視窗訊息（{exc}）。遊戲可能正忙或已經關閉。"
        ) from exc


def type_background(hwnd: int, text: str, delay: float = 0.02) -> None:
    """對視窗打字，**不搶前景、不動實體鍵盤**。

    ⚠ **一個字元只送一個 `WM_CHAR`。**
    早期版本三個訊息一組（KEYDOWN / CHAR / KEYUP），理由是「有些輸入框只收
    WM_CHAR 以外的」。RO 客戶端**三個都算一次輸入** —— 打 `s26016041`
    送出去的帳號變成 `s22266600011166600044411`（每個字重複三次），
    而且要等伺服器回登入失敗才會發現（實測踩過）。

    ⚠ **不要把間隔調到太小。** 客戶端自己的訊息迴圈跟不上就會掉字，
    而且掉得很安靜：打 `s26016041` 送出去變成 `s26011034`
    （間隔 0.01 秒時實測）—— 要等伺服器回登入失敗才會發現。
    """
    if not available():
        raise InputError("缺少 pywin32，無法送輸入。")
    for char in text:
        _post(hwnd, win32con.WM_CHAR, ord(char), 0)
        if delay:
            time.sleep(delay)


def press_background(hwnd: int, key: int, char: int | None = None) -> None:
    """按一個鍵（例如 Enter、Tab），背景有效。

    `char` 是這個鍵對應的字元碼；Enter 要送 13，否則有些輸入框不會當成送出。
    """
    if not available():
        raise InputError("缺少 pywin32，無法送輸入。")
    # ⚠ **功能鍵要送完整的一組**（KEYDOWN → CHAR → KEYUP）。
    # 只送 KEYDOWN 的話 Enter 不會觸發送出 —— 帳密打對了卻永遠不送出，
    # 症狀是「客戶端沒反應」，看起來像字沒進去（實測踩過）。
    #
    # 文字則相反：一個字元只送一個 `WM_CHAR`（見 `type_background`），
    # 三個都送會讓每個字重複三次。兩者不一樣，不要統一。
    _post(hwnd, win32con.WM_KEYDOWN, key, 0)
    time.sleep(0.01)
    if char is not None:
        _post(hwnd, win32con.WM_CHAR, char, 0)
        time.sleep(0.01)
    _post(hwnd, win32con.WM_KEYUP, key, 0)


def press_enter_background(hwnd: int) -> None:
    press_background(hwnd, win32con.VK_RETURN, 13)


def press_tab_background(hwnd: int) -> None:
    press_background(hwnd, win32con.VK_TAB, 9)


# ---- 前景通道（**只給合約書用**） -------------------------------------------


def click_foreground(hwnd: int, x: int, y: int, settle: float = 2.0) -> None:
    """把視窗拉到前景，用 `SendInput` 點一下螢幕座標 (x, y)。

    ⚠ **這是整個自動登入唯一會打擾使用者的一步。** 它會搶前景、把實體滑鼠
    游標移過去。只在合約書那一關用 —— 那個畫面不吃 `PostMessage`（[INP-001]）。

    座標是**螢幕絕對座標**，呼叫端要先 `ensure_dpi_aware()`。
    """
    if not available():
        raise InputError("缺少 pywin32，無法送輸入。")
    if not focus_window(hwnd, settle):
        # ⚠ **搶不到前景就絕對不能點。** `SendInput` 點的是螢幕座標，
        # 遊戲不在最上面的話那一下會落在使用者正在用的視窗上 ——
        # 可能按到別的程式的按鈕。寧可失敗也不要亂點。
        raise InputError(
            "沒辦法把遊戲視窗帶到前景，已放棄點擊（避免點到你正在用的視窗）。"
            "請把遊戲視窗點一下再試。"
        )

    width = _user32.GetSystemMetrics(0)
    height = _user32.GetSystemMetrics(1)
    if width <= 1 or height <= 1:
        raise InputError("取不到螢幕尺寸。")
    absolute_x = int(x * 65535 / (width - 1))
    absolute_y = int(y * 65535 / (height - 1))

    # ⚠ **游標移過去之後要停一下再按。**
    # 遊戲的 UI 是自己畫的：按鈕要先被「滑過」才會進入可按狀態。
    # 早期版本三個事件都間隔 0.06 秒，結果游標明明停在「同意」上面卻按不掉
    # （使用者親眼看到），而且時好時壞 —— 快的時候整輪自動登入就白做。
    # 實測 MOVE → 0.25s → DOWN → 0.10s → UP 一次就過。
    for flags, pause in (
        (_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, 0.25),
        (_MOUSEEVENTF_LEFTDOWN, 0.10),
        (_MOUSEEVENTF_LEFTUP, 0.05),
    ):
        event = _Input(
            _INPUT_MOUSE,
            _InputUnion(_MouseInput(absolute_x, absolute_y, 0, flags, 0, None)),
        )
        _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input))
        time.sleep(pause)


def _send(*inputs: _Input) -> None:
    """一次把整批事件送進系統輸入佇列。

    ⚠ 一次送一整批（而不是逐個呼叫）是刻意的：中間不會被別的輸入插隊。
    """
    if not inputs:
        return
    array = (_Input * len(inputs))(*inputs)
    sender = _user32_err or _user32
    sent = sender.SendInput(len(inputs), array, ctypes.sizeof(_Input))
    if sent != len(inputs):
        code = ctypes.get_last_error() if _user32_err else 0
        why = _SEND_INPUT_ERRORS.get(code, f"錯誤碼 {code}")
        raise InputError(
            f"送出鍵盤事件失敗（只送出 {sent}/{len(inputs)} 個）：{why}"
        )


def _key_event(code: int, unicode_char: bool, key_up: bool) -> _Input:
    flags = (_KEYEVENTF_UNICODE if unicode_char else 0) | (
        _KEYEVENTF_KEYUP if key_up else 0
    )
    key = _KeyboardInput(
        wVk=0 if unicode_char else code,
        wScan=code if unicode_char else 0,
        dwFlags=flags,
        time=0,
        dwExtraInfo=None,
    )
    return _Input(_INPUT_KEYBOARD, _InputUnion(ki=key))


def type_foreground(text: str) -> None:
    """對**目前的前景視窗**打字（Unicode 直送，不看鍵盤配置）。

    ⚠ 送到哪裡取決於「誰是前景」—— 呼叫端必須先確認遊戲在最前面
    （用 `LoginLock.reassert()`），否則這些字會打進使用者正在用的視窗。

    為什麼用 `KEYEVENTF_UNICODE` 而不是虛擬鍵碼：不受使用者的鍵盤配置
    （注音／英數／Dvorak）影響，打什麼進去就是什麼。
    """
    if not available():
        raise InputError("缺少 pywin32，無法送輸入。")
    events: list[_Input] = []
    for char in text:
        code = ord(char)
        events.append(_key_event(code, unicode_char=True, key_up=False))
        events.append(_key_event(code, unicode_char=True, key_up=True))
    _send(*events)


def press_foreground(virtual_key: int) -> None:
    """對前景視窗按一個功能鍵（Enter、Tab、Home、Delete…）。

    功能鍵沒有對應的 Unicode 字元，所以走虛擬鍵碼。
    """
    if not available():
        raise InputError("缺少 pywin32，無法送輸入。")
    _send(
        _key_event(virtual_key, unicode_char=False, key_up=False),
        _key_event(virtual_key, unicode_char=False, key_up=True),
    )


def focus_window(hwnd: int, settle: float = 0.7) -> bool:
    """把視窗帶到前景。成功回 True。

    Windows 會擋掉背景行程直接呼叫 `SetForegroundWindow`（防止程式亂搶焦點）。
    標準解法是先用 `AttachThreadInput` 把自己的輸入佇列接到目前前景那條執行緒上，
    這樣系統就當我們有資格換前景。用完要記得解開。

    **一定要驗證結果**再往下做 —— 早期版本沒驗證，搶不到前景還是照樣點下去，
    那一下就落在使用者正在用的視窗上。
    """
    if not available():
        return False

    # ⚠ **已經是前景就什麼都不要做，直接回去。**
    # 早期版本無條件先 `ShowWindow(SW_RESTORE)` 再判斷 —— 重新啟動視窗會把
    # 客戶端**欄位的焦點重設回第一格**：帳號打好、Tab 到密碼欄之後，
    # 下一批輸入前又呼叫一次這支，焦點被打回帳號欄，密碼就蓋掉帳號，
    # 送出去變成「帳號＝密碼」，伺服器回「帳密錯誤」（實測踩過）。
    current = _user32.GetForegroundWindow()
    if current == hwnd:
        return True

    _user32.ShowWindow(hwnd, 9)              # SW_RESTORE

    kernel32 = ctypes.windll.kernel32
    mine = kernel32.GetCurrentThreadId()
    theirs = _user32.GetWindowThreadProcessId(current, None)
    attached = bool(_user32.AttachThreadInput(mine, theirs, True)) if theirs else False
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(mine, theirs, False)

    # ⚠ 不是「睡 settle 秒然後假設成功」——**輪詢到真的變前景為止**
    #（CLAUDE.md 硬規則：等訊號，不等時間）。settle 只是放棄的上限。
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        if _user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return bool(_user32.GetForegroundWindow() == hwnd)


def is_foreground(hwnd: int) -> bool:
    """這個視窗現在是不是前景。用來驗證「背景」那條路真的在背景。"""
    return bool(_user32 and _user32.GetForegroundWindow() == hwnd)


def screen_position(hwnd: int, ratio_x: float, ratio_y: float) -> tuple[int, int]:
    """把「相對視窗的比例」換成螢幕座標。

    用比例而不是寫死像素：視窗大小、解析度、DPI 都可能不一樣。
    """
    if not available():
        raise InputError("缺少 pywin32。")
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return left + int((right - left) * ratio_x), top + int((bottom - top) * ratio_y)


def client_position(hwnd: int, ratio_x: float, ratio_y: float) -> int:
    """把比例換成 `PostMessage` 用的**客戶區**座標（打包成 lParam）。"""
    if not available():
        raise InputError("缺少 pywin32。")
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    return win32api.MAKELONG(
        int((right - left) * ratio_x), int((bottom - top) * ratio_y)
    )
