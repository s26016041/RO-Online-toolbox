"""自動掛機分頁的資料邏輯（不建整個頁面，避免碰到視窗掃描與計時器）。"""

from __future__ import annotations

from ro_toolbox.ui.pages.farm_page import FarmPage


class FakeBot:
    def __init__(self, loot: dict[int, int]) -> None:
        self._loot = loot

    def loot(self) -> dict[int, int]:
        return dict(self._loot)


def bare_page() -> FarmPage:
    """只要資料欄位，不跑 __init__（那會開計時器、掃視窗）。"""
    page = FarmPage.__new__(FarmPage)
    page._loot_totals = {}
    return page


def test_loot_survives_stopping_the_bot():
    """關掉自動掛機不該把『道具總攬』清空 —— 那是角色撿到的東西。"""
    page = bare_page()
    page._keep_loot(123, FakeBot({952: 3, 909: 1}))
    assert page._loot_totals[123] == {952: 3, 909: 1}


def test_loot_accumulates_across_sessions():
    """再開一次掛機，數量要累加上去，不是從零開始。"""
    page = bare_page()
    page._keep_loot(123, FakeBot({952: 3}))
    page._keep_loot(123, FakeBot({952: 2, 501: 1}))
    assert page._loot_totals[123] == {952: 5, 501: 1}


def test_loot_is_per_character():
    page = bare_page()
    page._keep_loot(1, FakeBot({952: 1}))
    page._keep_loot(2, FakeBot({909: 4}))
    assert page._loot_totals == {1: {952: 1}, 2: {909: 4}}


def test_page_survives_its_own_timers(qtbot, monkeypatch):
    """建出真的頁面並呼叫計時器會跑到的那幾條路。

    這條是補破網：`_bags` 少初始化過一次，單元測試用 `__new__` 建假物件所以沒抓到，
    結果實機每秒噴一次 AttributeError。真的建一次頁面就會抓到這種漏。
    """
    from ro_toolbox.services import window_list
    from ro_toolbox.ui.pages.farm_page import FarmPage

    monkeypatch.setattr(window_list, "enumerate_windows", lambda *a, **k: [])
    page = FarmPage()
    qtbot.addWidget(page)
    page._scan_timer.stop()
    page._read_timer.stop()
    # 計時器實際會呼叫的三條
    page._refresh_current_bag()
    page._refresh_loot()
    page._on_tab_changed()
    page._read_all()
    page.shutdown()


# ---- 自動尋路 -------------------------------------------------------


def make_card(qtbot):
    from ro_toolbox.ui.pages.farm_page import CharacterCard

    card = CharacterCard()
    qtbot.addWidget(card)
    return card


class FakeTravelStats:
    def __init__(self, **kw) -> None:
        self.running = kw.get("running", True)
        self.goal = kw.get("goal", "prt_fild08")
        self.goal_label = kw.get("goal_label", "普隆德拉原野")
        self.here = kw.get("here", "")
        self.hops_left = kw.get("hops_left", 0)
        self.note = kw.get("note", "")
        self.arrived = kw.get("arrived", False)


def test_travel_button_starts_released(qtbot):
    card = make_card(qtbot)
    assert card.auto_travel.isChecked() is False
    assert card.auto_hunt.isEnabled() is True


def test_travel_busy_locks_out_auto_hunt(qtbot):
    """趕路途中不准再開自動打怪 —— 兩個都在送走路封包會互相搶目標。"""
    card = make_card(qtbot)
    card.set_travel_busy(True)
    assert card.auto_hunt.isEnabled() is False
    card.set_travel_busy(False)
    assert card.auto_hunt.isEnabled() is True


def test_travel_button_pops_up_when_the_bot_stops(qtbot):
    """按鈕壓著卻沒在走 = 看起來像還在趕路，是最糟的失效方式。"""
    card = make_card(qtbot)
    card.auto_travel.setChecked(True)
    card._apply_travel_stats(
        FakeTravelStats(running=False, note="⚠ 讀不到導航目標 —— 請先在遊戲的尋路視窗設定目的地")
    )
    assert card.auto_travel.isChecked() is False


def test_travel_failure_goes_to_the_log_not_the_card(qtbot, caplog):
    """⚠ 提示字一律進**執行日誌**（使用者指定），但不准安靜地消失。"""
    import logging

    card = make_card(qtbot)
    card.set_note("PID 1234")
    with caplog.at_level(logging.WARNING):
        card._apply_travel_stats(
            FakeTravelStats(running=False, note="⚠ 讀不到導航目標")
        )
    assert card.status_label.text() == "PID 1234", "卡片上只放 PID"
    assert any("讀不到導航目標" in r.getMessage() for r in caplog.records)


def test_travel_progress_goes_to_the_log(qtbot, caplog):
    import logging

    card = make_card(qtbot)
    with caplog.at_level(logging.WARNING):
        card._apply_travel_stats(
            FakeTravelStats(hops_left=2, note="前往 prt_fild08：還要換 2 張圖")
        )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "普隆德拉原野" in text
    assert "還要換 2 張圖" in text


# ---- 目的地選單（中文／地圖代碼都能搜）------------------------------------


def test_destination_defaults_to_reading_the_game(qtbot):
    """沒挑就是 None ＝ 照舊讀遊戲自己的尋路目標。"""
    card = make_card(qtbot)
    assert card.chosen_destination() is None
    assert card.destination.itemData(0) is None


def test_destination_lists_chinese_and_code(qtbot):
    """兩種打法都要搜得到，所以同一行同時放中文名與地圖代碼。"""
    card = make_card(qtbot)
    texts = [card.destination.itemText(i) for i in range(card.destination.count())]
    joined = "\n".join(texts)
    assert "geffen" in joined
    assert "吉芬" in joined
    assert card.destination.findData("geffen") > 0


def test_choosing_a_destination_wins_over_the_game(qtbot):
    card = make_card(qtbot)
    card.destination.setCurrentIndex(card.destination.findData("geffen"))
    assert card.chosen_destination() == "geffen"


def test_destination_is_remembered_per_character(qtbot):
    from ro_toolbox.services.potion_store import PotionSaved

    card = make_card(qtbot)
    card.destination.setCurrentIndex(card.destination.findData("prontera"))
    assert card.saved_potion().travel_dest == "prontera"
    card.apply_saved_potion(PotionSaved(travel_dest="geffen"))
    assert card.chosen_destination() == "geffen"


def test_a_junk_saved_destination_falls_back_to_reading_the_game(qtbot):
    """檔案被手改壞就退回「讀遊戲」—— 亂猜一個地圖會把人送去不相干的地方。"""
    from ro_toolbox.services.potion_store import PotionSaved

    card = make_card(qtbot)
    card.apply_saved_potion(PotionSaved(travel_dest="不存在的圖"))
    assert card.chosen_destination() is None
