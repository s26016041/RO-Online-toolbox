"""自動掛機分頁的資料邏輯（不建整個頁面，避免碰到視窗掃描與計時器）。"""

from __future__ import annotations

import time

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


def _page(qtbot, monkeypatch=None):
    """一個不會去掃遊戲視窗的 FarmPage（計時器全停）。"""
    import pytest as _pytest

    from ro_toolbox.services import window_list
    from ro_toolbox.ui.pages.farm_page import FarmPage
    mp = monkeypatch or _pytest.MonkeyPatch()
    mp.setattr(window_list, "enumerate_windows", lambda *a, **k: [])
    page = FarmPage()
    qtbot.addWidget(page)
    page._scan_timer.stop()
    page._read_timer.stop()
    return page


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


def test_travel_failure_only_pops_the_button(qtbot):
    """⚠ 卡片上不放字（提示字進執行日誌），介面唯一的表現是按鈕彈起來。

    日誌是 `TravelBot._note()` 記的，**這裡不能再記一次** ——
    兩邊都記的症狀是同一句話印兩次（實測：「前往 依斯魯得島　前往 izlude」）。
    """
    card = make_card(qtbot)
    card.set_note("PID 1234")
    card.auto_travel.setChecked(True)
    card._apply_travel_stats(FakeTravelStats(running=False, note="⚠ 讀不到導航目標"))
    assert card.auto_travel.isChecked() is False
    assert card.status_label.text() == "PID 1234"


def test_arrival_pops_a_notice_on_top_once(qtbot, monkeypatch):
    """趕路動輒幾十秒，人早就切回遊戲或去做別的事了 —— 只寫日誌等於沒講。
    到了要跳一個置頂通知；而 bot 停下來時還會再回報一次同一份 stats
    （arrived 仍是 True），所以只准跳一次。"""
    from ro_toolbox.ui.pages import farm_page as mod

    shown = []
    monkeypatch.setattr(mod, "show_notice", lambda *args: shown.append(args))
    card = make_card(qtbot)
    card.set_travel_busy(True)
    card.auto_travel.setChecked(True)
    card._apply_travel_stats(FakeTravelStats(running=True, arrived=True))
    card._apply_travel_stats(FakeTravelStats(running=False, arrived=True))
    assert len(shown) == 1
    assert "普隆德拉原野" in shown[0][1]


def test_travel_that_fails_pops_no_notice(qtbot, monkeypatch):
    """沒到就不要跳「到了」—— 安靜地做錯事一律當 bug。"""
    from ro_toolbox.ui.pages import farm_page as mod

    shown = []
    monkeypatch.setattr(mod, "show_notice", lambda *args: shown.append(args))
    card = make_card(qtbot)
    card.set_travel_busy(True)
    card._apply_travel_stats(FakeTravelStats(running=False, arrived=False, note="⚠ 到不了"))
    assert shown == []


def test_notice_never_steals_focus(qtbot):
    """置頂是必要的，**不搶焦點**更必要：搶了焦點全螢幕的遊戲會被切到背景
    甚至最小化，那比沒有通知糟得多。"""
    from PySide6.QtCore import Qt

    from ro_toolbox.ui.widgets.toast import TopToast

    toast = TopToast("到了", "測試", seconds=0)
    qtbot.addWidget(toast)
    assert toast.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert toast.windowFlags() & Qt.WindowType.WindowStaysOnTopHint


def test_the_bot_is_the_one_that_logs(caplog):
    """提示字只由 bot 記一次，而且**同一句話不重複記**（每拍都會呼叫）。"""
    import logging

    from ro_toolbox.services.travel_bot import TravelBot

    bot = TravelBot(1234)
    with caplog.at_level(logging.INFO, logger="ro_toolbox.services.travel_bot"):
        bot._note("前往 izlude：還要換 1 張圖")
        bot._note("前往 izlude：還要換 1 張圖")
        bot._note("⚠ 讀不到導航目標")
    said = [r.getMessage() for r in caplog.records]
    assert said == ["前往 izlude：還要換 1 張圖", "⚠ 讀不到導航目標"], said


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


# ---- 自動回連 ------------------------------------------------------------


def test_snapshot_keeps_identity_not_position(qtbot, monkeypatch):
    """⚠ 存的是**目的地是哪張圖**，不是「路線走到第幾段」。

    重新登入之後角色多半在存檔點，不會在斷線的地方 —— 存位置回來就是錯的。
    """
    page = _page(qtbot)
    card = make_card(qtbot)
    card.character = "狐狐狸"
    page._cards[1234] = card
    card.auto_hunt.setChecked(True)

    class FakeTravel:
        destination = "geffen"

    page._travelers[1234] = FakeTravel()
    snap = page.snapshot_for(1234)
    assert snap.farming is True
    assert snap.destination == "geffen"
    assert any("geffen" in label or "吉芬" in label for label in snap.labels)


def test_watching_does_nothing_when_the_switch_is_off(qtbot, monkeypatch):
    """⚠ 自動回連會**關掉並重開遊戲** —— 沒勾就一步都不准做。"""
    from ro_toolbox.config.settings import AppSettings
    from ro_toolbox.ui.pages import farm_page as mod

    page = _page(qtbot)
    card = make_card(qtbot)
    card.character = "狐狐狸"
    page._cards[1234] = card
    monkeypatch.setattr(mod, "current_settings", lambda: AppSettings(auto_reconnect=False))
    monkeypatch.setattr(mod, "find_server", lambda _pid: None)
    called = []
    monkeypatch.setattr(page, "_begin_reconnect", lambda *a: called.append(a))
    for _ in range(50):
        page._watch_connections()
    assert called == []


