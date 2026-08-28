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
    noise = np.ascontiguousarray(
        rng.integers(40, 200, size=(height, width), dtype=np.uint8)
    )
    return QImage(
        noise.data, width, height, width, QImage.Format.Format_Grayscale8
    ).copy()


def _resize(image: QImage, scale: float) -> QImage:
    """把樣板縮放，模擬別台機器上按鈕的大小。"""
    return image.scaled(
        int(image.width() * scale),
        int(image.height() * scale),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


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


# ---- 換一台電腦：倍率不知道也要找得到 --------------------------------------


@pytest.mark.parametrize("scale", [0.5, 0.667, 0.833, 1.333, 1.8])
def test_it_finds_the_button_even_when_the_dpi_hint_is_wrong(scale):
    """**這一條是使用者朋友那台機器的回歸測試。**

    舊版只用 `GetDpiForWindow / 144` 算出**一個**倍率就去比對，猜錯就整個
    找不到 —— 而它很容易猜錯（客戶端不是 DPI-aware 時是 Windows 替它縮放，
    全螢幕拉伸更是差到 1.8 倍）。現在倍率是**從畫面裡試出來的**，
    所以這裡故意餵一個錯得離譜的 DPI 提示。
    """
    tpl = _resize(_template(), scale)
    screen = _paste(_canvas(900, 600), tpl, 260, 300)
    spot = game_screen.find_agree_button(screen, dpi=192)      # 提示故意給錯
    assert spot is not None
    assert abs(spot[0] - (260 + tpl.width() // 2)) <= 8
    assert abs(spot[1] - (300 + tpl.height() // 2)) <= 8


def test_the_match_says_which_scale_it_used():
    """找到之後要講得出憑什麼 —— 那些數字是別人的機器上唯一的線索。"""
    tpl = _resize(_template(), 0.667)
    screen = _paste(_canvas(900, 600), tpl, 100, 120)
    match = game_screen.find_agree_match(screen, dpi=0)
    assert match is not None and match.accepted
    assert match.scale == pytest.approx(0.667, abs=0.09)
    assert match.source == game_screen.AGREE_SOURCE_BUILTIN
    assert "倍" in match.describe()


def test_a_screen_without_the_button_says_so_at_every_scale():
    """試了一整排倍率之後**更**不能亂認 —— 沒有就是沒有。"""
    match = game_screen.find_agree_match(_canvas(900, 600), dpi=0)
    assert match is None or not match.accepted


# ---- 跟使用者學「它長什麼樣」 ----------------------------------------------


def _fake_button() -> QImage:
    """別的客戶端上那顆「同意」—— 內建樣板認不出來的東西。

    ⚠ 不要用白噪音當假按鈕：比對是**區塊平均**過的，噪音在平均之後只剩
    一片灰，測到的是演算法的相位誤差而不是它認不認得出東西。
    真的 UI 是成塊的色面加幾筆字 —— 這裡就照那個樣子做。
    """
    patch = np.full((36, 90), 210, dtype=np.uint8)
    patch[:3, :] = patch[-3:, :] = patch[:, :3] = patch[:, -3:] = 90   # 框
    for x in range(12, 78, 16):                                        # 「字」
        patch[10:26, x:x + 7] = 40
    patch[10:26, 40:47] = 150
    patch = np.ascontiguousarray(patch)
    return QImage(patch.data, 90, 36, 90, QImage.Format.Format_Grayscale8).copy()


def _use_temp_data_dir(monkeypatch, tmp_path):
    from ro_toolbox.config import paths

    monkeypatch.setattr(paths, "user_data_dir", lambda: tmp_path)


def test_learning_the_look_survives_the_window_moving(monkeypatch, tmp_path):
    """學的是**樣子**不是位置：對話框被拖到別的地方照樣找得到。

    這正是舊做法（存比例）壞掉的地方 —— 使用者朋友教過的位置指到空的地方，
    而且點空了不會有任何錯誤。
    """
    _use_temp_data_dir(monkeypatch, tmp_path)
    # 故意**不是**內建那顆按鈕：學到的樣子要派上用場，正是在內建樣板認不出來的
    # 機器上（別的客戶端版本、別的語系）。內建樣板在這張圖上會落空。
    button = _fake_button()
    first = _paste(_canvas(900, 600), button, 200, 150)
    click = (200 + button.width() // 2, 150 + button.height() // 2)

    assert game_screen.save_learned_agree(first, *click)
    assert (tmp_path / game_screen.LEARNED_AGREE_FILE).is_file()

    moved = _paste(_canvas(900, 600), button, 480, 320)
    match = game_screen.find_agree_match(moved, dpi=0)
    assert match is not None and match.accepted
    assert match.source == game_screen.AGREE_SOURCE_LEARNED
    assert abs(match.x - (480 + button.width() // 2)) <= 8
    assert abs(match.y - (320 + button.height() // 2)) <= 8


def test_learning_refuses_a_click_too_close_to_the_edge(monkeypatch, tmp_path):
    """剪不出完整的一塊就**不要學** —— 學一塊缺角的等於學了個錯的。"""
    _use_temp_data_dir(monkeypatch, tmp_path)
    assert not game_screen.save_learned_agree(_canvas(900, 600), 5, 5)
    assert not (tmp_path / game_screen.LEARNED_AGREE_FILE).is_file()


def test_no_learned_template_is_not_an_error(monkeypatch, tmp_path):
    """沒教過就是沒教過，回 None，不要丟例外。"""
    _use_temp_data_dir(monkeypatch, tmp_path)
    assert game_screen.learned_agree_template() is None


def test_learning_refuses_a_blank_screen(monkeypatch, tmp_path):
    """抓回來一片黑的時候**不准學**。

    `PrintWindow` 在全螢幕模式（或某些顯示卡）上會回一張全黑的圖。
    把一塊黑存成樣板的後果不是「下次找不到」，而是**下次到處都找得到** ——
    然後很有自信地點在螢幕的隨便一個角落。寧可退回比例法。
    """
    _use_temp_data_dir(monkeypatch, tmp_path)
    black = QImage(900, 600, QImage.Format.Format_Grayscale8)
    black.fill(0)
    assert not game_screen.save_learned_agree(black, 450, 300)
    assert not (tmp_path / game_screen.LEARNED_AGREE_FILE).is_file()
