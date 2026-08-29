"""隊友是誰、身上有什麼 —— 全部從封包學（不需要遊戲）。

版面全部來自實機擷取（2026-08-29，白狐 ＋ 狐狐狸同隊）：

    0x0107  1c907c01 3f00 5b00   隊員位置：AID(4) + x(2) + y(2)
    0x0983  0a00 b7ae7b01 01 80a90300 80a90300 …   EFST(2)+AID(4)+state(1)+total+remain
"""

from __future__ import annotations

import struct

from ro_toolbox.services.party import (
    FORGET_AFTER,
    OP_NAME_ACK,
    OP_PARTY_MOVE,
    OP_STATE_PLAIN,
    OP_STATE_TIMED,
    PartyWatch,
)

ME = 23810315          # 狐狐狸
MATE = 24940572        # 白狐
QUICKEN_EFST = 2
BLESSING_EFST = 10


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _move(aid: int, x: int = 63, y: int = 91) -> bytes:
    return struct.pack("<IHH", aid, x, y)


def _timed(efst: int, aid: int, state: int, total: int, remain: int) -> bytes:
    return struct.pack("<HIBII", efst, aid, state, total, remain) + b"\x00" * 12


def _plain(efst: int, aid: int, state: int) -> bytes:
    return struct.pack("<HIB", efst, aid, state)


def _watch():
    clock = Clock()
    return PartyWatch(ME, clock), clock


def test_a_party_move_packet_is_what_makes_someone_a_mate():
    """伺服器只把 `0x0107` 送給同一隊的人 —— 收到誰的，誰就是隊友。"""
    watch, _clock = _watch()
    assert watch.mates() == []

    watch.feed(OP_PARTY_MOVE, _move(MATE))
    assert [m.aid for m in watch.mates()] == [MATE]
    assert watch.mates()[0].cell == (63, 91)


def test_my_own_position_is_not_a_mate():
    watch, _clock = _watch()
    watch.feed(OP_PARTY_MOVE, _move(ME))
    assert watch.mates() == []


def test_state_is_only_tracked_for_confirmed_mates():
    """還沒收過某人的 `0x0107` 就不記他的狀態 —— 那可能只是路過的玩家。"""
    watch, clock = _watch()
    watch.feed(OP_STATE_TIMED, _timed(BLESSING_EFST, 999999, 1, 240000, 240000))
    assert watch.mates() == []

    watch.feed(OP_PARTY_MOVE, _move(MATE))
    watch.feed(OP_STATE_TIMED, _timed(BLESSING_EFST, MATE, 1, 240000, 240000))
    assert watch.mates()[0].has(BLESSING_EFST, clock())


def test_a_status_that_drops_is_forgotten():
    watch, clock = _watch()
    watch.feed(OP_PARTY_MOVE, _move(MATE))
    watch.feed(OP_STATE_TIMED, _timed(BLESSING_EFST, MATE, 1, 240000, 240000))
    watch.feed(OP_STATE_PLAIN, _plain(BLESSING_EFST, MATE, 0))
    assert not watch.mates()[0].has(BLESSING_EFST, clock())


def test_half_time_is_the_line_for_helping():
    """使用者指定：隊友剩不到總時長的 50% 就補。"""
    watch, clock = _watch()
    watch.feed(OP_PARTY_MOVE, _move(MATE))
    watch.feed(OP_STATE_TIMED, _timed(BLESSING_EFST, MATE, 1, 240000, 240000))
    mate = watch.mates()[0]

    clock.t += 100                     # 剩 140/240 = 58%
    assert not watch.needs(mate, BLESSING_EFST, 0.5)
    clock.t += 30                      # 剩 110/240 = 46%
    assert watch.needs(mate, BLESSING_EFST, 0.5)


def test_never_seen_counts_as_needing_it():
    """隊友的狀態只看得到「開始擷取之後的變化」——「查不到」不等於「他有」。

    放一次很便宜，而且放完就會收到 `0x0983`，之後就知道了。
    """
    watch, _clock = _watch()
    watch.feed(OP_PARTY_MOVE, _move(MATE))
    assert watch.needs(watch.mates()[0], BLESSING_EFST, 0.5)


def test_a_permanent_status_is_never_topped_up():
    watch, clock = _watch()
    watch.feed(OP_PARTY_MOVE, _move(MATE))
    watch.feed(OP_STATE_PLAIN, _plain(QUICKEN_EFST, MATE, 1))   # 沒有時間 = 無時限
    mate = watch.mates()[0]
    clock.t += 10_000
    assert not watch.needs(mate, QUICKEN_EFST, 0.5)


def test_a_mate_who_goes_quiet_is_forgotten():
    """換圖、離線、走遠 —— 太久沒消息就別再對他放。"""
    watch, clock = _watch()
    watch.feed(OP_PARTY_MOVE, _move(MATE))
    clock.t += FORGET_AFTER + 1
    assert watch.mates() == []


def test_the_name_comes_from_a_query_not_a_guess():
    """名字用 `0x0368` 查、`0x0095` 回 —— **不從實體封包猜偏移**。"""
    watch, _clock = _watch()
    watch.feed(OP_PARTY_MOVE, _move(MATE))
    assert watch.mates()[0].label() == f"#{MATE}"
    assert watch.unnamed() == [MATE]

    payload = struct.pack("<I", MATE) + "白狐".encode("cp950").ljust(24, b"\x00")
    watch.feed(OP_NAME_ACK, payload)
    assert watch.mates()[0].name == "白狐"
    assert watch.unnamed() == []


# ---- 哪些技能放得到別人身上 ------------------------------------------------


def test_only_skills_that_can_target_others_are_used_on_mates():
    """判準是遊戲說明的「對象」欄位，不是猜的。"""
    from ro_toolbox.services.buffs import can_target_others

    assert can_target_others(29)          # AL_INCAGI「目標1個」
    assert can_target_others(34)          # AL_BLESSING「目標1個」
    assert not can_target_others(60)      # KN_TWOHANDQUICKEN「自己」
    assert not can_target_others(8)       # SM_ENDURE「自己」


def test_no_target_data_means_no():
    """查不到「對象」就不幫別人放 —— 送出去只會被伺服器丟掉。"""
    from ro_toolbox.services.buffs import can_target_others

    assert not can_target_others(999999)
