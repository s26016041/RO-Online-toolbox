"""隔著障礙物「盯著怪發呆到黑名單」（[DAT-076]，2026-09-05）。

使用者：「練等很容易我跟怪物中間有障礙物，他不會繞過障礙物，只會一直盯著怪物
發呆到把他加黑名單，這很糟糕要修改。」

實機看得到的四件事：
1. 一份日誌裡「走到一半被擋住，換下一隻」17 次，**全在擊殺後 1~2 秒**
   ——那時旁邊的怪還在打人，被打有硬直，伺服器會直接丟掉那一拍的移動
   （沒有 0x0087）。舊版把它當「太遠被拒絕」：步幅 10→5→2→1 然後放棄整條路。
2. 走不成一次就把牠拉黑 30 秒。伺服器帶的路跟我們算的不一樣（偏離路徑）
   也算走不成。
3. 追怪 10 秒「直線距離沒變小」就**安靜地**拉黑 —— 繞山脊的時候直線距離
   十幾秒都不會變小（mjolnir_05 實測最長繞路 102 格）。
4. 那 10 秒日誌裡**一個字都沒有**。

修法：被打的硬直不縮步幅；走不成先重新規劃 3 次；還在走就不算打不到；
發呆 3 秒印一次幾何狀態。
"""

from __future__ import annotations

import logging

import numpy as np

from ro_toolbox.services import farm_bot as mod
from ro_toolbox.services.entities import MemoryEntity
from ro_toolbox.services.farm_bot import FarmBot
from ro_toolbox.services.mapdata import MapTerrain
from ro_toolbox.services.walker import ACK_TIMEOUT, MAX_RESEND, MAX_STEP, Walker

T0 = 1000.0


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sent: list[tuple[int, int]] = []

    def clock(self) -> float:
        return self.now

    def send(self, x: int, y: int) -> None:
        self.sent.append((x, y))


def _path(length: int) -> list[tuple[int, int]]:
    return [(10 + i, 50) for i in range(1, length + 1)]


# ---- Walker：被打的硬直不是「太遠被拒絕」 ------------------------------------------

def test_a_hit_lock_resends_the_same_leg_instead_of_shrinking_the_step():
    fake = _Clock()
    walker = Walker(fake.send, now=fake.clock, hit_locked=lambda: True)
    walker.set_path(_path(40))
    walker.update((10, 50))
    first = fake.sent[-1]

    fake.now += ACK_TIMEOUT + 0.01
    assert walker.update((10, 50)) == "walking"
    assert fake.sent[-1] == first, "同一段再送一次，不要換更短的"
    assert walker.rejected == 0 and walker.resent == 1
    assert walker._step == MAX_STEP, "步幅不准縮"


def test_the_hit_lock_excuse_is_bounded():
    """被打可以解釋幾拍，不能解釋永遠 —— 重送上限用完就回到原本的判斷。"""
    fake = _Clock()
    walker = Walker(fake.send, now=fake.clock, hit_locked=lambda: True)
    walker.set_path(_path(40))
    walker.update((10, 50))
    state = "walking"
    for _ in range(MAX_RESEND + 8):
        fake.now += ACK_TIMEOUT + 0.01
        state = walker.update((10, 50))
        if state == "blocked":
            break
    assert state == "blocked"
    assert walker.resent == MAX_RESEND
    assert walker.rejected >= 1, "重送用完之後要開始縮步幅，最後放棄"


def test_without_a_hit_the_old_rejection_logic_is_unchanged():
    fake = _Clock()
    walker = Walker(fake.send, now=fake.clock, hit_locked=lambda: False)
    walker.set_path(_path(40))
    walker.update((10, 50))
    fake.now += ACK_TIMEOUT + 0.01
    walker.update((10, 50))
    assert walker.rejected == 1 and walker._step == MAX_STEP // 2


# ---- FarmBot ------------------------------------------------------------------------

def _bot(monkeypatch, blocked=()):
    bot = FarmBot(1234)
    side = 60
    types = np.zeros((side, side), np.uint32)
    for x, y in blocked:
        types[y, x] = 1
    bot._terrain = MapTerrain(name="t", width=side, height=side, types=types)
    bot._world.set_map_size((side, side))
    monkeypatch.setattr(bot, "_send", lambda _data: None)
    return bot


