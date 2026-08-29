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


# ---- 幫隊友放（使用者指定：沒有或剩不到 50% 就補）--------------------------


class FakeMate:
    def __init__(self, aid: int, name: str = "",
                 cell: tuple[int, int] = (100, 100)) -> None:
        self.aid = aid
        self.name = name
        #: 隊友在哪一格（`0x0107`）。預設就站在我旁邊 —— 距離不是這些
        #: 測試要釘的東西，要釘距離的另外寫（見檔尾）。
        self.cell = cell
        self._has: dict[int, bool] = {}

    def label(self) -> str:
        return self.name or f"#{self.aid}"

    def has(self, efst: int, _now: float) -> bool:
        return self._has.get(efst, False)


class FakeParty:
    def __init__(self, mates) -> None:
        self._mates = list(mates)
        self.needed: dict[int, bool] = {}

    def mates(self):
        return list(self._mates)

    def needs(self, _mate, efst: int, _ratio: float) -> bool:
        return self.needed.get(efst, True)


MATE_AID = 24940572
INCAGI = 29          # AL_INCAGI「目標1個」→ 放得到別人
INCAGI_EFST = 12


#: 我站在哪（`FakeMate` 預設就站在同一格，也就是「在身邊」）。
MY_CELL = (100, 100)


def _party_keeper(fake, mates, me=MY_CELL):
    party = FakeParty(mates)
    keeper = BuffKeeper(fake.send, AID, fake.read, fake.now, party=party,
                        read_position=lambda: me)
    keeper.help_mates = True
    return keeper, party


def test_a_mate_who_needs_it_gets_it():
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])     # 自己已經有了
    mate = FakeMate(MATE_AID, "白狐")
    keeper, _party = _party_keeper(fake, [mate])
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    note = keeper.tick()
    assert fake.sent == [build_use_skill(10, INCAGI, MATE_AID)]
    assert "白狐" in note


def test_my_own_buffs_come_first():
    """自己倒了就沒人補得成 —— 自己的補完才輪到隊友。"""
    fake = Fake()                                       # 自己沒有
    keeper, _party = _party_keeper(fake, [FakeMate(MATE_AID)])
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    keeper.tick()
    assert fake.sent == [build_use_skill(10, INCAGI, AID)], "先補自己"


def test_skills_that_cannot_target_others_are_skipped():
    """「對象 : 自己」的技能放不到別人身上 —— 直接跳過，不要送。"""
    fake = Fake([FakeStatus(QUICKEN_EFST, 200_000)])    # 自己的快速劍還很久
    keeper, _party = _party_keeper(fake, [FakeMate(MATE_AID)])
    keeper.set_plans([BuffPlan(QUICKEN, 7)])            # KN_TWOHANDQUICKEN「自己」

    assert keeper.tick() is None
    assert fake.sent == []


def test_a_mate_who_already_has_it_is_left_alone():
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    mate = FakeMate(MATE_AID)
    keeper, party = _party_keeper(fake, [mate])
    party.needed[INCAGI_EFST] = False
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    assert keeper.tick() is None
    assert fake.sent == []


def test_helping_is_off_by_default():
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    party = FakeParty([FakeMate(MATE_AID)])
    keeper = BuffKeeper(fake.send, AID, fake.read, fake.now, party=party)
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    assert keeper.tick() is None, "沒勾就不該幫別人放"
    assert fake.sent == []


def test_no_sp_skips_mates_quietly_too(caplog):
    import logging

    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    keeper, _party = _party_keeper(fake, [FakeMate(MATE_AID)])
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    with caplog.at_level(logging.INFO):
        assert keeper.tick(sp=1) is None
    assert fake.sent == []
    assert caplog.text == ""


def test_the_mate_cast_is_confirmed_on_the_mate():
    """確認要看**他**身上有沒有，不是看自己的。"""
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    mate = FakeMate(MATE_AID, "白狐")
    keeper, _party = _party_keeper(fake, [mate])
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    keeper.tick()
    mate._has[INCAGI_EFST] = True
    fake.clock += 0.3
    assert "補上" in (keeper.tick() or "")
    assert keeper.stats.mate_cast == 1