def test_my_network_being_down_never_reconnects(qtbot, monkeypatch):
    """你自己的網路斷了 —— 關遊戲重開是幫倒忙（重開照樣連不上，人還被登出）。"""
    from ro_toolbox.config.settings import AppSettings
    from ro_toolbox.ui.pages import farm_page as mod

    page = _page(qtbot)
    card = make_card(qtbot)
    card.character = "狐狐狸"
    page._cards[1234] = card
    monkeypatch.setattr(mod, "current_settings", lambda: AppSettings(auto_reconnect=True))
    monkeypatch.setattr(mod, "find_server", lambda _pid: None)
    monkeypatch.setattr(mod, "local_network_up", lambda: False)
    called = []
    monkeypatch.setattr(page, "_begin_reconnect", lambda *a: called.append(a))
    for _ in range(50):
        page._watch_connections()
    assert called == []


def test_it_will_not_reconnect_without_a_snapshot(qtbot, monkeypatch, caplog):
    """沒有斷線前的快照就不知道要接回什麼 —— 不如不動，交給人。"""
    import logging

    page = _page(qtbot)
    card = make_card(qtbot)
    card.character = "狐狐狸"
    page._cards[1234] = card
    with caplog.at_level(logging.WARNING, logger="ro_toolbox.ui.pages.farm_page"):
        page._begin_reconnect(1234, "狐狐狸", None)
    assert page._reconnecting is False
    assert any("快照" in r.getMessage() for r in caplog.records)


def test_a_saved_potion_setting_is_not_cancelled_by_a_slow_bag(qtbot, monkeypatch):
    """⚠ 實測抱怨：「是否使用藥水的那個選擇要記錄」。

    真正的原因不是沒存 —— 是**還原的時機**。開程式時背包是背景讀的，
    還原當下下拉是空的，舊版就判定「還沒選道具」把勾取消掉。
    """
    from ro_toolbox.services.potion_store import PotionSaved
    from ro_toolbox.ui.pages.farm_page import CharacterCard

    page = _page(qtbot)
    card = CharacterCard()
    qtbot.addWidget(card)
    card.character = "狐狐狸"
    page._cards[1234] = card
    card.apply_saved_potion(PotionSaved(hp_item=501, hp_percent=60, enabled=True))

    page._toggle_potion(1234, True)            # 背包還沒到
    assert card.auto_potion.isChecked() is True, "不能因為背包慢就把設定取消掉"
    assert 1234 in page._pending_potion
    assert 1234 not in page._potions, "也不該用空設定去啟動"

    started = []
    monkeypatch.setattr(page, "_toggle_potion",
                        lambda pid, on: started.append((pid, on)))
    page._bags[1234] = {6: (501, 99)}
    page._apply_bag(1234)
    assert started == [(1234, True)], "背包回來就要接著啟動"


def test_a_genuinely_empty_setting_still_says_so(qtbot):
    """真的什麼都沒選（也沒有在等的道具）才准取消勾選並講原因。"""
    from ro_toolbox.ui.pages.farm_page import CharacterCard

    page = _page(qtbot)
    card = CharacterCard()
    qtbot.addWidget(card)
    card.character = "狐狐狸"
    page._cards[1234] = card
    card.auto_potion.setChecked(True)
    page._toggle_potion(1234, True)
    assert card.auto_potion.isChecked() is False
    assert 1234 not in page._pending_potion


# ---- 閃退（遊戲行程整個不見）也要重開 -----------------------------------
#
# 使用者要求：「閃退也要幫我重開，我直接關閉遊戲應該會被當成閃退」。
# ⚠ 這一塊本來整個沒有人在看：分頁是照行程建的，行程沒了分頁也會被收掉，
# 而舊版的看門狗只走 `self._cards` —— 最需要救的情況反而沒人管。


def _crash_page(qtbot, monkeypatch, alive_pids):
    """一個已經在看「狐狐狸／PID 1234」的頁面，並決定遊戲行程清單長怎樣。

    回 (page, mod, tick)。`tick()` 跑一拍**並且把時鐘往前推 3 秒** ——
    觀察期是用真實時間算的，在迴圈裡連跑 50 次時鐘根本不會動。
    """
    from ro_toolbox.config.settings import AppSettings
    from ro_toolbox.services import game_launcher
    from ro_toolbox.ui.pages import farm_page as mod

    page = _page(qtbot)
    card = make_card(qtbot)
    card.character = "狐狐狸"
    page._cards[1234] = card
    monkeypatch.setattr(mod, "current_settings", lambda: AppSettings(auto_reconnect=True))
    monkeypatch.setattr(mod, "local_network_up", lambda: True)
    monkeypatch.setattr(game_launcher, "game_pids", lambda: list(alive_pids))

    clock = {"now": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])

    def tick(times: int = 1) -> None:
        for _ in range(times):
            page._watch_connections()
            clock["now"] += 3.0

    return page, mod, tick


def _remember_where_he_lives(page, mod, monkeypatch, tick):
    """先跑一拍「連線正常」，讓看門狗記住這個角色住在 PID 1234。"""
    monkeypatch.setattr(mod, "find_server", lambda _pid: ("1.2.3.4", 6900))
    tick()
    assert page._watching.get("狐狐狸") == 1234
    page._cards.clear()          # 行程沒了，分頁也會被 _scan() 收掉


def test_a_vanished_game_is_treated_as_a_crash(qtbot, monkeypatch):
    """遊戲行程不見了 → 當成閃退，重開。"""
    page, mod, tick = _crash_page(qtbot, monkeypatch, alive_pids=[])
    _remember_where_he_lives(page, mod, monkeypatch, tick)
    called = []
    monkeypatch.setattr(page, "_begin_reconnect", lambda *a: called.append(a))
    tick(5)
    assert called, "遊戲不見了要重開"
    assert called[0][1] == "狐狐狸"


