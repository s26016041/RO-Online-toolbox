"""看見遊戲畫面，並判斷它現在停在哪一關。

## 為什麼需要這支

自動登入是一連串「做一件事 → 確認到了下一關 → 再做下一件事」。
沒有畫面就只能用固定的 `sleep` 硬等，那在讀取慢的時候會整個錯位，
而且**失敗時完全不知道卡在哪**。

## 關鍵發現：背景也看得到

`PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` **抓得到背景 DirectX 視窗的畫面** ——
不必把遊戲拉到前景、不占用你的螢幕。這是整套背景自動登入的眼睛（[INP-001]）。

`grabWindow` 那類「抓螢幕某塊區域」的做法**不行**：遊戲在背景時抓到的是蓋在
上面的其他視窗（踩過，抓到使用者的編輯器）。

## 怎麼判斷在哪一關

用兩塊區域的「淺色像素比例」。區域用**相對比例**定義，跟視窗大小無關。
門檻是實測出來的，不是猜的（1942x1256 視窗，2026-08-25）：

    畫面        合約書區   登入框區
    合約書       0.892      0.054
    登入畫面     0.310      0.891

兩者差距很大，門檻抓 0.7 / 0.3 就夠穩。
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass
from enum import Enum

from PySide6.QtGui import QImage

from ro_toolbox.services import window_list

try:
    import win32gui
    import win32ui
except ImportError:  # pragma: no cover - 取決於安裝方式
    win32gui = None
    win32ui = None

log = logging.getLogger(__name__)

#: PrintWindow 的旗標。**一定要用這個**，用 0 的話 DirectX 內容不會被畫進來。
PW_RENDERFULLCONTENT = 2

#: 遊戲主視窗的類別名稱。
WINDOW_CLASS = "Ragnarok"


class Stage(Enum):
    """遊戲目前停在哪一關。"""

    UNKNOWN = "不確定"
    EULA = "使用者合約書"
    LOGIN = "登入畫面"


@dataclass(frozen=True, slots=True)
class _Region:
    """畫面上的一塊區域，用**相對比例**表示（跟視窗大小無關）。"""

    left: float
    top: float
    right: float
    bottom: float

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            int(width * self.left),
            int(height * self.top),
            int(width * self.right),
            int(height * self.bottom),
        )


#: 合約書對話框佔的位置（畫面正中偏上）。
EULA_REGION = _Region(0.41, 0.41, 0.60, 0.61)
#: 登入輸入框佔的位置（中下方的小框）。
LOGIN_REGION = _Region(0.44, 0.645, 0.57, 0.725)

#: 「同意」按鈕中心的相對位置。實測 1942x1256 視窗上是視窗座標 (1093,780)。
AGREE_BUTTON = (0.5628, 0.6210)

_PALE = 200          # 「淺色」的門檻（RGB 都要大於它）
_PRESENT = 0.70      # 超過就算「這一塊有對話框」
_ABSENT = 0.30       # 低於就算「沒有」


class ScreenError(RuntimeError):
    """抓不到畫面。訊息是要直接給使用者看的。"""


def available() -> bool:
    return win32gui is not None and win32ui is not None


def find_window(pid: int) -> int | None:
    """找出這個行程的遊戲主視窗。找不到回 None。

    歸屬一律問 `window_list.window_pid`（ctypes 直打 Win32）。踩過的坑見那支
    函式的註解：pywin32 那版在剛啟動遊戲的行程裡會把 pid 讀成 0，於是這裡
    永遠比對不中，自動登入就**安靜地**卡在「等遊戲視窗」直到逾時。

    所以這裡多做一件事：看到遊戲類別的視窗卻**讀不出擁有者**，就記一筆警告。
    寧可吵，也不要再出現一次「明明視窗就在眼前，程式說沒有」。
    """
    if not available():
        return None
    found: list[int] = []
    unattributed: list[int] = []

    def visit(hwnd, _):
        # ⚠ 一律回 True。回 False 會讓 pywin32 把「提早結束列舉」當成錯誤拋出來。
        if win32gui.GetClassName(hwnd) != WINDOW_CLASS:
            return True
        owner = window_list.window_pid(hwnd)
        if owner is None:
            unattributed.append(hwnd)
        elif owner == pid:
            found.append(hwnd)
        return True

    win32gui.EnumWindows(visit, None)
    if not found and unattributed:
        log.warning(
            "看到 %d 個遊戲視窗但讀不出它屬於哪個行程（要找 PID %s）——"
            "先當成不是它，但這通常代表歸屬查詢出問題了",
            len(unattributed), pid,
        )
    return found[0] if found else None


def window_ratio_of(hwnd: int, x: int, y: int) -> tuple[float, float] | None:
    """把一個螢幕座標換算成**視窗內的比例**。算不出來（視窗沒了）回 None。

    存比例不存座標：視窗會移動、會改大小、DPI 也會變。
    """
    if not available() or is_minimised(hwnd):
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    return (x - left) / width, (y - top) / height


def is_minimised(hwnd: int) -> bool:
    """視窗有沒有被最小化。

    這件事要**大聲**：最小化的視窗**完全不處理 PostMessage**（實測），
    而且 `GetWindowRect` 會回一個縮圖大小的負座標矩形（例如 237x39 @ -32000），
    照著算按鈕位置會得到完全錯誤的螢幕座標。自動登入前一定要先確認不是最小化。
    """
    return bool(ctypes.windll.user32.IsIconic(hwnd))


def capture(hwnd: int) -> QImage:
    """抓遊戲視窗目前的畫面。**背景也有效，不搶前景。**"""
    if not available():
        raise ScreenError("缺少 pywin32，無法抓取遊戲畫面。")
    if is_minimised(hwnd):
        raise ScreenError("遊戲視窗被最小化了 —— 這種狀態抓不到畫面也送不進輸入。")
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ScreenError("遊戲視窗尺寸不合理（可能已經關掉）。")

    window_dc = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(window_dc)
    target = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, width, height)
    target.SelectObject(bitmap)
    try:
        ok = ctypes.windll.user32.PrintWindow(
            hwnd, target.GetSafeHdc(), PW_RENDERFULLCONTENT
        )
        if not ok:
            raise ScreenError("PrintWindow 失敗，抓不到遊戲畫面。")
        raw = bitmap.GetBitmapBits(True)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        target.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)

    # ⚠ **不要翻轉。** `GetBitmapBits` 給的 BGRA 順序剛好對得上
    # QImage 的 Format_RGB32，方向也是正的。多翻一次會讓整張圖上下顛倒 ——
    # 而且不會報錯，只會讓判別區域全部取到錯的位置（踩過：登入框區量到 0.593
    # 這種不上不下的值，查了半天才發現畫面是倒的）。
    return QImage(raw, width, height, QImage.Format.Format_RGB32).copy()


def pale_ratio(image: QImage, region: _Region, step: int = 4) -> float:
    """區域內「淺色像素」的比例。對話框是淺底，背景圖是彩色的，分得很開。"""
    x0, y0, x1, y1 = region.pixels(image.width(), image.height())
    pale = total = 0
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            colour = image.pixelColor(x, y)
            total += 1
            if (
                colour.red() > _PALE
                and colour.green() > _PALE
                and colour.blue() > _PALE
            ):
                pale += 1
    return pale / total if total else 0.0


def detect(image: QImage) -> Stage:
    """判斷畫面停在哪一關。認不出來就回 UNKNOWN —— **不准猜**。"""
    eula = pale_ratio(image, EULA_REGION)
    login = pale_ratio(image, LOGIN_REGION)
    if eula > _PRESENT and login < _ABSENT:
        # ⚠ 「那裡有一塊淺色」不等於「合約書畫好了」。遊戲剛開的那一兩秒
        # 畫面還在鋪，淺色比例就已經過關 —— 那時點下去會點在空的地方
        # （[INP-008]）。一定要跟真的合約書比對過才算數。
        difference = eula_difference(image)
        if difference <= _EULA_TOLERANCE:
            return Stage.EULA
        log.debug("那裡有淺色但還不是畫好的合約書（差異 %.1f）", difference)
        return Stage.UNKNOWN
    if login > _PRESENT:
        # ⚠ 光看「那裡有個淺色框」不夠 —— 遊戲的「公告／請稍候」對話框
        # 位置幾乎一樣。要跟真的輸入框比對過才算數，否則會對著公告打字。
        difference = login_box_difference(image)
        if difference <= _MATCH_TOLERANCE:
            return Stage.LOGIN
        log.debug("那裡有框但不是登入輸入框（差異 %.1f，多半是公告）", difference)
    log.debug("認不出目前畫面（合約書區 %.2f、登入框區 %.2f）", eula, login)
    return Stage.UNKNOWN


#: 樣板是在這個 DPI 下抓的。別台機器的 DPI 不同時，按同一個比例把樣板縮放 ——
#: 那個按鈕是**固定像素大小的圖**，只有 Windows 的 DPI 縮放會改變它的大小，
#: 遊戲解析度不會（解析度只改變它在畫面上的位置）。
AGREE_TEMPLATE_DPI = 144
#: 樣板檔（灰階 PNG）。存成檔案而不是一大串數字：5760 個像素寫進原始碼沒人看得懂，
#: 而且改版要重抓時，換一張圖比改一頁常數容易。
AGREE_TEMPLATE_FILE = "eula-agree.png"
#: 比對前把畫面與樣板都**縮小**幾倍。用區塊平均，不是抽樣 ——
#: 抽樣（`[::4]`）在縮放過的畫面上會因為相位對不上而認錯（實測：0.833 倍時整個找錯地方）。
_AGREE_SHRINK = 4
#: 每個像素平均差多少以內算「找到了」。
#: 實測（1942x1256、DPI 144）：真的按鈕 9.31、最像的假貨 24.08、整張中位數 64.67。
_AGREE_MAX_MAD = 20.0
#: 最佳與「其他地方最像的那個」至少要差這麼多倍，才算分得開。
_AGREE_MARGIN = 1.4


def _gray_array(image: QImage):
    import numpy as np

    small = image.convertToFormat(QImage.Format.Format_Grayscale8)
    height, width = small.height(), small.width()
    flat = np.frombuffer(small.constBits(), dtype=np.uint8, count=small.sizeInBytes())
    return flat.reshape(height, small.bytesPerLine())[:, :width].astype(np.int32)


def _shrink(arr, step: int):
    """區塊平均縮圖。**不要用抽樣** —— 見 `_AGREE_SHRINK` 的說明。"""
    height, width = arr.shape
    h2, w2 = height // step * step, width // step * step
    if h2 < step or w2 < step:
        return None
    return (arr[:h2, :w2]
            .reshape(h2 // step, step, w2 // step, step)
            .mean(axis=(1, 3)))


def _scaled_template(filename: str, base_dpi: int, dpi: int) -> QImage | None:
    """載入樣板並依 DPI 縮放。載不到回 None —— 呼叫端要**安全退化**，不准猜。

    樣板都是遊戲**自己畫**的、固定像素大小的圖：遊戲解析度只改變它在畫面上的
    **位置**，會改變它**大小**的只有 Windows 的 DPI 縮放。所以縮放的分母是
    「抓樣板當時的 DPI」，不是視窗大小。
    """
    from PySide6.QtCore import Qt

    from ro_toolbox.config.paths import RESOURCES_DIR

    path = RESOURCES_DIR / filename
    image = QImage(str(path))
    if image.isNull():
        log.warning("載不到樣板：%s", path)
        return None
    scale = (dpi or base_dpi) / base_dpi
    if abs(scale - 1.0) < 0.01:
        return image
    return image.scaled(
        max(8, int(image.width() * scale)),
        max(8, int(image.height() * scale)),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _best_match(screen, patch):
    """在 `screen` 裡找最像 `patch` 的位置。大小不合回 None。

    回 `((y, x), 每像素平均差, 其他地方最像的那個的平均差)`。

    ⚠ **「次佳」不是附贈資訊，是判斷「分不分得開」的依據。**
    只看絕對值的話，一張整體偏亮的畫面到處都「有點像」。
    """
    import numpy as np

    if screen is None or patch is None:
        return None
    ph, pw = patch.shape
    sh, sw = screen.shape
    if sh < ph or sw < pw:
        return None

    scores = np.empty((sh - ph + 1, sw - pw + 1))
    for y in range(sh - ph + 1):
        band = np.lib.stride_tricks.sliding_window_view(screen[y:y + ph], (ph, pw))[0]
        scores[y] = np.abs(band - patch).sum(axis=(1, 2))
    scores /= ph * pw

    top = np.unravel_index(scores.argmin(), scores.shape)
    best = float(scores[top])
    away = np.ones_like(scores, dtype=bool)
    away[max(0, top[0] - ph):top[0] + ph, max(0, top[1] - pw):top[1] + pw] = False
    rival = float(scores[away].min()) if away.any() else float("inf")
    return (int(top[0]), int(top[1])), best, rival


def _best_correlation(screen, patch) -> float | None:
    """把 `patch` 在 `screen` 裡滑一遍，回**最高的正規化相關係數**（-1~1）。

    跟 `_best_match` 的差別是它**不看絕對灰階，只看亮暗的形狀** ——
    所以樣板被縮放、重取樣、整體變亮變暗都還認得出來。
    細筆畫的文字就是這種情況（見 `_DISCONNECT_TEXT_MIN_CORR` 的實測數字）。

    大小不合或樣板整片同色（沒有形狀可比）回 None。
    """
    import numpy as np

    if screen is None or patch is None:
        return None
    ph, pw = patch.shape
    sh, sw = screen.shape
    if sh < ph or sw < pw:
        return None
    wanted = patch.astype(np.float64) - patch.mean()
    scale = float(np.linalg.norm(wanted))
    if scale == 0:
        return None

    best = -1.0
    for y in range(sh - ph + 1):
        band = np.lib.stride_tricks.sliding_window_view(
            screen[y:y + ph], (ph, pw))[0].astype(np.float64)
        band -= band.mean(axis=(1, 2), keepdims=True)
        norms = np.sqrt((band * band).sum(axis=(1, 2)))
        with np.errstate(invalid="ignore", divide="ignore"):
            corr = (band * wanted).sum(axis=(1, 2)) / norms / scale
        best = max(best, float(np.nan_to_num(corr, nan=-1.0).max()))
    return best


def agree_template(dpi: int) -> QImage | None:
    """「同意」按鈕的樣板，依 DPI 縮放。載不到回 None（呼叫端要退回比例法）。"""
    return _scaled_template(AGREE_TEMPLATE_FILE, AGREE_TEMPLATE_DPI, dpi)


def find_agree_button(image: QImage, dpi: int) -> tuple[int, int] | None:
    """在畫面上找「同意」按鈕，回傳**視窗內**的中心座標。找不到回 None。

    ## 為什麼要用找的，不用算的

    合約書是遊戲自己畫的一個**小視窗，而且可以拖動**（使用者實測）——
    所以任何「用視窗大小算比例」的做法本來就靠不住：解析度不同會跑掉，
    被拖一下也會跑掉。認得它長什麼樣才是穩的。

    ## 怎麼確定「找到了」

    兩個條件都要成立，否則寧可回 None 讓呼叫端退回比例法：

    1. 每像素平均差 ≤ `_AGREE_MAX_MAD`。
    2. 最佳的分數要比「其他地方最像的那個」好 `_AGREE_MARGIN` 倍以上 ——
       只看絕對值的話，一張整體偏亮的畫面到處都「有點像」。

    實測（把整張畫面平移模擬拖動、縮放模擬不同 DPI）：
    原圖、±平移、0.667/0.833/1.333 倍縮放，全部命中，誤差 ≤2 像素。
    """
    template = agree_template(dpi)
    if template is None:
        return None
    found = _best_match(
        _shrink(_gray_array(image), _AGREE_SHRINK),
        _shrink(_gray_array(template), _AGREE_SHRINK),
    )
    if found is None:
        return None
    top, best, rival = found

    if best > _AGREE_MAX_MAD or best * _AGREE_MARGIN > rival:
        log.info("畫面上找不到夠像的同意按鈕（最佳 %.1f、次佳 %.1f）", best, rival)
        return None
    x = int(top[1] * _AGREE_SHRINK + template.width() / 2)
    y = int(top[0] * _AGREE_SHRINK + template.height() / 2)
    log.info("找到同意按鈕：視窗內 (%d,%d)，每像素差 %.1f（次佳 %.1f）",
             x, y, best, rival)
    return x, y


def agree_button_by_look(hwnd: int) -> tuple[int, int] | None:
    """從畫面把「同意」按鈕找出來，回傳**螢幕**座標。找不到回 None。"""
    if not available() or is_minimised(hwnd):
        return None
    import ctypes

    dpi = 0
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
    except Exception as exc:  # noqa: BLE001 - 舊版 Windows 沒這支
        log.debug("問不到視窗 DPI（%s），當作跟樣板一樣", exc)
    spot = find_agree_button(capture(hwnd), dpi or AGREE_TEMPLATE_DPI)
    if spot is None:
        return None
    left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
    return left + spot[0], top + spot[1]


def agree_button_position(
    hwnd: int, ratio: tuple[float, float] | None = None
) -> tuple[int, int]:
    """「同意」按鈕中心的**螢幕**座標。

    `ratio` 給了就用它，否則用內建的 `AGREE_BUTTON`。
    給的理由是**別人的客戶端解析度不同，按鈕位置就不一樣** ——
    那時候會跟使用者學一次並存進設定（見 `settings.agree_button`）。

    ⚠ 呼叫端必須是 DPI-aware 的行程，而且要在碰任何視窗 API **之前**就宣告 ——
    中途才改會前後座標不一致（踩過：要求 (1495,864) 實際落在 (667,1088)）。
    """
    if not available():
        raise ScreenError("缺少 pywin32。")
    if is_minimised(hwnd):
        # 最小化時 GetWindowRect 回的是縮圖矩形，算出來會是螢幕外的負座標。
        raise ScreenError("遊戲視窗被最小化了，算不出按鈕位置。")
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    rx, ry = ratio if ratio else AGREE_BUTTON
    return left + int(width * rx), top + int(height * ry)


#: 比對用的縮圖尺寸。縮到這麼小是刻意的：解析度、字型、記住的帳號文字都會變，
#: 縮小之後只剩「版面結構」，比對才穩。
_PATCH_SIZE = (32, 12)

#: 真正的登入輸入框長什麼樣（32x12 灰階，從 2026-08-25 的乾淨登入畫面抽出來）。
#:
#: ⚠ 為什麼需要它：登入輸入框與遊戲的「公告／請稍候」對話框**位置幾乎重疊**，
#: 而且淺色比例（0.891 vs 0.875）、藍色比例、深色比例、框高全部分不開 ——
#: 全都試過。沒有這張參考的話，狀態機會對著一個沒有輸入框的公告視窗打字，
#: 然後回報「輸入沒進去」，而真正的原因是**認錯畫面**。
#: 用縮圖差異（MAD）就分得很開：對自己 0.0、對公告 10.1。
LOGIN_BOX_REFERENCE = (
    240, 240, 240, 240, 240, 240, 240, 240,
    240, 240, 240, 239, 239, 240, 241, 242,
    242, 241, 241, 241, 240, 240, 242, 241,
    239, 240, 240, 242, 240, 240, 240, 240,
    255, 255, 255, 255, 255, 255, 255, 255,
    255, 247, 246, 246, 246, 246, 246, 246,
    246, 246, 246, 246, 246, 246, 246, 246,
    246, 250, 255, 255, 255, 255, 255, 255,
    255, 255, 255, 255, 255, 255, 218, 186,
    255, 233, 199, 176, 189, 182, 169, 186,
    216, 247, 247, 247, 247, 247, 248, 248,
    248, 245, 255, 209, 230, 195, 192, 225,
    255, 255, 255, 255, 255, 255, 239, 224,
    255, 234, 212, 214, 222, 216, 216, 234,
    227, 242, 242, 242, 242, 242, 242, 243,
    243, 245, 255, 240, 250, 243, 238, 244,
    255, 255, 255, 255, 255, 255, 255, 255,
    255, 255, 255, 255, 255, 255, 255, 255,
    255, 255, 255, 255, 255, 255, 255, 255,
    255, 255, 255, 255, 255, 255, 255, 255,
    255, 255, 255, 255, 255, 255, 255, 255,
    255, 245, 245, 245, 245, 245, 245, 245,
    245, 245, 245, 245, 245, 245, 245, 245,
    245, 248, 255, 255, 255, 255, 255, 255,
    255, 255, 255, 255, 219, 161, 180, 150,
    255, 211, 247, 247, 247, 247, 247, 247,
    247, 247, 247, 247, 247, 247, 248, 248,
    248, 245, 255, 255, 255, 255, 255, 255,
    255, 255, 255, 255, 240, 220, 238, 223,
    255, 240, 243, 243, 243, 243, 243, 243,
    243, 243, 243, 243, 243, 243, 243, 243,
    243, 247, 255, 255, 255, 255, 255, 255,
    253, 254, 254, 254, 254, 254, 253, 254,
    254, 254, 253, 254, 254, 254, 253, 254,
    254, 254, 253, 253, 254, 253, 254, 254,
    253, 254, 254, 254, 253, 254, 253, 254,
    230, 237, 235, 235, 230, 229, 229, 232,
    232, 230, 233, 238, 240, 233, 230, 232,
    230, 230, 230, 231, 232, 228, 230, 230,
    233, 231, 233, 234, 230, 228, 214, 227,
    206, 190, 201, 195, 211, 223, 198, 200,
    200, 205, 210, 219, 218, 210, 209, 207,
    199, 203, 208, 215, 204, 206, 236, 148,
    206, 193, 232, 216, 196, 169, 159, 185,
    203, 190, 201, 191, 198, 202, 193, 192,
    192, 192, 197, 206, 204, 190, 190, 194,
    189, 191, 200, 216, 199, 192, 208, 145,
    183, 188, 205, 203, 184, 151, 170, 186,
)

#: 平均絕對差低於這個值就算「是登入輸入框」。實測 0.0 vs 10.1，取中間。
_MATCH_TOLERANCE = 6.0

#: 合約書對話框長什麼樣（32x12 灰階，2026-08-25 從實際畫面抽出來）。
#:
#: ⚠ 為什麼需要它：原本只看「那塊區域有多少淺色像素」，但遊戲剛開的那一兩秒
#: 畫面還在鋪、對話框還沒定位好，淺色比例就已經夠高了 —— 狀態機判定成合約書、
#: 立刻照固定比例算座標點下去，**點在空的地方**，然後一路卡到逾時
#: （實測：視窗 9.6 秒出現、10.5 秒就點了，點完合約書原封不動還在）。
#: 用縮圖差異就分得很開：對合約書畫面 0.0、對登入畫面 43.4。
EULA_REFERENCE = (
    222, 220, 211, 217, 219, 226, 230, 227,
    214, 225, 223, 210, 218, 216, 218, 222,
    221, 217, 223, 227, 220, 238, 247, 247,
    247, 247, 247, 247, 247, 247, 245, 238,
    195, 178, 191, 191, 236, 194, 195, 191,
    214, 209, 202, 202, 193, 212, 197, 194,
    197, 190, 192, 204, 236, 216, 219, 185,
    181, 201, 212, 235, 247, 247, 247, 249,
    193, 195, 182, 187, 190, 202, 186, 192,
    206, 203, 190, 210, 212, 223, 247, 247,
    247, 247, 247, 247, 247, 247, 247, 247,
    247, 247, 247, 247, 247, 247, 247, 249,
    210, 196, 230, 238, 215, 209, 196, 204,
    228, 247, 247, 247, 247, 247, 247, 247,
    247, 247, 247, 247, 247, 247, 247, 247,
    247, 247, 247, 247, 247, 247, 247, 249,
    191, 229, 218, 216, 229, 214, 214, 208,
    201, 224, 198, 181, 184, 185, 194, 175,
    210, 235, 239, 245, 240, 237, 239, 238,
    236, 235, 242, 247, 247, 247, 247, 249,
    166, 177, 220, 209, 206, 201, 209, 193,
    187, 200, 201, 198, 224, 219, 228, 187,
    230, 227, 204, 230, 211, 197, 195, 198,
    211, 196, 216, 246, 242, 247, 247, 249,
    189, 165, 182, 196, 197, 219, 212, 222,
    215, 216, 207, 205, 217, 220, 203, 232,
    215, 233, 232, 226, 238, 247, 247, 247,
    247, 247, 247, 247, 247, 247, 247, 249,
    211, 165, 195, 223, 208, 204, 219, 215,
    216, 218, 208, 212, 221, 204, 232, 240,
    237, 241, 239, 240, 244, 247, 247, 247,
    247, 247, 247, 247, 247, 247, 247, 249,
    235, 178, 182, 237, 218, 212, 210, 215,
    230, 204, 188, 187, 188, 200, 183, 212,
    238, 218, 220, 193, 184, 208, 217, 223,
    247, 247, 247, 247, 247, 247, 247, 249,
    202, 218, 182, 184, 194, 190, 195, 182,
    184, 195, 228, 215, 219, 205, 183, 201,
    205, 180, 198, 226, 203, 181, 178, 178,
    195, 240, 190, 208, 203, 245, 247, 249,
    187, 197, 192, 189, 210, 191, 171, 201,
    188, 185, 187, 216, 211, 189, 182, 181,
    184, 213, 200, 187, 201, 230, 235, 243,
    242, 235, 237, 236, 238, 246, 247, 249,
    217, 241, 201, 213, 209, 206, 210, 222,
    202, 201, 195, 202, 197, 225, 231, 215,
    223, 205, 204, 215, 198, 192, 194, 206,
    200, 204, 216, 201, 191, 243, 244, 239,
)

#: 平均絕對差低於這個值就算「合約書已經完整畫出來了」。實測 0.0 vs 43.4，
#: 取一個離兩邊都很遠的值：容得下字型／捲軸位置的小差異，又不會把別的畫面認成它。
_EULA_TOLERANCE = 15.0


def _patch(image: QImage, region: _Region) -> list[int]:
    """把區域縮成固定大小的灰階小圖，給差異比對用。"""
    from PySide6.QtCore import QRect, Qt

    x0, y0, x1, y1 = region.pixels(image.width(), image.height())
    small = (
        image.copy(QRect(x0, y0, x1 - x0, y1 - y0))
        .scaled(
            _PATCH_SIZE[0],
            _PATCH_SIZE[1],
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        .convertToFormat(QImage.Format.Format_Grayscale8)
    )
    return [
        small.pixelColor(x, y).red()
        for y in range(_PATCH_SIZE[1])
        for x in range(_PATCH_SIZE[0])
    ]


def login_box_difference(image: QImage) -> float:
    """畫面上那個框跟「真的登入輸入框」差多少。越小越像。"""
    patch = _patch(image, LOGIN_REGION)
    return sum(
        abs(a - b) for a, b in zip(patch, LOGIN_BOX_REFERENCE, strict=True)
    ) / len(patch)


def eula_difference(image: QImage) -> float:
    """畫面上那塊東西跟「畫好的合約書」差多少。越小越像。"""
    patch = _patch(image, EULA_REGION)
    return sum(
        abs(a - b) for a, b in zip(patch, EULA_REFERENCE, strict=True)
    ) / len(patch)


# ---- 斷線對話框 ---------------------------------------------------------
#
# 2026-08-28 06:14 實機採證（GAMEDATA [INP-012]）：斷線時遊戲**不會**回到登入
# 畫面 —— 它停在原本的世界畫面，中間跳一個自己畫的小訊息框寫「與伺服器斷線」，
# 左上角的角色面板照樣顯示 HP 2311/2311。所以：
#
# * **不能看記憶體**判斷有沒有登入（角色結構還在），要問連線 —— 印證 [MEM-029]。
# * **不能看畫面動不動**：實測斷線時每秒每像素平均差 2.4~2.6、5.9% 的像素在變
#   （客戶端照樣在本地跑動畫）。「斷線＝畫面靜止」這條**不成立**，已否決。
# * `EnumChildWindows` 回 **0 個子視窗** —— 它不是 Win32 視窗，讀不到標題文字，
#   只能從畫面認。
#
# ## 為什麼要有它（連線表已經看得出來了，為什麼還要看畫面）
#
# 連線表分不出「真斷線」和「換地圖的過渡」，所以 `reconnect.py` 只能等 20 秒
# 觀察期。這個對話框是**獨立的第二個來源**：看到它就當場確認，不必等 ——
# 換地圖的過渡不會跳這個框。看不到就退回原本的觀察期（**安全退化**）。

#: 整個訊息框（粗定位用）與「與伺服器斷線」那六個字（確認用）。
DISCONNECT_BOX_FILE = "disconnect-box.png"
DISCONNECT_TEXT_FILE = "disconnect-text.png"
#: 樣板是在這個 DPI 下抓的（同 `AGREE_TEMPLATE_DPI` 的道理）。
#: 實機：視窗 1295x837 邏輯 / 1942x1256 實體 = 1.5 倍 = DPI 144。
DISCONNECT_TEMPLATE_DPI = 144
#: 那六個字在**框樣板**裡的左上角座標（樣板像素）。
#: 量法：框原點 (837,602)、文字原點 (845,607)。
DISCONNECT_TEXT_OFFSET = (8, 5)

#: 粗定位的縮小倍率。**細字不能用縮小的比對** —— 實測縮 2 倍時最佳 34.87、
#: 次佳 36.51（1.0 倍，等於分不開）：區塊平均把細筆畫糊掉，而且樣板與畫面的
#: 縮放相位對不上。所以粗定位認的是「整個白框」（大塊、糊了也還在）。
_DISCONNECT_SHRINK = 8
#: 粗定位門檻。實測（1942x1256 的普隆德拉擺攤街，畫面非常吵）：
#: 真的框 3.18、畫面上其他地方最像的 32.21。取一個離兩邊都很遠的值。
_DISCONNECT_BOX_MAX_MAD = 12.0
_DISCONNECT_BOX_MARGIN = 2.0
#: 確認那六個字用的門檻：**正規化相關係數**，不是平均差。
#:
#: 為什麼不用平均差：樣板是從 **1.5 倍放大**的畫面裁的（Windows 幫非 DPI-aware
#: 的遊戲放大），縮回別的 DPI 等於被重取樣兩次，細筆畫的灰階整個變掉 ——
#: 實測縮到 0.667 倍時對的字平均差 31.96，比門檻還高，**真的斷線會被判成沒斷線**。
#: 相關係數只看「亮暗的形狀」，不看絕對灰階，縮放之後還撐得住。
#:
#: 實測（同一張畫面縮到各 DPI；反面對照是把框裡那六個字換成**同一個框裡的
#: 「確定」按鈕** —— 一樣的白底、一樣的字型、不一樣的字）。
#: ⚠ 這些是**搜尋過對齊之後**的值：搜尋會挑最高分，所以假貨也會被抬高，
#: 拿「固定對齊」量出來的數字訂門檻會訂得太鬆（踩過：那樣量「確定」只有 0.11，
#: 實際搜尋後是 0.39）。
#:
#:     DPI      對的字   換成「確定」   整片白
#:      96       0.631      0.385       0.366
#:     120       0.804      0.391       0.355
#:     144       1.000      0.304       0.294
#:     192       0.945      0.390       0.361
#:
#: 門檻取 0.50：最差的對的字（0.631）與最像的假貨（0.391）中間，兩邊都留 1.26 倍。
#: 寧可漏判（退回 20 秒觀察期，只是慢一點）也不要誤判 —— 誤判會把好好在玩的
#: 遊戲關掉重開。
#:
#: 校準用的是**真的會出現在遊戲裡的東西**（另一段遊戲文字、空白）。
#: 刻意做出來的贗品（例如把同一串字左右翻轉）分數會到 0.5 上下 ——
#: 那不是 RO 畫得出來的畫面，不列入校準。
_DISCONNECT_TEXT_MIN_CORR = 0.50
#: 粗定位有 ±1 個區塊的誤差，全解析度確認時在預期位置附近多搜這麼多像素。
_DISCONNECT_TEXT_SLACK = 12


def disconnect_templates(dpi: int) -> tuple[QImage, QImage] | None:
    """（框, 文字）兩張樣板，依 DPI 縮放。少一張就回 None。"""
    box = _scaled_template(DISCONNECT_BOX_FILE, DISCONNECT_TEMPLATE_DPI, dpi)
    text = _scaled_template(DISCONNECT_TEXT_FILE, DISCONNECT_TEMPLATE_DPI, dpi)
    if box is None or text is None:
        return None
    return box, text


def find_disconnect_dialog(image: QImage, dpi: int) -> tuple[int, int] | None:
    """畫面上有沒有「與伺服器斷線」？有的話回框的**視窗內**左上角座標。

    兩段式，因為兩件事的最佳解析度不一樣：

    1. **粗定位**：整個白框，縮 `_DISCONNECT_SHRINK` 倍全畫面搜。
       實測 70 ms、最佳 3.18／次佳 32.21（差 10 倍）。
    2. **確認**：那六個字，**全解析度**、只在粗定位算出來的位置附近搜。
       全畫面用全解析度搜要 17 秒（實測），不可能每拍做。

    只做第 1 段不行 —— 那只認得出「有一個訊息框」，認不出它寫什麼。
    只做第 2 段也不行 —— 太慢。
    """
    found = disconnect_templates(dpi)
    if found is None:
        return None
    box, text = found
    screen = _gray_array(image)

    coarse = _best_match(
        _shrink(screen, _DISCONNECT_SHRINK),
        _shrink(_gray_array(box), _DISCONNECT_SHRINK),
    )
    if coarse is None:
        return None
    top, best, rival = coarse
    if best > _DISCONNECT_BOX_MAX_MAD or best * _DISCONNECT_BOX_MARGIN > rival:
        log.debug("畫面上沒有斷線訊息框（最佳 %.1f、次佳 %.1f）", best, rival)
        return None

    box_x = top[1] * _DISCONNECT_SHRINK
    box_y = top[0] * _DISCONNECT_SHRINK
    scale = (dpi or DISCONNECT_TEMPLATE_DPI) / DISCONNECT_TEMPLATE_DPI
    want_x = box_x + int(DISCONNECT_TEXT_OFFSET[0] * scale)
    want_y = box_y + int(DISCONNECT_TEXT_OFFSET[1] * scale)

    slack = _DISCONNECT_SHRINK + _DISCONNECT_TEXT_SLACK
    patch = _gray_array(text)
    ph, pw = patch.shape
    y0 = max(0, want_y - slack)
    x0 = max(0, want_x - slack)
    window = screen[y0:want_y + ph + slack, x0:want_x + pw + slack]
    corr = _best_correlation(window, patch)
    if corr is None:
        log.debug("斷線訊息框找到了，但框太小放不下那六個字")
        return None
    if corr < _DISCONNECT_TEXT_MIN_CORR:
        # 有訊息框但寫的不是「與伺服器斷線」（例如別的公告）——
        # **不准當成斷線**，退回連線表的觀察期。
        log.info("畫面上有訊息框，但不是「與伺服器斷線」（相關 %.2f）", corr)
        return None

    log.info("畫面上看到「與伺服器斷線」：視窗內 (%d,%d)（框差 %.1f／次佳 %.1f、字相關 %.2f）",
             box_x, box_y, best, rival, corr)
    return box_x, box_y


def disconnected_by_look(hwnd: int) -> bool | None:
    """畫面上有沒有「與伺服器斷線」對話框。

    ⚠ **看不了的時候回 `None`，不是 `False`。** 視窗最小化、抓不到畫面、
    樣板載不到都算「不知道」—— 呼叫端要退回連線表的觀察期，而不是
    當成「沒斷線」（那會讓真的斷線永遠等不到重連）。
    """
    if not available() or is_minimised(hwnd):
        return None
    dpi = 0
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
    except Exception as exc:  # noqa: BLE001 - 舊版 Windows 沒這支
        log.debug("問不到視窗 DPI（%s），當作跟樣板一樣", exc)
    try:
        image = capture(hwnd)
    except Exception as exc:  # noqa: BLE001 - 看不了就是「不知道」，不准往上丟
        # ⚠ 不能只接 `ScreenError`。視窗在 `find_window()` 與這裡之間關掉是常態
        # （斷線之後使用者自己把遊戲關了就會這樣），那時 `GetWindowRect` 丟的是
        # `pywintypes.error: (1400, '無效的視窗控制代碼')` —— 踩過，它會一路逃到
        # 呼叫端。這條路只有兩種結果：看到了，或不知道。
        log.debug("抓不到畫面，這一拍不判斷斷線對話框：%s", exc)
        return None
    if disconnect_templates(dpi or DISCONNECT_TEMPLATE_DPI) is None:
        return None      # 樣板不在 → 不知道，不是「沒斷線」
    return find_disconnect_dialog(image, dpi or DISCONNECT_TEMPLATE_DPI) is not None
