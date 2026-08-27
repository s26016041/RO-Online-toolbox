"""跨地圖尋路的行為驗證（不需遊戲）。

重點不是「會不會走」，而是三個**不准安靜出錯**的規則：

1. 換圖成功只認地圖名變了，不靠時間；
2. 傳點踩不過去要列黑名單並重新規劃，不能傻等；
3. 規劃不出路線／地形讀不到，一律回 blocked，不能繼續亂走。
"""

from __future__ import annotations

import numpy as np
import pytest

from ro_toolbox.services import travel
from ro_toolbox.services.mapdata import GatError, MapTerrain
from ro_toolbox.services.travel import Hop, Traveler, nearest_walkable, plan_route

# ---- 假資料 ---------------------------------------------------------

#: 一條線：a → b → c，另有孤島 z。座標刻意不同，才看得出挑到哪一個傳點。
FAKE_WARPS = {
    "a": [(10, 10, "b", 90, 90), (20, 20, "dead_end", 5, 5)],
    "b": [(30, 30, "c", 70, 70), (31, 31, "a", 11, 11)],
    "c": [(40, 40, "b", 32, 32)],
    "dead_end": [(6, 6, "a", 21, 21)],
}


@pytest.fixture()
def fake_warps(monkeypatch):
    monkeypatch.setattr(travel, "warps_on_map", lambda m: FAKE_WARPS.get(m, []))
    return FAKE_WARPS


def open_terrain(name: str, side: int = 100) -> MapTerrain:
    """整張都可走的地圖（type 0 = 可走）。A* 用真的，路徑才有意義。"""
    return MapTerrain(name=name, width=side, height=side, types=np.zeros((side, side), np.uint32))


class FakeWalker:
    """只回報「呼叫端叫它走完了沒」，不模擬封包 —— Walker 自己有 test_walker.py。"""

    def __init__(self) -> None:
        self.paths: list[list[tuple[int, int]]] = []
        self.state = "idle"
        self.cleared = 0

    def clear(self) -> None:
        self.cleared += 1
        self.state = "idle"

    def set_path(self, cells) -> None:
        self.paths.append(list(cells))
        self.state = "walking"

    def update(self, pos):  # noqa: ANN001, ARG002
        return self.state


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(loader=None):
    walker = FakeWalker()
    clock = Clock()
    loader = loader or (lambda name: open_terrain(name))
    return Traveler(walker, clock, terrain_loader=loader), walker, clock


# ---- 路線規劃 -------------------------------------------------------


def test_same_map_needs_no_hops(fake_warps):
    assert plan_route("a", "a") == []


def test_route_is_fewest_map_changes(fake_warps):
    route = plan_route("a", "c")
    assert [h.to_map for h in route] == ["b", "c"]
    assert route[0] == Hop("a", 10, 10, "b", 90, 90)


def test_unreachable_map_returns_none(fake_warps):
    assert plan_route("a", "nowhere") is None


def test_avoided_warp_is_routed_around(fake_warps):
    """黑名單掉 a→b 之後就真的沒路了 —— 不准偷偷再用同一個傳點。"""
    assert plan_route("a", "c", avoid={("a", 10, 10)}) is None


# ---- 目標格修正 -----------------------------------------------------


def test_nearest_walkable_returns_cell_itself_when_open():
    terrain = open_terrain("x")
    assert nearest_walkable(terrain, (50, 50)) == (50, 50)


def test_nearest_walkable_steps_out_to_find_ground():
    terrain = open_terrain("x")
    terrain.types[49:52, 49:52] = 1  # 目標那一小塊都不可走
    found = nearest_walkable(terrain, (50, 50))
    assert found is not None
    assert terrain.is_walkable(*found)
    assert max(abs(found[0] - 50), abs(found[1] - 50)) == 2


def test_nearest_walkable_gives_up_instead_of_guessing():
    terrain = open_terrain("x")
    terrain.types[:, :] = 1
    assert nearest_walkable(terrain, (50, 50), radius=3) is None


# ---- Traveler 狀態機 ------------------------------------------------


