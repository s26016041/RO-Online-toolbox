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


def window_point_of(hwnd: int, x: int, y: int) -> tuple[int, int] | None:
    """把一個螢幕座標換算成**視窗內的像素**座標。算不出來回 None。

    `capture()` 抓的是整個視窗（含外框），所以樣板比對出來的座標也是這一套。
    """
    if not available() or is_minimised(hwnd):
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    if right - left <= 0 or bottom - top <= 0:
        return None
    return x - left, y - top


def is_minimised(hwnd: int) -> bool:
    """視窗有沒有被最小化。

    這件事要**大聲**：最小化的視窗**完全不處理 PostMessage**（實測），
    而且 `GetWindowRect` 會回一個縮圖大小的負座標矩形（例如 237x39 @ -32000），
    照著算按鈕位置會得到完全錯誤的螢幕座標。自動登入前一定要先確認不是最小化。
    """
    return bool(ctypes.windll.user32.IsIconic(hwnd))


def _release_capture(hwnd: int, window_dc, target, bitmap) -> None:
    """把抓圖用掉的 GDI 資源還回去。**每一步各自吞例外，一步都不能少。**

    ## 為什麼長這樣（2026-08-29，使用者朋友的機器）

    實機症狀（他的日誌）：自動登入請他手動按「同意」之後 37 秒，
    `win32ui.error: DeleteDC failed` 從這裡的 `finally` 一路炸穿工作執行緒 ——
    **整條自動登入當場死掉**，所以他後來按不按同意都沒有意義了，沒人在等他。
    學按鈕那個迴圈每 0.4 秒抓一張圖，等於連抓了 90 幾張才炸。

    兩個修法，理由不同：

    1. **收尾不准丟例外**（這是真正治症狀的）。收尾失敗頂多是資源沒還乾淨，
       不該讓抓圖失敗，更不該讓登入死掉。三步各自 try，失敗只記 debug。
    2. **視窗 DC 只能 `ReleaseDC`，不准 `DeleteDC`**。舊版兩個都做了。
       `GetWindowDC` 借來的是系統的快取 DC，MSDN 明講不能刪；
       視窗如果是 `CS_OWNDC`，刪掉就是把人家的私有 DC 砍掉。
       開發機上這一刀**每次都成功**（實測 400 次沒有一次失敗，遊戲視窗
       `CS_OWNDC=False`），所以以前完全看不出有問題。

    ⚠ **沒能重現的部分要說清楚**：為什麼是「第 90 幾次」才開始失敗，
    開發機上重現不出來（400 次連續 GetWindowDC + DeleteDC 都沒事，
    GDI 物件數也沒有成長）。上面第 2 點是最有嫌疑的解釋，但**還沒證實**。
    第 1 點跟原因無關 —— 不管誰讓收尾失敗，都不該把整支程式帶走。

    順序：先刪記憶體 DC，再刪點陣圖（DC 沒了才不會有「還被選著」的問題），
    最後把視窗 DC 還回去。
    """
    steps = []
    if target is not None:
        steps.append(("記憶體 DC", target.DeleteDC))
    if bitmap is not None:
        steps.append(("點陣圖", lambda: win32gui.DeleteObject(bitmap.GetHandle())))
    if window_dc:
        steps.append(("視窗 DC", lambda: win32gui.ReleaseDC(hwnd, window_dc)))
    for what, action in steps:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - 收尾失敗不該蓋掉真正的結果
            log.debug("還不掉%s：%s", what, exc)


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

    window_dc = target = bitmap = None
    try:
        window_dc = win32gui.GetWindowDC(hwnd)
        source = win32ui.CreateDCFromHandle(window_dc)
        target = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source, width, height)
        target.SelectObject(bitmap)
        ok = ctypes.windll.user32.PrintWindow(
            hwnd, target.GetSafeHdc(), PW_RENDERFULLCONTENT
        )
        if not ok:
            raise ScreenError("PrintWindow 失敗，抓不到遊戲畫面。")
        raw = bitmap.GetBitmapBits(True)
    except ScreenError:
        raise
    except Exception as exc:  # noqa: BLE001 - win32 的例外一律翻成 ScreenError
        # ⚠ 不准讓 `win32ui.error` 這種例外直接往上跑：呼叫端接的是
        # `ScreenError`，漏接的那一個會把整條自動登入炸掉（實機踩過）。
        raise ScreenError(f"抓遊戲畫面失敗：{exc}") from exc
    finally:
        _release_capture(hwnd, window_dc, target, bitmap)

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

