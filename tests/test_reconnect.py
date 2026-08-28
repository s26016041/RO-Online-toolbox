"""自動回連的判斷邏輯（純邏輯，不碰遊戲）。

守三件使用者明確要求的事：
  1. **你自己的網路斷了就什麼都不做** —— 關遊戲重開是幫倒忙
  2. **換地圖的瞬間不算斷線** —— 那時候連線本來就會短暫消失
  3. **不准無腦一直試** —— 失敗要退避，間隔一次比一次長
"""

from __future__ import annotations

from ro_toolbox.services import reconnect
from ro_toolbox.services.reconnect import (
    BACKOFF,
    NO_NETWORK,
    OK,
    RECONNECT,
    WATCHING,
    ReconnectDecider,
)

T0 = 1000.0


def test_connected_is_ok():
    d = ReconnectDecider()
    assert d.decide(has_server=True, network_up=True, now=T0) == OK


def test_my_own_network_down_never_touches_the_game():
    """**這是使用者明確要求的**：我的網路斷了就等它回來，不要關遊戲。

    重開之後照樣連不上，而且原本還在線上的角色被登出了。
    """
    d = ReconnectDecider()
    for i in range(20):
        got = d.decide(has_server=False, network_up=False, now=T0 + i * 30)
        assert got == NO_NETWORK, "網路沒回來之前一律不動遊戲"
    assert "等它回來" in d.note


def test_the_grace_window_starts_only_after_the_network_is_back():
    """網路斷著的那段時間不算觀察期 —— 不然網路一回來就立刻重開遊戲。"""
    d = ReconnectDecider(grace=20)
    for i in range(10):
        d.decide(has_server=False, network_up=False, now=T0 + i * 30)
    assert d.decide(has_server=False, network_up=True, now=T0 + 300) == WATCHING
    assert d.decide(has_server=False, network_up=True, now=T0 + 315) == WATCHING
    assert d.decide(has_server=False, network_up=True, now=T0 + 321) == RECONNECT


def test_one_lone_sample_of_no_connection_does_not_kill_the_game():
    """看到一拍沒連線就重開遊戲＝拿單一次讀數做不可逆的事。

    ⚠ 這一條的理由**不是換地圖**。[PKT-063] 量過換圖那一刻的連線表：
    新舊兩條並存（舊的過 11 分鐘才收），所以換圖根本不會讓
    `find_server()` 變 None。留著觀察期是因為 `find_server()` 讀的是
    Windows TCP 表的快照，單一次取樣可能因為讀取失敗或時序落差而騙人 ——
    而重連會**關掉使用者正在玩的遊戲**。
    """
    d = ReconnectDecider(grace=20)
    assert d.decide(has_server=False, network_up=True, now=T0) == WATCHING
    assert d.decide(has_server=False, network_up=True, now=T0 + 3) == WATCHING
    assert d.decide(has_server=True, network_up=True, now=T0 + 5) == OK
    # 觀察期要歸零，不能累積到下一次
    assert d.decide(has_server=False, network_up=True, now=T0 + 6) == WATCHING
    assert d.decide(has_server=False, network_up=True, now=T0 + 20) == WATCHING


def test_real_disconnect_asks_for_a_reconnect():
    d = ReconnectDecider(grace=20)
    d.decide(has_server=False, network_up=True, now=T0)
    assert d.decide(has_server=False, network_up=True, now=T0 + 21) == RECONNECT


def test_failures_back_off_instead_of_retrying_blindly():
    """**使用者明確說「無腦嘗試很糟糕」。** 維修時我們分不出來，
    所以至少不能一直重開遊戲 —— 間隔要一次比一次長。"""
    d = ReconnectDecider(grace=20)
    d.decide(has_server=False, network_up=True, now=T0)
    assert d.decide(has_server=False, network_up=True, now=T0 + 21) == RECONNECT

    now = T0 + 21
    waits = []
    for _ in range(4):
        d.note_attempt_failed(now)
        assert d.decide(has_server=False, network_up=True, now=now + 1) == BACKOFF
        wait = reconnect.BACKOFF_SEC[min(d.failures - 1, len(reconnect.BACKOFF_SEC) - 1)]
        waits.append(wait)
        now += wait + 1
        d.decide(has_server=False, network_up=True, now=now)          # 觀察期重新開始
        now += 21
        assert d.decide(has_server=False, network_up=True, now=now) == RECONNECT
    assert waits == sorted(waits), "間隔要越等越久"
    assert waits[0] < waits[-1]


def test_backoff_is_cleared_once_we_are_connected_again():
    d = ReconnectDecider(grace=20)
    d.note_attempt_failed(T0)
    assert d.failures == 1
    assert d.decide(has_server=True, network_up=True, now=T0 + 1) == OK
    assert d.failures == 0, "連上了就把退避歸零，下次斷線不該被上次的失敗拖累"


def test_the_grace_window_is_short_because_it_is_only_about_sampling():
    """觀察期只需要「連續幾拍」，不需要幾十秒。

    呼叫端每秒取樣一次，所以預設 5 秒 ≈ 連續 5 拍都沒有連線。
    整條回連（關遊戲→重開→重新登入）本來就要三十秒級，
    前面這 5 秒在體感上等於即時。
    """
    assert reconnect.GRACE_SEC == 5.0
    d = ReconnectDecider()
    for i in range(5):
        assert d.decide(False, True, T0 + i) == WATCHING, "5 秒內不准動手"
    assert d.decide(False, True, T0 + 6) == RECONNECT