def test_helping_a_mate_does_not_wait_for_my_own_confirmation():
    """等自己那一發確認的時候照樣可以幫隊友放。

    使用者回報「在白狐旁邊等 10 秒才放」—— 序列化沒有好處：目標不同、
    狀態也分開確認，硬排隊只會讓「幫隊友」慢上好幾秒。
    """
    fake = Fake()                                       # 自己什麼都沒有
    mate = FakeMate(MATE_AID, "白狐")
    keeper, _party = _party_keeper(fake, [mate])
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    keeper.tick()                                       # 先補自己
    assert fake.sent == [build_use_skill(10, INCAGI, AID)]

    fake.clock += MIN_GAP                               # 自己那發還沒確認
    keeper.tick()
    assert fake.sent[-1] == build_use_skill(10, INCAGI, MATE_AID), "不該等"


def test_several_mates_are_not_serialised():
    """三個隊友不該變成三倍的等待 —— 每個 (技能, 隊友) 各自獨立。"""
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])     # 自己已經有了
    mates = [FakeMate(100 + i) for i in range(3)]
    keeper, _party = _party_keeper(fake, mates)
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    for _ in range(3):
        keeper.tick()
        fake.clock += MIN_GAP + 0.01      # 浮點數：剛好等於門檻會擦邊
    targets = [int.from_bytes(p[6:10], "little") for p in fake.sent]
    assert sorted(targets) == [100, 101, 102]


def test_the_same_mate_is_not_spammed():
    """同一個 (技能, 隊友) 還在等結果就不要重送。"""
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    keeper, _party = _party_keeper(fake, [FakeMate(MATE_AID)])
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    keeper.tick()
    for _ in range(5):
        fake.clock += MIN_GAP
        keeper.tick()
    assert len(fake.sent) == 1


# ---- 太遠不放（使用者：「不然會無腦一直放」）--------------------------------


def test_a_mate_too_far_away_is_left_alone():
    """超過**技能自己的射程**就不放：伺服器只會安靜地丟掉，等於白放。

    ⚠ 判準是射程，不是一個寫死的格數 —— 使用者 2026-08-29 指定
    「隊友在我施放範圍就要馬上幫他放」。夾小的副作用是「明明放得到卻不放」。
    """
    from ro_toolbox.services.buffs import MATE_NEAR_CELLS

    reach = MATE_NEAR_CELLS
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])     # 自己已經有了
    far = FakeMate(MATE_AID, "白狐", cell=(MY_CELL[0] + reach + 1, MY_CELL[1]))
    keeper, _party = _party_keeper(fake, [far])
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    assert keeper.tick() is None
    assert fake.sent == []


def test_a_mate_right_on_the_line_still_gets_it():
    """剛好站在觸發距離上也要放。

    ⚠ 使用者 2026-08-30 改了規則：「隊友只要離我 **3 格內** 我就會完全停下來
    檢查幫她放」—— 所以觸發距離是 `MATE_NEAR_CELLS`，不是技能射程。
    （指定型技能仍然要在自己的射程內，見 `_reach_for`。）
    """
    from ro_toolbox.services.buffs import MATE_NEAR_CELLS

    reach = MATE_NEAR_CELLS
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    edge = FakeMate(MATE_AID, "白狐", cell=(MY_CELL[0] + reach, MY_CELL[1]))
    keeper, _party = _party_keeper(fake, [edge])
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    keeper.tick()
    assert fake.sent == [build_use_skill(10, INCAGI, MATE_AID)]


def test_distance_is_square_not_straight_line():
    """RO 的距離是正方形的（斜的一步也算一格），所以取兩軸差的最大值。"""
    from ro_toolbox.services.buffs import cells_between

    assert cells_between((10, 10), (13, 14)) == 4      # 直線距離是 5
    assert cells_between((10, 10), (10, 10)) == 0


def test_no_position_means_no_mate_buffs():
    """量不出距離就不放（安全退化）—— 不知道遠近而硬放就是無腦一直放。"""
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    party = FakeParty([FakeMate(MATE_AID, "白狐")])
    keeper = BuffKeeper(fake.send, AID, fake.read, fake.now, party=party,
                        read_position=lambda: None)
    keeper.help_mates = True
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    assert keeper.tick() is None
    assert fake.sent == []