def _see(bot: FarmBot, gid: int, x: int, y: int) -> None:
    bot._world.sync_from_memory([MemoryEntity(gid, 1052, x, y, addr=0)])


def test_recently_hit_reads_the_aggro_timestamps():
    bot = FarmBot(1234)
    assert bot._recently_hit(T0) is False
    bot._aggro[7] = T0 - 0.2
    assert bot._recently_hit(T0) is True
    assert bot._recently_hit(T0 + 5.0) is False


def test_a_blocked_walk_is_replanned_before_the_monster_is_dropped(monkeypatch, caplog):
    """走不成 → 清掉路徑重算，前 3 次都不准拉黑。"""
    bot = _bot(monkeypatch)
    _see(bot, 5, 20, 10)
    bot._update_aim(T0, (10, 10))
    assert bot._aim is not None
    monkeypatch.setattr(bot._walker, "update", lambda _pos: "blocked")

    with caplog.at_level(logging.INFO):
        for i in range(1, mod._APPROACH_REPLANS + 1):
            assert bot._approach((10, 10), (20, 10)) is None, f"第 {i} 次要重規劃"
            assert bot._aim.replans == i
            assert not bot._walker.active, "路徑要清掉，下一拍才會從現在的位置重算"
        why = bot._approach((10, 10), (20, 10))
    assert why is not None and "重新規劃" in why and "走到一半被擋住" in why
    assert any("重新規劃" in r.getMessage() for r in caplog.records)


def test_still_walking_around_an_obstacle_is_not_given_up_after_ten_seconds(monkeypatch):
    """繞路中直線距離不變小 —— 只要人還在動就不放棄，直到硬上限。"""
    bot = _bot(monkeypatch)
    _see(bot, 5, 30, 10)
    bot._update_aim(T0, (10, 10))
    aim = bot._aim
    assert aim is not None
    # 12 秒內一直在走（位置每拍都變），但離怪一直是 20 格（繞路）
    pos = (10, 10)
    for step in range(1, 61):
        pos = (10, 10 + (step % 2))          # 來回動，直線距離不變
        bot._update_aim(T0 + step * 0.2, pos)
        assert bot._aim is aim, f"第 {step} 拍：還在走就不准放棄"
    # 超過硬上限就要放棄
    bot._update_aim(T0 + mod._CHASE_CAP_SEC + 1.0, (10, 11))
    assert bot._aim is None and 5 in bot._skip


def test_standing_still_for_ten_seconds_still_gives_up_and_says_why(monkeypatch, caplog):
    bot = _bot(monkeypatch)
    _see(bot, 5, 30, 10)
    bot._update_aim(T0, (10, 10))
    bot._update_aim(T0 + 0.2, (10, 10))          # 第一拍量到距離，計時從這裡起算
    with caplog.at_level(logging.INFO):
        bot._update_aim(T0 + 0.2 + mod._GIVE_UP_SEC + 1.0, (10, 10))
    assert bot._aim is None and 5 in bot._skip
    said = [r.getMessage() for r in caplog.records if "沒更靠近" in r.getMessage()]
    assert said and "直線乾淨" in said[0], "放棄要講原因，不准安靜地拉黑"


def test_staring_at_a_monster_for_three_seconds_is_reported_once(monkeypatch, caplog):
    """使用者看到的「發呆」那一刻要留下幾何狀態（只印一次）。"""
    wall = [(15, y) for y in range(0, 60) if y != 40]     # 牆，缺口在很遠的地方
    bot = _bot(monkeypatch, blocked=wall)
    _see(bot, 5, 20, 10)
    monkeypatch.setattr(bot._walker, "update", lambda _pos: "walking")   # 假裝在走，其實沒動
    bot._update_aim(T0, (10, 10))
    with caplog.at_level(logging.WARNING):
        for step in range(1, 25):
            now = T0 + step * 0.2
            bot._update_aim(now, (10, 10))
            bot._fight(now, (10, 10))
    stares = [r for r in caplog.records if "發呆" in r.getMessage()]
    assert len(stares) == 1
    text = stares[0].getMessage()
    assert "直線乾淨=False" in text and "旁邊格=" in text and "目標=" in text
