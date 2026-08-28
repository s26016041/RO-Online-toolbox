"""自動補 buff：學對應、確認上身、失敗就停用（不需要遊戲）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from ro_toolbox.core.ro_protocol import build_use_skill
from ro_toolbox.services.buffs import (
    CONFIRM_TIMEOUT,
    MAX_FAILURES,
    MIN_GAP,
    REFRESH_BELOW_MS,
    BuffKeeper,
    BuffPlan,
)

AID = 0x016B510B
QUICKEN = 60          # KN_TWOHANDQUICKEN
QUICKEN_EFST = 2      # EFST_TWOHANDQUICKEN（實機封包 0x0983 給的）
ENDURE = 8            # SM_ENDURE


@dataclass
class FakeStatus:
    efst: int
    remaining_ms: int | None = 200_000


class Fake:
    """假的連線與狀態來源。時間自己推，測試不睡覺。"""

    def __init__(self, statuses=None) -> None:
        self.sent: list[bytes] = []
        self.statuses: list[FakeStatus] | None = list(statuses or [])
        self.clock = 1000.0
        self.ok = True

    def send(self, data: bytes) -> bool:
        if self.ok:
            self.sent.append(data)
        return self.ok

    def read(self):
        return None if self.statuses is None else list(self.statuses)

    def now(self) -> float:
        return self.clock

    def keeper(self, **kwargs) -> BuffKeeper:
        return BuffKeeper(self.send, AID, self.read, self.now, **kwargs)


def test_casts_a_buff_that_is_missing():
    fake = Fake()
    keeper = fake.keeper(learned={QUICKEN: QUICKEN_EFST})
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()
    assert fake.sent == [build_use_skill(7, QUICKEN, AID)]


def test_does_not_recast_while_the_buff_is_still_long():
    fake = Fake([FakeStatus(QUICKEN_EFST, 200_000)])
    keeper = fake.keeper(learned={QUICKEN: QUICKEN_EFST})
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()
    assert fake.sent == []


def test_recasts_when_it_is_about_to_run_out():
    fake = Fake([FakeStatus(QUICKEN_EFST, REFRESH_BELOW_MS - 1)])
    keeper = fake.keeper(learned={QUICKEN: QUICKEN_EFST})
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()
    assert len(fake.sent) == 1


def test_learns_which_status_the_skill_gives():
    """技能編號跟狀態編號是兩套 —— 名字像不算證據，要施放一次看多出哪個。"""
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()                                   # 不知道對應 → 先放一次
    assert len(fake.sent) == 1
    assert QUICKEN not in keeper.learned

    fake.statuses = [FakeStatus(QUICKEN_EFST)]      # 狀態上身了
    fake.clock += 0.3
    note = keeper.tick()
    assert keeper.learned == {QUICKEN: QUICKEN_EFST}
    assert "學到" in note

    # 學到之後就不會再重放（剩餘時間還很長）
    fake.clock += 10
    keeper.tick()
    assert len(fake.sent) == 1


def test_does_not_learn_when_several_statuses_appear_at_once():
    """同時被怪上了 debuff 的話分不出是哪個 —— 這次就不學，不要賭。"""
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()
    fake.statuses = [FakeStatus(QUICKEN_EFST), FakeStatus(99)]
    fake.clock += 0.3
    keeper.tick()
    assert keeper.learned == {}


def test_gives_up_loudly_after_repeated_failures(caplog):
    """放了沒反應就停用並說清楚 —— 每 0.5 秒安靜重送是最糟的結果。"""
    fake = Fake()
    keeper = fake.keeper(learned={QUICKEN: QUICKEN_EFST})
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    with caplog.at_level(logging.WARNING):
        for _ in range(MAX_FAILURES):
            keeper.tick()                       # 送出
            fake.clock += CONFIRM_TIMEOUT + 0.1
            keeper.tick()                       # 逾時 → 這次失敗
            fake.clock += MIN_GAP

    assert QUICKEN in keeper.stats.disabled
    assert "停用" in caplog.text
    before = len(fake.sent)
    fake.clock += 100
    keeper.tick()
    assert len(fake.sent) == before, "停用之後不該再送"


def test_unchecking_and_rechecking_clears_the_failure_count():
    fake = Fake()
    keeper = fake.keeper(learned={QUICKEN: QUICKEN_EFST})
    keeper.set_plans([BuffPlan(QUICKEN, 7)])
    for _ in range(MAX_FAILURES):
        keeper.tick()
        fake.clock += CONFIRM_TIMEOUT + 0.1
        keeper.tick()
        fake.clock += MIN_GAP
    assert keeper.stats.disabled

    keeper.set_plans([])                        # 取消勾選
    keeper.set_plans([BuffPlan(QUICKEN, 7)])    # 再勾回來 = 再試一次
    assert not keeper.stats.disabled
    fake.clock += MIN_GAP
    keeper.tick()
    assert len(fake.sent) == MAX_FAILURES + 1


def test_unreadable_status_does_nothing():
    """「問不出來」不等於「身上沒有」—— 混在一起會對著還在的 buff 一直重放。"""
    fake = Fake()
    fake.statuses = None
    keeper = fake.keeper(learned={QUICKEN: QUICKEN_EFST})
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    assert "讀不到" in (keeper.tick() or "")
    assert fake.sent == []


def test_permanent_status_is_not_refreshed():
    fake = Fake([FakeStatus(QUICKEN_EFST, None)])
    keeper = fake.keeper(learned={QUICKEN: QUICKEN_EFST})
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()
    assert fake.sent == []


def test_one_cast_per_tick():
    """一拍只做一件事 —— 整排 buff 一起噴出去的話伺服器只會理第一個。"""
    fake = Fake()
    keeper = fake.keeper(learned={QUICKEN: QUICKEN_EFST, ENDURE: 30})
    keeper.set_plans([BuffPlan(QUICKEN, 7), BuffPlan(ENDURE, 8)])

    keeper.tick()
    keeper.tick()
    assert len(fake.sent) == 1


def test_no_sp_means_no_cast():
    fake = Fake()
    keeper = fake.keeper(learned={QUICKEN: QUICKEN_EFST})
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    assert "SP" in (keeper.tick(sp=0) or "")
    assert fake.sent == []


def test_nothing_checked_means_nothing_happens():
    fake = Fake()
    keeper = fake.keeper()
    assert keeper.tick() is None
    assert not keeper.active


@pytest.mark.parametrize(
    ("level", "skill_id", "target", "expected"),
    [
        # 使用者實際擷取的兩包（見 GAMEDATA [PKT-081]）
        (7, 0x3C, 0x016B510B, "3804 0700 3c00 0b516b01"),
        (3, 0x1D, 0x017C901C, "3804 0300 1d00 1c907c01"),
    ],
)
def test_use_skill_packet_matches_the_real_capture(level, skill_id, target, expected):
    assert build_use_skill(level, skill_id, target).hex() == expected.replace(" ", "")
