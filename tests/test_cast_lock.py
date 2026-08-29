"""詠唱期間「大家別動」的那一小片共用狀態。

釘的是使用者 2026-08-29 指定的優先序：**幫隊友放 buff 高於打怪跟尋路**。

實機證據（白狐掛機中）：封包送得出去、隊友也在旁邊，就是連續三次
「沒上身」—— 因為自動打怪一路在送走路封包，而**移動會打斷詠唱**。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import cast_lock


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _clean():
    cast_lock.clear()
    yield
    cast_lock.clear()


def test_holding_makes_everyone_else_wait():
    clock = Clock()
    assert not cast_lock.held(1234, clock)
    cast_lock.hold(1234, 1.0, clock)
    assert cast_lock.held(1234, clock)


def test_a_hold_expires_on_its_own():
    """⚠ **不能卡死。** 補 buff 那條掛掉的話，打怪最多停 `MAX_HOLD` 秒。"""
    clock = Clock()
    cast_lock.hold(1234, 1.0, clock)
    clock.t += 1.1
    assert not cast_lock.held(1234, clock)


def test_a_hold_is_capped():
    clock = Clock()
    cast_lock.hold(1234, 999.0, clock)
    clock.t += cast_lock.MAX_HOLD + 0.1
    assert not cast_lock.held(1234, clock), "再長也不准超過上限"


def test_releasing_gives_the_road_back_immediately():
    """上身了就馬上放行 —— 讓路是為了讓那一發打得出去，不是為了等結果。"""
    clock = Clock()
    cast_lock.hold(1234, cast_lock.MAX_HOLD, clock)
    cast_lock.release(1234)
    assert not cast_lock.held(1234, clock)


def test_each_game_is_independent():
    """一台機器開三個遊戲，一隻在詠唱不該讓另外兩隻站住。"""
    clock = Clock()
    cast_lock.hold(1234, 1.0, clock)
    assert cast_lock.held(1234, clock)
    assert not cast_lock.held(5678, clock)


def test_the_longer_hold_wins():
    """兩發連著放的時候，後面那發的讓路時間不能被前一發縮短。"""
    clock = Clock()
    cast_lock.hold(1234, 2.0, clock)
    cast_lock.hold(1234, 0.1, clock)
    clock.t += 0.5
    assert cast_lock.held(1234, clock)
