"""座標只有「進圖座標」可用時，不准被當成「角色沒在動」。

回歸測試，症狀是使用者實測回報的「無法自動打怪」：
剛換圖還沒走過路 → 移動元件驗不出來 → `read_position()` 回進圖座標 →
那個值**角色跑再遠也不會變** → 自動打怪的卡住偵測 45 秒後把自己關掉。
"""

from __future__ import annotations

import logging

from ro_toolbox.services import player_position as pp
from ro_toolbox.services.farm_bot import FarmBot


class FakeReader:
    """只回答 `_alive()` 會用到的東西。"""

    def __init__(self, live: bool) -> None:
        self.position_live = live
        self.pos = (19, 377)
        self.hp = 100

    def read(self):
        class Status:
            hp = 100
        return Status()

    def read_position(self):
        return self.pos


def _bot(live: bool) -> FarmBot:
    bot = FarmBot(1234)
    bot._link.reader = FakeReader(live)
    return bot


def test_a_frozen_entry_position_does_not_look_like_being_stuck():
    """座標不是即時的時候，改看「送出去幾個移動封包」。"""
    bot = _bot(live=False)
    now = 1000.0
    assert bot._alive(now)

    # 角色其實在走（walker 一直在送），只是座標來源是靜態的進圖座標。
    # 每一輪都跨過 45 秒的門檻 —— 舊版在第一輪就會把自己關掉。
    for _ in range(5):
        bot._walker.sent += 1
        now += 20.0
        assert bot._alive(now), "還在送移動就不算卡住"


def test_really_stuck_is_still_caught_without_a_live_position():
    """連移動都不送了 —— 那才是真的卡住，照樣要抓到。

    ⚠ 「抓到」＝**清狀態重來並留紀錄**，不是關掉自動打怪
    （使用者訂的規則：只有死掉才准關，見 GAMEDATA [DAT-050]）。
    """
    bot = _bot(live=False)
    assert bot._alive(1000.0)
    assert bot._alive(1000.0 + 46.0), "卡住不是關掉自動打怪的理由"
    assert bot._stuck == 1, "但要算進去、留紀錄"
    assert "沒進展" in bot.stats.note


def test_a_live_position_still_uses_the_coordinates():
    """座標是即時的時候，維持原本的判準（位置沒變就是沒動）。"""
    bot = _bot(live=True)
    assert bot._alive(1000.0)
    bot._link.reader.pos = (20, 377)
    assert bot._alive(1000.0 + 46.0), "位置變了就是有在動"
    assert bot._stuck == 0, "有在動就不算卡住"
    assert bot._alive(1000.0 + 100.0)
    assert bot._stuck == 1, "位置又不動了就該抓到"


def test_missing_component_is_said_once_not_every_tick(caplog):
    """這條路每 0.3 秒走一次，每次都印就是一秒三行的洗版。"""
    position = pp.PlayerPosition(scanner=None)
    position._aid = 23810315
    position._candidates = [0x1000]
    position._scan = lambda _aid: [0x1000]
    # ⚠ 假的是 `_look_at`（「這是不是本人 / 它說自己在哪」）—— 那才是掃描與
    #   每拍讀取共用的那一支（[MEM-060] 之後 `_component_at` 只是它的薄殼）。
    position._look_at = lambda _addr: (False, None)

    with caplog.at_level(logging.INFO):
        for _ in range(10):
            position._last_full = position._now()      # 別重新全掃
            assert not position._locate_component()
    assert caplog.text.count("還沒找到角色的移動元件") == 1

    # 找到之後再掉出來，要能再說一次（不然改版壞掉時沒人知道）。
    position._look_at = lambda _addr: (True, (100, 100))
    assert position._locate_component()
    position._look_at = lambda _addr: (False, None)
    with caplog.at_level(logging.INFO):
        position._last_full = position._now()
        assert not position._locate_component()
    assert caplog.text.count("還沒找到角色的移動元件") == 2