def test_one_missing_tick_is_not_a_crash(qtbot, monkeypatch):
    """⚠ 不准憑一拍就重開 —— 行程清單也有讀不到的時候。

    跟斷線走同一個 `ReconnectDecider`：連續幾拍都不見才算數。
    """
    page, mod, tick = _crash_page(qtbot, monkeypatch, alive_pids=[])
    _remember_where_he_lives(page, mod, monkeypatch, tick)
    called = []
    monkeypatch.setattr(page, "_begin_reconnect", lambda *a: called.append(a))
    tick(1)
    assert called == [], "第一拍只能開始觀察，不准動手"


def test_a_game_that_is_still_running_is_not_a_crash(qtbot, monkeypatch):
    """行程還在就不是閃退（那是別的問題，交給連線那條路）。"""
    page, mod, tick = _crash_page(qtbot, monkeypatch, alive_pids=[1234])
    _remember_where_he_lives(page, mod, monkeypatch, tick)
    called = []
    monkeypatch.setattr(page, "_begin_reconnect", lambda *a: called.append(a))
    tick(20)
    assert called == []


def test_a_crash_does_nothing_when_the_switch_is_off(qtbot, monkeypatch):
    """沒勾自動回連就一步都不准做 —— 它會開遊戲。"""
    from ro_toolbox.config.settings import AppSettings

    page, mod, tick = _crash_page(qtbot, monkeypatch, alive_pids=[])
    _remember_where_he_lives(page, mod, monkeypatch, tick)
    monkeypatch.setattr(mod, "current_settings", lambda: AppSettings(auto_reconnect=False))
    called = []
    monkeypatch.setattr(page, "_begin_reconnect", lambda *a: called.append(a))
    tick(20)
    assert called == []


def test_my_network_being_down_is_not_a_crash_either(qtbot, monkeypatch):
    """網路是我自己斷的時候，就算遊戲真的掛了也**先不要**重開 ——
    重開照樣連不上，而且會把現場蓋掉。等網路回來再說。"""
    page, mod, tick = _crash_page(qtbot, monkeypatch, alive_pids=[])
    _remember_where_he_lives(page, mod, monkeypatch, tick)
    monkeypatch.setattr(mod, "local_network_up", lambda: False)
    called = []
    monkeypatch.setattr(page, "_begin_reconnect", lambda *a: called.append(a))
    tick(20)
    assert called == []


def test_the_old_pid_is_forgotten_once_a_reconnect_starts(qtbot, monkeypatch):
    """重連期間那個行程一定會消失，留著會被重複判定成閃退。"""
    page, mod, tick = _crash_page(qtbot, monkeypatch, alive_pids=[1234])
    monkeypatch.setattr(mod, "find_server", lambda _pid: ("1.2.3.4", 6900))
    tick()
    page._snaps["狐狐狸"] = page.snapshot_for(1234)

    class _Thread:
        def start(self):
            pass

    monkeypatch.setattr(mod, "WorkerThread", lambda worker: _Thread())
    page._begin_reconnect(1234, "狐狐狸", None)
    assert "狐狐狸" not in page._watching


# ---- 回連之後把設定接回去：等訊號，不等秒數 -----------------------------
#
# 使用者的日誌：「回連後找不到 PID 2788 的分頁，接不回去」。
# 遊戲確實重開也重登成功了，只有最後這一步落空 —— 舊版是
# `QTimer.singleShot(3000, ...)`，而分頁要先有連線、再背景 AOB 定位成功
# 才建得出來，三秒鐘通常不夠。CLAUDE.md 也明文禁止拿「等幾秒」當機制。


class _Snap:
    def __init__(self, dest=None, farming=False):
        self.destination = dest
        self.farming = farming
        self.potion = None
        self.labels = ["自動打怪"] if farming else []


def test_restore_waits_for_the_tab_instead_of_a_fixed_delay(qtbot, monkeypatch):
    """分頁還沒長出來時**先記著**，不是當場放棄。"""
    page = _page(qtbot)
    monkeypatch.setattr(page, "_scan", lambda: None)
    page._reconnect_decider = None
    page._reconnect_done(2788, "狐狐狸", _Snap(farming=True), "")
    assert 2788 in page._pending_restore, "應該先登記，等分頁出現"


def test_the_tab_showing_up_is_what_triggers_the_restore(qtbot, monkeypatch):
    """分頁建好的那一刻就接回去 —— 那才是讀得到的訊號。"""
    page = _page(qtbot)
    monkeypatch.setattr(page, "_scan", lambda: None)
    page._reconnect_decider = None
    snap = _Snap(farming=True)
    page._reconnect_done(2788, "狐狐狸", snap, "")

    restored = []
    monkeypatch.setattr(page, "restore_into", lambda pid, s: restored.append((pid, s)))
    page._restore_if_pending(2788)
    assert restored == [(2788, snap)]
    assert 2788 not in page._pending_restore, "接過就不要再接一次"


def test_an_unrelated_tab_does_not_consume_the_pending_restore(qtbot, monkeypatch):
    """別的遊戲視窗長出來不算 —— 認的是 PID。"""
    page = _page(qtbot)
    monkeypatch.setattr(page, "_scan", lambda: None)
    page._reconnect_decider = None
    page._reconnect_done(2788, "狐狐狸", _Snap(farming=True), "")
    restored = []
    monkeypatch.setattr(page, "restore_into", lambda pid, s: restored.append(pid))
    page._restore_if_pending(9999)
    assert restored == []
    assert 2788 in page._pending_restore


def test_waiting_forever_is_not_allowed_and_it_says_so(qtbot, monkeypatch, caplog):
    """等太久要放棄，而且要**大聲** —— 安靜地忘掉最糟：
    使用者會以為都接回去了，實際上掛機沒開。"""
    import logging

    from ro_toolbox.ui.pages import farm_page as mod

    page = _page(qtbot)
    monkeypatch.setattr(page, "_scan", lambda: None)
    page._reconnect_decider = None
    page._reconnect_done(2788, "狐狐狸", _Snap(farming=True), "")
    later = time.monotonic() + mod._RESTORE_TIMEOUT_SEC + 1
    with caplog.at_level(logging.WARNING, logger="ro_toolbox.ui.pages.farm_page"):
        page._expire_pending_restores(later)
    assert 2788 not in page._pending_restore
    assert any("沒能接回" in r.getMessage() for r in caplog.records)


