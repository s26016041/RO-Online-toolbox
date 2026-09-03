"""「撿取黑名單」的視窗與卡片上那顆按鈕。

使用者指定（2026-09-04）：「跟寄信一樣點黑名單會出現一個視窗，然後可以打字
搜尋要加入的，搜尋的時候會出現名字跟物品圖案」、「這個是永遠開啟的，
所以不會有開關」。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402

from ro_toolbox.ui.pages.farm_page import CharacterCard  # noqa: E402
from ro_toolbox.ui.widgets.blacklist_dialog import BlacklistDialog  # noqa: E402

RED = 501          # 紅色藥水
JELLOPY = 909


@pytest.fixture
def dialog(qtbot):
    widget = BlacklistDialog(
        saved=[JELLOPY], character="商狐", bag_counts={RED: 12, JELLOPY: 300}
    )
    qtbot.addWidget(widget)
    return widget


def ids(listing) -> list[int]:
    return [
        listing.item(i).data(Qt.ItemDataRole.UserRole)
        for i in range(listing.count())
    ]


# ---- 視窗 ------------------------------------------------------------------


def test_it_opens_with_what_was_saved(dialog):
    assert ids(dialog.chosen) == [JELLOPY]


def test_nothing_is_listed_until_you_type(dialog):
    """⚠ 兩萬多筆全部列出來（每筆一張圖示）會把視窗卡死。"""
    assert dialog.found.count() == 0


def test_typing_searches_every_item_not_just_the_bag(dialog):
    """使用者指定要搜尋**全部**物品 —— 所以不需要背包，也不吃背包。"""
    dialog.search.setText("紅色藥水")
    assert RED in ids(dialog.found)


def test_each_row_shows_the_name_and_the_icon(dialog):
    """「搜尋的時候會出現名字跟物品圖案」。"""
    dialog.search.setText("紅色藥水")
    row = dialog.found.item(ids(dialog.found).index(RED))
    assert "紅色藥水" in row.text()
    assert not row.icon().isNull()


def test_picking_one_puts_it_on_the_list(dialog):
    dialog.search.setText("紅色藥水")
    dialog.found.item(ids(dialog.found).index(RED)).setSelected(True)
    dialog._add()
    assert set(ids(dialog.chosen)) == {JELLOPY, RED}


def test_adding_the_same_thing_twice_does_not_duplicate_it(dialog):
    dialog.search.setText("紅色藥水")
    for _ in range(2):
        dialog.found.item(ids(dialog.found).index(RED)).setSelected(True)
        dialog._add()
    assert ids(dialog.chosen).count(RED) == 1


def test_removing_takes_it_off(dialog):
    dialog.chosen.item(0).setSelected(True)
    dialog._remove()
    assert ids(dialog.chosen) == []


def test_cancel_changes_nothing(dialog):
    """`items` 只有按下儲存才有值 —— 取消不該把編輯到一半的名單洩出去。"""
    dialog.chosen.item(0).setSelected(True)
    dialog._remove()
    dialog.reject()
    assert dialog.items is None


def test_saving_hands_back_the_item_ids(dialog):
    dialog._accept()
    assert dialog.items == frozenset({JELLOPY})


def test_there_is_no_on_off_switch(dialog):
    """使用者指定「這個是永遠開啟的，所以不會有開關」。"""
    from PySide6.QtWidgets import QCheckBox
    assert dialog.findChildren(QCheckBox) == []


# ---- 「背包」那一頁（「在遊戲裡點右鍵」的替代品）---------------------------


def test_the_bag_tab_lists_what_we_are_carrying(dialog):
    """撿到垃圾之後最想擋的那一樣通常就在背包裡 —— 不用打字就找得到。"""
    assert set(ids(dialog.bag)) == {RED, JELLOPY}


def test_the_bag_rows_show_how_many_we_have(dialog):
    row = dialog.bag.item(ids(dialog.bag).index(RED))
    assert "紅色藥水" in row.text() and "12" in row.text()
    assert not row.icon().isNull()


def test_adding_from_the_bag_tab_works(dialog):
    dialog.tabs.setCurrentWidget(dialog.bag)
    dialog.bag.item(ids(dialog.bag).index(RED)).setSelected(True)
    dialog._add()
    assert set(ids(dialog.chosen)) == {JELLOPY, RED}


def test_the_add_button_follows_the_visible_tab(dialog):
    """兩頁共用一顆按鈕 —— 加的一定是**現在看得到的那一頁**選中的東西。"""
    dialog.search.setText("紅色藥水")
    dialog.found.item(ids(dialog.found).index(RED)).setSelected(True)
    dialog.tabs.setCurrentWidget(dialog.bag)      # 切到背包頁（那邊沒選東西）
    dialog._add()
    assert ids(dialog.chosen) == [JELLOPY]        # 沒有把搜尋頁的選取偷渡進來


def test_no_bag_does_not_block_the_window(qtbot):
    """背包還沒讀到只是那一頁列不出東西 —— 搜尋那一頁本來就不需要背包。"""
    widget = BlacklistDialog(saved=[], bag_counts={})
    qtbot.addWidget(widget)
    widget.search.setText("紅色藥水")
    assert RED in ids(widget.found)
    assert ids(widget.bag) == [None]               # 只有一行說明，不是道具


def test_an_item_with_no_name_is_still_listed(qtbot):
    """名單裡有、道具表查不到名字 —— 照樣列出來（顯示編號）。
    查不到名字就默默弄丟一條，使用者永遠不會知道。"""
    widget = BlacklistDialog(saved=[65534])
    qtbot.addWidget(widget)
    assert ids(widget.chosen) == [65534]
    assert "65534" in widget.chosen.item(0).text()


# ---- 卡片上的按鈕 -----------------------------------------------------------


@pytest.fixture
def card(qtbot):
    widget = CharacterCard()
    qtbot.addWidget(widget)
    return widget


def test_the_button_sits_under_the_mail_button(card):
    column = card.mail_button.parentWidget().layout()
    order = [column.itemAt(i).widget() for i in range(column.count())]
    assert order.index(card.blacklist_button) > order.index(card.mail_button)


def test_an_empty_list_shows_nothing(card):
    card.set_blacklist_summary([])
    assert not card.blacklist_summary.isVisibleTo(card)


def test_it_says_what_will_not_be_picked_up(card):
    """黑名單沒有開關，唯一能確認它有生效的方式就是這行字。"""
    card.set_blacklist_summary([RED])
    assert card.blacklist_summary.isVisibleTo(card)
    assert "紅色藥水" in card.blacklist_summary.text()


def test_a_long_list_is_trimmed(card):
    """整串倒出來會把側欄撐開，把主欄擠掉。"""
    card.set_blacklist_summary(range(501, 511))
    assert "等 10 樣" in card.blacklist_summary.text()


# ---- 「道具總攬」右鍵加入 ---------------------------------------------------
#
# 使用者本來要的是「在遊戲裡對道具點右鍵」。那條做不到（右鍵開的是客戶端自己的
# 說明視窗，不送封包，要接到只能注入 —— GAMEDATA [DAT-069]），這是替代品：
# 撿到垃圾的那一刻，在我們自己的清單上按右鍵。


@pytest.fixture
def page_with_loot(qtbot, monkeypatch, tmp_path):
    from ro_toolbox.services import loot_store, window_list
    from ro_toolbox.ui.pages.farm_page import FarmPage

    monkeypatch.setattr(loot_store, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(window_list, "enumerate_windows", lambda *a, **k: [])
    page = FarmPage()
    qtbot.addWidget(page)
    page._scan_timer.stop()
    page._read_timer.stop()
    page._names[1234] = "白狐"
    page._loot_totals[1234] = {RED: 3, JELLOPY: 40}
    monkeypatch.setattr(page, "_current_pid", lambda: 1234)
    page._refresh_loot()
    return page


def loot_row(page, item_id: int) -> int:
    for i in range(page.loot_table.rowCount()):
        cell = page.loot_table.item(i, 0)
        if cell.data(Qt.ItemDataRole.UserRole) == item_id:
            return i
    raise AssertionError(f"清單裡沒有 {item_id}")


def test_each_loot_row_carries_its_item_id(page_with_loot):
    """⚠ 編號要掛在列上 —— 從中文名反查會**安靜地擋錯東西**（同名的不只一個）。"""
    assert page_with_loot.loot_table.item(loot_row(page_with_loot, RED), 0).text() \
        == "紅色藥水"


def test_adding_from_the_loot_table_saves_and_shows_it(page_with_loot, qtbot):
    from ro_toolbox.services import loot_store
    from ro_toolbox.ui.pages.farm_page import CharacterCard

    card = CharacterCard()
    qtbot.addWidget(card)
    page_with_loot._cards[1234] = card
    page_with_loot._apply_blacklist(1234, {JELLOPY})
    assert loot_store.get("白狐") == frozenset({JELLOPY})
    assert card.blacklist_summary.isVisibleTo(card)


def test_adding_from_the_loot_table_reaches_a_running_bot(page_with_loot):
    """撿到垃圾按右鍵，**下一個就不撿了** —— 不用停掛機再開。"""
    class _Bot:
        def __init__(self):
            self.got = None

        def set_blacklist(self, ids):
            self.got = frozenset(ids)

    bot = _Bot()
    page_with_loot._bots[1234] = bot
    page_with_loot._apply_blacklist(1234, {JELLOPY})
    assert bot.got == frozenset({JELLOPY})