def test_a_short_range_skill_uses_its_own_range():
    """門檻是**技能自己的射程**，`MATE_MAX_CELLS` 只在查不到射程時才用。

    塗毒（138）射程 1 —— 要貼著才放得到，5 格是放不到的。
    """
    from ro_toolbox.services.buffs import skill_range

    assert skill_range(138, 1) == 1, "塗毒射程 1"
    assert skill_range(INCAGI, 10) == 9, "加速術射程 9"

    from ro_toolbox.services.buffs import buff_efst

    fake = Fake([FakeStatus(buff_efst(138), 200_000)])   # 自己已經有了
    mate = FakeMate(MATE_AID, "白狐", cell=(MY_CELL[0] + 3, MY_CELL[1]))
    keeper, _party = _party_keeper(fake, [mate])
    keeper.set_plans([BuffPlan(138, 1)])
    keeper.tick()
    assert fake.sent == [], "射程 1 的技能，隔 3 格不准送"


# ---- 「自己和隊員」是範圍技，不是可以指定隊友 ------------------------------


def test_self_centred_party_skills_are_not_targeted_at_a_mate():
    """⚠ 遊戲說明的「對象」寫「自己和隊員」的是**以自己為中心的範圍技**。

    內容講得很清楚 —— 凶砍：「增加自己及**周圍**隊員的物理傷害」；
    天使之障壁：「提升自己和**畫面內**隊員的…」。送「目標＝隊友 GID」的
    `0x0438` 出去伺服器不會理，那個技能會一直重試然後安靜地什麼都沒發生。
    這類技能正確的用法是當成**自己的 buff** 勾起來，範圍效果自然罩到隊友。
    """
    from ro_toolbox.services.buffs import can_target_others

    assert not can_target_others(33), "天使之障壁（立即施展）"
    assert not can_target_others(111), "速度激發（自己和隊員）"
    assert not can_target_others(112), "無視體型攻擊（自己和隊員）"
    assert not can_target_others(113), "凶砍（自己和隊員）"
    assert not can_target_others(74), "聖母之頌歌（立即施展）"


def test_skills_that_name_one_person_are_still_targetable():
    from ro_toolbox.services.buffs import can_target_others

    assert can_target_others(29), "加速術（目標1個）"
    assert can_target_others(34), "天使之賜福（目標1個）"
    assert can_target_others(138), "塗毒（自己和隊友1名 —— 說明寫「指定目標」）"


# ---- 詠唱時要叫走路讓路 ----------------------------------------------------


def test_casting_for_a_mate_asks_everyone_else_to_hold_still():
    """使用者：「自動戰鬥時也要幫隊友放，並且是最高優先，高於打怪跟尋路」。

    ⚠ **移動與攻擊會打斷詠唱。** 實機日誌：封包送得出去、隊友也在旁邊，
    就是連續三次「沒上身」—— 被自己的走路封包打斷。
    """
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])     # 自己已經有了
    party = FakeParty([FakeMate(MATE_AID, "白狐")])
    holds = []
    keeper = BuffKeeper(fake.send, AID, fake.read, fake.now, party=party,
                        read_position=lambda: MY_CELL,
                        hold=holds.append, release=lambda: holds.append("release"))
    keeper.help_mates = True
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    keeper.tick()
    assert holds and holds[0] > 0, "送出去之前就要先叫大家別動"
    assert fake.sent, "還是要真的送出去"


def test_the_road_is_given_back_once_there_is_nothing_left_to_do():
    """使用者：「停下一切動作幫她把**需要的放完**再繼續」。

    所以放行的條件不是「這一發上身了」，是「沒有隊友還需要補了」——
    可能還有第二個 buff、第二個隊友。⚠ 但也不能傻傻一直等
    （`MATE_SESSION_MAX`），對方有可能只是經過。
    """
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    mate = FakeMate(MATE_AID, "白狐")
    party = FakeParty([mate])
    holds = []
    keeper = BuffKeeper(fake.send, AID, fake.read, fake.now, party=party,
                        read_position=lambda: MY_CELL,
                        hold=lambda _s: holds.append("hold"),
                        release=lambda: holds.append("release"))
    keeper.help_mates = True
    keeper.set_plans([BuffPlan(INCAGI, 10)])
    keeper.tick()

    mate._has[INCAGI_EFST] = True          # 隊友身上出現了
    party.needed[INCAGI_EFST] = False
    fake.clock += 0.5
    keeper.tick()          # 這一拍把那一發結算掉
    keeper.tick()          # 沒事做了 → 放行
    assert "release" in holds


