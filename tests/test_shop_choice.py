"""使用者自己設定藥水商人（2026-09-05）：存得住、只去那一家、視窗列得出來。

使用者：「商人我們根本不知道要找誰，所以改成使用者自己設定 —— 補水右邊多一個
『設定藥水商人』按鈕，先選城鎮再選商人，這樣就不會有找不到藥水商人的問題。」
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6.QtWidgets")

from ro_toolbox.services import potion_store
from ro_toolbox.services import restock_bot as mod
from ro_toolbox.services.potion_store import PotionSaved
from ro_toolbox.services.restock_bot import RestockBot

PID = 4242
IZLUDE_IN = "izlude_in"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from ro_toolbox.services import shop_reach

    monkeypatch.setattr(potion_store, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(shop_reach, "_path", lambda: tmp_path / "shop_reach.json")
    return tmp_path


def _first_seller(map_name: str):
    sellers = mod.potion_sellers_on(map_name)
    if not sellers:
        pytest.skip(f"NPC 表裡 {map_name} 沒有藥水商人（資產沒載到）")
    return sellers[0]


# ---- 存檔 ------------------------------------------------------------------------

def test_the_chosen_shop_survives_a_round_trip():
    potion_store.save("狐狐狸", PotionSaved(hp_item=502, shop_map=IZLUDE_IN, shop_cell=(57, 110)))
    got = potion_store.get("狐狐狸")
    assert got.shop == (IZLUDE_IN, (57, 110))


def test_a_half_written_or_garbage_shop_is_dropped(_isolated):
    (_isolated / "potion_settings.json").write_text(
        json.dumps({
            "a": {"shop_map": IZLUDE_IN},                         # 沒有格子
            "b": {"shop_cell": [57, 110]},                        # 沒有地圖
            "c": {"shop_map": IZLUDE_IN, "shop_cell": [0, 0]},    # (0,0) 是邊界
            "d": {"shop_map": IZLUDE_IN, "shop_cell": ["x", 1]},
        }),
        encoding="utf-8",
    )
    for who in "abcd":
        assert potion_store.get(who).shop is None, who


def test_the_shop_is_not_wiped_by_an_unrelated_save():
    """[DAT-073]：沒動過的欄位不准被空值蓋掉 —— 商人也一樣。"""
    potion_store.save("狐狐狸", PotionSaved(hp_item=502, shop_map=IZLUDE_IN, shop_cell=(57, 110)))
    stored = potion_store.save("狐狐狸", PotionSaved(hp_item=502))
    assert stored.shop == (IZLUDE_IN, (57, 110))


def test_the_user_can_go_back_to_automatic():
    potion_store.save("狐狐狸", PotionSaved(hp_item=502, shop_map=IZLUDE_IN, shop_cell=(57, 110)))
    stored = potion_store.save(
        "狐狐狸", PotionSaved(hp_item=502), cleared=frozenset({"shop_map", "shop_cell"})
    )
    assert stored.shop is None
    assert potion_store.get("狐狐狸").shop is None


# ---- RestockBot：只去那一家 ---------------------------------------------------------

def _bot_on(map_name: str, shop, monkeypatch) -> RestockBot:
    from ro_toolbox.services import character

    class _Status:
        def __init__(self) -> None:
            self.map_name = map_name

    class _Reader:
        def attach(self, pid, should_stop=None):  # noqa: ARG002
            return True

        def read(self):
            return _Status()

        def close(self) -> None:
            pass

    monkeypatch.setattr(character, "CharacterReader", _Reader)
    return RestockBot(PID, hp_item=502, shop=shop)


def test_the_chosen_shop_is_used_even_when_another_is_closer(monkeypatch):
    x, y, name, look = _first_seller(IZLUDE_IN)
    bot = _bot_on("prontera", (IZLUDE_IN, (x, y)), monkeypatch)
    plan = bot._find_shop()
    assert plan is not None
    target_map, cell, seller_cell, got_look, got_name = plan
    assert (target_map, seller_cell, got_look, got_name) == (IZLUDE_IN, (x, y), look, name)
    assert cell != (x, y), "要走到他旁邊，不是走到他身上（[DAT-066]）"


def test_the_chosen_shop_ignores_the_unreachable_memory(monkeypatch):
    """使用者指定的就是答案 —— 以前記的「走不到」不准替他改主意。"""
    from ro_toolbox.services import shop_reach

    x, y, *_ = _first_seller(IZLUDE_IN)
    shop_reach.note_bad(IZLUDE_IN, (x, y))
    bot = _bot_on("prontera", (IZLUDE_IN, (x, y)), monkeypatch)
    assert bot._find_shop() is not None


def test_an_unknown_shop_is_refused_loudly(monkeypatch):
    bot = _bot_on("prontera", (IZLUDE_IN, (1, 1)), monkeypatch)
    assert bot._find_shop() is None
    assert "找不到" in bot.stats.note and "重新設定" in bot.stats.note


def test_it_never_switches_to_another_shop(monkeypatch):
    """走不到就講清楚是**你設定的那家**走不到，不換家。"""
    x, y, name, _look = _first_seller(IZLUDE_IN)
    bot = _bot_on("prontera", (IZLUDE_IN, (x, y)), monkeypatch)
    assert bot._find_shop({(IZLUDE_IN, (x, y))}) is None
    assert name in bot.stats.note and "走不到" in bot.stats.note


def test_without_a_choice_the_old_nearest_logic_still_runs(monkeypatch):
    bot = _bot_on(IZLUDE_IN, None, monkeypatch)
    plan = bot._find_shop()
    assert plan is not None and plan[0] == IZLUDE_IN


# ---- 視窗 ------------------------------------------------------------------------

def test_the_dialog_lists_towns_then_sellers(qtbot):
    from ro_toolbox.ui.widgets.shop_dialog import ShopDialog

    x, y, name, _look = _first_seller(IZLUDE_IN)
    dialog = ShopDialog(current=(IZLUDE_IN, (x, y)))
    qtbot.addWidget(dialog)
    assert dialog.town.count() > 1
    assert dialog.town.currentData() == IZLUDE_IN, "開起來就停在上次選的城鎮"
    assert dialog.picked() == (IZLUDE_IN, (x, y)), "上次選的商人要是選中的"
    assert name in dialog.sellers.currentItem().text()

    dialog._use_auto()
    assert dialog.choice == ("", None)


def test_towns_are_ordered_nearest_first_from_the_current_map(qtbot, monkeypatch):
    """★ 使用者（2026-09-05）：城鎮清單要照當前位置由近排到遠。"""
    from ro_toolbox.ui.widgets import shop_dialog

    codes = list(shop_dialog.maps_with_potion_sellers())
    if len(codes) < 3:
        pytest.skip("藥水商人地圖太少，排序看不出來")
    # 塞一組已知距離：把清單裡第 3 個設成最近、其它兩個依序遠一點
    near, mid, far = codes[2], codes[0], codes[1]
    fake = {near: 0, mid: 1, far: 2}
    monkeypatch.setattr(shop_dialog, "map_hop_distances", lambda *a, **k: fake)

    dialog = shop_dialog.ShopDialog(current_map="wherever")
    qtbot.addWidget(dialog)
    order = [dialog.town.itemData(i) for i in range(dialog.town.count())]
    assert order.index(near) < order.index(mid) < order.index(far), "近的要排前面"
    other = next(c for c in codes if c not in fake)
    assert order.index(far) < order.index(other), "有距離的排在沒距離的前面"


def test_without_a_current_map_towns_fall_back_to_name_order(qtbot, monkeypatch):
    """讀不到當前位置（沒登入／還沒讀到）就退回照名字排 —— 不能整個空掉。"""
    from ro_toolbox.ui.widgets import shop_dialog

    def _boom(*_a, **_k):
        raise AssertionError("沒有當前地圖就不該去算距離")

    monkeypatch.setattr(shop_dialog, "map_hop_distances", _boom)
    dialog = shop_dialog.ShopDialog(current_map="")
    qtbot.addWidget(dialog)
    assert dialog.town.count() > 1


def test_the_dialog_returns_what_was_picked(qtbot):
    from ro_toolbox.ui.widgets.shop_dialog import ShopDialog

    x, y, *_ = _first_seller(IZLUDE_IN)
    dialog = ShopDialog()
    qtbot.addWidget(dialog)
    dialog.town.setCurrentIndex(dialog.town.findData(IZLUDE_IN))
    dialog.sellers.setCurrentRow(0)
    dialog._accept()
    assert dialog.choice == (IZLUDE_IN, (x, y))


def test_describe_shop_names_the_town_and_the_merchant():
    from ro_toolbox.ui.widgets.shop_dialog import describe_shop

    x, y, name, _look = _first_seller(IZLUDE_IN)
    text = describe_shop(IZLUDE_IN, (x, y))
    assert name in text and "自動" not in text
    assert "自動" in describe_shop(None, None)