def test_walks_towards_the_next_warp(fake_warps):
    traveler, walker, _clock = make()
    traveler.set_goal("c")
    assert traveler.update("a", (5, 5)) == "walking"
    # 第一段的目標是 a 上通往 b 的傳點 (10,10)
    assert walker.paths[-1][-1] == (10, 10)
    assert [h.to_map for h in traveler.route] == ["b", "c"]


def test_map_change_is_the_only_signal_that_advances_the_route(fake_warps):
    """時間過再久都不會前進；地圖名一變才重新規劃。"""
    traveler, walker, clock = make()
    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    walker.state = "arrived"
    for _ in range(5):
        clock.now += 10.0
        traveler.update("a", (10, 10))
    assert [h.to_map for h in traveler.route] == ["b", "c"]  # 還在 a，沒有偷偷前進

    walker.state = "idle"
    assert traveler.update("b", (90, 90)) == "walking"
    assert [h.to_map for h in traveler.route] == ["c"]
    assert walker.paths[-1][-1] == (30, 30)  # b 上通往 c 的傳點


def test_arriving_on_goal_map_reports_arrived(fake_warps):
    traveler, _walker, _clock = make()
    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    assert traveler.update("c", (70, 70)) == "arrived"
    assert not traveler.active


def test_goal_cell_keeps_walking_after_reaching_the_map(fake_warps):
    """有指定座標時，踏進目的地圖還不算到 —— 要繼續走到那一格。"""
    traveler, walker, _clock = make()
    traveler.set_goal("c", (12, 34))
    traveler.update("a", (5, 5))
    assert traveler.update("c", (70, 70)) == "walking"
    assert walker.paths[-1][-1] == (12, 34)
    walker.state = "arrived"
    assert traveler.update("c", (12, 34)) == "arrived"


def test_dead_warp_is_blacklisted_then_route_fails_loudly(fake_warps):
    """踩不過去的傳點要列黑名單；沒有替代路線就 blocked，不准繼續磨。"""
    traveler, walker, clock = make()
    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    walker.state = "arrived"
    traveler.update("a", (10, 10))  # 走到傳點了，開始踩

    clock.now += travel.WARP_GIVEUP_SEC + 1
    assert traveler.update("a", (10, 10)) == "blocked"
    assert "過不去" in traveler.note or "找不到" in traveler.note


def test_unknown_destination_is_blocked_not_wandering(fake_warps):
    traveler, walker, _clock = make()
    traveler.set_goal("nowhere")
    assert traveler.update("a", (5, 5)) == "blocked"
    assert "找不到" in traveler.note
    assert walker.paths == []


def test_missing_terrain_is_blocked_not_guessed(fake_warps):
    def boom(name: str):
        raise GatError(f"找不到地形檔：{name}")

    traveler, walker, _clock = make(loader=boom)
    traveler.set_goal("c")
    assert traveler.update("a", (5, 5)) == "blocked"
    assert "地形" in traveler.note
    assert walker.paths == []


def test_stale_position_after_map_change_waits_instead_of_failing(fake_warps):
    """[MEM-022]：換圖後座標會停在上一張圖。那個值合法但不在這張圖上 ——
    不准拿去算 A*（會得到「走不到」），要等它更新。"""
    traveler, walker, clock = make(loader=lambda name: open_terrain(name, side=60))
    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    walker.paths.clear()

    # 進到 b，但座標還是 a 上的 (90, 90) —— b 只有 60x60，根本不在圖上
    assert traveler.update("b", (90, 90)) == "walking"
    assert walker.paths == []  # 一步都不准規劃
    assert "等座標" in traveler.note

    # 座標更新之後才開始走
    assert traveler.update("b", (10, 10)) == "walking"
    assert walker.paths[-1][-1] == (30, 30)
    # 實測踩過：座標追上來了，狀態文字卻一路掛著「等座標更新」39 秒 ——
    # 人在走、UI 說在等，那是另一種安靜的錯。
    assert "等座標" not in traveler.note
    assert "還要換 1 張圖" in traveler.note


def test_position_that_never_lands_on_this_map_fails_loudly(fake_warps):
    traveler, walker, clock = make(loader=lambda name: open_terrain(name, side=60))
    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    assert traveler.update("b", (90, 90)) == "walking"
    clock.now += travel.STALE_POS_SEC + 1
    assert traveler.update("b", (90, 90)) == "blocked"
    assert "仍不在這張圖上" in traveler.note