def test_a_send_failure_gives_the_road_back_too():
    """送不出去就不要占著路，不然打怪白站 MAX_HOLD 秒。"""
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    fake.ok = False
    party = FakeParty([FakeMate(MATE_AID, "白狐")])
    holds = []
    keeper = BuffKeeper(fake.send, AID, fake.read, fake.now, party=party,
                        read_position=lambda: MY_CELL,
                        hold=lambda _s: holds.append("hold"),
                        release=lambda: holds.append("release"))
    keeper.help_mates = True
    keeper.set_plans([BuffPlan(INCAGI, 10)])
    keeper.tick()
    assert holds[-1] == "release"


def test_a_mate_buff_that_never_lands_backs_off():
    """⚠⚠ **沒有退避的話打怪會永遠不動。**

    幫隊友放會叫打怪讓路（`cast_lock`）。放不上去卻每 `MIN_GAP` 秒重試一次的話
    等於一直占著路 —— 打怪站在原地，45 秒之後還會被「毫無進展」判成卡住。
    放不上去的原因多半是短時間內好不了的（隊友換圖了、伺服器判定距離不夠）。
    """
    from ro_toolbox.services.buffs import BACKOFF_START, CONFIRM_TIMEOUT

    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])     # 自己已經有了
    keeper, _party = _party_keeper(fake, [FakeMate(MATE_AID, "白狐")])
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    keeper.tick()
    assert len(fake.sent) == 1

    fake.clock += CONFIRM_TIMEOUT + 0.1
    keeper.tick()                                  # 沒上身 → 開始退避
    assert len(fake.sent) == 1

    fake.clock += BACKOFF_START / 2
    keeper.tick()
    assert len(fake.sent) == 1, "退避時間還沒到，不准再送"

    fake.clock += BACKOFF_START
    keeper.tick()
    assert len(fake.sent) == 2, "退避時間到了要再試一次"


def test_a_successful_mate_buff_clears_the_backoff():
    """成功一次就把退避歸零 —— 下次他掉了要馬上補回去。"""
    from ro_toolbox.services.buffs import CONFIRM_TIMEOUT

    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    mate = FakeMate(MATE_AID, "白狐")
    keeper, party = _party_keeper(fake, [mate])
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    keeper.tick()
    fake.clock += CONFIRM_TIMEOUT + 0.1
    keeper.tick()                                  # 第一次沒上身
    assert keeper._mate_retry

    mate._has[INCAGI_EFST] = True
    party.needed[INCAGI_EFST] = False
    fake.clock += 60
    keeper.tick()
    keeper.tick()
    assert not keeper._mate_retry, "上身了就把退避清掉"


def test_a_mate_just_passing_through_does_not_stall_farming():
    """⚠ 使用者：「當然不要傻傻一直等，因為對方有可能只是經過」。

    停是整段停（放完為止），但整段有上限；時間到就放行，剩下的交給退避。
    """
    from ro_toolbox.services.buffs import MATE_SESSION_MAX

    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    mate = FakeMate(MATE_AID, "白狐")
    party = FakeParty([mate])
    holds = []
    keeper = BuffKeeper(fake.send, AID, fake.read, fake.now, party=party,
                        read_position=lambda: MY_CELL,
                        hold=lambda _s: holds.append("hold"),
                        release=lambda: holds.append("release"))
    keeper.help_mates = True
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    keeper.tick()
    assert holds and holds[0] == "hold"

    fake.clock += MATE_SESSION_MAX + 1
    keeper.tick()
    assert holds[-1] == "release", "停太久要放行，不能讓打怪一直站著"


def test_a_mate_who_walks_out_of_range_gives_the_road_back():
    """他走遠了就沒事做了 —— 馬上放行，別占著路。"""
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])
    mate = FakeMate(MATE_AID, "白狐")
    party = FakeParty([mate])
    holds = []
    keeper = BuffKeeper(fake.send, AID, fake.read, fake.now, party=party,
                        read_position=lambda: MY_CELL,
                        hold=lambda _s: holds.append("hold"),
                        release=lambda: holds.append("release"))
    keeper.help_mates = True
    keeper.set_plans([BuffPlan(INCAGI, 10)])
    keeper.tick()

    mate.cell = (MY_CELL[0] + 50, MY_CELL[1])     # 走掉了
    keeper._mate_pending.clear()
    keeper.tick()
    assert holds[-1] == "release"


