"""「在遊戲裡對道具按右鍵，程式認出它是什麼」。

使用者指定（2026-09-04）：「我想要我在遊戲右鍵或左鍵物品，程式可以識別，
然後加入黑名單」，隨後改口「**改成右鍵，不要左鍵**」。

⚠ 這裡釘住的重點是**認錯比認不出來嚴重**：加錯一樣道具＝從此再也不撿它，
而且完全不會有人發現。所以對不上一律回空的，不准挑一個最像的湊數。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtGui")

import numpy as np  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402

from ro_toolbox.services import item_pick  # noqa: E402

RED = 501          # 紅色藥水
JELLOPY = 909
ORANGE = 502       # 橘色藥水（跟紅色藥水長得像，但不一樣）

SIDE = item_pick.ICON_SIDE


def screen(width=200, height=150, background=(40, 40, 60)) -> np.ndarray:
    """一張假的遊戲畫面（純色底）。"""
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:, :] = background
    return canvas


def paste(canvas: np.ndarray, item_id: int, at: tuple[int, int]) -> np.ndarray:
    """把某個道具的圖示畫上去（洋紅當透明，跟遊戲一樣不畫）。"""
    arr, solid = item_pick.icon_array(item_id)
    assert arr is not None, f"{item_id} 沒有圖示，這個測試挑錯道具了"
    x, y = at
    block = canvas[y : y + SIDE, x : x + SIDE]
    block[solid] = arr.astype(np.uint8)[solid]
    return canvas


def as_image(canvas: np.ndarray) -> QImage:
    data = np.ascontiguousarray(canvas).tobytes()
    return QImage(
        data, canvas.shape[1], canvas.shape[0], canvas.shape[1] * 3,
        QImage.Format.Format_RGB888,
    ).copy()


# ---- 認得出來 --------------------------------------------------------------


def test_it_recognises_what_was_clicked():
    canvas = paste(screen(), RED, (60, 40))
    pick = item_pick.identify(as_image(canvas), (72, 52), {RED, JELLOPY})
    assert pick.items == (RED,)
    assert pick.score > 0.99


def test_clicking_a_corner_of_the_icon_still_works():
    """圖示只有 24×24 —— 不能要求使用者點正中央那一個像素。"""
    canvas = paste(screen(), JELLOPY, (60, 40))
    for point in ((60, 40), (83, 63), (60, 63), (83, 40)):
        pick = item_pick.identify(as_image(canvas), point, {RED, JELLOPY})
        assert pick.items == (JELLOPY,), point


def test_the_background_behind_the_transparent_pixels_does_not_matter():
    """洋紅是透明色，那些位置畫的是背包視窗的底 —— 比對要整個略過。"""
    for bg in ((0, 0, 0), (255, 255, 255), (12, 90, 200)):
        canvas = paste(screen(background=bg), RED, (60, 40))
        pick = item_pick.identify(as_image(canvas), (72, 52), {RED, JELLOPY})
        assert pick.items == (RED,), bg


def test_similar_looking_items_are_still_told_apart():
    """紅色藥水與橘色藥水只差顏色 —— 這種才是最容易安靜認錯的。"""
    canvas = paste(screen(), ORANGE, (60, 40))
    pick = item_pick.identify(as_image(canvas), (72, 52), {RED, ORANGE, JELLOPY})
    assert pick.items == (ORANGE,)


# ---- 認不出來的時候要說 ------------------------------------------------------


def test_clicking_empty_space_recognises_nothing():
    """⚠ 最重要的一條：沒東西就是沒東西，不准挑一個最像的。"""
    pick = item_pick.identify(as_image(screen()), (100, 75), {RED, JELLOPY})
    assert pick.items == ()
    assert pick.why


def test_a_half_covered_icon_is_not_guessed():
    """tooltip 蓋住一半的時候寧可叫他再點一次，也不要猜。"""
    canvas = paste(screen(), RED, (60, 40))
    canvas[40:52, 60:84] = (255, 255, 255)      # 上半被蓋掉
    pick = item_pick.identify(as_image(canvas), (72, 58), {RED, JELLOPY})
    assert pick.items == ()


def test_an_item_we_are_not_carrying_is_never_returned():
    """候選只有背包裡真的有的東西 —— 身上沒有的不該被認出來。"""
    canvas = paste(screen(), RED, (60, 40))
    pick = item_pick.identify(as_image(canvas), (72, 52), {JELLOPY})
    assert pick.items == ()


def test_no_bag_says_so_instead_of_guessing():
    pick = item_pick.identify(as_image(screen()), (100, 75), set())
    assert pick.items == ()
    assert "背包" in pick.why


def test_clicking_near_the_edge_says_why():
    """整格圖示不在畫面裡就一定認不出來 —— 直接說原因，不要跑一輪再說。"""
    pick = item_pick.identify(as_image(screen()), (3, 3), {RED})
    assert pick.items == ()
    assert "邊緣" in pick.why


# ---- 只認右鍵 ----------------------------------------------------------------


def test_only_the_right_button_is_watched():
    """使用者指定「改成右鍵，不要左鍵」。

    ⚠ 左鍵在背包裡是拿起／拖曳 —— 等的期間順手一點就會誤加一樣，
    而黑名單沒有開關，錯加的那一樣會安靜地一直生效。
    """
    assert item_pick._VK_RBUTTON == 0x02
    assert not hasattr(item_pick, "_VK"), "左鍵那條路要整個拿掉，不是留著不用"


def test_a_left_click_is_ignored(monkeypatch):
    """按左鍵不該被當成訊號 —— 一路等到逾時，回 None。"""
    watcher = item_pick.ClickWatcher.__new__(item_pick.ClickWatcher)
    watcher._hwnd = 1
    watcher._items = frozenset({RED})
    state = {"down": False}

    def _get_async_key_state(vk):
        # 左鍵一直壓著；右鍵從來沒按過
        return -0x8000 if vk == 0x01 and state["down"] else 0

    watcher._user32 = SimpleNamespace(GetAsyncKeyState=_get_async_key_state)
    monkeypatch.setattr(item_pick, "_POLL", 0.0)
    monkeypatch.setattr(
        watcher, "_at_cursor", lambda: pytest.fail("左鍵不該觸發辨識")
    )
    state["down"] = True
    assert watcher.wait(0.05) is None


# ---- 不准回傳 QImage 的 view（會閃退）----------------------------------------


def test_the_pixel_array_owns_its_memory():
    """⚠⚠ 2026-09-04 實機：在遊戲裡按右鍵，**整個程式當場閃退**。

    `_rgb_array()` 當時回傳的是 `convertToFormat()` 那張**暫時 QImage** 的 view，
    函式一 return 圖就被釋放 —— 之後每一次讀都是 use-after-free。
    測試裡看起來是好的（那塊記憶體還沒被拿去用），只有實機會炸。
    """
    arr = item_pick._rgb_array(as_image(screen()))
    assert arr.flags.owndata, "回傳 view = 借別人的記憶體，那張圖等一下就沒了"


def test_identify_survives_the_image_being_dropped():
    """畫面物件被丟掉之後，比對出來的東西還要是對的（不是垃圾、也不是崩潰）。"""
    import gc

    canvas = paste(screen(), RED, (60, 40))
    image = as_image(canvas)
    pick = item_pick.identify(image, (72, 52), {RED, JELLOPY})
    del image
    gc.collect()
    assert pick.items == (RED,)
