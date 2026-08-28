"""自動補 buff：查表對狀態、SP 不夠安靜等、沒上身就退避（不需要遊戲）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from ro_toolbox.core.ro_protocol import build_use_skill
from ro_toolbox.services.buffs import (
    BACKOFF_START,
    CONFIRM_TIMEOUT,
    MIN_GAP,
    REFRESH_BELOW_MS,
    BuffKeeper,
    BuffPlan,
    buff_efst,
    sp_cost,
)

AID = 0x016B510B
QUICKEN = 60          # KN_TWOHANDQUICKEN → EFST 2（實機驗證過）
QUICKEN_EFST = 2
ENDURE = 8            # SM_ENDURE → EFST 1（實機驗證過）
ENDURE_EFST = 1
VENDING = 41          # MC_VENDING「露天商店」—— 不上狀態，查不到 EFST


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

    def keeper(self) -> BuffKeeper:
        return BuffKeeper(self.send, AID, self.read, self.now)


# ---- 對照表（靜態資料，不需要遊戲也不需要學）-------------------------------


def test_skill_to_status_mapping_matches_the_live_capture():
    """實機封包 0x0983 說：霸體 → 狀態 1、雙手劍攻擊速度增加 → 狀態 2。"""
    assert buff_efst(ENDURE) == ENDURE_EFST
    assert buff_efst(QUICKEN) == QUICKEN_EFST


def test_skills_that_give_no_status_have_no_mapping():
    """「露天商店」不上狀態 —— 查不到才是對的（那種不該進自動補的清單）。"""
    assert buff_efst(VENDING) is None
    assert buff_efst(5) is None            # SM_BASH 狂擊，攻擊技能


def test_sp_cost_comes_from_the_game_table():
    """雙手劍攻擊速度增加 Lv7 要 38 SP —— 記憶體、封包、lub 三方一致。"""
    assert sp_cost(QUICKEN, 7) == 38
    assert sp_cost(QUICKEN, 1) == 14
    assert sp_cost(QUICKEN, 99) is None


# ---- 施放 -----------------------------------------------------------------


def test_casts_a_buff_that_is_missing():
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()
    assert fake.sent == [build_use_skill(7, QUICKEN, AID)]


def test_does_not_recast_while_the_buff_is_still_long():
    fake = Fake([FakeStatus(QUICKEN_EFST, 200_000)])
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()
    assert fake.sent == []


def test_recasts_when_it_is_about_to_run_out():
    fake = Fake([FakeStatus(QUICKEN_EFST, REFRESH_BELOW_MS - 1)])
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()
    assert len(fake.sent) == 1


def test_confirms_by_reading_the_status_not_by_waiting():
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()
    fake.statuses = [FakeStatus(QUICKEN_EFST)]
    fake.clock += 0.3
    assert "補上了" in (keeper.tick() or "")
    assert keeper.stats.cast == 1


def test_permanent_status_is_not_refreshed():
    fake = Fake([FakeStatus(QUICKEN_EFST, None)])
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick()
    assert fake.sent == []


def test_one_cast_per_tick():
    """一拍只做一件事 —— 整排 buff 一起噴出去的話伺服器只會理第一個。"""
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7), BuffPlan(ENDURE, 8)])

    keeper.tick()
    keeper.tick()
    assert len(fake.sent) == 1


def test_unreadable_status_does_nothing():
    """「問不出來」不等於「身上沒有」—— 混在一起會對著還在的 buff 一直重放。"""
    fake = Fake()
    fake.statuses = None
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    assert "讀不到" in (keeper.tick() or "")
    assert fake.sent == []


# ---- SP 不夠：安靜等，不是失敗 ---------------------------------------------


def test_not_enough_sp_waits_quietly(caplog):
    """SP 不夠是暫時狀態，等回滿了自然補得上 —— 不放、不記失敗、不跳訊息。"""
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])         # 要 38 SP

    with caplog.at_level(logging.INFO):
        assert keeper.tick(sp=37) is None
    assert fake.sent == []
    assert keeper.stats.waiting_sp == 1
    assert keeper.stats.note == "", "SP 不夠不該在介面上跳任何字"
    assert caplog.text == "", "SP 不夠不該寫 INFO 以上的日誌"


def test_casts_once_sp_comes_back():
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    keeper.tick(sp=10)
    assert fake.sent == []
    fake.clock += MIN_GAP
    keeper.tick(sp=38)
    assert len(fake.sent) == 1


def test_a_lower_level_costs_less_and_still_goes_out():
    """調低等級就是為了省 SP —— Lv1 只要 14。"""
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 1)])

    keeper.tick(sp=14)
    assert fake.sent == [build_use_skill(1, QUICKEN, AID)]


# ---- 沒上身：退避重試，不是停用 --------------------------------------------


def test_backs_off_instead_of_spamming(caplog):
    """交戰中詠唱被打斷很常見 —— 退避重試就好，不要停用也不要每拍重送。"""
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    with caplog.at_level(logging.INFO):
        keeper.tick()                               # 送出
        fake.clock += CONFIRM_TIMEOUT + 0.1
        keeper.tick()                               # 逾時 → 退避
    assert len(fake.sent) == 1
    assert "再試" in caplog.text

    fake.clock += BACKOFF_START / 2                 # 還在退避裡
    keeper.tick()
    assert len(fake.sent) == 1

    fake.clock += BACKOFF_START                     # 退避過了
    keeper.tick()
    assert len(fake.sent) == 2, "退避結束後要再試，不能永久放棄"


def test_backoff_grows():
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])

    waits = []
    for _ in range(3):
        keeper.tick()
        fake.clock += CONFIRM_TIMEOUT + 0.1
        note = keeper.tick() or ""
        waits.append(note)
        fake.clock += 100                            # 直接跳過退避
    assert "1 秒" in waits[0] and "2 秒" in waits[1] and "4 秒" in waits[2]


def test_unchecking_and_rechecking_clears_the_backoff():
    fake = Fake()
    keeper = fake.keeper()
    keeper.set_plans([BuffPlan(QUICKEN, 7)])
    keeper.tick()
    fake.clock += CONFIRM_TIMEOUT + 0.1
    keeper.tick()

    keeper.set_plans([])                             # 取消勾選
    keeper.set_plans([BuffPlan(QUICKEN, 7)])         # 再勾回來 = 再試一次
    fake.clock += MIN_GAP
    keeper.tick()
    assert len(fake.sent) == 2


# ---- 補不了的技能 ----------------------------------------------------------


def test_skills_without_a_status_are_refused_loudly_once(caplog):
    """勾了「露天商店」這種不上狀態的技能 —— 說一次，然後就不要碰它。"""
    fake = Fake()
    keeper = fake.keeper()

    with caplog.at_level(logging.WARNING):
        keeper.set_plans([BuffPlan(VENDING, 8)])
    assert keeper.stats.unusable == {VENDING}
    assert "沒辦法確認" in caplog.text

    keeper.tick()
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
