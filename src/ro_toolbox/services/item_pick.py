"""「在遊戲裡點一下道具，程式認出它是什麼」。

給撿取黑名單用（使用者 2026-09-04：「我想要我在遊戲右鍵或左鍵物品，
程式可以識別，然後加入黑名單」，隨後改口「**改成右鍵，不要左鍵**」）。

⚠ **只認右鍵。** 左鍵在 RO 背包裡是拿起／拖曳／點按鈕，等黑名單的時候
順手左鍵一下就會被吃掉，變成「莫名其妙多了一樣不撿的東西」——
而黑名單沒有開關，錯加的那一樣會安靜地一直生效。

## ⛔ 先講不能做的那條路

**不准去攔遊戲自己的滑鼠事件。** 那要 hook 客戶端的 UI，就是注入／寫記憶體，
GameGuard 會讓遊戲當機或封號（[PKT-011]、[PKT-013]）。而且右鍵道具開的是
**客戶端自己的說明視窗**，網路上一個封包都不會送 —— 擷取那條線也看不到。

## 這裡怎麼做（完全不碰遊戲行程）

1. **我們自己看滑鼠**：`GetAsyncKeyState` 的**按下緣** ＋ `GetCursorPos`。
   這跑在我們的程式裡，跟遊戲行程一點關係都沒有
   （同一招 `auto_login` 學「同意」按鈕已經在用）。
2. 按下去的點落在遊戲視窗裡 → `game_screen.capture()` 抓那一瞬間的畫面。
3. **候選只有「背包裡真的有的東西」**（`services/bag.py` 從記憶體讀）——
   通常幾十樣，不是兩萬樣。
4. 拿那幾十張 24×24 圖示（`assets/icons.bin`）去比對他點的那一格的**像素**。
   對上了才回答。

⚠ 比對只看圖示裡**不是洋紅**的像素：洋紅是 RO 的透明色，那些位置畫的是
背包視窗的底，不是道具本身。

⚠ **對不上就回空的**，不准挑一個最像的湊數 —— 加錯一樣道具的後果是
「從此再也不撿它」，而且完全不會有人發現（CLAUDE.md：安靜地做錯事一律當 bug）。

⚠ **好幾樣道具共用同一張圖示是常態**（卡片、各種礦石）。分不出來的時候
把候選**全部回傳**讓呼叫端問人，不准自己挑第一個。
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass, field

from ro_toolbox.services import game_screen, icons

log = logging.getLogger(__name__)

#: 道具圖示的邊長（`assets/icons.bin` 全部都是 24×24，見 `tools/build_icons.py`）。
ICON_SIDE = 24
#: RO 的圖示用洋紅當透明色（實測 501 的左上角像素就是 #ff00ff）。
#: 這些位置畫的是背包視窗的底 —— 比對時要整個略過。
TRANSPARENT = (255, 0, 255)
#: 單一像素允許差多少（0~255）。抓圖經過 GDI，跟原圖不會 byte 對 byte 相等；
#: 但這是**同一份點陣圖畫到螢幕上**，差距只會來自捨入，給 24 已經很鬆。
_PIXEL_TOLERANCE = 24
#: 要有這個比例的像素對得上才算「認出來了」。
#:
#: ⚠ 不能訂太低：背包裡幾十樣東西的圖示彼此很像（同一系列的礦石、卡片背面），
#: 門檻鬆一點就會**很有自信地認錯**。寧可回「認不出來」叫他再點一次。
_MATCH_RATIO = 0.92
#: 比最佳分數低這麼多以內的，一律當成「分不出來」一起回傳。
#: 同一張圖示的兩樣道具分數會一模一樣，這個餘裕是給抓圖雜訊的。
_TIE_MARGIN = 0.02

#: 只看右鍵（`VK_RBUTTON`）。使用者指定「改成右鍵，不要左鍵」——
#: 左鍵在背包裡是拿起／拖曳，等的期間隨手一點就會誤加。
_VK_RBUTTON = 0x02
#: 監看滑鼠的取樣間隔。按一下最短也有幾十毫秒，25ms 不會漏。
_POLL = 0.025


@dataclass(frozen=True, slots=True)
class Pick:
    """使用者點了一下的結果。

    `items` 是**所有對得上的道具編號**：
    - 空的 = 認不出來（呼叫端要說一聲，不准亂猜）
    - 一個 = 就是它
    - 兩個以上 = 好幾樣共用同一張圖示，**要問人**
    """

    items: tuple[int, ...] = ()
    score: float = 0.0
    #: 認不出來時要顯示給使用者的原因（要看得懂，不是內部訊息）。
    why: str = ""
    #: 診斷用：分數最高的前幾名 `(道具編號, 分數)`。
    ranked: tuple[tuple[int, float], ...] = field(default=())


# ---- 圖片 → 陣列 ------------------------------------------------------------


def _rgb_array(image):
    """QImage → (高, 寬, 3) 的 uint8 陣列。**回傳的是自己的一份拷貝。**

    ⚠⚠ **一定要 `.copy()`，不能回傳 view。** `convertToFormat()` 產生的是
    一個**這個函式裡的暫時 QImage**；`np.frombuffer()` 只是借它的記憶體，
    函式一 return 那張圖就被釋放 —— 之後每一次讀都是 use-after-free。
    症狀是**整個程式當場閃退**（2026-09-04 使用者實機：在遊戲裡按右鍵，
    工具直接消失，連日誌都來不及寫）。它在測試裡看起來是好的，
    因為那塊記憶體還沒被拿去用。

    ⚠ QImage 每一列會補到 4 byte 對齊（`bytesPerLine` > `width * 3`）。
    不把那段 padding 切掉的話整張圖會每列往右斜一點點，比對就永遠對不上 ——
    而且**不會報錯**，只會一直說「認不出來」（`services/qr` 踩過同一個坑）。
    """
    import numpy as np
    from PySide6.QtGui import QImage

    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    height, width, stride = rgb.height(), rgb.width(), rgb.bytesPerLine()
    flat = np.frombuffer(rgb.constBits(), dtype=np.uint8, count=rgb.sizeInBytes())
    return flat.reshape(height, stride)[:, : width * 3].reshape(
        height, width, 3
    ).copy()


def icon_array(item_id: int):
    """一張道具圖示的 `(24, 24, 3)` 陣列 ＋ 「這個像素算不算數」的遮罩。

    找不到圖示回 `(None, None)` —— 那一樣就不當候選（不能拿別的圖來頂）。
    """
    import numpy as np
    from PySide6.QtGui import QImage

    data = icons.icon_bytes(item_id)
    if data is None:
        return None, None
    image = QImage()
    if not image.loadFromData(data) or image.isNull():
        return None, None
    if image.width() != ICON_SIDE or image.height() != ICON_SIDE:
        return None, None       # 版面跟預期不同就別猜，直接不當候選
    arr = _rgb_array(image).astype(np.int16)
    solid = np.any(arr != np.array(TRANSPARENT, dtype=np.int16), axis=2)
    return arr, solid


# ---- 比對 -------------------------------------------------------------------


def identify(image, point: tuple[int, int], candidates) -> Pick:
    """畫面 `image` 上的 `point`（視窗內像素）點到的是 `candidates` 裡的哪一樣。

    `candidates` 是道具編號的集合 —— **一定要是「背包裡真的有的東西」**：
    拿整份兩萬筆的道具表去比不只慢，還會把分數推給一堆你身上根本沒有的東西。
    """
    import numpy as np

    ids = sorted({int(i) for i in candidates or ()})
    if not ids:
        return Pick(why="讀不到背包，認不出你點的是什麼")

    shot = _rgb_array(image).astype(np.int16)
    height, width = shot.shape[:2]
    x, y = point
    # 他點的那一點一定落在圖示裡面 → 圖示的左上角在 (x-23..x, y-23..y) 之間。
    left, top = x - (ICON_SIDE - 1), y - (ICON_SIDE - 1)
    right, bottom = x + ICON_SIDE, y + ICON_SIDE
    if left < 0 or top < 0 or right > width or bottom > height:
        # 點太靠邊，整格圖示不在畫面裡 —— 這時候一定認不出來，直接說。
        return Pick(why="點的位置太靠視窗邊緣，看不到完整的道具圖示")
    patch = shot[top:bottom, left:right]

    # 把 47×47 的區塊攤成「24×24 的所有位移」：(24, 24, 24, 24, 3)
    windows = np.lib.stride_tricks.sliding_window_view(
        patch, (ICON_SIDE, ICON_SIDE), axis=(0, 1)
    )                                    # (24, 24, 3, 24, 24)
    windows = np.moveaxis(windows, 2, -1)  # (dy, dx, 24, 24, 3)

    ranked: list[tuple[int, float]] = []
    for item_id in ids:
        arr, solid = icon_array(item_id)
        if arr is None:
            continue
        count = int(solid.sum())
        if count < ICON_SIDE:            # 幾乎全透明的圖示分不出東西
            continue
        close = (np.abs(windows - arr) <= _PIXEL_TOLERANCE).all(axis=-1) & solid
        ranked.append((item_id, float(close.sum(axis=(2, 3)).max()) / count))

    if not ranked:
        return Pick(why="背包裡的東西都沒有圖示可以比對")
    ranked.sort(key=lambda kv: -kv[1])
    best = ranked[0][1]
    if best < _MATCH_RATIO:
        return Pick(
            score=best,
            why="認不出你點的是什麼（圖示被蓋住了？請點道具圖案的正中央再試一次）",
            ranked=tuple(ranked[:5]),
        )
    hits = tuple(i for i, s in ranked if s >= best - _TIE_MARGIN)
    return Pick(items=hits, score=best, ranked=tuple(ranked[:5]))


# ---- 監看滑鼠 ---------------------------------------------------------------


class ClickWatcher:
    """等使用者在遊戲視窗裡按**右鍵**，回報那一下點到什麼。

    ⚠ **完全不碰遊戲行程**：只用 `GetAsyncKeyState`（問的是我們自己的
    輸入狀態）與 `GetCursorPos`，再加上抓圖。沒有 hook、沒有注入。

    ⚠ 抓圖要在**按下緣的那一拍**做：晚一拍的話 tooltip／說明視窗已經蓋上去了，
    比對就對不上（而且症狀是「一直認不出來」，看不出是時序問題）。
    """

    def __init__(self, hwnd: int, bag_items) -> None:
        self._hwnd = hwnd
        #: 候選（背包裡有的道具編號）。開始等之前現查一次就好 ——
        #: 等的期間背包會變，但使用者要點的東西本來就得先在背包裡。
        self._items = frozenset(int(i) for i in bag_items or ())
        self._user32 = ctypes.windll.user32
        self._user32.GetAsyncKeyState.restype = ctypes.c_short

    def _down(self) -> bool:
        return bool(self._user32.GetAsyncKeyState(_VK_RBUTTON) & 0x8000)

    def _cursor(self) -> tuple[int, int] | None:
        point = wintypes.POINT()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            return None
        return (point.x, point.y)

    def wait(self, timeout: float, should_stop=None) -> Pick | None:
        """等一下按鍵。回 `Pick`；逾時或被叫停回 None。

        ⚠ 逾時只是**放棄的上限**，不是成功的依據（CLAUDE.md）——
        沒等到就回 None，呼叫端要說「沒等到你點」，不准假裝認出了什麼。
        """
        deadline = time.monotonic() + timeout
        was = self._down()
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                return None
            now = self._down()
            pressed, was = (now and not was), now
            if pressed:
                pick = self._at_cursor()
                if pick is not None:
                    return pick
            time.sleep(_POLL)
        return None

    def _at_cursor(self) -> Pick | None:
        """游標在遊戲視窗裡的話，抓圖比對。不在視窗裡回 None（那一下不算）。"""
        screen = self._cursor()
        if screen is None:
            return None
        point = game_screen.window_point_of(self._hwnd, *screen)
        if point is None or point[0] < 0 or point[1] < 0:
            return None          # 按在別的地方 —— 不是要給我們的訊號
        try:
            shot = game_screen.capture(self._hwnd)
        except game_screen.ScreenError as exc:
            log.warning("認道具時抓不到遊戲畫面：%s", exc)
            return Pick(why=f"抓不到遊戲畫面（{exc}）")
        if shot.isNull():
            return Pick(why="抓到的遊戲畫面是空的")
        return identify(shot, point, self._items)
