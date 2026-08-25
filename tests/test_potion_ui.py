"""自動補水的介面邏輯：下拉選單怎麼填、設定怎麼組出來。"""

from __future__ import annotations

import pytest

from ro_toolbox.ui.pages.farm_page import CharacterCard

pytest.importorskip("PySide6.QtWidgets")

# 501 紅色藥水補 HP、505 藍色藥水補 SP、940 蝗蟲後腿不是補品、
# 1201 小刀是裝備（iteminfo 裡查得到名字，但不補血也不補魔）
RED, BLUE, LEG, KNIFE = 501, 505, 940, 1201
#: 記憶體讀回來的背包：{格號: (道具編號, 數量)}（services/bag.py）
ROWS = {6: (RED, 30), 7: (BLUE, 12), 43: (LEG, 108), 2: (KNIFE, 1)}


@pytest.fixture
def card(qtbot):
    widget = CharacterCard()
    qtbot.addWidget(widget)
    return widget


def _entries(combo):
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def test_hp_combo_only_lists_hp_items(card):
    """只列補 HP 的。選單的值是**道具編號**，不是格號。"""
    card.set_slots(ROWS)
    ids = [data for _text, data in _entries(card.hp_item)]
    assert ids[0] is None            # 「未選擇」
    assert RED in ids                # 紅色藥水，補 HP
    assert BLUE not in ids           # 藍色藥水只補 SP
    assert LEG not in ids            # 蝗蟲後腿不是補品
    assert KNIFE not in ids          # 裝備不是補品


def test_sp_combo_only_lists_sp_items(card):
    card.set_slots(ROWS)
    ids = [data for _text, data in _entries(card.sp_item)]
    assert BLUE in ids
    assert RED not in ids


def test_items_show_only_name_and_count(card):
    """格號是內部資料，不給使用者看。"""
    card.set_slots(ROWS)
    texts = dict((data, text) for text, data in _entries(card.hp_item))
    assert texts[RED] == "紅色藥水 × 30"


def test_selection_survives_a_refresh(card):
    """每秒重讀背包（數量變了）不該把使用者選的清掉。"""
    card.set_slots(ROWS)
    card.hp_item.setCurrentIndex(card.hp_item.findData(RED))
    card.set_slots({**ROWS, 6: (RED, 29)})
    assert card.hp_item.currentData() == RED
    assert card.hp_item.currentText() == "紅色藥水 × 29"


def test_selection_survives_the_item_moving_to_another_slot(card):
    """道具換格號不影響選擇 —— 因為存的是道具編號（[MEM-028]）。"""
    card.set_slots(ROWS)
    card.hp_item.setCurrentIndex(card.hp_item.findData(RED))
    moved = {k: v for k, v in ROWS.items() if k != 6}
    card.set_slots({**moved, 18: (RED, 30)})
    assert card.hp_item.currentData() == RED


def test_config_is_read_from_the_widgets(card):
    card.set_slots(ROWS)
    card.hp_item.setCurrentIndex(card.hp_item.findData(RED))
    card.hp_threshold.setValue(55)
    card.sp_item.setCurrentIndex(card.sp_item.findData(BLUE))
    card.sp_threshold.setValue(30)
    config = card.potion_config()
    assert (config.hp_item, config.hp_percent) == (RED, 55)
    assert (config.sp_item, config.sp_percent) == (BLUE, 30)
    assert config.wants_hp() and config.wants_sp()


def test_threshold_range_is_zero_to_hundred(card):
    """直接打數字，範圍 0~100，超出的夾住。"""
    assert (card.hp_threshold.minimum(), card.hp_threshold.maximum()) == (0, 100)
    card.hp_threshold.setValue(250)
    assert card.hp_threshold.value() == 100
    card.hp_threshold.setValue(-5)
    assert card.hp_threshold.value() == 0


def test_threshold_has_no_spin_arrows_and_no_suffix(card):
    """沒有上下箭頭、% 不在輸入框裡 —— 使用者要求直接打數字。"""
    from PySide6.QtWidgets import QAbstractSpinBox

    assert card.hp_threshold.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
    assert card.hp_threshold.suffix() == ""
    card.hp_threshold.setValue(45)
    assert card.hp_threshold.text() == "45"


def test_zero_threshold_means_off(card):
    card.set_slots(ROWS)
    card.hp_item.setCurrentIndex(card.hp_item.findData(RED))
    card.hp_threshold.setValue(0)
    assert card.potion_config().wants_hp() is False


def test_empty_bag_leaves_only_the_placeholder(card):
    """讀不到背包時選單只剩「未選擇」—— 不編一個清單出來。"""
    card.set_slots(ROWS)
    assert card.hp_item.count() > 1
    card.set_slots({})
    assert [data for _text, data in _entries(card.hp_item)] == [None]


def test_counts_update_without_rebuilding_the_list(card):
    """數量每秒自己更新，但選單不重建 —— 重建會閃爍、也會打斷正在挑的人。"""
    card.set_slots(ROWS)
    before = [text for text, _d in _entries(card.hp_item)]
    card.set_slots({**ROWS, 6: (RED, 7)})
    after = [text for text, _d in _entries(card.hp_item)]
    assert before != after
    assert "紅色藥水 × 7" in after
    assert len(before) == len(after)


def test_selected_item_shows_its_icon(card):
    """選到的道具右邊要有小圖（解包出來的圖示）。"""
    card.set_slots(ROWS)
    card.hp_item.setCurrentIndex(card.hp_item.findData(RED))
    assert not card.hp_icon.pixmap().isNull(), "紅色藥水應該找得到圖示"


def test_bot_stopping_unchecks_the_box(card):
    """bot 自己停掉（藥水喝完／喝不到）就要把勾拿掉，不能勾著卻沒在跑。"""
    from ro_toolbox.services.potion import PotionStats

    card.auto_potion.setChecked(True)
    card._apply_potion_stats(PotionStats(running=False, note="⚠ 藥水用完了"))
    assert card.auto_potion.isChecked() is False
    assert card.potion_label.text() == "⚠ 藥水用完了"
