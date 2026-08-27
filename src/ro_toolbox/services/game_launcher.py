"""把遊戲叫起來。

事實見 GAMEDATA [PKT-047]，都是實機量出來的：

- `Ragexe.exe` **不帶參數會失敗** —— 兩秒後跳一個空的 `Error` 對話框。
  正確的命令列是 `Ragexe.exe 1rag1`（攔截行程建立抓到的，不是照 RO 通例猜的）。
- 啟動器 `Ragnarok.exe` 的「開始遊戲」按鈕**吃 PostMessage**，可以背景點。
  （天使之戀那款的登入機按鈕沒有 hwnd、背景點無效，兩款不一樣，別套用。）

## 兩條路怎麼選

預設走**啟動器**：它會先做版本更新檢查，官方改版時不會用舊檔連上去。
直接開 `Ragexe.exe 1rag1` 比較快，但**跳過更新檢查** —— 只在使用者明確
要求時才走這條。

## 找按鈕不准寫死座標

視窗會被拖動，版面也可能改。「開始遊戲」用**規則**現找：
在啟動器的 `AfxWnd140s` 子控制項裡，取「位在右下半部、面積最大」的那個。
實測四個控制項中它面積 6,649，次大的只有 45%，分得開。
**找不到唯一候選就停手**，不亂按 —— 按錯可能按到設定或關閉。
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ro_toolbox.services import window_list

try:
    import psutil
except ImportError:  # pragma: no cover - 取決於安裝方式
    psutil = None

try:
    import win32api
    import win32con
    import win32gui
except ImportError:  # pragma: no cover
    win32gui = None

log = logging.getLogger(__name__)

LAUNCHER_EXE = "Ragnarok.exe"
GAME_EXE = "Ragexe.exe"
#: 客戶端要的啟動參數。少了它會跳 Error 對話框（[PKT-047]）。
GAME_ARG = "1rag1"
_BUTTON_CLASS = "AfxWnd140s"
#: 次大候選超過最大者這個比例就視為分不出來，停手不按。
_AMBIGUOUS_RATIO = 0.6


class LaunchError(RuntimeError):
    """啟動失敗。訊息是要直接給使用者看的。"""


@dataclass(frozen=True, slots=True)
class GamePaths:
    """從使用者設定的那一個路徑推出其他東西。

    使用者設的是啟動器（`…\\Ragnarok.exe`），遊戲本體與工作目錄都在同一層。
    """

    launcher: Path

    @property
    def directory(self) -> Path:
        return self.launcher.parent

    @property
    def game(self) -> Path:
        return self.directory / GAME_EXE

    def problem(self) -> str:
        """檢查路徑，回一句人話；沒問題回空字串。"""
        if not self.launcher.name:
            return "還沒設定遊戲路徑。"
        if not self.launcher.exists():
            return f"找不到檔案：{self.launcher}"
        if self.launcher.suffix.lower() != ".exe":
            return f"這不是執行檔：{self.launcher.name}"
        if not self.game.exists():
            return f"同一層找不到 {GAME_EXE}（{self.directory}）—— 路徑選錯了？"
        return ""


def _require_deps() -> None:
    if psutil is None:
        raise LaunchError("缺少 psutil，無法管理遊戲行程。")
    if win32gui is None:
        raise LaunchError("缺少 pywin32，無法操作啟動器視窗。")


def pids_of(name: str) -> list[int]:
    if psutil is None:
        return []
    target = name.lower()
    return [
        p.info["pid"]
        for p in psutil.process_iter(["pid", "name"])
        if (p.info["name"] or "").lower() == target
    ]


def game_pids() -> list[int]:
    return pids_of(GAME_EXE)


# ---- 直接開遊戲 -------------------------------------------------------------


def launch_game_directly(paths: GamePaths) -> int:
    """跳過啟動器，直接開 `Ragexe.exe 1rag1`。回新行程的 PID。

    ⚠ 這條**不會跑版本更新檢查**。官方改版之後要自己開一次啟動器更新，
    否則會拿舊客戶端連上去。
    """
    _require_deps()
    problem = paths.problem()
    if problem:
        raise LaunchError(problem)

    before = set(game_pids())
    proc = subprocess.Popen(
        [str(paths.game), GAME_ARG], cwd=str(paths.directory)
    )
    log.info("直接啟動遊戲：%s %s（PID %s）", paths.game, GAME_ARG, proc.pid)
    return _wait_for_new_game(before, timeout=30.0, fallback=proc.pid)


# ---- 走啟動器 ---------------------------------------------------------------


def launch_launcher(paths: GamePaths) -> int:
    """開啟動器（已經開著就沿用）。回啟動器的 PID。"""
    _require_deps()
    problem = paths.problem()
    if problem:
        raise LaunchError(problem)

    existing = pids_of(LAUNCHER_EXE)
    if existing:
        log.info("啟動器已經在跑：PID %s", existing[0])
        return existing[0]

    subprocess.Popen([str(paths.launcher)], cwd=str(paths.directory))
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        time.sleep(0.4)
        found = pids_of(LAUNCHER_EXE)
        if found:
            log.info("啟動器起來了：PID %s", found[0])
            return found[0]
    raise LaunchError("啟動器開了但沒有出現行程，可能被安全軟體擋掉。")


def find_launcher_window(pid: int) -> int | None:
    """啟動器的主視窗 hwnd。"""
    _require_deps()
    found: list[int] = []

    def visit(hwnd, _):
        # ⚠ 這裡**一律回 True**。回 False 會讓 EnumWindows 傳回 FALSE，
        # pywin32 把它當成錯誤拋出來，看起來像「找不到視窗」其實是找到了。
        owner = window_list.window_pid(hwnd)
        if owner == pid and win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def find_start_button(window: int) -> int | None:
    """「開始遊戲」的 hwnd。分不出來就回 None（呼叫端要停手，不准亂按）。"""
    _require_deps()
    left, top, right, bottom = win32gui.GetWindowRect(window)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    kids: list[tuple[int, int, int, int]] = []   # (hwnd, 相對x, 相對y, 面積)

    def visit(hwnd, _):
        if win32gui.GetClassName(hwnd) == _BUTTON_CLASS and win32gui.IsWindowVisible(hwnd):
            kl, kt, kr, kb = win32gui.GetWindowRect(hwnd)
            kids.append((hwnd, kl - left, kt - top, (kr - kl) * (kb - kt)))
        return True

    win32gui.EnumChildWindows(window, visit, None)

    candidates = [k for k in kids if k[1] > width * 0.5 and k[2] > height * 0.5]
    candidates.sort(key=lambda k: -k[3])
    if not candidates:
        log.warning("啟動器右下半部找不到按鈕候選（共 %d 個控制項）", len(kids))
        return None
    if len(candidates) > 1 and candidates[1][3] > candidates[0][3] * _AMBIGUOUS_RATIO:
        log.warning("啟動器有兩個大小相近的按鈕候選，分不出哪個是開始遊戲")
        return None
    return candidates[0][0]


def click(hwnd: int) -> None:
    """對控制項中心送一次左鍵。背景也有效，不會搶使用者的滑鼠。"""
    _require_deps()
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    point = win32api.MAKELONG((right - left) // 2, (bottom - top) // 2)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, point)
    time.sleep(0.08)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, point)


def launch_via_launcher(paths: GamePaths, wait: float = 60.0) -> int:
    """開啟動器 → 按「開始遊戲」→ 回新遊戲行程的 PID。"""
    _require_deps()
    before = set(game_pids())
    pid = launch_launcher(paths)

    window = None
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        window = find_launcher_window(pid)
        if window is not None:
            break
        time.sleep(0.4)
    if window is None:
        raise LaunchError("啟動器沒有畫出視窗。")

    # 視窗剛出現時按鈕可能還沒建立，等它長齊。
    button = None
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        button = find_start_button(window)
        if button is not None:
            break
        time.sleep(0.4)
    if button is None:
        raise LaunchError(
            "在啟動器上認不出「開始遊戲」按鈕，已停手不亂按。"
            "啟動器版面可能改了，需要重新確認（見 GAMEDATA [PKT-047]）。"
        )

    click(button)
    log.info("已按下啟動器的開始遊戲")
    return _wait_for_new_game(before, timeout=wait)


def _wait_for_new_game(before: set[int], timeout: float, fallback: int | None = None) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fresh = [pid for pid in game_pids() if pid not in before]
        if fresh:
            log.info("遊戲行程 %s", fresh[0])
            return fresh[0]
        time.sleep(0.3)
    if fallback is not None and psutil is not None and psutil.pid_exists(fallback):
        return fallback
    raise LaunchError(f"等了 {timeout:.0f} 秒，遊戲行程沒有出現。")
