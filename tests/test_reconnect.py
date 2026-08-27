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


def test_a_map_change_does_not_count_as_a_disconnect():
    """換地圖時伺服器會把連線移到另一台 map server（[PKT-038]），
    那個瞬間 find_server() 就是 None。看到一次就重開＝每次換圖都把自己踢掉。"""
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
