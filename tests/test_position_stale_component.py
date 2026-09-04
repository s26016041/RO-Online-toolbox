"""換圖之後被挑到的是**上一張圖的殘留元件**（[DAT-072]）。

使用者 2026-09-04：「到底為啥每次換圖都會找不到 奇怪了」。

實機日誌（白狐，11:10:07 被移到 `mjolnir_12` (199,375)，人在 `aldebaran`
(133,103)）：

    11:10:08  地圖從 aldebaran 換到 mjolnir_12，重新定位角色移動元件
    11:10:08  角色移動元件定位於 0x1fe02aa0     ← aldebaran 那一顆，(133,103)
    11:10:09  角色移動元件 0x1fe02aa0 已失效     ← 客戶端終於把它回收
    11:10:13  讀不到角色座標，送一步移動把位置逼出來
    11:10:16  mjolnir_12 上挑了 657 個問位置的目標

換圖那一刻的真實狀況：

- **新元件**已經在堆積上（`GID == AID` 掃得到），但狀態欄位還是 0 → 驗不過。
- **舊元件**要再過 1~2 秒客戶端才回收；在那之前 `state==1`、`dest` 還是上一張
  圖的格子 → `gid`／`state`／`dest 範圍` **三關全過**。

於是「通過驗證的剛好一個」成立，`_closest()`（只在候選 >1 時才跑）根本沒被叫到，
殘留物就這樣被當成本人。`mjolnir_12` 夠大、(133,103) 在上面站得住 →
`_on_map()` 也放行 → `read()` 回上一張圖的座標，還把 `_moved_here` 設成 True。
`_moved_here` 一旦為 True 就**永遠**不准再退回進圖座標（那條規則是對的），
所以兩秒後殘留物被回收，`read()` 一路回 None ——「每次換圖都找不到」。

修法：把 `_closest()` 的判準（**位置在時間上是連續的**）從「平手時的 tie-break」
升級成**每次都跑的過濾**，見 `_near_reference()`。
"""

from __future__ import annotations

from ro_toolbox.services import player_position as pp

OLD_CELL = (133, 103)      # aldebaran 上最後站的地方
NEW_CELL = (199, 375)      # 伺服器說「你被移到 mjolnir_12 (199,375)」
STALE, FRESH = 0x1FE02AA0, 0x320B0298


def _locator(*, entry, warped_at=None, last=None, last_at=0.0, now=1000.0):
    loc = pp.PlayerPosition.__new__(pp.PlayerPosition)
    loc._now = lambda: now
    loc._last_pos = last
    loc._last_pos_at = last_at
    loc._warped_at = warped_at
    loc._said_far = False
    loc._entry_pos = lambda: entry
    loc._on_map = lambda pos, map_name: True     # 兩張圖都夠大，都站得住
    return loc


def test_the_leftover_component_from_the_previous_map_is_rejected():
    """★★ 只有殘留物驗得過的時候，**不准**把它當本人。

    這正是實機 11:10:08 的形狀：新元件還沒填好、舊元件還沒被回收。
    正確答案是「這張圖上還沒有活的元件」→ 讓 `read()` 退回**進圖座標**
    （伺服器剛講過的落點），而不是回上一張圖的位置。
    """
    loc = _locator(entry=NEW_CELL, warped_at=1000.0 - 1.0)   # 1 秒前才換圖
    assert loc._near_reference([STALE], {STALE: OLD_CELL}, "mjolnir_12") == []


def test_the_real_component_next_to_the_landing_cell_is_kept():
    """走一步之後冒出來的新元件就在落點旁邊 —— 不准被同一道關卡誤殺。"""
    loc = _locator(entry=NEW_CELL, warped_at=1000.0 - 1.0)
    seen = {STALE: OLD_CELL, FRESH: (202, 375)}
    assert loc._near_reference([STALE, FRESH], seen, "mjolnir_12") == [FRESH]


def test_the_allowance_grows_with_time_since_the_warp():
    """錨會隨時間變鬆：換圖很久了，角色本來就可能走很遠。"""
    gap = max(abs(OLD_CELL[0] - NEW_CELL[0]), abs(OLD_CELL[1] - NEW_CELL[1]))
    long_ago = (gap - pp._DRIFT_SLACK) / pp._DRIFT_CELLS_PER_SEC + 1
    loc = _locator(entry=NEW_CELL, warped_at=1000.0 - long_ago)
    assert loc._near_reference([STALE], {STALE: OLD_CELL}, "mjolnir_12") == [STALE]


def test_the_entry_cell_is_not_an_anchor_until_we_saw_the_warp():
    """⚠ 沒親眼看到伺服器移動角色，進圖座標只是「上次進圖時在哪」。

    那時角色早就走遠了 —— 拿它當錨會把**活的**元件誤殺，
    症狀比原本的 bug 還糟（走路功能整個停掉）。
    """
    loc = _locator(entry=NEW_CELL, warped_at=None)
    assert loc._near_reference([STALE], {STALE: OLD_CELL}, "mjolnir_12") == [STALE]


def test_a_landing_cell_that_is_not_on_this_map_is_not_an_anchor():
    """換圖訊號到了、進圖座標全域還沒寫 —— 那個值不能拿來挑人。"""
    loc = _locator(entry=NEW_CELL, warped_at=1000.0 - 1.0)
    loc._on_map = lambda pos, map_name: False
    assert loc._near_reference([STALE], {STALE: OLD_CELL}, "mjolnir_12") == [STALE]


def test_the_last_reading_wins_over_the_landing_cell():
    """圖中間元件被回收重配：上一次讀到的位置比落點新，用它當錨。"""
    loc = _locator(entry=NEW_CELL, warped_at=1000.0 - 60,
                   last=(202, 375), last_at=1000.0 - 0.3)
    seen = {STALE: OLD_CELL, FRESH: (204, 375)}
    assert loc._near_reference([STALE, FRESH], seen, "mjolnir_12") == [FRESH]


def test_nothing_is_dropped_when_there_is_no_reference_at_all():
    """連參考點都沒有就什麼都不做（安全退化，不是拒絕）。"""
    loc = _locator(entry=None, warped_at=1000.0 - 1.0)
    assert loc._near_reference([STALE], {STALE: OLD_CELL}, "") == [STALE]


def test_the_filter_message_is_not_spammed(caplog):
    """這條路每 0.3 秒走一次 —— 不擋就是一秒三行（三個分身更慘）。"""
    import logging

    loc = _locator(entry=NEW_CELL, warped_at=1000.0 - 1.0)
    with caplog.at_level(logging.INFO):
        for _ in range(20):
            loc._near_reference([STALE], {STALE: OLD_CELL}, "mjolnir_12")
    assert len(caplog.records) == 1
