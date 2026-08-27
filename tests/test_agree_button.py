"""從畫面找「同意」按鈕：位置、解析度、DPI 都不該影響結果。

合約書是遊戲自己畫的**小視窗，而且可以拖動**，所以「用視窗大小算比例」本來就
靠不住。這一支盯的是找得到、找錯要說找不到、以及縮放之後還找得到。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtGui")
pytest.importorskip("numpy")

import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402

from ro_toolbox.services import game_screen  # noqa: E402

DPI = game_screen.AGREE_TEMPLATE_DPI


def _template() -> QImage:
    tpl = game_screen.agree_template(DPI)
    if tpl is None:
        pytest.skip("樣板檔不在（eula-agree.png）")
    return tpl


def _canvas(width: int, height: int) -> QImage:
    """一張有紋理的底圖 —— 全白的話任何東西都「像」，測不出鑑別力。"""
    rng = np.random.default_rng(1234)
    noise = rng.integers(40, 200, size=(height, width), dtype=np.uint8)
    image = QImage(width, height, QImage.Format.Format_Grayscale8)
    for y in range(height):
        for x in range(width):
            image.setPixel(x, y, int(noise[y, x]) * 0x010101)
    return image


def _paste(canvas: QImage, patch: QImage, x: int, y: int) -> QImage:
    out = QImage(canvas)
    painter = QPainter(out)
    painter.drawImage(x, y, patch)
    painter.end()
    return out


def test_it_finds_the_button_where_it_was_pasted():
    tpl = _template()
    screen = _paste(_canvas(900, 600), tpl, 300, 400)
    spot = game_screen.find_agree_button(screen, DPI)
    assert spot is not None
    x, y = spot
    assert abs(x - (300 + tpl.width() // 2)) <= 6
    assert abs(y - (400 + tpl.height() // 2)) <= 6


def test_moving_it_moves_the_answer():
    """對話框被拖走 —— 找出來的位置要跟著走，這正是不用比例法的理由。"""
    tpl = _template()
    first = game_screen.find_agree_button(_paste(_canvas(900, 600), tpl, 100, 80), DPI)
    second = game_screen.find_agree_button(_paste(_canvas(900, 600), tpl, 500, 300), DPI)
    assert first is not None and second is not None
    assert second[0] - first[0] == pytest.approx(400, abs=8)
    assert second[1] - first[1] == pytest.approx(220, abs=8)


def test_a_screen_without_the_button_says_so():
    """沒有按鈕就要回 None —— 硬給一個位置等於亂點。"""
    assert game_screen.find_agree_button(_canvas(900, 600), DPI) is None


@pytest.mark.parametrize("dpi", [96, 120, 192])
def test_other_dpi_still_finds_it(dpi):
    """別台機器的 DPI 不同，按鈕圖會跟著縮放 —— 樣板也要跟著縮。

    實測過 0.667／0.833／1.333 倍都命中，誤差 ≤2 像素。
    """
    scale = dpi / DPI
    tpl = _template().scaled(
        int(_template().width() * scale),
        int(_template().height() * scale),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    screen = _paste(_canvas(900, 600), tpl, 240, 260)
    spot = game_screen.find_agree_button(screen, dpi)
    assert spot is not None
    assert abs(spot[0] - (240 + tpl.width() // 2)) <= 8
    assert abs(spot[1] - (260 + tpl.height() // 2)) <= 8


def test_a_tiny_screen_does_not_explode():
    """視窗比樣板還小（最小化、剛開）—— 回 None，不要丟例外。"""
    assert game_screen.find_agree_button(_canvas(40, 20), DPI) is None
