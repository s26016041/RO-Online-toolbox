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
    monkeypatch.setattr(mod, "show_toast", lambda *args: shown.append(args))
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
    monkeypatch.setattr(mod, "show_toast", lambda *args: shown.append(args))
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
