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


def test_the_first_few_failures_retry_immediately():
    """使用者 2026-08-29 指定：「登入到一半出錯或登入失敗都不等待，馬上關閉重開」。

    登入失敗多半是卡登／輸入被搶走那種**重來就好**的問題 ——
    等 30 秒只是讓角色多躺 30 秒。
    """
    d = ReconnectDecider(grace=20)
    d.decide(has_server=False, network_up=True, now=T0)
    assert d.decide(has_server=False, network_up=True, now=T0 + 21) == RECONNECT

    d.note_attempt_failed(T0 + 21)
    assert "馬上" in d.note
    assert d.decide(has_server=False, network_up=True, now=T0 + 22) == RECONNECT


def test_failures_back_off_eventually_instead_of_retrying_forever():
    """**使用者也說過「無腦嘗試很糟糕」。** 連續失敗幾次之後就開始等 ——
    那時候多半是真的有問題（帳密錯、伺服器維修），一直狂開遊戲只會更糟。"""
    d = ReconnectDecider(grace=20)
    now = T0
    waits = []
    for _ in range(len(reconnect.BACKOFF_SEC)):
        d.note_attempt_failed(now)
        waits.append(reconnect.BACKOFF_SEC[
            min(d.failures - 1, len(reconnect.BACKOFF_SEC) - 1)])
        now += waits[-1] + 1
    assert waits == sorted(waits), "間隔只准越等越久"
    assert waits[0] == 0.0, "前幾次馬上重試"
    assert waits[-1] > 0.0, "最後總要開始等"
    # 退避期間問它，要老實說「還在等」，不准又叫人開遊戲
    assert d.decide(has_server=False, network_up=True,
                    now=now - waits[-1]) == BACKOFF


def test_backoff_is_cleared_once_we_are_connected_again():
    d = ReconnectDecider(grace=20)
    d.note_attempt_failed(T0)
    assert d.failures == 1
    assert d.decide(has_server=True, network_up=True, now=T0 + 1) == OK
    assert d.failures == 0, "連上了就把退避歸零，下次斷線不該被上次的失敗拖累"


def test_the_grace_window_gives_the_connection_a_chance_to_come_back():
    """使用者 2026-08-29 指定：斷線要**等 30 秒**才關閉重開。

    關掉重開的代價很大（重登、重選角、重新掛機），而斷線有時候會自己回來 ——
    寧可多等半分鐘，也不要白重開一次。呼叫端每秒取樣一次，
    所以 30 秒 ≈ 連續 30 拍都沒有連線。
    """
    assert reconnect.GRACE_SEC == 30.0
    d = ReconnectDecider()
    for i in range(30):
        assert d.decide(False, True, T0 + i) == WATCHING, "30 秒內不准動手"
    assert d.decide(False, True, T0 + 31) == RECONNECT
