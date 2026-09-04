"""自動補水的介面邏輯：下拉選單怎麼填、設定怎麼組出來。"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QIcon

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
    """選到的道具右邊要有小圖（解包出來的圖示）。

    圖示來自 `RODATA/`（24 萬檔、18 GB），**不隨版發布**。所以沒有那份
    解包資料的機器（乾淨 clone、CI）跳過這一項 —— 不是放水：
    下一個測試會驗「沒有圖示時介面照樣正常」，那才是那些機器上的真實情境。
    """
    from ro_toolbox.services import icons

    if icons.icon_path(RED) is None:
        pytest.skip("本機沒有 RODATA 解包資料，沒有圖示可驗")
    card.set_slots(ROWS)
    card.hp_item.setCurrentIndex(card.hp_item.findData(RED))
    assert not card.hp_icon.pixmap().isNull(), "紅色藥水應該找得到圖示"


def test_the_list_still_works_without_any_icons(card, monkeypatch):
    """找不到圖示只是少一張圖，名稱與數量照樣要在（安全退化）。

    這是**沒有 RODATA 的機器上的真實情境** —— 一定要正常，不能空白也不能爆。
    """
    from ro_toolbox.ui.pages import farm_page

    monkeypatch.setattr(farm_page, "item_icon", lambda _item_id: QIcon())
    card.set_slots(ROWS)
    texts = dict((data, text) for text, data in _entries(card.hp_item))
    assert texts[RED] == "紅色藥水 × 30"
    card.hp_item.setCurrentIndex(card.hp_item.findData(RED))
    assert card.potion_config().hp_item == RED


def test_the_box_stays_ticked_even_when_the_bot_stops(card):
    """⚠⚠ 使用者指定（2026-08-30）：「**自動補水不管何時都不需要關閉**」。

    藥水喝完是去補貨的理由，不是關掉補水的理由 —— 關掉就是下一次沒人喝水。
    「勾著卻沒在跑」由頁面的 `_watch_potion_alive()` 負責把它補回來，
    不是把勾勾拿掉。

    原因由 `PotionBot._note()` 記進執行日誌 —— 這裡**不能再記一次**，
    兩邊都記會把同一句話印兩遍。
    """
    from ro_toolbox.services.potion import PotionStats

    card.set_note("PID 1234")
    card.auto_potion.setChecked(True)
    card._apply_potion_stats(PotionStats(running=False, note="⚠ 藥水用完了"))
    assert card.auto_potion.isChecked() is True
    assert card.status_label.text() == "PID 1234", "卡片上只放 PID"


def test_the_potion_bot_logs_it_once(caplog):
    import logging

    from ro_toolbox.services.potion import PotionBot, PotionConfig

    bot = PotionBot(1234, PotionConfig())
    with caplog.at_level(logging.INFO, logger="ro_toolbox.services.potion"):
        bot._note("⚠ 藥水用完了")
        bot._note("⚠ 藥水用完了")
    assert [r.getMessage() for r in caplog.records] == ["⚠ 藥水用完了"]


# ---- 存檔還原：程式改 UI 不算使用者的意思 ---------------------------------


def test_restoring_saved_settings_does_not_count_as_a_user_change(card):
    """還原存檔時 `quiet` 要立起來 —— 否則會立刻再存一次（無害但沒必要），
    更重要的是同一道閘門擋著「bot 失敗自動取消勾選」覆蓋設定。"""
    from ro_toolbox.services.potion_store import PotionSaved

    seen = []
    card.potion_changed.connect(lambda: seen.append(card.quiet))
    card.potion_toggled.connect(lambda _on: seen.append(card.quiet))
    card.apply_saved_potion(PotionSaved(hp_percent=55, enabled=True))

    assert seen, "應該有發出變動訊號"
    assert all(quiet is True for quiet in seen), "還原期間 quiet 必須是 True"
    assert card.quiet is False, "還原完要放下來"
    assert card.hp_threshold.value() == 55
    assert card.auto_potion.isChecked() is True


def test_a_failed_start_never_switches_auto_potion_off(card):
    """⚠⚠ 使用者指定（2026-08-30）：「**自動補水不管何時都不需要關閉**」。

    喝水是活命的東西 —— 停用不是安全退化，是致命的（實機踩過：補水被停用
    之後角色死了）。啟動失敗（沒登入、定位失敗）只是這一次沒起來，
    勾勾要留著，頁面的 `_watch_potion_alive()` 會把它重新開起來。
    """
    from ro_toolbox.services.potion import PotionStats

    card.auto_potion.setChecked(True)
    card._apply_potion_stats(PotionStats(running=False, note="找不到遊戲 socket"))
    assert card.auto_potion.isChecked() is True


def test_saved_settings_keep_the_item_id_not_the_slot(card):
    """存的是道具編號 —— 格號會挪動，存格號遲早會喝錯東西（[MEM-028]）。"""
    card.set_slots({44: (501, 10), 45: (505, 3)})
    card.hp_item.setCurrentIndex(card.hp_item.findData(501))
    saved = card.saved_potion()
    assert saved.hp_item == 501


def test_the_restored_item_shows_up_before_the_bag_does(card):
    """★ 背包是非同步讀的 —— 但**記住的東西不必等它**。

    使用者兩次回報「藥水跟回程使用物品一直不見沒紀錄」。真正看到的是：
    分頁剛長出來時下拉一片「未選擇」（背包還在背景讀，而且**只讀正在看的
    那一頁**），看起來就像沒記住。存的是道具編號，所以這裡直接把它放上去，
    數量寫 0，等背包回來再補上真的數量。
    """
    from ro_toolbox.services.potion_store import PotionSaved

    card.apply_saved_potion(PotionSaved(hp_item=501, hp_percent=40))
    assert card.hp_item.currentData() == 501, "記住的那個要馬上看得到"
    assert "× 0" in card.hp_item.currentText(), "數量還不知道，先寫 0"

    card.set_slots({44: (501, 10)})
    assert card.hp_item.currentData() == 501, "背包回來還是同一個"
    assert "× 10" in card.hp_item.currentText(), "數量補上了"


# ---- 水用完回程 --------------------------------------------------------


def test_home_combo_lists_the_whole_bag(card):
    """回程那個下拉**不過濾** —— 道具表認不出哪個是回程道具
    （蝴蝶翅膀寫「移動至儲存的位置」、蒼蠅翅膀寫「移動至任意的位置」，
    差別只在描述文字），靠關鍵字猜就是很有自信的錯。所以讓人自己挑。"""
    card.set_slots(ROWS)
    ids = [data for _text, data in _entries(card.home_item)]
    for item_id in (RED, BLUE, LEG, KNIFE):
        assert item_id in ids, f"{item_id} 應該也列出來"


def test_unchecked_means_no_return_item_in_the_config(card):
    """沒勾就不能把道具帶進設定 —— 沒勾卻回程是安靜地做錯事。"""
    card.set_slots(ROWS)
    card.home_item.setCurrentIndex(card.home_item.findData(LEG))
    card.go_home.setChecked(False)
    assert card.potion_config().home_item is None
    card.go_home.setChecked(True)
    assert card.potion_config().home_item == LEG


def test_go_home_settings_are_saved_and_restored(card):
    from ro_toolbox.services.potion_store import PotionSaved

    card.set_slots(ROWS)
    card.apply_saved_potion(PotionSaved(
        hp_item=RED, hp_percent=50, go_home=True, home_item=LEG,
    ))
    assert card.go_home.isChecked() is True
    assert card.home_item.currentData() == LEG
    saved = card.saved_potion()
    assert saved.go_home is True
    assert saved.home_item == LEG


def test_auto_hunt_is_deliberately_not_saved(card):
    """⚠ 只有自動戰鬥不記錄（使用者指定）。開著程式回來就繼續打怪
    太容易變成意外掛機；其他設定存回來只是填好表單，不會自己動作。"""
    from ro_toolbox.services.potion_store import PotionSaved

    card.auto_hunt.setChecked(True)
    assert not hasattr(card.saved_potion(), "auto_hunt")
    card.apply_saved_potion(PotionSaved(hp_item=RED, hp_percent=50))
    assert card.auto_hunt.isChecked() is True, "還原設定不該去動自動戰鬥"


def test_going_home_tells_the_page_instead_of_touching_auto_hunt(card):
    """已經回城了還勾著自動打怪，只會站在城裡空轉 —— 但**卡片不准自己動它**。

    ⚠⚠ 卡片直接 `setChecked(False)` 的話，頁面那邊會把它讀成「使用者不想
    掛機了」而把 `_want_farm` 清掉 —— 補完貨走回練功點之後就再也接不回去
    （2026-08-30 實機：日誌印了「掛機接回去了」，FarmBot 卻從頭到尾沒啟動）。
    所以只發訊號，讓頁面用 `keep_intent=True` 去關。
    """
    from ro_toolbox.services.potion import PotionStats

    said = []
    card.auto_hunt.setChecked(True)
    card.went_home.connect(lambda: said.append(True))
    card._apply_potion_stats(
        PotionStats(running=False, went_home=True, note="已用蝴蝶翅膀回程")
    )
    assert said == [True], "要通知頁面"
    assert card.auto_hunt.isChecked() is True, "卡片不准自己拿掉勾勾"


def test_the_card_only_ever_shows_the_pid(card):
    """⚠ 卡片上唯一的那行字是 PID。提示字一律進執行日誌，不放介面。"""
    from ro_toolbox.services.potion import PotionStats

    card.set_note("PID 1234")
    card._apply_potion_stats(PotionStats(running=True, note="HP 60% → 喝了第 6 格"))
    assert card.status_label.text() == "PID 1234"
    card._apply_potion_stats(PotionStats(running=True, note="⚠ 連續喝不到"))
    assert card.status_label.text() == "PID 1234", "連警示都不上介面"



# ---- 「有沒有開自動補水」要真的記得住 ------------------------------------


def test_enabled_survives_a_restart(card):
    """使用者：「是否使用藥水的那個選擇要記錄」。"""

    card.set_slots(ROWS)
    card.hp_item.setCurrentIndex(card.hp_item.findData(RED))
    card.hp_threshold.setValue(60)
    card.auto_potion.setChecked(True)
    saved = card.saved_potion()
    assert saved.enabled is True

    back = CharacterCard()
    back.set_slots(ROWS)
    back.apply_saved_potion(saved)
    assert back.auto_potion.isChecked() is True
    assert back.hp_item.currentData() == RED


def test_pending_items_means_the_bag_has_not_arrived_yet(card):
    """⚠ 開程式時背包是**背景**讀的，還原存檔時下拉通常還是空的。

    這時候「看起來沒選道具」只是暫時的 —— 不能拿它當作「使用者沒設定」。
    """
    from ro_toolbox.services.potion_store import PotionSaved

    card.apply_saved_potion(PotionSaved(hp_item=RED, hp_percent=60, enabled=True))
    assert card.pending_items() == [RED], "應該記著還在等哪個道具"
    assert card.auto_potion.isChecked() is True

    card.set_slots(ROWS)                       # 背包回來了
    assert card.pending_items() == []
    assert card.hp_item.currentData() == RED


# ---- ⛔ 記住的道具不准被洗成「未選擇」（2026-09-03：一直沒紀錄）------------


def _page(monkeypatch, tmp_path):
    """一個只有「存設定」那幾條線的 FarmPage（不開 timer、不掃 process）。"""
    from ro_toolbox.services import potion_store
    from ro_toolbox.ui.pages.farm_page import FarmPage

    monkeypatch.setattr(potion_store, "_path",
                        lambda: tmp_path / "potion_settings.json")
    page = FarmPage.__new__(FarmPage)
    page._cards = {}
    page._bags = {}
    page._potions = {}
    return page


def test_a_remembered_item_survives_a_save_while_the_bag_is_unread(
    qtbot, monkeypatch, tmp_path
):
    """★★ 使用者 2026-09-03：「我的程式一直沒紀錄　藥水跟回程道具」。
    2026-09-04 再一次：「為何每次重開我之前設定的藥水以及回程道具都要重新設定」。

    下拉是照背包重建的，而背包是背景執行緒讀的、又只讀「正在看的那一頁」。
    背景分頁的下拉可能一直是空的 —— 舊版的 `saved_potion()` 就是去掃那三個
    下拉，所以**任何別的動作**（改門檻、勾自動補水、勾水用完回程）都會觸發
    存檔，而空下拉只能回 `None`，一次就把記住的藥水與回程道具洗掉。

    現在設定本尊是 `card._settings`，畫面只是它的顯示方式 —— 下拉空不空
    都跟「使用者記住的是什麼」無關。
    """
    from ro_toolbox.services import potion_store
    from ro_toolbox.services.potion_store import PotionSaved
    from ro_toolbox.ui.pages.farm_page import CharacterCard

    orange, wing = 502, 23455
    page = _page(monkeypatch, tmp_path)
    card = CharacterCard()
    qtbot.addWidget(card)
    card.character = "狐狐狸"
    page._cards[1] = card
    potion_store.save("狐狐狸", PotionSaved(hp_item=orange, hp_percent=60,
                                          home_item=wing, go_home=True, enabled=True))

    card.apply_saved_potion(potion_store.get("狐狐狸"))
    assert card.hp_item.currentData() == orange, "前提：還原時有選起來"

    # 背包一直讀不到（背景分頁）→ 下拉被重建成只剩「未選擇」
    for combo in (card.hp_item, card.home_item):
        combo.clear()
        combo.addItem("未選擇", None)
    # 使用者只是改了門檻 —— 這**不是**「把藥水清掉」的意思
    card.hp_threshold.setValue(45)
    page._save_potion(1)

    after = potion_store.get("狐狐狸")
    assert after.hp_percent == 45, "改的那一項要存進去"
    assert after.hp_item == orange, "背包沒讀到，不准把記住的藥水洗掉"
    assert after.home_item == wing, "回程道具同理"


def test_the_user_really_can_clear_it_once_the_bag_is_read(
    qtbot, monkeypatch, tmp_path
):
    """⚠ 保險不可以變成「改不掉」：背包讀到了，空白就是使用者的意思。"""
    from ro_toolbox.services import potion_store
    from ro_toolbox.services.potion_store import PotionSaved
    from ro_toolbox.ui.pages.farm_page import CharacterCard

    page = _page(monkeypatch, tmp_path)
    card = CharacterCard()
    qtbot.addWidget(card)
    card.character = "狐狐狸"
    page._cards[1] = card
    page._bags[1] = {6: (502, 5)}          # 這一頁的背包讀到了
    potion_store.save("狐狐狸", PotionSaved(hp_item=502, home_item=23455))

    card.apply_saved_potion(potion_store.get("狐狐狸"))
    card._want_item = {"hp": None, "sp": None, "home": None}
    for combo in (card.hp_item, card.home_item):
        # 使用者自己在下拉裡挑「未選擇」。Qt 挑完會發 `activated`，
        # **程式改值不會** —— 這一整條保險就是靠這個分辨真人的。
        combo.setCurrentIndex(combo.findData(None))
        combo.activated.emit(combo.currentIndex())
    page._save_potion(1)

    assert potion_store.get("狐狐狸").hp_item is None
    assert potion_store.get("狐狐狸").home_item is None


def test_the_program_clearing_a_combo_is_not_the_user_clearing_it(
    qtbot, monkeypatch, tmp_path
):
    """★★ 第五次回報的根因（[DAT-073]）：**只有真人能把設定清掉**。

    上面那條保險看的是「背包讀到了沒」，繞得過去 —— 下拉被重建、
    分頁剛長出來、回連把卡片重接，這幾條路上下拉都是「有清單但選在
    未選擇」，跟使用者自己清掉長得一模一樣。實機就是這樣把
    502／50%／23455 洗成 `PotionSaved(enabled=True)` 的。

    現在分辨真人靠 Qt 的 `activated`／`clicked`（程式改值不發），
    而且最後一道關卡在**存檔那一層**，介面怎麼繞都繞不過去。
    """
    from ro_toolbox.services import potion_store
    from ro_toolbox.services.potion_store import PotionSaved
    from ro_toolbox.ui.pages.farm_page import CharacterCard

    page = _page(monkeypatch, tmp_path)
    card = CharacterCard()
    qtbot.addWidget(card)
    card.character = "狐狐狸"
    page._cards[1] = card
    page._bags[1] = {6: (502, 5)}
    potion_store.save("狐狐狸", PotionSaved(
        hp_item=502, hp_percent=50, home_item=23455, go_home=True, enabled=True))

    card.apply_saved_potion(potion_store.get("狐狐狸"))
    card._want_item = {"hp": None, "sp": None, "home": None}
    # 程式把下拉洗成「未選擇」、順手把勾勾拿掉（回連、重建清單都會這樣）
    card.hp_item.setCurrentIndex(card.hp_item.findData(None))
    card.home_item.setCurrentIndex(card.home_item.findData(None))
    card.go_home.setChecked(False)
    page._save_potion(1)

    after = potion_store.get("狐狐狸")
    assert after.hp_item == 502, "沒有真人動過，不准把藥水清掉"
    assert after.home_item == 23455, "回程道具同理"
    assert after.go_home is True
    assert card.saved_potion().hp_item == 502, "擋下來之後卡片也要接回正確那份"


def test_a_card_that_never_read_the_store_is_never_written_back(
    qtbot, monkeypatch, tmp_path
):
    """沒讀過存檔就不准寫回去 —— 那是拿一張空卡片去蓋掉使用者的設定。"""
    from ro_toolbox.services import potion_store
    from ro_toolbox.services.potion_store import PotionSaved
    from ro_toolbox.ui.pages.farm_page import CharacterCard

    page = _page(monkeypatch, tmp_path)
    card = CharacterCard()
    qtbot.addWidget(card)
    card.character = "狐狐狸"
    page._cards[1] = card
    potion_store.save("狐狐狸", PotionSaved(hp_item=502, hp_percent=50))

    assert not card.settings_loaded
    card.auto_potion.setChecked(True)      # 還沒還原就有人動了畫面
    page._save_potion(1)

    assert potion_store.get("狐狐狸").hp_item == 502


def test_clearing_it_for_real_survives_qts_signal_order(qtbot, monkeypatch, tmp_path):
    """★ Qt 先發 `currentIndexChanged` 才發 `activated`（實測過）。

    也就是說「使用者剛剛清掉的」那一次存檔跑在**授權登記之前** ——
    照著寫的話存檔會擋下來，使用者挑的「未選擇」看起來像沒有效果。
    所以登記授權之後要**再存一次**（`CharacterCard._touch()`）。
    這條測試照真實的訊號順序跑一遍。
    """
    from ro_toolbox.services import potion_store
    from ro_toolbox.services.potion_store import PotionSaved
    from ro_toolbox.ui.pages.farm_page import CharacterCard

    orange, wing = 502, 23455
    page = _page(monkeypatch, tmp_path)
    card = CharacterCard()
    qtbot.addWidget(card)
    card.character = "狐狐狸"
    page._cards[1] = card
    # 分頁接上時就是這樣接線的（`_on_attached`）：設定一變就存
    card.potion_changed.connect(lambda: page._save_potion(1))
    potion_store.save("狐狐狸", PotionSaved(
        hp_item=orange, hp_percent=50, home_item=wing, go_home=True))
    card.apply_saved_potion(potion_store.get("狐狐狸"))
    card.set_slots({6: (orange, 5), 7: (wing, 2)})
    assert card.hp_item.currentData() == orange, "前提：還原時有選起來"

    # 使用者自己在下拉裡挑「未選擇」
    card.hp_item.setCurrentIndex(card.hp_item.findData(None))
    card.hp_item.activated.emit(card.hp_item.currentIndex())

    after = potion_store.get("狐狐狸")
    assert after.hp_item is None, "使用者真的清掉了就要清得掉"
    assert after.home_item == wing, "沒動到的那一項不該被順手清掉"
    assert card.saved_potion().hp_item is None


def test_the_clearing_permission_is_used_once_not_forever(
    qtbot, monkeypatch, tmp_path
):
    """授權是「他剛剛按的那一下」，不是「這一欄從此隨便清」。

    留著的話，使用者選過一次藥水之後那一欄就又變回可以被程式安靜清掉的
    —— 回連重接卡片、下拉重建都會送空值進來，[DAT-073] 的洞就開回去了。
    """
    from ro_toolbox.services import potion_store
    from ro_toolbox.services.potion_store import PotionSaved
    from ro_toolbox.ui.pages.farm_page import CharacterCard

    orange = 502
    page = _page(monkeypatch, tmp_path)
    card = CharacterCard()
    qtbot.addWidget(card)
    card.character = "狐狐狸"
    page._cards[1] = card
    card.potion_changed.connect(lambda: page._save_potion(1))
    potion_store.save("狐狐狸", PotionSaved(hp_percent=50))
    card.apply_saved_potion(potion_store.get("狐狐狸"))
    card.set_slots({6: (orange, 5)})

    # 使用者自己挑了藥水（用掉一次授權）
    card.hp_item.setCurrentIndex(card.hp_item.findData(orange))
    card.hp_item.activated.emit(card.hp_item.currentIndex())
    assert potion_store.get("狐狐狸").hp_item == orange

    # 之後程式把下拉洗成「未選擇」（回連、重建清單都會這樣）
    card.hp_item.setCurrentIndex(card.hp_item.findData(None))
    assert potion_store.get("狐狐狸").hp_item == orange, "授權不能留到下一次"
