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
