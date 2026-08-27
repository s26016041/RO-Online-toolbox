"""從畫面認出「與伺服器斷線」對話框。

2026-08-28 06:14 實機採證（GAMEDATA [INP-012]）：RO 斷線時**不會**回到登入畫面，
它停在原本的世界畫面，中間跳一個遊戲自己畫的訊息框。`EnumChildWindows` 回
0 個子視窗 —— 那不是 Win32 視窗，讀不到標題，只能從畫面認。

這一支盯的是四件事：
  1. **認得出來** —— 貼上樣板就要找到，位置要對。
  2. **別的東西不算** —— 沒有框、只有雜訊、框裡寫別的字，都要說「沒有」。
  3. **縮放之後還認得** —— 別台機器的 DPI 不一樣，樣板會被縮放。
  4. **看不了要回 None，不是 False** —— 那是「不知道」，呼叫端要退回觀察期。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtGui")
pytest.importorskip("numpy")

import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402

from ro_toolbox.services import game_screen  # noqa: E402

DPI = game_screen.DISCONNECT_TEMPLATE_DPI


def _templates():
    pair = game_screen.disconnect_templates(DPI)
    if pair is None:
        pytest.skip("樣板檔不在（disconnect-box.png／disconnect-text.png）")
    return pair


def _canvas(width: int, height: int, seed: int = 1234) -> QImage:
    """有紋理的底圖 —— 全白的話任何東西都「像」，測不出鑑別力。"""
    rng = np.random.default_rng(seed)
    noise = rng.integers(40, 200, size=(height, width), dtype=np.uint8)
    image = QImage(width, height, QImage.Format.Format_Grayscale8)
    for y in range(height):
        for x in range(width):
            image.setPixel(x, y, int(noise[y, x]) * 0x010101)
    return image


def _paste(base: QImage, piece: QImage, x: int, y: int) -> QImage:
    out = base.convertToFormat(QImage.Format.Format_RGB32)
    painter = QPainter(out)
    painter.drawImage(x, y, piece)
    painter.end()
    return out


def _scaled(image: QImage, factor: float) -> QImage:
    return image.scaled(
        int(image.width() * factor),
        int(image.height() * factor),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


# ---- 認得出來 -----------------------------------------------------------


def test_finds_the_dialog_where_it_was_pasted():
    box, _text = _templates()
    screen = _paste(_canvas(1200, 800), box, 400, 300)
    spot = game_screen.find_disconnect_dialog(screen, DPI)
    assert spot is not None, "貼上去的斷線對話框要找得到"
    assert abs(spot[0] - 400) <= game_screen._DISCONNECT_SHRINK
    assert abs(spot[1] - 300) <= game_screen._DISCONNECT_SHRINK


def test_finds_it_anywhere_on_screen():
    """對話框可以被拖動，所以不准用「視窗大小算比例」那種寫法。"""
    box, _text = _templates()
    for x, y in ((10, 10), (700, 500), (450, 120)):
        screen = _paste(_canvas(1200, 800), box, x, y)
        spot = game_screen.find_disconnect_dialog(screen, DPI)
        assert spot is not None, f"貼在 ({x},{y}) 也要找得到"
        assert abs(spot[0] - x) <= game_screen._DISCONNECT_SHRINK
        assert abs(spot[1] - y) <= game_screen._DISCONNECT_SHRINK


# ---- 別的東西不算 -------------------------------------------------------


def test_plain_noise_is_not_a_disconnect():
    assert game_screen.find_disconnect_dialog(_canvas(1200, 800), DPI) is None


#: 反面對照：**同一次實機擷取**、同一個對話框裡的「確定」按鈕那一塊。
#: 一樣的白底、一樣的字型、不一樣的字 —— 這是「別的訊息框」最貼近的樣本。
OTHER_TEXT_FILE = Path(__file__).parent / "data" / "disconnect-other-text.png"


def _box_saying(piece: QImage, dpi: int = DPI) -> QImage:
    """把對話框裡那六個字換掉，做出「長一樣但寫別的」的訊息框。"""
    box, text = _templates()
    scale = dpi / DPI
    x = int(game_screen.DISCONNECT_TEXT_OFFSET[0] * scale)
    y = int(game_screen.DISCONNECT_TEXT_OFFSET[1] * scale)
    piece = piece.scaled(
        text.width(), text.height(),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    out = box.convertToFormat(QImage.Format.Format_RGB32)
    painter = QPainter(out)
    painter.fillRect(x - 2, y - 2, text.width() + 4, text.height() + 4,
                     Qt.GlobalColor.white)
    painter.drawImage(x, y, piece)
    painter.end()
    return out


def test_a_message_box_with_other_words_is_not_a_disconnect():
    """框長一樣但字不一樣（例如別的公告）**不准**當成斷線 ——
    誤判會把好好在玩的遊戲關掉重開。

    用的是同一次擷取裡「確定」按鈕那塊真實的遊戲文字，不是自己畫的假圖。
    """
    other = QImage(str(OTHER_TEXT_FILE))
    if other.isNull():
        pytest.skip(f"反面對照圖不在：{OTHER_TEXT_FILE}")
    screen = _paste(_canvas(1200, 800), _box_saying(other), 400, 300)
    assert game_screen.find_disconnect_dialog(screen, DPI) is None


def test_an_empty_message_box_is_not_a_disconnect():
    """框在但沒字也不算 —— 只認得出「有訊息框」是不夠的。"""
    blank = QImage(60, 14, QImage.Format.Format_Grayscale8)
    blank.fill(0xFFFFFF)
    screen = _paste(_canvas(1200, 800), _box_saying(blank), 400, 300)
    assert game_screen.find_disconnect_dialog(screen, DPI) is None


def test_too_small_a_screen_says_no():
    assert game_screen.find_disconnect_dialog(_canvas(60, 40), DPI) is None


# ---- 縮放之後還認得 -----------------------------------------------------


@pytest.mark.parametrize("dpi", [96, 120, 192])
def test_survives_other_dpi(dpi):
    """別台機器的 DPI 不一樣 —— 樣板會被縮放，還是要認得出來。

    ⚠ 這一條踩過：本來用平均差當判準，縮到 0.667 倍時對的字平均差 31.96，
    比門檻還高 —— **真的斷線會被判成沒斷線**。改用相關係數才過。
    """
    box, _text = _templates()
    screen = _paste(_canvas(1200, 800), box, 400, 300)
    factor = dpi / DPI
    spot = game_screen.find_disconnect_dialog(_scaled(screen, factor), dpi)
    assert spot is not None, f"DPI {dpi} 也要認得出來"
    slack = game_screen._DISCONNECT_SHRINK + 4
    assert abs(spot[0] - 400 * factor) <= slack
    assert abs(spot[1] - 300 * factor) <= slack


# ---- 看不了要回 None ----------------------------------------------------


def test_cannot_look_returns_none_not_false(monkeypatch):
    """最小化／抓不到畫面是「不知道」，**不是**「沒斷線」。

    回 False 的話 `ReconnectDecider` 會以為畫面確認過沒事，
    真的斷線就永遠等不到重連。
    """
    monkeypatch.setattr(game_screen, "available", lambda: True)
    monkeypatch.setattr(game_screen, "is_minimised", lambda _hwnd: True)
    assert game_screen.disconnected_by_look(1234) is None

    monkeypatch.setattr(game_screen, "is_minimised", lambda _hwnd: False)

    def _boom(_hwnd):
        raise game_screen.ScreenError("抓不到")

    monkeypatch.setattr(game_screen, "capture", _boom)
    assert game_screen.disconnected_by_look(1234) is None


def test_missing_template_is_unknown_not_healthy(monkeypatch):
    """樣板載不到也是「不知道」 —— 不准當成「沒斷線」。"""
    monkeypatch.setattr(game_screen, "available", lambda: True)
    monkeypatch.setattr(game_screen, "is_minimised", lambda _hwnd: False)
    monkeypatch.setattr(game_screen, "capture", lambda _hwnd: _canvas(400, 300))
    monkeypatch.setattr(game_screen, "disconnect_templates", lambda _dpi: None)
    assert game_screen.disconnected_by_look(1234) is None


def test_a_window_that_died_mid_check_is_unknown(monkeypatch):
    """視窗在 `find_window()` 與抓圖之間關掉是常態（斷線後使用者自己關掉遊戲）。

    ⚠ 踩過：那時 `GetWindowRect` 丟的是 `pywintypes.error`，不是 `ScreenError`，
    會一路逃到呼叫端。這條路只有兩種結果：看到了，或不知道。
    """
    monkeypatch.setattr(game_screen, "available", lambda: True)
    monkeypatch.setattr(game_screen, "is_minimised", lambda _hwnd: False)

    def _dead(_hwnd):
        raise OSError(1400, "無效的視窗控制代碼")

    monkeypatch.setattr(game_screen, "capture", _dead)
    assert game_screen.disconnected_by_look(1234) is None
