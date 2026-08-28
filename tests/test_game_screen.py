"""判斷遊戲停在哪一關。

門檻是**實測**出來的（2026-08-25，1942x1256 視窗）：

    畫面        合約書區   登入框區
    合約書       0.892      0.054
    登入畫面     0.310      0.891

兩者差很開，所以門檻抓 0.70 / 0.30。這一支用合成影像把規則釘住。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtGui")

from PySide6.QtGui import QColor, QImage  # noqa: E402

from ro_toolbox.services import game_screen  # noqa: E402
from ro_toolbox.services.game_screen import Stage  # noqa: E402

W, H = 400, 300


def _canvas(*pale_regions) -> QImage:
    """做一張測試圖：底色是彩色的，指定區域塗成淺色（模擬對話框）。"""
    img = QImage(W, H, QImage.Format.Format_RGB32)
    img.fill(QColor(40, 120, 60))          # 遊戲背景是彩色的
    for region in pale_regions:
        x0, y0, x1, y1 = region.pixels(W, H)
        for y in range(y0, y1):
            for x in range(x0, x1):
                img.setPixelColor(x, y, QColor(245, 245, 245))
    return img


def test_regions_scale_with_the_window():
    """區域用比例定義，換解析度不用改程式。"""
    small = game_screen.EULA_REGION.pixels(1000, 500)
    big = game_screen.EULA_REGION.pixels(2000, 1000)
    assert [v * 2 for v in small] == list(big)


def _painted(region, reference) -> QImage:
    """做一張「指定區域長得跟參考樣板一樣」的圖。"""
    img = QImage(W, H, QImage.Format.Format_RGB32)
    img.fill(QColor(40, 120, 60))
    x0, y0, x1, y1 = region.pixels(W, H)
    pw, ph = game_screen._PATCH_SIZE
    for row in range(ph):
        for col in range(pw):
            value = reference[row * pw + col]
            colour = QColor(value, value, value)
            for y in range(y0 + (y1 - y0) * row // ph, y0 + (y1 - y0) * (row + 1) // ph):
                for x in range(x0 + (x1 - x0) * col // pw, x0 + (x1 - x0) * (col + 1) // pw):
                    img.setPixelColor(x, y, colour)
    return img


def test_detects_the_eula():
    """光是「有一塊淺色」不夠，要跟真的合約書比對得上。"""
    real = _painted(game_screen.EULA_REGION, game_screen.EULA_REFERENCE)
    assert game_screen.detect(real) is Stage.EULA


def test_blank_pale_block_is_not_the_eula():
    """畫面還在鋪的時候淺色比例就夠高了 —— 那時點下去會點在空的地方。

    實測踩過（[INP-008]）：視窗 9.6 秒出現、10.5 秒判定成合約書就點，
    對話框原封不動還在，然後一路卡到逾時。所以純淺色方塊一律不算合約書。
    """
    assert game_screen.detect(_canvas(game_screen.EULA_REGION)) is Stage.UNKNOWN


def _with_login_box() -> QImage:
    """做一張「登入框區域長得跟參考一樣」的圖。"""
    img = QImage(W, H, QImage.Format.Format_RGB32)
    img.fill(QColor(40, 120, 60))
    x0, y0, x1, y1 = game_screen.LOGIN_REGION.pixels(W, H)
    pw, ph = game_screen._PATCH_SIZE
    for row in range(ph):
        for col in range(pw):
            value = game_screen.LOGIN_BOX_REFERENCE[row * pw + col]
            colour = QColor(value, value, value)
            for y in range(y0 + (y1 - y0) * row // ph, y0 + (y1 - y0) * (row + 1) // ph):
                for x in range(x0 + (x1 - x0) * col // pw, x0 + (x1 - x0) * (col + 1) // pw):
                    img.setPixelColor(x, y, colour)
    return img


def test_detects_the_login_screen():
    """要跟參考縮圖對得上才算登入畫面。"""
    assert game_screen.detect(_with_login_box()) is Stage.LOGIN


def test_a_different_dialog_in_the_same_place_is_not_the_login_box():
    """遊戲的「公告／請稍候」位置幾乎重疊，只看淺色比例會認錯 ——
    然後對著沒有輸入框的公告打字，回報「輸入沒進去」（實際踩過）。"""
    notice = _canvas(game_screen.LOGIN_REGION)      # 一片純淺色，不是輸入框
    assert game_screen.login_box_difference(notice) > game_screen._MATCH_TOLERANCE
    assert game_screen.detect(notice) is Stage.UNKNOWN


def test_plain_background_is_unknown():
    """什麼對話框都沒有時回 UNKNOWN —— **不准猜**。"""
    assert game_screen.detect(_canvas()) is Stage.UNKNOWN


def test_reference_patch_has_the_declared_size():
    assert len(game_screen.LOGIN_BOX_REFERENCE) == (
        game_screen._PATCH_SIZE[0] * game_screen._PATCH_SIZE[1]
    )


def test_measured_thresholds_are_documented():
    """門檻改動要有實測依據，這裡把當初的數值釘住。"""
    assert game_screen._PRESENT == 0.70
    assert game_screen._ABSENT == 0.30
    # 實測值離門檻都有很大餘裕
    assert 0.892 > game_screen._PRESENT
    assert 0.054 < game_screen._ABSENT
    assert 0.891 > game_screen._PRESENT


def test_agree_button_ratio_matches_the_measurement():
    """1942x1256 的視窗上量到視窗座標 (1093,780)。"""
    x = int(1942 * game_screen.AGREE_BUTTON[0])
    y = int(1256 * game_screen.AGREE_BUTTON[1])
    assert abs(x - 1093) <= 2
    assert abs(y - 780) <= 2


def test_full_screen_capture_flag_is_the_working_one():
    """PrintWindow 要用 flag 2，用 0 的話 DirectX 內容不會被畫進來。"""
    assert game_screen.PW_RENDERFULLCONTENT == 2


# ---- 收尾（GDI）------------------------------------------------------------


class _Explodes:
    """每一個收尾動作都失敗的假物件。"""

    def __init__(self, log_):
        self._log = log_

    def DeleteDC(self):                      # noqa: N802 - 對齊 pywin32 命名
        self._log.append("dc")
        raise RuntimeError("DeleteDC failed")

    def GetHandle(self):                     # noqa: N802 - 對齊 pywin32 命名
        return 1234


def test_releasing_the_capture_never_raises(monkeypatch):
    """**收尾失敗不准把程式帶走。**

    使用者朋友的機器上，`win32ui.error: DeleteDC failed` 從 `capture()` 的
    `finally` 一路炸穿自動登入的工作執行緒 —— 那時他正被要求手動按「同意」，
    結果按了也沒人在等他（2026-08-29 的實機日誌）。
    收尾失敗頂多是資源沒還乾淨，不該讓抓圖失敗，更不該讓登入死掉。
    """
    calls: list[str] = []

    class _Gui:
        @staticmethod
        def DeleteObject(handle):            # noqa: N802 - 對齊 pywin32 命名
            calls.append("bitmap")
            raise RuntimeError("DeleteObject failed")

        @staticmethod
        def ReleaseDC(hwnd, dc):             # noqa: N802 - 對齊 pywin32 命名
            calls.append("release")
            raise RuntimeError("ReleaseDC failed")

    monkeypatch.setattr(game_screen, "win32gui", _Gui)
    broken = _Explodes(calls)
    game_screen._release_capture(1, 99, broken, broken)
    # 三步都要試過 —— 前面失敗不能讓後面的沒機會還。
    assert calls == ["dc", "bitmap", "release"]