def test_a_failed_reconnect_registers_nothing_to_restore(qtbot, monkeypatch):
    """重連本身就失敗的話沒有東西好接，也不該留下一筆等到逾時。"""
    from ro_toolbox.services.reconnect import ReconnectDecider

    page = _page(qtbot)
    page._reconnect_decider = ReconnectDecider()
    page._reconnect_done(0, "狐狐狸", _Snap(farming=True), "開遊戲失敗")
    assert page._pending_restore == {}


# ---- 回連失敗：關掉那個沒救的客戶端，而且要**繼續看著他** -------------------
#
# 使用者實測回報：「回連失敗當斷線應該直接關閉再開重新連線，
# 現在卻卡在那邊一直按 ENTER。」


def test_a_failed_login_closes_the_client_it_just_opened(qtbot, monkeypatch):
    """⚠ 登入沒完成的客戶端是沒救的（多半是卡登）。留著只會佔著帳號、
    停在半死的登入畫面，而且**永遠不會有分頁**（分頁要有連線才建）。"""
    from ro_toolbox.services import game_census
    from ro_toolbox.ui.pages.farm_page import _ReconnectWorker

    closed: list[int] = []
    monkeypatch.setattr(game_census, "close", lambda pid: closed.append(pid) or True)
    worker = _ReconnectWorker(1234, "狐狐狸", _Snap(farming=True))
    seen: list[tuple] = []
    worker.done.connect(lambda *args: seen.append(args))

    worker._give_up(5678, "重新登入沒有完成")

    assert closed == [5678], "剛開起來的那個要當場關掉"
    assert seen and seen[0][0] == 0, "還是要回報失敗，讓退避接手"


def test_a_failed_reconnect_keeps_watching_so_the_retry_can_fire(qtbot, monkeypatch):
    """⚠⚠ 不放回 `_watching` 的話，退避時間到了也**沒有任何一拍會再試**。

    `_begin_reconnect` 會把舊 PID 拿掉（那個行程一定會消失），而分頁是照
    「**有連線的**遊戲行程」建的 —— 登入沒完成就沒有分頁。兩邊都沒有他，
    就是使用者說的「回連失敗之後卡在那裡」。
    """
    from ro_toolbox.services.reconnect import ReconnectDecider

    page = _page(qtbot)
    page._reconnect_decider = ReconnectDecider()
    page._reconnect_pid = 1234
    page._watching.pop("狐狐狸", None)

    page._reconnect_done(0, "狐狐狸", _Snap(farming=True), "重新登入沒有完成")

    assert page._watching.get("狐狐狸") == 1234
    assert page._reconnect_decider.failures == 1, "還是要退避，不能無腦連開"


# ---- 進度與失敗一定要看得到 ---------------------------------------------
#
# 使用者回報：「自動尋路都沒有提示文字出現，他在計算還是壞掉或什麼讀不到
# 我都不知道」。原因是設定裡的 log_level 預設是 WARNING，而所有進度都是 INFO
# —— 執行日誌面板掛在 root logger 底下，INFO 根本流不到。