#: 粗篩用的縮圖倍率（挑倍率用的，不是最後的分數）。
_AGREE_COARSE = 8
#: 粗篩之後有幾個倍率值得用細網格再比一次。
_AGREE_FINALISTS = 3
#: 要試的倍率。涵蓋 DPI 96～240（0.667～1.667 倍）與全螢幕拉伸（最多約 2 倍）。
#: ⚠ 一定要**整排都試**，不能只信 DPI 算出來的那一個 —— 見 `_candidate_scales`。
_AGREE_SCALES = (0.5, 0.583, 0.667, 0.75, 0.833, 0.917, 1.0,
                 1.083, 1.167, 1.25, 1.333, 1.5, 1.667, 1.833, 2.0)
#: 使用者教過的樣子是從**他自己的畫面**剪下來的，倍率本來就該是 1；
#: 只留一點餘裕給「教完之後換了縮放設定」。
_LEARNED_SCALES = (0.833, 1.0, 1.2)
#: 剪下來那一塊至少要有這麼多起伏（灰階標準差）才值得學。
#: 全黑的畫面標準差是 0 —— 那種東西學起來會「到處都命中」。
_LEARN_MIN_SPREAD = 8.0
#: 平均亮度低於這個值就當作「根本沒抓到畫面」，要講出來。
_BLANK_BRIGHTNESS = 8.0

#: 跟使用者學的時候剪多大一塊（他按的那一點在正中央）。
#: 不用按鈕的大小：他那台的縮放倍率我們並不知道。
_LEARN_CROP = (140, 56)
#: 學到的樣子存在使用者資料夾（不是資源目錄 —— 那裡在打包後是唯讀的暫存區）。
LEARNED_AGREE_FILE = "eula-agree-learned.png"

AGREE_SOURCE_LEARNED = "你教過的樣子"
AGREE_SOURCE_BUILTIN = "內建樣板"


@dataclass(frozen=True, slots=True)
class AgreeMatch:
    """畫面上找到的「同意」按鈕，連同**憑什麼相信它**的那幾個數字。

    `accepted=False` 代表「有找到最像的，但不夠像」——
    呼叫端不准拿它去點，但要把它印出來（那是別人的機器上唯一的線索）。
    """

    x: int
    y: int
    scale: float
    score: float
    rival: float
    source: str
    accepted: bool

    def describe(self) -> str:
        verdict = "" if self.accepted else " —— 不夠像，不採用"
        return (f"{self.source}／{self.scale:g} 倍，視窗內 ({self.x},{self.y})，"
                f"每像素差 {self.score:.1f}（次佳 {self.rival:.1f}）{verdict}")


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


def _load_template(filename: str, base_dir=None) -> QImage | None:
    """載入樣板圖。載不到回 None —— 呼叫端要**安全退化**，不准猜。"""
    from ro_toolbox.config.paths import RESOURCES_DIR

    path = (base_dir or RESOURCES_DIR) / filename
    image = QImage(str(path))
    if image.isNull():
        log.warning("載不到樣板：%s", path)
        return None
    return image