def test_standing_on_unwalkable_cell_snaps_instead_of_giving_up(fake_warps):
    """站在 gat 說不可走的格子上（type 5 語意未確認）不代表走不了，往旁邊挪一格。"""
    terrain = open_terrain("a")
    terrain.types[4:7, 4:7] = 1
    traveler, walker, _clock = make(loader=lambda name: terrain)
    traveler.set_goal("c")
    assert traveler.update("a", (5, 5)) == "walking"
    assert walker.paths[-1][-1] == (10, 10)


def test_wrong_warp_self_heals_by_replanning_from_where_we_are(fake_warps):
    """走錯傳點掉到 dead_end：不當機、不硬走，從那裡重新規劃回去。"""
    traveler, walker, _clock = make()
    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    assert traveler.update("dead_end", (50, 50)) == "walking"
    assert [h.to_map for h in traveler.route] == ["a", "b", "c"]
    assert walker.paths[-1][-1] == (6, 6)  # dead_end 上回 a 的傳點


# ---- 要跟 NPC 講話才過得去的連結 ------------------------------------------


def test_npc_links_are_not_walkable_routes(monkeypatch):
    """⚠ NPC 連結**不准**放進 plan_route。走到那一格什麼都不會發生 ——
    放進去的話 bot 會走過去然後站在那裡等到天荒地老。"""
    from ro_toolbox.services import travel as mod

    monkeypatch.setattr(mod, "warps_on_map", lambda _m: [])
    monkeypatch.setattr(
        mod, "npc_links_on_map",
        lambda m: [(108, 27, "izlude", 195, 210, "船員", 100)] if m == "izlu2dun" else [],
    )
    assert mod.plan_route("izlu2dun", "izlude") is None


def test_why_no_route_names_the_npc(monkeypatch):
    """算不出路的時候要講得出**卡在哪個 NPC** —— 不然使用者看到的是
    「遊戲裡箭頭好好的，你卻說找不到」（實測回報）。"""
    from ro_toolbox.services import travel as mod

    monkeypatch.setattr(mod, "warps_on_map", lambda _m: [])
    monkeypatch.setattr(
        mod, "npc_links_on_map",
        lambda m: [(108, 27, "izlude", 195, 210, "船員", 100)] if m == "izlu2dun" else [],
    )
    assert mod.why_no_route("izlu2dun", "izlude") == (
        "izlu2dun", 108, 27, "izlude", "船員"
    )
    note = mod._no_route_note("izlu2dun", "izlude")
    assert "船員" in note and "izlu2dun (108,27)" in note


def test_why_no_route_returns_none_when_there_is_really_no_way(monkeypatch):
    """就算把 NPC 連結也算進去還是到不了 —— 那才是真的沒路，別亂指人。"""
    from ro_toolbox.services import travel as mod

    monkeypatch.setattr(mod, "warps_on_map", lambda _m: [])
    monkeypatch.setattr(mod, "npc_links_on_map", lambda _m: [])
    assert mod.why_no_route("izlu2dun", "geffen") is None
    assert "找不到通往 geffen 的路" in mod._no_route_note("izlu2dun", "geffen")


def test_the_shipped_table_has_both_kinds():
    """實際打包的資產要真的分成兩種 —— 舊版只收型別 200，丟掉 883 條（約 20%），
    症狀就是島嶼／地城這種靠船進出的地圖算不出任何路線。"""
    from ro_toolbox.services.gamedata import npc_links_on_map, warps_on_map

    assert warps_on_map("izlu2dun") == [(108, 83, "iz_dun00", 168, 168)]
    npc = npc_links_on_map("izlu2dun")
    assert npc and npc[0][2] == "izlude", f"應該有一條搭船回 izlude，實際 {npc}"
    assert npc[0][5], "NPC 名字要留著，訊息要講得出去找誰"


def test_izlu2dun_to_geffen_explains_the_boat():
    """使用者實際踩到的那一條：人在拜倫島，要去 geffen。"""
    from ro_toolbox.services import travel as mod

    assert mod.plan_route("izlu2dun", "geffen") is None, "船那段不該自動走"
    note = mod._no_route_note("izlu2dun", "geffen")
    assert "船員" in note and "izlude" in note