# ---- 範圍型「幫隊友」技能：放自己身上，罩到旁邊的隊友 ----------------------


def test_an_aura_skill_counts_as_helping_mates():
    """⚠ 使用者兩次回報「天使之障壁放不出來」。

    它的「對象」寫「立即施展」，所以 `can_target_others()` 是 False ——
    舊版的「幫隊友放」整個跳過它。但它**內容**寫得很清楚：
    「提升自己和**畫面內隊員**的 VIT 物理防禦力及 MaxHP」。

    使用者：「以後會有更多這種技能，所以不能每次都壞掉」。
    """
    from ro_toolbox.services.buffs import (
        can_target_others,
        helps_mates,
        is_party_aura,
    )

    for skill_id, name in ((33, "天使之障壁"), (74, "聖母之頌歌"),
                           (75, "幸運之頌歌"), (66, "神威祈福")):
        assert is_party_aura(skill_id), name
        assert helps_mates(skill_id), name
        assert not can_target_others(skill_id), f"{name} 不能指定目標"

    for skill_id, name in ((8, "霸體"), (60, "雙手劍攻擊速度增加")):
        assert not is_party_aura(skill_id), f"{name} 是純自己的"
        assert not helps_mates(skill_id), name


def test_an_aura_skill_is_cast_on_myself_not_on_the_mate():
    """⚠ 目標填隊友的 GID 伺服器會直接丟掉（[DAT-045]）—— 要填自己。"""
    from ro_toolbox.services.buffs import cast_target_of

    assert cast_target_of(33, MATE_AID, AID) == AID, "天使之障壁放自己"
    assert cast_target_of(INCAGI, MATE_AID, AID) == MATE_AID, "加速術指定隊友"


def test_a_mate_missing_an_aura_buff_makes_me_cast_it_on_myself():
    """隊友身上沒有天使之障壁 → 我在自己身上放一次，他就吃到了。"""
    from ro_toolbox.services.buffs import buff_efst

    angelus = 33
    fake = Fake([FakeStatus(buff_efst(angelus), 200_000)])   # 我自己已經有了
    mate = FakeMate(MATE_AID, "白狐")
    keeper, _party = _party_keeper(fake, [mate])
    keeper.set_plans([BuffPlan(angelus, 10)])

    note = keeper.tick()
    assert fake.sent == [build_use_skill(10, angelus, AID)], "目標要是自己"
    assert "白狐" in (note or "")


def test_my_own_buff_also_makes_everyone_hold_still():
    """⚠⚠ 使用者兩次回報「天使之障壁還是沒放」—— 它其實一直在放，
    只是每一發都被自動打怪的走路封包打斷：

        「加速術」放了沒上身，1 秒後再試
        「天使之障壁」放了沒上身，2 秒後再試
        …退避到 30 秒…

    移動會打斷詠唱，所以讓路不能只為隊友，**自己的 buff 也要**。
    """
    fake = Fake()                       # 自己身上什麼都沒有
    holds = []
    keeper = BuffKeeper(fake.send, AID, fake.read, fake.now,
                        hold=lambda _s: holds.append("hold"),
                        release=lambda: holds.append("release"))
    keeper.set_plans([BuffPlan(INCAGI, 10)])

    keeper.tick()
    assert holds and holds[0] == "hold", "自己的 buff 也要叫大家別動"
    assert fake.sent, "還是要真的送出去"


def test_the_road_is_free_once_my_own_buffs_are_all_on():
    fake = Fake([FakeStatus(INCAGI_EFST, 200_000)])   # 已經有了
    holds = []
    keeper = BuffKeeper(fake.send, AID, fake.read, fake.now,
                        hold=lambda _s: holds.append("hold"),
                        release=lambda: holds.append("release"))
    keeper.set_plans([BuffPlan(INCAGI, 10)])
    keeper.tick()
    assert holds == [] or holds[-1] == "release"
    assert fake.sent == []