def _scaled(image: QImage, scale: float) -> QImage:
    """把樣板放大縮小。**要平滑縮放** —— 最近鄰在 0.667 倍會把細筆畫整條吃掉。"""
    from PySide6.QtCore import Qt

    if abs(scale - 1.0) < 0.01:
        return image
    return image.scaled(
        max(8, int(round(image.width() * scale))),
        max(8, int(round(image.height() * scale))),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _scaled_template(filename: str, base_dpi: int, dpi: int) -> QImage | None:
    """載入樣板並依 DPI 縮放。載不到回 None —— 呼叫端要**安全退化**，不准猜。"""
    image = _load_template(filename)
    if image is None:
        return None
    return _scaled(image, (dpi or base_dpi) / base_dpi)


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


def agree_template(dpi: int) -> QImage | None:
    """「同意」按鈕的樣板，依 DPI 縮放。載不到回 None（呼叫端要退回比例法）。"""
    return _scaled_template(AGREE_TEMPLATE_FILE, AGREE_TEMPLATE_DPI, dpi)


def learned_agree_path():
    """使用者教過的「按鈕長什麼樣」存在哪。"""
    from ro_toolbox.config.paths import user_data_dir

    return user_data_dir() / LEARNED_AGREE_FILE


def learned_agree_template() -> QImage | None:
    """使用者教過的按鈕樣子。沒教過回 None。

    ⚠ 這裡用「檔案在不在」判斷是**對的**：這張圖是在**使用者自己的機器上**
    學出來寫下去的，不是只有開發機才有的資源（CLAUDE.md 禁的是後者）。
    """
    path = learned_agree_path()
    if not path.is_file():
        return None
    return _load_template(path.name, path.parent)


def save_learned_agree(image: QImage, x: int, y: int) -> bool:
    """把使用者按下去的地方**長什麼樣**剪下來存起來（`x,y` 是視窗內座標）。

    ## 為什麼存「長什麼樣」而不是只存座標

    存座標（比例）是**存位置**，正是 CLAUDE.md 明令禁止的那一類：合約書是
    可拖動的小視窗，換個解析度、被拖一下，那個比例就指到空的地方 ——
    而且**不會報錯**，只會安靜地點在背景圖上（使用者朋友的機器實際踩過：
    教過的位置 (940,749) 照樣點不到）。

    存樣子就沒有這個問題：下一次照樣**當場在畫面上把它找出來**，
    視窗搬到哪、放多大都找得到。剪一塊**固定大小**（不是按鈕大小）是因為
    我們不知道他那台的縮放倍率 —— 剪下來的那塊裡有什麼都好，
    只要下次找得到同一塊，中心點就是他按過的那一點。
    """
    width, height = _LEARN_CROP
    left, top = x - width // 2, y - height // 2
    if (left < 0 or top < 0
            or left + width > image.width() or top + height > image.height()):
        log.info("按的位置太靠邊（%d,%d），這次不學按鈕的樣子", x, y)
        return False
    crop = image.copy(left, top, width, height).convertToFormat(
        QImage.Format.Format_Grayscale8
    )
    # ⚠ **一片平的東西不准學。** `PrintWindow` 在全螢幕模式或某些顯示卡上會
    # 抓回全黑的畫面；把一塊黑存成樣板的後果不是「找不到」，而是**到處都找得到**
    # —— 下次會很有自信地點在螢幕的隨便一個角落。寧可不學，退回比例法。
    if _spread(crop) < _LEARN_MIN_SPREAD:
        log.warning(
            "剪下來那一塊幾乎沒有紋理（起伏 %.1f）—— 多半是根本沒抓到畫面"
            "（全螢幕模式常這樣，改用視窗模式試試）。這次不學。",
            _spread(crop),
        )
        return False
    path = learned_agree_path()
    if not crop.save(str(path), "PNG"):
        log.warning("學到的按鈕樣子存不起來：%s", path)
        return False
    log.info("記住「同意」按鈕在你的畫面上長什麼樣了：%s", path)
    return True


def _candidate_scales(dpi: int, ladder=None) -> list[float]:
    """要試哪些倍率。**DPI 只是提示，不是答案。**

    ⚠ 舊版只用 `GetDpiForWindow(hwnd) / 144` 算出一個倍率就去比對，
    倍率猜錯就整個找不到 —— 而它很容易猜錯：客戶端不見得是 DPI-aware，
    那時候是 Windows 替它縮放，`PrintWindow` 抓回來的按鈕大小跟 DPI 對不上；
    全螢幕把 800x600 拉伸到 1920x1080 更是差到 1.8 倍。
    所以一律**把整排倍率都試一遍**，讓畫面自己說它是哪一個。
    """
    scales = set(ladder if ladder is not None else _AGREE_SCALES)
    if dpi:
        scales.add(round(dpi / AGREE_TEMPLATE_DPI, 3))
    return sorted(scales)


def _search_scales(fine, coarse, template: QImage, source: str,
                   scales) -> AgreeMatch | None:
    """在畫面裡找這張樣板，倍率未知就每個都試。找不出候選回 None。

    兩段式（整排倍率都用細網格跑要好幾秒，太慢）：

    1. **粗篩**：縮 8 倍，每個倍率各跑一次，只是要挑出「哪幾個倍率有搞頭」。
    2. **細比**：粗篩前幾名才用縮 4 倍重跑，分數與「次佳」都以這一輪為準。
    """
    ranked = []
    for scale in scales:
        patch = _shrink(_gray_array(_scaled(template, scale)), _AGREE_COARSE)
        found = _best_match(coarse, patch)
        if found is not None:
            ranked.append((found[1], scale))
    ranked.sort()

    best: AgreeMatch | None = None
    for _rough, scale in ranked[:_AGREE_FINALISTS]:
        scaled = _scaled(template, scale)
        found = _best_match(fine, _shrink(_gray_array(scaled), _AGREE_SHRINK))
        if found is None:
            continue
        top, score, rival = found
        match = AgreeMatch(
            x=int(top[1] * _AGREE_SHRINK + scaled.width() / 2),
            y=int(top[0] * _AGREE_SHRINK + scaled.height() / 2),
            scale=scale,
            score=score,
            rival=rival,
            source=source,
            accepted=score <= _AGREE_MAX_MAD and score * _AGREE_MARGIN <= rival,
        )
        if best is None or match.score < best.score:
            best = match
    return best


def find_agree_match(image: QImage, dpi: int = 0) -> AgreeMatch | None:
    """在畫面上找「同意」按鈕，連**為什麼相信它**一起回報。

    回 None 代表「連比都沒得比」（樣板載不到、畫面比樣板還小）。
    找到了但不夠像會回一個 `accepted=False` 的結果 —— 呼叫端**不准拿它去點**，
    但要把它印出來：那些數字是別人的機器上唯一追得到的線索。
    """
    grey = _gray_array(image)
    fine = _shrink(grey, _AGREE_SHRINK)
    coarse = _shrink(grey, _AGREE_COARSE)
    if fine is None or coarse is None:
        return None

    best: AgreeMatch | None = None
    # ⚠ **內建樣板先試，教過的樣子後試。** 內建那張是「同意」按鈕本人，
    # 教過的那塊只是「他那次按在哪」剪下來的 —— 萬一他先點了一下標題列才按
    # 按鈕，學到的就是標題列。真的按鈕認得出來時，一律以它為準。
    builtin = _load_template(AGREE_TEMPLATE_FILE)
    if builtin is not None:
        best = _search_scales(fine, coarse, builtin, AGREE_SOURCE_BUILTIN,
                              _candidate_scales(dpi))
        if best is not None and best.accepted:
            return best
    learned = learned_agree_template()
    if learned is None:
        return best
    match = _search_scales(fine, coarse, learned, AGREE_SOURCE_LEARNED,
                           _candidate_scales(0, _LEARNED_SCALES))
    if match is None:
        return best
    if best is None or match.accepted or match.score < best.score:
        return match
    return best


def find_agree_button(image: QImage, dpi: int = 0) -> tuple[int, int] | None:
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

    ⚠ **倍率不准只信 DPI 算出來的那一個**（見 `_candidate_scales`）。
    `dpi` 現在只是提示：整排倍率都會試，讓畫面自己說它是哪一個。

    實測（把整張畫面平移模擬拖動、縮放模擬不同解析度）：
    0.5～2.0 倍全部命中，誤差 ≤ 半個縮圖格。
    """
    match = find_agree_match(image, dpi)
    if match is None:
        return None
    if not match.accepted:
        log.info("畫面上找不到夠像的同意按鈕（%s）", match.describe())
        return None
    log.info("找到同意按鈕：%s", match.describe())
    return match.x, match.y


def _spread(image: QImage) -> float:
    """這張圖有多少起伏（灰階標準差）。算不出來回 0 —— 當作「沒東西」。"""
    try:
        return float(_gray_array(image).std())
    except Exception as exc:  # noqa: BLE001 - 算不出來就當它沒紋理
        log.debug("算不出畫面起伏：%s", exc)
        return 0.0


def image_note(image: QImage) -> str:
    """一句話描述這張畫面（尺寸＋平均亮度）。

    平均亮度是有用的：`PrintWindow` 在某些顯示卡上抓 DirectX 視窗會回**全黑**，
    那種情況下比對永遠不可能命中，而症狀跟「解析度不一樣」一模一樣。
    亮度接近 0 就知道要往「根本沒抓到畫面」查，不是往「認不出按鈕」查。
    """
    try:
        return (f"{image.width()}x{image.height()}、"
                f"平均亮度 {_gray_array(image).mean():.0f}")
    except Exception as exc:  # noqa: BLE001 - 只是說明文字，算不出來不該擋住流程
        return f"{image.width()}x{image.height()}（亮度算不出來：{exc}）"


def agree_button_report(hwnd: int) -> tuple[tuple[int, int] | None, str]:
    """從畫面把「同意」按鈕找出來。回 (螢幕座標或 None, 給人看的說明)。

    說明一定要有東西：這支跑在**子行程**裡，它的 `log` 不會進到主程式的日誌
    （[INP-009]）。別人的機器上出問題時，主行程印出來的這一句是唯一的線索。
    """
    if not available():
        return None, "沒有 pywin32，看不到畫面"
    if is_minimised(hwnd):
        return None, "遊戲視窗被最小化了，抓不到畫面"
    import ctypes

    dpi = 0
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
    except Exception as exc:  # noqa: BLE001 - 舊版 Windows 沒這支
        log.debug("問不到視窗 DPI（%s），當作跟樣板一樣", exc)
    image = capture(hwnd)
    match = find_agree_match(image, dpi)
    note = f"畫面 {image_note(image)}、DPI {dpi or '不明'}"
    if _spread(image) < _LEARN_MIN_SPREAD:
        # 這不是「認不出按鈕」，是**根本沒看到畫面** —— 兩者的解法完全不同，
        # 別人的機器上要一眼分得出來（全螢幕模式最常這樣）。
        return None, (f"{note}；`PrintWindow` 抓回來的畫面幾乎是空的 —— "
                      "遊戲多半在全螢幕模式，請改成視窗模式再試")
    if match is None:
        return None, f"{note}；比都比不了（樣板載不到或畫面比樣板還小）"
    if not match.accepted:
        return None, f"{note}；{match.describe()}"
    left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
    return (left + match.x, top + match.y), f"{note}；{match.describe()}"


def agree_button_by_look(hwnd: int) -> tuple[int, int] | None:
    """從畫面把「同意」按鈕找出來，回傳**螢幕**座標。找不到回 None。"""
    return agree_button_report(hwnd)[0]


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