# ---- 停在 NPC 前面等人手動做完 --------------------------------------------


def _npc_world(monkeypatch):
    """izlu2dun 只能靠「船員」回 izlude，izlude 走一個傳點到 geffen。"""
    from ro_toolbox.services import travel as mod

    walk = {"izlude": [(50, 50, "geffen", 100, 100)]}
    npc = {"izlu2dun": [(108, 27, "izlude", 195, 210, "船員", 100)]}
    monkeypatch.setattr(mod, "warps_on_map", lambda m: walk.get(m, []))
    monkeypatch.setattr(mod, "npc_links_on_map", lambda m: npc.get(m, []))
    return mod


def test_walking_route_wins_over_the_npc_one(monkeypatch):
    """⚠ 走得到就別麻煩人。BFS 只看換圖次數，不擋的話它會為了少換一張圖
    就叫你去搭船（甚至去付錢搭卡普拉）。"""
    from ro_toolbox.services import travel as mod

    monkeypatch.setattr(mod, "warps_on_map",
                        lambda m: [(9, 9, "izlude", 1, 1)] if m == "izlu2dun" else [])
    monkeypatch.setattr(mod, "npc_links_on_map",
                        lambda m: [(108, 27, "izlude", 195, 210, "船員", 100)]
                        if m == "izlu2dun" else [])
    route = mod.plan_route("izlu2dun", "izlude")
    assert route and route[0].npc == "", "純走路走得到就不該挑 NPC 那條"


def test_npc_hops_carry_the_name(monkeypatch):
    mod = _npc_world(monkeypatch)
    route = mod.plan_route("izlu2dun", "geffen", allow_npc=True)
    assert [h.npc for h in route] == ["船員", ""]


def _npc_traveler(goal):
    """全可走的地形。要 200x200 —— NPC 在 (108,27)，100x100 裝不下會被夾到邊界。"""
    traveler, walker, clock = make(loader=lambda name: open_terrain(name, side=200))
    traveler.set_goal(goal)
    return traveler, walker, clock


def test_traveler_stops_and_waits_at_the_npc(monkeypatch):
    """走到 NPC 面前就**停住等人**，而且不再送走路封包 ——
    人在跟 NPC 對話時被拉著走，選單會被打斷。"""
    _npc_world(monkeypatch)
    t, walker, _clock = _npc_traveler("geffen")
    assert t.update("izlu2dun", (5, 5)) == "walking"      # 先規劃、往 NPC 走
    assert walker.paths[-1][-1] == (108, 27), "第一段的目標就是 NPC 那一格"
    walker.state = "arrived"
    assert t.update("izlu2dun", (108, 27)) == "waiting"
    assert "船員" in t.note and "izlude" in t.note
    assert walker.state == "idle", "等待期間不准還有走路路徑"


def test_the_map_changing_is_the_only_resume_signal(monkeypatch):
    """⚠ 不是等幾秒，是**地圖真的變成那一張**（記憶體讀得到）才繼續。"""
    _npc_world(monkeypatch)
    t, walker, clock = _npc_traveler("geffen")
    t.update("izlu2dun", (5, 5))
    walker.state = "arrived"
    assert t.update("izlu2dun", (108, 27)) == "waiting"
    # 時間過去但地圖沒變 → 還是等，不准自己往下走
    clock.now += 300.0
    assert t.update("izlu2dun", (108, 27)) == "waiting"
    # 地圖變了 → 自動重新規劃並繼續
    walker.state = "idle"
    assert t.update("izlude", (60, 60)) in {"walking", "arrived"}
    assert t._npc_wait is None


def test_waiting_gives_up_loudly_not_silently(monkeypatch):
    """逾時只能當放棄的上限，不能當成功的依據 —— 逾時要停，不准假裝過去了。"""
    _npc_world(monkeypatch)
    t, walker, clock = _npc_traveler("geffen")
    t.update("izlu2dun", (5, 5))
    walker.state = "arrived"
    assert t.update("izlu2dun", (108, 27)) == "waiting"
    clock.now += 10_000.0
    assert t.update("izlu2dun", (108, 27)) == "blocked"
    assert "已停止" in t.note