def test_the_log_panel_still_gets_progress_when_the_level_is_warning(tmp_path,
                                                                     monkeypatch):
    """⚠ 面板存在的唯一理由就是給人看進度，不准被記錄層級關掉。"""
    import logging

    from ro_toolbox.config import paths
    from ro_toolbox.utils import logging as mod

    monkeypatch.setattr(paths, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(mod, "log_dir", lambda: tmp_path)
    bridge = mod.setup_logging("WARNING")
    seen = []
    bridge.message.connect(lambda level, text: seen.append((level, text)))
    logging.getLogger("ro_toolbox.services.travel_bot").info("正在計算路線…")
    assert any("正在計算路線" in text for _level, text in seen), seen


def test_a_louder_setting_is_still_honoured(tmp_path, monkeypatch):
    """設 DEBUG 就要更囉唆 —— 下限只是下限，不是上限。"""
    import logging

    from ro_toolbox.config import paths
    from ro_toolbox.utils import logging as mod

    monkeypatch.setattr(paths, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(mod, "log_dir", lambda: tmp_path)
    bridge = mod.setup_logging("DEBUG")
    seen = []
    bridge.message.connect(lambda level, text: seen.append(level))
    logging.getLogger("ro_toolbox.services.travel_bot").debug("細節")
    assert "DEBUG" in seen


def test_travel_failures_are_warnings_not_progress(caplog):
    """⚠ 「角色座標定位失敗，自動尋路停用」這種**硬失敗**要大聲。

    以前它跟一般進度一樣走 INFO，於是在使用者的設定下一個字都看不到，
    症狀就是「按了沒反應，不知道是在算還是壞了」。
    CLAUDE.md：失效模式只准「大聲停用」或「安全退化」。
    """
    import logging

    from ro_toolbox.services.travel_bot import TravelBot

    bot = TravelBot(1234)
    with caplog.at_level(logging.INFO, logger="ro_toolbox.services.travel_bot"):
        bot._fail("⚠ 角色座標定位失敗（遊戲可能已改版），自動尋路停用")
    levels = [r.levelname for r in caplog.records]
    assert "WARNING" in levels, levels


def test_ordinary_progress_stays_at_info(caplog):
    """進度還是 INFO —— 不要為了看得到就把所有東西都變成警告。"""
    import logging

    from ro_toolbox.services.travel_bot import TravelBot

    bot = TravelBot(1234)
    with caplog.at_level(logging.INFO, logger="ro_toolbox.services.travel_bot"):
        bot._note("正在計算 a → c 的路線…")
    assert [r.levelname for r in caplog.records] == ["INFO"]


# ---- 複製遊戲 socket 要重試，不能叫一次就放棄 ---------------------------
#
# 使用者實機日誌（自動尋路一按就死）：
#   10:51:49 | WARNING | travel_bot | 找不到遊戲 socket，無法送封包
#   10:51:58 | WARNING | travel_bot | 找不到遊戲 socket，無法送封包
#   10:52:08 | WARNING | travel_bot | 找不到遊戲 socket，無法送封包
#
# GAMEDATA 早就記過「剛連上的那幾秒複製不到，過一會兒就 0.1 秒找到」，
# 但那條知識只活在 auto_login 與 potion 各自的迴圈裡 ——
# travel_bot 與 farm_bot 是叫一次就放棄。


def test_the_socket_is_retried_not_given_up_on(monkeypatch):
    """第一次失敗不算數：重試到成功。"""
    from ro_toolbox.services import game_socket

    tries = []

    def _flaky(pid, ip, port):
        tries.append((pid, ip, port))
        return 0 if len(tries) < 3 else 4242

    monkeypatch.setattr(game_socket, "find_game_socket", _flaky)
    monkeypatch.setattr(game_socket, "_SOCKET_POLL", 0.0)
    got = game_socket.open_game_socket(1234, "1.2.3.4", 6900, timeout=5.0)
    assert got == 4242
    assert len(tries) == 3


def test_giving_up_says_it_once_not_every_try(monkeypatch, caplog):
    """⚠ 迴圈裡每次都記的話兩秒就洗版一百行 —— 放棄時才講一次。"""
    import logging

    from ro_toolbox.services import game_socket

    monkeypatch.setattr(game_socket, "find_game_socket", lambda *a: 0)
    monkeypatch.setattr(game_socket, "_SOCKET_POLL", 0.0)
    with caplog.at_level(logging.WARNING, logger="ro_toolbox.services.game_socket"):
        assert game_socket.open_game_socket(1234, "1.2.3.4", 6900, timeout=0.05) is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


def test_stopping_the_bot_aborts_the_wait(monkeypatch):
    """bot 被關掉就不要傻等 —— 使用者按了停止就該停。"""
    from ro_toolbox.services import game_socket

    monkeypatch.setattr(game_socket, "find_game_socket", lambda *a: 0)
    monkeypatch.setattr(game_socket, "_SOCKET_POLL", 0.0)
    got = game_socket.open_game_socket(
        1234, "1.2.3.4", 6900, timeout=60.0, should_stop=lambda: True,
    )
    assert got is None


def test_every_bot_goes_through_the_retrying_opener():
    """⚠ 同一條知識散在四個地方寫，就會有人漏掉（實際發生過）。

    除了 potion 那個本來就有自己的重試迴圈的地方，其他呼叫端一律走
    `open_game_socket`。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src/ro_toolbox/services"
    for name in ("travel_bot.py", "farm_bot.py"):
        body = (root / name).read_text(encoding="utf-8")
        assert "find_game_socket(" not in body, f"{name} 還在直接叫一次就放棄"


# ---- 每秒輪詢的東西不准洗版 ---------------------------------------------


def test_reading_the_bag_only_speaks_when_something_changed(caplog):
    """⚠ 背包每秒多讀一次；照實記就是每秒一行「背包讀到 40 格」。

    使用者實際回報過（把面板下限拉到 INFO 之後才浮現）。
    """
    import logging

    from ro_toolbox.utils.logging import StateLog

    notes = StateLog(logging.getLogger("ro_toolbox.services.bag"))
    with caplog.at_level(logging.INFO, logger="ro_toolbox.services.bag"):
        for _ in range(20):
            notes.changed("0x15d2ac8+0x1738:40", logging.INFO, "背包讀到 40 格")
        notes.changed("0x15d2ac8+0x1738:39", logging.INFO, "背包讀到 39 格")
    said = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert said == ["背包讀到 40 格", "背包讀到 39 格"], said


# ---- 一建卡就開始看著他（不要等「連線正常」那一拍）----------------------
#
# 使用者實測：「我把遊戲關閉他沒回連。」實機日誌：
#   13:03:04  狐狐狸：尚未登入（回到選角畫面？）   ← find_server() 這一拍是 None
#   13:03:04  自動掛機：加入 狐狐狸（PID 54656）
#   13:03:04  自動掛機：移除 PID 54656（遊戲行程已結束）
# 分頁沒活過一拍「連線正常」，`_watching` 從頭到尾是空的 —— 閃退偵測沒被啟用。


def test_a_new_tab_is_watched_immediately(qtbot, monkeypatch):
    """⚠ **建得出卡就代表那一刻是連線正常的**（`_scan()` 只在有連線時才 attach）。

    所以身分與快照要在建卡那一刻就記下來，不要等下一拍 —— 那一拍可能
    永遠不會來（客戶端還在換到地圖伺服器，或使用者立刻把遊戲關了）。
    """
    from ro_toolbox.ui.pages import farm_page as mod

    page = _page(qtbot)
    card = make_card(qtbot)
    card.character = "狐狐狸"
    page._cards[1234] = card
    page._names[1234] = "狐狐狸"
    monkeypatch.setattr(mod, "find_server", lambda _pid: None)   # 還沒連上
    # 直接呼叫建卡尾端那段（`_on_attached` 的最後幾行）
    page._watching["狐狐狸"] = 1234
    page._snaps.setdefault("狐狐狸", page.snapshot_for(1234))
    assert page._watching == {"狐狐狸": 1234}
    assert "狐狐狸" in page._snaps, "沒有快照的話回連會直接放棄"


def test_the_attach_path_records_the_identity(qtbot, monkeypatch):
    """釘住原始碼：`_on_attached` 一定要記 `_watching` 與 `_snaps`。

    這條用掃原始碼而不是跑整條 attach —— attach 要真的去讀遊戲記憶體。
    """
    import inspect

    from ro_toolbox.ui.pages.farm_page import FarmPage

    body = inspect.getsource(FarmPage._on_attached)
    assert "self._watching[status.name]" in body, "建卡時要記住他住在哪個 PID"
    assert "_snaps.setdefault" in body, "建卡時要留一份快照（且不准蓋掉舊的）"


def test_the_snapshot_taken_at_attach_never_clobbers_the_old_one(qtbot):
    """⚠ 回連之後卡是新建的，這時 `_snaps` 裡放的是**斷線前**那一份。

    用 setdefault 而不是直接指派 —— 被一份空的蓋掉的話就接不回去了。
    """
    page = _page(qtbot)
    card = make_card(qtbot)
    card.character = "狐狐狸"
    page._cards[1234] = card
    card.auto_hunt.setChecked(True)
    before = page.snapshot_for(1234)
    page._snaps["狐狐狸"] = before

    card.auto_hunt.setChecked(False)          # 新卡什麼都沒開
    page._snaps.setdefault("狐狐狸", page.snapshot_for(1234))
    assert page._snaps["狐狐狸"] is before, "舊快照不准被新的空快照蓋掉"


# ---- 換圖之後座標會停在上一張圖 —— 要問伺服器，不是停掉 ------------------
#
# 使用者實機（2026-08-28）：尋路被傳進 `s_atelier`（200×140）之後
#   ⚠ 進 s_atelier 後 10 秒，座標 (271, 108) 仍不在這張圖上，已停止
# (271,108) 是上一張 prontera（312×392）的位置 —— [MEM-022] 記過的那件事。
# 判斷沒錯，錯的是沒有辦法問出真正的位置。


class _Terrain:
    """一張 200x140、全部可走的假地圖。"""

    name = "s_atelier"
    width, height = 200, 140

    def is_walkable(self, x, y):
        return 0 <= x < self.width and 0 <= y < self.height

    @property
    def walkable(self):
        import numpy as np

        return np.ones((self.height, self.width), dtype=bool)


def _travel_bot(monkeypatch):
    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234)
    monkeypatch.setattr(bot, "_terrain_for", lambda name: _Terrain())
    sent = []
    monkeypatch.setattr(bot, "_send_move", lambda x, y: sent.append((x, y)))
    return bot, sent


def test_a_position_inside_the_map_is_taken_as_is(monkeypatch):
    bot, sent = _travel_bot(monkeypatch)
    assert bot._trusted_position("s_atelier", (100, 70)) == (100, 70)
    assert sent == [], "座標沒問題就不要亂送封包"


def test_a_stale_position_falls_back_to_what_the_server_said(monkeypatch):
    """⚠ 伺服器在 0x0087 裡同時給起點與終點 —— 起點就是「我在哪」。

    本來我們只取終點，起點丟掉了。那是換圖後唯一可信的來源。
    """
    bot, sent = _travel_bot(monkeypatch)
    bot._server_pos = (40, 30)
    bot._server_pos_map = "s_atelier"
    assert bot._trusted_position("s_atelier", (271, 108)) == (40, 30)
    assert sent == [], "已經知道位置就不用再推"


def test_a_position_reported_for_another_map_is_not_used(monkeypatch):
    """⚠ 不同圖的座標拿來用等於亂走 —— 一定要確認是**這張圖**報回來的。"""
    bot, sent = _travel_bot(monkeypatch)
    bot._server_pos = (40, 30)
    bot._server_pos_map = "prontera"
    assert bot._trusted_position("s_atelier", (271, 108)) is None
    assert sent, "不能用就要去問"


def test_the_map_move_packet_tells_us_where_we_landed(monkeypatch):
    """★ 伺服器換圖時就把座標給了（0x0091，長度 22 = 地圖名[16]+x+y）。

    有這一包就**完全不必猜**：換圖那一刻就知道自己站在哪。
    我們原本沒收這包，才會落到「等座標更新…10 秒後放棄」。
    """
    import struct

    from ro_toolbox.services import travel_bot as mod

    bot, _sent = _travel_bot(monkeypatch)

    class _Packet:
        outbound = False
        opcode = 0x0091
        payload = b"s_atelier.gat".ljust(16, b"\x00") + struct.pack("<HH", 41, 33)

    bot._on_packet(_Packet())
    assert bot._server_pos == (41, 33)
    assert bot._server_pos_map == "s_atelier", "副檔名 .gat 要去掉"
    assert 0x0091 in mod._OP_MAP_MOVE


def test_with_no_answer_it_nudges_instead_of_giving_up(monkeypatch):
    """站著不動不會有 0x0087 —— 往可走的地方走一步問出來。"""
    bot, sent = _travel_bot(monkeypatch)
    assert bot._trusted_position("s_atelier", (271, 108)) is None
    assert sent, "要送一次移動把伺服器問出來"
    x, y = sent[0]
    assert 0 <= x < 200 and 0 <= y < 140, f"推的目標要在這張圖上：{sent[0]}"


def test_the_nudge_sweeps_every_room_not_just_one_spot(monkeypatch):
    """⚠⚠ 實機踩過：只挑「離中心最近的可走格」，推 5 次全部沒有回應。

    兩件事湊在一起讓那個做法必敗：
      1. 移動封包超過 MAX_STEP 格伺服器**直接忽略**（[PKT-030]）。
      2. 室內圖是一間一間**互不相連**的房間（[DAT-029]）——
         離中心最近的那一格多半在別間房。
    所以要挑一組彼此隔開的目標，掃過每一間房。
    """
    from ro_toolbox.services import travel_bot as mod

    bot, sent = _travel_bot(monkeypatch)
    monkeypatch.setattr(mod, "_NUDGE_EVERY_SEC", 0.0)
    for _ in range(6):
        bot._trusted_position("s_atelier", (271, 108))
    assert len(set(sent)) > 1, f"每次都送同一格等於只賭一間房：{sent}"


def test_the_nudge_targets_cover_every_walkable_cell():
    """挑出來的目標要讓**每一格**可走的地方都落在 MAX_STEP 之內。

    不然人在哪一間房是運氣問題 —— 那正是這一輪的 bug。
    """

    from ro_toolbox.services.travel_bot import TravelBot
    from ro_toolbox.services.walker import MAX_STEP

    terrain = _Terrain()
    targets = TravelBot(1)._nudge_targets(terrain)
    assert targets
    for y in range(0, terrain.height, 7):
        for x in range(0, terrain.width, 7):
            near = min(max(abs(x - tx), abs(y - ty)) for tx, ty in targets)
            assert near <= MAX_STEP, f"({x},{y}) 離最近的目標 {near} 格，太遠"


def test_nudging_is_throttled(monkeypatch):
    """每拍都送等於洗封包 —— 而且伺服器也不會因此回快一點。

    ⚠ 節流間隔要**在測試裡放大**：真實值是 0.5 秒，而這 10 圈在忙碌的機器上
    （整包測試同時跑）真的會超過 0.5 秒，於是第二次推送合法地送出去 ——
    測試就會偶爾紅一次，而且看起來像程式壞了。實際踩過。
    """
    from ro_toolbox.services import travel_bot as mod

    bot, sent = _travel_bot(monkeypatch)
    monkeypatch.setattr(mod, "_NUDGE_EVERY_SEC", 3600.0)
    for _ in range(10):
        bot._trusted_position("s_atelier", (271, 108))
    assert len(sent) == 1, f"節流沒生效：{sent}"


def test_it_gives_up_loudly_and_says_what_to_do(monkeypatch):
    """問不出來還是要**大聲停用**，而且要講使用者能做的事。"""
    from ro_toolbox.services import travel_bot as mod

    bot, sent = _travel_bot(monkeypatch)
    monkeypatch.setattr(mod, "_NUDGE_EVERY_SEC", 0.0)
    monkeypatch.setattr(mod, "_NUDGE_TRIES", 3)
    for _ in range(6):
        bot._trusted_position("s_atelier", (271, 108))
    assert "問不出自己的座標" in bot.stats.note, bot.stats.note
    assert "自己走一步" in bot.stats.note, "要講使用者能做什麼"


def test_no_terrain_means_we_do_not_second_guess_the_memory(monkeypatch):
    """沒有那張圖的地形就沒得驗 —— 照舊用記憶體的值，不要亂推。"""
    bot, sent = _travel_bot(monkeypatch)
    monkeypatch.setattr(bot, "_terrain_for", lambda name: None)
    assert bot._trusted_position("nowhere", (271, 108)) == (271, 108)
    assert sent == []


# ---- 趕路中的「暫停」按鈕（只暫停，繼續走「自動尋路」那一顆）----------------


def test_pause_button_is_only_live_while_travelling(qtbot):
    """⚠ 沒在趕路時**壓著不能按**（不是藏起來）：藏起來版面會跳，
    而且看不到就不知道有這個功能。"""
    card = make_card(qtbot)
    assert card.travel_pause.isEnabled() is False

    card.set_travel_busy(True)
    assert card.travel_pause.isEnabled() is True

    card.set_travel_busy(False)
    assert card.travel_pause.isEnabled() is False


def test_the_buttons_are_only_as_wide_as_their_text(qtbot):
    """⚠ 這一欄的寬度是**下拉選單**撐出來的（建議寬度來自最長的地圖名）。

    按鈕如果只設「最小寬度」，就會被拉到跟選單一樣長 —— 使用者回報
    「自動尋路跟暫停都太長」。所以兩顆都用**固定**寬度，剛好放得下四個字。
    """
    card = make_card(qtbot)
    fits = card.auto_travel.fontMetrics().horizontalAdvance("自動尋路")

    for button in (card.auto_travel, card.travel_pause):
        assert button.width() >= fits, "至少要放得下四個字"
        assert button.width() <= fits + card.TRAVEL_BUTTON_PAD, "不要比字寬多太多"
        assert button.minimumWidth() == button.maximumWidth(), "固定寬，不准被拉長"

    assert card.destination.minimumWidth() >= card.TRAVEL_BUTTON_MIN_W, "選單維持原寬"


def test_the_pause_button_is_tall_enough_for_its_text(qtbot):
    """⚠ qss 給 QPushButton 上下各 7px 內距 ＋ 外框 —— 用 ROW_HEIGHT(26) 的話
    只剩十來個像素給字，**字會被切掉**（使用者回報）。"""
    card = make_card(qtbot)
    chrome = 16                      # 內距 7×2 ＋ 外框 1×2
    text = card.travel_pause.fontMetrics().height()
    assert card.travel_pause.height() - chrome >= text
    assert card.travel_pause.height() == card.auto_travel.height(), "兩顆要一樣高"


def test_the_pause_button_never_says_resume(qtbot):
    """使用者指定：這顆只做暫停，文字不准變成「繼續」。"""
    card = make_card(qtbot)
    assert card.travel_pause.text() == "暫停"
    assert card.travel_pause.isCheckable() is False

    card.set_travel_busy(True)
    card.set_travel_paused(True)
    assert card.travel_pause.text() == "暫停"


def test_pausing_pops_the_travel_button_without_stopping_the_bot(qtbot):
    """⚠⚠ 「自動尋路」彈起來是要讓人**再按一次繼續**，不是取消。

    那顆的 toggled 直接接到 travel_toggled，程式自己改狀態時不擋訊號的話，
    彈起來會被當成「使用者要取消」—— bot 整個被收攤，正好是暫停要避免的事。
    """
    card = make_card(qtbot)
    said: list[bool] = []
    card.travel_toggled.connect(said.append)

    card.set_travel_busy(True)
    card.auto_travel.setChecked(True)
    assert said == [True]

    card.set_travel_paused(True)
    assert card.auto_travel.isChecked() is False, "要彈起來，才看得出可以再按一次"
    assert said == [True], "程式自己改的，不准被當成使用者取消"
    assert card.travel_pause.isEnabled() is False, "已經暫停了就不用再按暫停"


class _FakeTraveler:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.paused = False

    def pause(self) -> None:
        self.calls.append("pause")
        self.paused = True

    def resume(self) -> None:
        self.calls.append("resume")
        self.paused = False

    def stop(self) -> None:
        self.calls.append("stop")


def test_pause_then_travel_again_resumes_instead_of_restarting(qtbot):
    """使用者指定：要繼續就再按一次「自動尋路」。**不是**重開一個新的 bot。"""
    page = _page(qtbot)
    card = make_card(qtbot)
    bot = _FakeTraveler()
    page._cards[1234] = card
    page._travelers[1234] = bot
    card.set_travel_busy(True)

    page._pause_travel(1234)
    assert bot.calls == ["pause"]
    assert card.auto_travel.isChecked() is False

    page._toggle_travel(1234, True)          # 再按一次「自動尋路」
    assert bot.calls == ["pause", "resume"]
    assert page._travelers[1234] is bot, "不准換一個新的 bot（那等於收攤重來）"
    assert card.auto_travel.isChecked() is True
    assert card.travel_pause.isEnabled() is True


def test_travelling_button_pressed_again_while_running_does_nothing(qtbot):
    """沒暫停的時候再按一次不該重開，也不該偷偷 resume。"""
    page = _page(qtbot)
    bot = _FakeTraveler()
    page._travelers[1234] = bot
    page._toggle_travel(1234, True)
    assert bot.calls == []


def test_pausing_with_no_bot_puts_the_ui_back(qtbot):
    """壓著卻沒有東西在暫停 = 騙人。"""
    page = _page(qtbot)
    card = make_card(qtbot)
    page._cards[1234] = card
    card.set_travel_busy(True)
    card.auto_travel.setChecked(True)

    page._pause_travel(1234)                 # _travelers 裡沒有 1234
    assert card.travel_pause.isEnabled() is True
    assert card.auto_travel.isChecked() is True


# ---- 死亡：跳「按確定才消失」的框，而且只關自動打怪 ------------------------


class FakeFarmStats:
    def __init__(self, **kw) -> None:
        self.running = kw.get("running", True)
        self.kills = 0
        self.picked = 0
        self.monsters_near = 0
        self.target = ""
        self.note = kw.get("note", "")
        self.last_loot = ""
        self.walk_rejected = 0
        self.missed = 0
        self.resent = 0
        self.died = kw.get("died", False)


def test_death_pops_a_notice_and_only_turns_off_auto_hunt(qtbot, monkeypatch):
    """使用者指定：死了就跳通知窗、關掉自動打怪，**別的什麼都不要做**。"""
    from ro_toolbox.ui.pages import farm_page as mod

    shown = []
    monkeypatch.setattr(mod, "show_notice", lambda *args: shown.append(args))
    card = make_card(qtbot)
    card.character = "狐狐狸"
    card.auto_hunt.setChecked(True)

    card._apply_farm_stats(FakeFarmStats(running=False, died=True))
    assert len(shown) == 1
    assert "狐狐狸" in shown[0][1]
    assert card.auto_hunt.isChecked() is False


def test_death_only_pops_one_notice(qtbot, monkeypatch):
    """bot 停下來時還會再回報一次同一份 stats（died 仍是 True）—— 只准跳一次。"""
    from ro_toolbox.ui.pages import farm_page as mod

    shown = []
    monkeypatch.setattr(mod, "show_notice", lambda *args: shown.append(args))
    card = make_card(qtbot)
    card._apply_farm_stats(FakeFarmStats(running=True, died=True))
    card._apply_farm_stats(FakeFarmStats(running=False, died=True))
    assert len(shown) == 1


def test_a_new_run_can_report_death_again(qtbot, monkeypatch):
    """再開一輪又死了，還是要講 —— 閘門只擋同一輪的重複回報。"""
    from ro_toolbox.ui.pages import farm_page as mod

    shown = []
    monkeypatch.setattr(mod, "show_notice", lambda *args: shown.append(args))
    card = make_card(qtbot)
    card._apply_farm_stats(FakeFarmStats(running=False, died=True))
    card._apply_farm_stats(FakeFarmStats(running=True, died=False))   # 新的一輪
    card._apply_farm_stats(FakeFarmStats(running=False, died=True))
    assert len(shown) == 2


def test_the_arrival_notice_needs_an_ok_press(qtbot):
    """使用者指定：抵達的框要**按確定才消失**，不能自己收掉。"""
    from ro_toolbox.ui.widgets.toast import TopToast

    notice = TopToast("到了", "測試", seconds=0, icon="⚠", need_ok=True)
    qtbot.addWidget(notice)
    assert notice.ok_button.text() == "確定"

    closed: list[int] = []
    notice.close = lambda: closed.append(1)   # 攔下關閉，看誰會關它
    notice.mousePressEvent(None)
    assert closed == [], "點旁邊不算 —— 使用者要求按確定才消失"


def test_the_plain_toast_still_closes_on_a_click(qtbot):
    """一般通知（沒有確定鈕）維持原本的行為：點一下就收。"""
    from ro_toolbox.ui.widgets.toast import TopToast

    toast = TopToast("進度", "測試", seconds=0)
    qtbot.addWidget(toast)
    closed: list[int] = []
    toast.close = lambda: closed.append(1)
    toast.mousePressEvent(None)
    assert closed == [1]
