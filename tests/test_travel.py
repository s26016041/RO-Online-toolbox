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
        #: 每一段被交代「這些格不准經過」的集合（尋路用它繞開別的傳點）
        self.avoids: list[frozenset[tuple[int, int]]] = []
        #: 直接精準踩上去的那些格（踩傳點用 `step_onto`，見 [DAT-077]）
        self.steps: list[tuple[int, int]] = []
        self.state = "idle"
        self.cleared = 0

    def step_onto(self, x, y) -> None:  # noqa: ANN001
        self.steps.append((x, y))
        self.state = "idle"           # 送完就沒有路徑了，跟真的 Walker 一樣

    def clear(self) -> None:
        self.cleared += 1
        self.state = "idle"

    def set_path(self, cells, avoid=None) -> None:
        self.paths.append(list(cells))
        self.avoids.append(frozenset(avoid or ()))
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


def test_map_hop_distances_are_fewest_map_changes(fake_warps):
    """★ 「設定藥水商人」的城鎮照這個距離由近排到遠（使用者 2026-09-05）。

    一次 BFS 要同時給出每一張圖的距離；到不了的不放進結果。
    """
    from ro_toolbox.services.travel import map_hop_distances

    d = map_hop_distances("a", {"a", "b", "c", "dead_end", "z"}, allow_npc=False)
    assert d["a"] == 0, "站著這張圖就是 0（最近）"
    assert d["b"] == 1 and d["dead_end"] == 1, "隔一道門是 1"
    assert d["c"] == 2, "a→b→c 是 2"
    assert "z" not in d, "到不了的（孤島）不放進結果"


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


# ---- 一張圖裡好幾間互不相連的房間（主城的商店） ----------------------------


def test_picks_the_gate_it_can_actually_reach(monkeypatch):
    """室內圖是**一張地圖裡好幾間互不相連的店**（`prt_in` 實測 26 個區塊、
    22 道各自獨立通往 prontera 的門）。`plan_route` 是 BFS，挑到哪一道門
    完全是任意的 —— 挑到別間的門，A* 回「走不到」，整段被判失敗、
    傳點列黑名單，下一拍再挑到另一道也走不到的門，磨到上限才放棄。
    症狀就是使用者回報的「主城商店裡面沒辦法尋路」。"""
    from ro_toolbox.services import travel as mod

    terrain = open_terrain("shop")
    terrain.types[:, 20] = 1  # 一道整片的牆，把地圖切成左右兩間房
    monkeypatch.setattr(
        mod, "warps_on_map",
        lambda m: [(5, 5, "b", 1, 1), (35, 35, "b", 2, 2)] if m == "shop" else [],
    )
    traveler, walker, _clock = make(loader=lambda _name: terrain)
    traveler.set_goal("b")

    # 人在右邊那間。BFS 先挑到的是左邊 (5,5) 那道門，但那道走不到。
    assert traveler.update("shop", (30, 30)) == "walking"
    assert walker.paths[-1][-1] == (35, 35)
    assert traveler.route[0].cell == (35, 35)


def test_no_reachable_gate_fails_loudly(monkeypatch):
    """一道門都走不到就要大聲停下 —— 不准站在原地磨到重規劃上限。"""
    from ro_toolbox.services import travel as mod

    terrain = open_terrain("shop")
    terrain.types[:, 20] = 1
    monkeypatch.setattr(
        mod, "warps_on_map",
        lambda m: [(5, 5, "b", 1, 1)] if m == "shop" else [],
    )
    traveler, walker, _clock = make(loader=lambda _name: terrain)
    traveler.set_goal("b")
    assert traveler.update("shop", (30, 30)) == "blocked"
    assert "走不到" in traveler.note
    assert walker.paths == []


def test_arriving_on_a_multi_room_map_says_so(monkeypatch):
    """遊戲的尋路目標只給**地圖名**，但主城室內圖是一張圖裡好幾間店。
    我們只保證進得了這張圖 —— 那就講清楚，不要安靜地宣告成功。"""
    from ro_toolbox.services import travel as mod

    terrain = open_terrain("prt_in")
    terrain.types[:, 20] = 1  # 一道整片的牆，把地圖切成左右兩間房
    monkeypatch.setattr(mod, "warps_on_map", lambda _m: [])
    # 三個入口，兩個落在左邊那間 —— 我站在右邊，所以「過半的入口在別間」
    monkeypatch.setattr(mod, "warp_landings_on", lambda _m: ((5, 5), (8, 8), (35, 35)))
    traveler, _walker, _clock = make(loader=lambda _name: terrain)
    traveler.set_goal("prt_in")
    assert traveler.update("prt_in", (30, 30)) == "arrived"
    assert "走不過去" in traveler.note


def test_a_stray_pocket_does_not_cry_wolf(monkeypatch):
    """野外圖角落常有一兩個走不進去的小口袋（實測 prt_fild08 是 6 個入口裡有 1 個）。
    那種每次都跳警告的話，警告就等於沒有了。"""
    from ro_toolbox.services import travel as mod

    terrain = open_terrain("prt_fild08")
    terrain.types[:, 20] = 1
    monkeypatch.setattr(mod, "warps_on_map", lambda _m: [])
    monkeypatch.setattr(
        mod, "warp_landings_on", lambda _m: ((5, 5), (30, 30), (40, 40), (60, 60)))
    traveler, _walker, _clock = make(loader=lambda _name: terrain)
    traveler.set_goal("prt_fild08")
    assert traveler.update("prt_fild08", (30, 30)) == "arrived"
    assert traveler.note == "已抵達 prt_fild08"


# ---- 狀態要寫進執行日誌 ----------------------------------------------------


def test_planning_says_what_it_is_doing(fake_warps, caplog):
    """按下按鈕到走出第一步中間有一段空白。那段沒有任何字，看起來就像沒反應。"""
    import logging

    traveler, _walker, _clock = make()
    traveler.set_goal("c")
    with caplog.at_level(logging.INFO, logger="ro_toolbox.services.travel"):
        traveler.update("a", (5, 5))
    assert "正在計算" in caplog.text
    assert "路線算好了" in caplog.text


def test_travel_bot_logs_the_traveler_progress(caplog):
    """`Traveler` 一路算出來的狀態要真的進執行日誌。

    舊版是直接指派給 `stats.note`，而介面**刻意不顯示** `note`
    （提示字一律走日誌）—— 結果整段趕路過程在日誌裡是全白的，
    使用者只看得到「前往 X」跟「已抵達 X」，中間幾十秒完全沒有字。
    """
    import logging

    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234, destination="prontera")

    class FakeStatus:
        hp = 100
        map_name = "prt_fild08"

    class FakeReader:
        def read(self):
            return FakeStatus()

        def read_position(self):
            return (50, 50)

    class FakeTraveler:
        note = "前往 prontera：還要換 2 張圖"
        route: list = []
        terrain = None

        def __init__(self, stop) -> None:
            self._stop = stop

        def update(self, *_args):
            self._stop.set()   # 只跑一拍
            return "walking"

    bot._reader = FakeReader()
    bot._traveler = FakeTraveler(bot._stop)
    bot._keep_in_sync = lambda _now: True
    with caplog.at_level(logging.INFO, logger="ro_toolbox.services.travel_bot"):
        bot._loop()
    assert "還要換 2 張圖" in caplog.text


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



# ---- 認不出 NPC 時：講清楚要人做什麼 -------------------------------------


class FakeReader:
    def __init__(self, pos):
        self.pos = pos

    def read_position(self):
        return self.pos


def test_it_says_which_map_to_choose(caplog):
    """⚠ 認不出來就要**講出要選哪張地圖** —— 只說「請自己講話」沒有用。"""
    import logging

    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234)
    hop = Hop("izlu2dun", 108, 27, "izlude", 195, 210, "船員", 100)
    with caplog.at_level(logging.WARNING, logger="ro_toolbox.services.travel_bot"):
        bot._ask_for_help(hop)
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "船員" in said
    assert "依斯魯得島" in said, "要講出目的地的中文名，不是地圖代碼"


def test_it_only_asks_once_per_leg(caplog):
    """每拍都會呼叫 —— 講一次就好，不要洗版。"""
    import logging

    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234)
    hop = Hop("izlu2dun", 108, 27, "izlude", 195, 210, "船員", 100)
    with caplog.at_level(logging.WARNING, logger="ro_toolbox.services.travel_bot"):
        for _ in range(5):
            bot._ask_for_help(hop)
    assert len(caplog.records) == 1


def test_a_different_leg_asks_again(caplog):
    import logging

    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234)
    with caplog.at_level(logging.WARNING, logger="ro_toolbox.services.travel_bot"):
        bot._ask_for_help(Hop("izlu2dun", 108, 27, "izlude", 1, 1, "船員", 100))
        bot._ask_for_help(Hop("izlude", 128, 148, "geffen", 1, 1, "卡普拉職員", 117))
    assert len(caplog.records) == 2


# ---- 送封包的那條路真的存在嗎 --------------------------------------------


def test_the_bot_can_send_any_packet_not_just_moves():
    """⚠ 實測炸過：`_run_dialog` 呼叫 `self._send(data)`，但 TravelBot 只有
    `_send_move` —— 走到卡普拉旁邊時整條執行緒 AttributeError 掛掉，人卡在原地。

    這條測的不是「送得出去」（那要遊戲），是**那個方法存在而且吃 bytes**。
    """
    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234)
    assert hasattr(bot, "_send"), "對話封包沒有出口"
    assert bot._send(b"\x90\x00\x01\x00\x00\x00\x01") is False, "沒有 socket 要安全回 False"
    bot._send_move(10, 20)          # 不該炸


def test_every_packet_the_dialog_makes_can_be_sent(monkeypatch):
    """把對話會產生的每一種封包都餵一次，確認送出那條路吃得下。"""
    from ro_toolbox.services import game_link
    from ro_toolbox.services import npc_dialog as nd
    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234)
    sent = []
    bot._sock = 42
    monkeypatch.setattr(
        game_link.game_socket, "send_on_socket",
        lambda _sock, data: (sent.append(data), len(data))[1],
    )
    for data in (nd.build_contact(91), nd.build_next(91), nd.build_choose(91, 1)):
        assert bot._send(data) is True
    assert len(sent) == 3


def test_the_whole_dialog_step_runs_without_exploding(monkeypatch):
    """⚠ 這條擋的是實測炸掉的那種：`_run_dialog` 用到不存在的屬性。

    把整個「停在 NPC 前面」那一步真的跑一遍（socket、記憶體全是假的），
    任何 AttributeError 都會在這裡炸出來，而不是在使用者的角色卡在卡普拉旁邊時。
    """
    from ro_toolbox.services import game_link
    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234)
    bot._sock = 42
    monkeypatch.setattr(game_link.game_socket, "send_on_socket", lambda *a: 8)
    bot._reader = FakeReader((109, 27))
    bot._traveler._terrain = open_terrain("t", side=200)
    bot._traveler._route_map = "t"
    hop = Hop("t", 108, 27, "izlude", 1, 1, "船員", 100)
    bot._traveler._npc_wait = hop
    bot._traveler._route = [hop]

    bot._watch_next_npc()          # 開始盯那隻 NPC
    bot._run_dialog()              # 還認不出來 → 走遠再走回來
    bot._npc_gid = 91              # 假裝收到他的實體封包
    bot._run_dialog()              # 這一步會真的組對話封包送出去
    assert bot._talk is not None, "應該已經開始對話"


def test_an_entity_packet_teaches_us_the_npc_gid(monkeypatch):
    """實體封包進來要真的認出 NPC —— 這是整條自動對話的入口。"""
    from ro_toolbox.services import travel_bot as mod

    class Pkt:
        outbound = False
        opcode = 0x09FF

        def __init__(self, payload):
            self.payload = payload

    bot = mod.TravelBot(1234)
    hop = Hop("t", 108, 27, "izlude", 1, 1, "船員", 100)
    bot._traveler._route = [hop]
    bot._watch_next_npc()

    payload = bytearray(70)
    payload[0:2] = (70).to_bytes(2, "little")
    payload[mod._ENT_OBJTYPE] = mod._OBJTYPE_NPC
    payload[mod._ENT_GID:mod._ENT_GID + 4] = (91).to_bytes(4, "little")
    payload[mod._ENT_CLASS:mod._ENT_CLASS + 2] = (100).to_bytes(2, "little")
    x, y = 108, 27
    payload[mod._ENT_POS:mod._ENT_POS + 3] = bytes([
        x >> 2, ((x & 0x03) << 6) | ((y >> 4) & 0x3F), (y & 0x0F) << 4,
    ])
    bot._on_packet(Pkt(bytes(payload)))
    assert bot._npc_gid == 91

    # 別人（其他玩家 objtype 0）不能被當成那隻 NPC
    other = bytearray(payload)
    other[mod._ENT_OBJTYPE] = 0
    other[mod._ENT_GID:mod._ENT_GID + 4] = (777).to_bytes(4, "little")
    bot._npc_gid = None
    bot._on_packet(Pkt(bytes(other)))
    assert bot._npc_gid is None, "objtype 不對就不能認"


def test_the_shake_never_sends_a_move_the_server_will_ignore(monkeypatch):
    """⚠ 單次移動超過 17 格伺服器直接忽略（[PKT-030]）。

    實測踩過：`_shake_view` 自己送一個 22 格的走路封包 → 石沉大海 →
    人站在卡普拉旁邊發呆，**一個錯誤訊息都沒有**。所以一定要走 Walker，
    它會把路徑切成 14 格一段。
    """
    from ro_toolbox.services import game_link
    from ro_toolbox.services import travel_bot as mod
    from ro_toolbox.services.walker import MAX_STEP

    bot = mod.TravelBot(1234)
    bot._sock = 42
    bot._reader = FakeReader((109, 27))
    bot._traveler._terrain = open_terrain("t", side=200)
    # ⚠ 要攔在**真正送出去**的那一層：Walker 建構時就抓走了 _send_move 的
    # 綁定方法，改 bot 的屬性攔不到（這個坑差點讓這條測試變成假的）。
    from ro_toolbox.core.ro_protocol import unpack_position

    sent = []
    monkeypatch.setattr(
        game_link.game_socket, "send_on_socket",
        lambda _sock, data: (sent.append(data), len(data))[1],
    )
    hop = Hop("t", 108, 27, "izlude", 1, 1, "船員", 100)
    bot._shake_view(hop)
    assert sent, "應該有送出走路封包"
    for data in sent:
        x, y, _d = unpack_position(data[2:5])
        step = max(abs(x - 109), abs(y - 27))
        assert step <= MAX_STEP, f"送了 {step} 格，伺服器會忽略"


def test_the_shake_walks_all_the_way_out_then_back(monkeypatch):
    """出視野看**真的座標**，不是等秒數；回來也是。"""
    from ro_toolbox.services import game_link
    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234)
    bot._reader = FakeReader((109, 27))
    bot._traveler._terrain = open_terrain("t", side=200)
    bot._sock = 42
    monkeypatch.setattr(game_link.game_socket, "send_on_socket", lambda *a: 8)
    # 目標固定、Walker 一律回「還在走」—— 這條測的是**判斷分支**，
    # 不是 Walker（它自己有 test_walker.py）。不固定的話結果會隨機。
    monkeypatch.setattr(mod.random, "choice", lambda seq: seq[0])
    monkeypatch.setattr(bot, "_walk_to",
                        lambda _s, _g: True)      # 路怎麼算不是這條在測
    monkeypatch.setattr(bot._walker, "update", lambda _pos: "walking")
    hop = Hop("t", 108, 27, "izlude", 1, 1, "船員", 100)

    bot._shake_view(hop)
    assert bot._shake == "away"

    for step in (5, 10, mod.OUT_OF_VIEW - 1):
        bot._reader.pos = (108 + step, 27)
        bot._shake_view(hop)
        assert bot._shake == "away", f"才 {step} 格就說出視野了"

    bot._reader.pos = (108 + mod.OUT_OF_VIEW, 27)    # 出去了
    bot._shake_view(hop)
    assert bot._shake == "back"

    bot._reader.pos = (109, 27)                       # 回來了
    bot._shake_view(hop)
    assert bot._shake is None


def test_a_failed_dialog_does_not_loop_back_to_cannot_see_him(monkeypatch, caplog):
    """⚠ 實測踩過：看不懂選單之後，下一拍又走「認不出他」那條路，
    印出「你按下按鈕時已經站在他旁邊」—— 完全不對的原因，而且會一直來回走位。

    認人是成功的，失敗的是「看不懂選單」。兩件事不能混。
    """
    import logging

    from ro_toolbox.services import game_link
    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234)
    bot._sock = 42
    monkeypatch.setattr(game_link.game_socket, "send_on_socket", lambda *a: 8)
    bot._reader = FakeReader((120, 63))
    bot._traveler._terrain = open_terrain("t", side=200)
    bot._traveler._route_map = "t"
    hop = Hop("t", 120, 62, "prontera", 1, 1, "卡普拉 職員", 115)
    bot._traveler._npc_wait = hop
    bot._traveler._route = [hop]
    bot._watch_next_npc()
    bot._npc_gid = 145
    bot._run_dialog()                      # 開始對話
    assert bot._talk is not None

    # 伺服器丟一個我們看不懂的選單
    bad = "記憶點:結束:".encode("cp950") + b"\x00"
    bot._talk.feed(
        mod.npc_dialog.ZC_MENU_LIST,
        b"\x14\x00" + (145).to_bytes(4, "little") + bad,
    )
    bot._run_dialog()
    assert bot._talk is None and bot._dialog_dead is not None

    with caplog.at_level(logging.WARNING, logger="ro_toolbox.services.travel_bot"):
        for _ in range(20):
            bot._run_dialog()
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "站在他旁邊" not in said, "不該再講認不出他"
    assert bot._shake is None, "不該再來回走位"


# ---- 在兩張圖之間來回刷會把自己刷到斷線 ---------------------------------
#
# 使用者實測：自動尋路走進一間店（s_atelier）之後又馬上出來、再進去…
# 來回刷換圖，最後**整個連線被伺服器斷掉**，接著才是回連、卡登那一串。
#
# 會這樣是因為換圖之後立刻重新規劃，而腳下那道門就是新路線的第一段。
# 正確的路線修法還沒有足夠證據，但「把自己刷到斷線」這件事本身就該擋。


def test_bouncing_between_two_maps_stops_loudly():
    """⚠ 一直做會造成傷害的動作，既不是「大聲停用」也不是「安全退化」。"""
    from ro_toolbox.services import travel as mod

    traveler, _walker_, clock = make()
    traveler._route_map = "prontera"
    seen = []
    for i in range(mod.BOUNCE_LIMIT + 1):
        clock.now += 2.0
        other = "s_atelier" if i % 2 == 0 else "prontera"
        seen.append(traveler._bouncing(other))
        traveler._route_map = other
    assert seen[-1] is True, f"來回 {mod.BOUNCE_LIMIT} 次就該喊停：{seen}"
    assert seen[0] is False, "第一次換圖不算來回"


def test_normal_map_changes_are_not_bouncing():
    """一路往前走過好幾張圖不是來回 —— 不准把正常跨圖砍掉。"""

    traveler, _walker_, clock = make()
    traveler._route_map = "prontera"
    for nxt in ("prt_fild05", "prt_fild08", "geffen", "gef_fild00", "cmd_fild03"):
        clock.now += 5.0
        assert traveler._bouncing(nxt) is False, f"{nxt} 被誤判成來回"
        traveler._route_map = nxt


def test_slow_back_and_forth_is_allowed():
    """⚠ 隔很久的來回是正常的（例如先去買東西再回來）—— 只擋短時間內的刷。"""
    from ro_toolbox.services import travel as mod

    traveler, _walker_, clock = make()
    traveler._route_map = "prontera"
    for i in range(6):
        clock.now += mod.BOUNCE_WINDOW_SEC + 1
        other = "s_atelier" if i % 2 == 0 else "prontera"
        assert traveler._bouncing(other) is False
        traveler._route_map = other


def test_a_new_goal_forgets_the_old_bouncing():
    """換目的地就是新的一趟，舊的換圖紀錄不該跟著。"""
    from ro_toolbox.services import travel as mod

    traveler, _walker_, clock = make()
    traveler._route_map = "prontera"
    for i in range(mod.BOUNCE_LIMIT):
        clock.now += 1.0
        traveler._bouncing("s_atelier" if i % 2 == 0 else "prontera")
    traveler.set_goal("geffen")
    assert traveler._hops == []


# ---- 走進房子又回頭出來（使用者實測：來回刷到被伺服器斷線）-----------------
#
# 兩個獨立的洞，缺一條都還是會斷線：
#   1. `plan_route` 是**地圖層級**的 BFS，以為「進得去那張圖就走得到圖上任何
#      一道門」。室內圖不是這樣（`s_atelier` 是一張圖四個互不相連的房間）。
#   2. A* 不知道傳點的存在。**出門就站在門邊**，只要目標在門的另一側，
#      路徑第一格就是那道門 —— 被傳回去、走出來、又被傳回去。

#: a 有兩道門：進 b 會落在「跟出口隔著一道牆」的房間，進 d 沒問題。
SEALED_WARPS = {
    "a": [(10, 10, "b", 90, 90), (12, 12, "d", 50, 50)],
    "b": [(30, 30, "c", 70, 70)],
    "d": [(60, 60, "c", 71, 71)],
    "c": [],
}


def sealed_terrain(name: str) -> MapTerrain:
    """x=40 一整排牆，把落地點 (90,90) 跟出口 (30,30) 隔開。"""
    terrain = open_terrain(name)
    terrain.types[:, 40] = 1
    return terrain


def _sealed(monkeypatch, warps=SEALED_WARPS):
    monkeypatch.setattr(travel, "warps_on_map", lambda m: warps.get(m, []))
    monkeypatch.setattr(travel, "warp_cells", lambda _m: frozenset())
    return make(lambda name: sealed_terrain(name) if name == "b" else open_terrain(name))


def test_a_route_landing_in_a_sealed_room_is_dropped_before_walking_in(monkeypatch):
    """⚠ 重點是**還沒走過去之前**就換路，不是走進去發現走不出來才回頭。

    實機那一次就是走進 s_atelier（落地 (13,119)，那一塊只有 282/2179 格），
    往 rachel／yuno／lighthalzen 的門全部走不到，只好原路走回 prontera。
    """
    traveler, walker, _clock = _sealed(monkeypatch)
    traveler.set_goal("c")

    assert traveler.update("a", (5, 5)) == "walking"
    assert [hop.to_map for hop in traveler.route] == ["d", "c"], "應該改走 d 那道門"
    assert walker.paths, "要真的開始走，不是停在那裡"


def test_no_alternative_route_still_walks_instead_of_stopping(monkeypatch):
    """繞不開的時候**照原路走**，不要把整趟停掉。

    地圖層級的 BFS 表達不出「同一張圖裡再踩一次內部傳點換房間」
    （`s_atelier` 的房間之間就是這樣連的），所以「驗不過」不等於「路不通」。
    真的踩不過去還有傳點黑名單與來回偵測收尾。
    """
    only = {"a": [(10, 10, "b", 90, 90)], "b": [(30, 30, "c", 70, 70)], "c": []}
    traveler, walker, _clock = _sealed(monkeypatch, only)
    traveler.set_goal("c")

    assert traveler.update("a", (5, 5)) == "walking"
    assert [hop.to_map for hop in traveler.route] == ["b", "c"]


#: a 上有兩道門：(10,10) 是我們要走的，(50,50) 是**別人的**，踩到就被傳走。
CROSSING_WARPS = {
    "a": [(10, 10, "b", 90, 90), (50, 50, "dead_end", 5, 5)],
    "b": [(30, 30, "c", 70, 70)],
    "c": [],
    "dead_end": [(6, 6, "a", 51, 50)],
}


def test_the_path_refuses_to_cross_another_warp(monkeypatch):
    """⚠ 這是斷線那一整串的起點。

    實機日誌：人剛從 s_atelier 走出來站在 prontera (271,108)，要去
    prt_fild06 的門 (289,203) —— 而 s_atelier 的門就在 (272,108)。
    舊版的 A* 不知道傳點的存在，第一步就踩回去，再走出來、再踩回去，
    來回刷到 `send 失敗，WSA 錯誤 10054`。
    """
    monkeypatch.setattr(travel, "warps_on_map", lambda m: CROSSING_WARPS.get(m, []))
    monkeypatch.setattr(
        travel,
        "warp_cells",
        lambda m: frozenset((x, y) for x, y, *_ in CROSSING_WARPS.get(m, [])),
    )
    traveler, walker, _clock = make()
    traveler.set_goal("c")

    # 站在別人那道門的正旁邊，目標在門的另一側 —— 直線會直接穿過去
    assert traveler.update("a", (51, 50)) == "walking"
    assert (50, 50) not in walker.paths[0], "路徑踩到別的傳點＝會被傳到別張地圖"
    assert (50, 50) in walker.avoids[0], "走路那一層也要知道這格不准經過"


def test_the_door_we_are_heading_for_is_not_blocked(monkeypatch):
    """⚠ 只擋**別的**門。連要踩的那道都擋掉的話，每一道門都變成「走不到」。"""
    monkeypatch.setattr(travel, "warps_on_map", lambda m: CROSSING_WARPS.get(m, []))
    monkeypatch.setattr(
        travel,
        "warp_cells",
        lambda m: frozenset((x, y) for x, y, *_ in CROSSING_WARPS.get(m, [])),
    )
    traveler, walker, _clock = make()
    traveler.set_goal("c")

    assert traveler.update("a", (80, 80)) == "walking"
    assert walker.paths[0][-1] == (10, 10), "最後一步就是要踩上那道門"
    assert (10, 10) not in walker.avoids[0]


def test_pass_by_warps_get_the_wide_clearance_in_the_open(monkeypatch):
    """★ 使用者：「自動尋路要離路過的傳點遠一點」（2026-09-05）。

    開闊地要留 `PASS_CLEAR` 格，不是舊的 `KEEP_OUT`（3）—— 這樣就算伺服器
    在兩個走點之間抄近路，也踩不到旁邊那道門。
    """
    monkeypatch.setattr(travel, "warps_on_map", lambda m: [(50, 50, "x", 1, 1)])
    monkeypatch.setattr(travel, "warp_cells", lambda m: frozenset({(50, 50)}))
    traveler, _walker, _clock = make()
    terrain = open_terrain("a")
    # 直線 (40,50)→(60,50) 會正中傳點 —— 開闊地應該大大繞開
    path, _avoid = traveler._path_to(terrain, "a", (40, 50), (60, 50))
    assert path is not None
    nearest = min(max(abs(x - 50), abs(y - 50)) for x, y in path)
    assert nearest > travel.KEEP_OUT, f"路徑貼到傳點 {nearest} 格內（沒比舊的遠）"
    assert nearest > travel.PASS_CLEAR - 1, "開闊地留得住整個 PASS_CLEAR 才對"


def test_a_tight_spot_steps_down_but_never_walks_onto_the_warp(monkeypatch):
    """★ prontera 補水「彈進彈出房門」的修法（[travel] 2026-09-05）。

    實機 18:52：走過 prontera 去補水，一路撞上通往 prt_in 的房門，被傳進去
    又走出來，來回 4 次才放棄。根因：半徑 3 的禁區把滿是房門的城裡切碎、
    A* 算不出路，舊版就**直接退到「完全不擋」**，於是貼著門走、被伺服器
    抄近路踩進去。現在窄到留不住大半徑時**一階一階退**，只要「只擋門本體」
    還走得通，就一定不會踩上那道門。
    """
    monkeypatch.setattr(travel, "warps_on_map", lambda m: [(50, 50, "x", 1, 1)])
    monkeypatch.setattr(travel, "warp_cells", lambda m: frozenset({(50, 50)}))
    # 只有 3 格高的走廊，傳點卡在正中央 —— 大半徑會把走廊整段封死
    types = np.ones((100, 100), np.uint32)
    types[49:52, :] = 0
    terrain = MapTerrain(name="a", width=100, height=100, types=types)
    traveler, _walker, _clock = make()
    path, avoid = traveler._path_to(terrain, "a", (40, 50), (60, 50))
    assert path is not None, "退到只擋本體時還是走得過去"
    assert (50, 50) not in path, "再窄也不准踩上那道門"
    assert (50, 50) in avoid, "門本體始終擋著（沒有一路退到完全不擋）"


def test_sibling_doors_stay_blocked_even_in_the_targets_hole(monkeypatch):
    """★ prontera 底下 15 道門都通 prt_in、彼此才隔幾格 —— 踩錯一道就進錯房間。

    要踩的那道門周圍一定要留洞（不然 A* 到不了），但洞裡**別人的門格**要照擋，
    不然伺服器抄近路就把人送進隔壁那道門的房間（實機 18:52 的彈進彈出）。
    """
    # 目標 (10,10) 與姊妹門 (12,10) 都通 b，只隔 2 格 —— 姊妹門落在目標的洞裡
    monkeypatch.setattr(
        travel, "warps_on_map",
        lambda m: [(10, 10, "b", 90, 90), (12, 10, "b", 80, 80)] if m == "a" else [],
    )
    monkeypatch.setattr(
        travel, "warp_cells",
        lambda m: frozenset({(10, 10), (12, 10)}) if m == "a" else frozenset(),
    )
    traveler, _walker, _clock = make()
    terrain = open_terrain("a")
    path, avoid = traveler._path_to(terrain, "a", (40, 40), (10, 10))
    assert path is not None and path[-1] == (10, 10), "要踩的那道門走得到"
    assert (10, 10) not in avoid, "要踩的那道門不能擋"
    assert (12, 10) in avoid, "姊妹門即使在洞裡也要擋 —— 不然被抄近路傳進錯房間"
    assert (12, 10) not in path, "路徑不准踩上姊妹門"


# ---- 暫停：站著不動，但**不要把時間算在別人頭上** -------------------------


def test_resume_does_not_blame_the_warp_for_time_we_paused(fake_warps):
    """⚠⚠ 暫停五分鐘再回來，傳點不可以被誤判成「踩不過去」。

    這支狀態機有三個「逾時＝放棄」的計時器（踩傳點、等座標、等 NPC），
    全都是拿現在的時間減起算時間。暫停期間那段時間是**我們自己停掉的**，
    不歸零的話一放開就會一次全部到期 —— 傳點被列黑名單、路線被判死。
    """
    traveler, walker, clock = make()
    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    walker.state = "arrived"
    traveler.update("a", (10, 10))          # 走到傳點了，開始踩

    clock.now += travel.WARP_GIVEUP_SEC * 20   # 暫停很久
    traveler.resume()
    assert traveler.update("a", (10, 10)) != "blocked", "暫停的時間不算在傳點頭上"

    clock.now += travel.WARP_GIVEUP_SEC + 1     # 這次是真的踩不過去
    assert traveler.update("a", (10, 10)) == "blocked"


def test_resume_keeps_the_route_and_the_blacklist(fake_warps):
    """暫停不是重來：路線與「這個傳點踩不過去」的黑名單都要留著。"""
    traveler, walker, clock = make()
    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    before = [hop.to_map for hop in traveler.route]

    traveler.resume()
    assert [hop.to_map for hop in traveler.route] == before
    assert traveler.goal_map == "c"


def test_resume_drops_the_stale_path(fake_warps):
    """暫停期間人可能被伺服器帶完最後幾格、也可能自己走開 ——
    舊路徑不再有效，一定要重算，不能沿著過期的路走。"""
    traveler, walker, clock = make()
    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    walked = len(walker.paths)

    traveler.resume()
    traveler.update("a", (7, 7))            # 暫停期間被移動到別的地方
    assert len(walker.paths) > walked, "要從現在真的站的位置重算路徑"
    assert walker.paths[-1][-1] == (10, 10)


# ---- TravelBot 的暫停：不收攤 ---------------------------------------------


def test_pause_does_not_tear_the_bot_down():
    """⚠ 暫停 ≠ 取消。取消會關 socket、關封包擷取、忘掉這一趟學到的東西，
    再開一次要重新 AOB 定位、重新複製 socket（剛換頻道那幾秒常常複製不到）。"""
    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234, destination="prontera")
    assert bot.paused is False

    bot.pause()
    assert bot.paused is True
    assert bot.stats.paused is True
    assert bot.stats.goal == "prontera", "目的地要留著"

    bot.resume()
    assert bot.paused is False
    assert bot.stats.paused is False


def test_stopping_clears_the_pause():
    """停掉之後再開新的一趟，不可以一開始就是暫停狀態。"""
    from ro_toolbox.services import travel_bot as mod

    bot = mod.TravelBot(1234, destination="prontera")
    bot.pause()
    bot.stop()
    assert bot.paused is False


# ---- 「最近的商人在哪張圖」：一次 BFS，不是對 43 張圖各算一次 ---------------


def test_nearest_map_stops_at_the_first_goal_it_reaches(fake_warps):
    """BFS 本來就一層一層往外走 —— **第一個碰到的就是最近的**。"""
    found = travel.nearest_map_with("a", {"c", "dead_end"})
    assert found is not None
    route, where = found
    assert where == "dead_end", "a 一步就到 dead_end，c 要兩步"
    assert [hop.to_map for hop in route] == ["dead_end"]


def test_standing_on_a_goal_map_needs_no_route(fake_warps):
    assert travel.nearest_map_with("c", {"c", "a"}) == ([], "c")


def test_no_goal_reachable_returns_none(fake_warps):
    assert travel.nearest_map_with("a", {"nowhere"}) is None


def test_the_avoid_list_is_respected(fake_warps):
    """踩不過去的傳點在這裡也要繞開，否則會挑到一條已知走不通的路。"""
    assert travel.nearest_map_with("a", {"dead_end"}, avoid={("a", 20, 20)}) is None


# ---- 一步都沒動就不要一直重算（使用者實測：izlu2dun 磨了一分鐘）------------


def _fail_leg(traveler, walker, pos):
    """讓走路那一層回一次 blocked。

    ⚠ 每一拍都要重設：`_replan()` 會 `walker.clear()`，假的走路器就回 idle 了。
    """
    walker.state = "blocked"
    return traveler.update("a", pos)


def test_repeated_failures_without_moving_stop_loudly(fake_warps):
    """⚠ 使用者實測：路徑算得出來（172 格）、封包也送了，角色卻一步都沒動，
    座標一分鐘停在同一格。舊版每次只是「重新規劃」→ 同一條路 → 同一批封包 →
    再失敗，磨到 MAX_REPLANS 才停，而且那句話還在講「路線」。

    一步都沒動 = 不是路的問題，重算幾次都一樣。

    ⚠ 失敗與重算是**輪流**發生的（重算那一拍會把走路器清掉再送新的一段），
    所以連續三次失敗要花六拍左右 —— 這裡只釘住「會停」與「停下來說了什麼」。
    """
    traveler, walker, _clock = make()
    traveler.set_goal("c")
    traveler.update("a", (5, 5))

    states = [_fail_leg(traveler, walker, (5, 5)) for _ in range(8)]
    assert "blocked" in states, f"人一步都沒動，不該一直重算：{states}"
    assert "一步都沒動" in traveler.note
    assert "背包太重" in traveler.note, "要講人能處理的事，不是只說走不成"


def test_making_progress_resets_the_counter(fake_warps):
    """中間真的往前走了就重新起算 —— 那種是「路上被打斷」，本來就該重新規劃。"""
    traveler, walker, _clock = make()
    traveler.set_goal("c")
    traveler.update("a", (5, 5))

    for step in range(6):
        state = _fail_leg(traveler, walker, (5 + step, 5 + step))
        assert state == "walking", f"第 {step + 1} 拍：有在前進就不該放棄"


def test_a_new_goal_starts_the_count_again(fake_warps):
    traveler, walker, _clock = make()
    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    for _ in range(4):
        _fail_leg(traveler, walker, (5, 5))

    traveler.set_goal("c")
    traveler.update("a", (5, 5))
    states = [_fail_leg(traveler, walker, (5, 5)) for _ in range(3)]
    assert "blocked" not in states, f"換目的地要重新起算：{states}"


# ---- 傳點本體那一格 --------------------------------------------------------


def test_the_door_cell_itself_is_the_first_thing_we_try(monkeypatch):
    """⚠⚠ 2026-08-30 實機：一道**好好的門**被列進黑名單。

    `Walker._reached_goal()` 的容忍是**一格**，所以角色常常停在門旁邊一格。
    舊版踩傳點時先 `_warp_try += 1` 再取候選，於是 `_ring_cell` 的 index 0
    （＝門本身）從來沒被試過 —— 只繞著門走它周圍，15 秒後把門黑名單掉。
    狐狐狸因此改走「緊急傳送職員」，補給卡在野外。
    """
    monkeypatch.setattr(travel, "warps_on_map",
                        lambda m: [(134, 221, "prt_in", 131, 71)] if m == "prontera" else [])
    t, walker, clock = make(loader=lambda name: open_terrain(name, side=312))
    t.set_goal("prt_in")
    door = (134, 221)
    t._route = [Hop("prontera", door[0], door[1], "prt_in", 131, 71)]
    t._terrain = open_terrain("prontera", side=312)
    t._warp_cell = door
    t._warp_since = clock.now
    t._warp_try = 0
    walker.state = "arrived"
    clock.now += travel.WARP_SETTLE_SEC + 0.1
    t._push_warp((134, 219))          # 站在門旁邊一格
    # ⚠ 要**直接精準踩上門那一格**，不是用 set_path（那會停在 ≤1 格外，[DAT-077]）。
    assert walker.steps, "門就在腳邊，應該直接踩上去"
    assert walker.steps[-1] == door, f"第一個要踩的就是門本身，卻踩去 {walker.steps[-1]}"


def test_standing_one_cell_from_the_door_actually_steps_onto_it(monkeypatch):
    """★ [DAT-077] 的本體：角色停在門旁邊一格，要真的踩上門，不是繞著門走。

    舊版用 `set_path([door])`，而 `Walker._reached_goal()` 容忍一格 —— 交一條
    「走到門」的路徑給它，角色已在門旁邊一格就當「到了」，最後那一步永遠不送，
    15 秒後把好好的門黑名單。使用者：「卡傳點前面不進去還黑名單」。
    """
    monkeypatch.setattr(travel, "warps_on_map",
                        lambda m: [(134, 221, "prt_in", 131, 71)] if m == "prontera" else [])
    t, walker, clock = make(loader=lambda name: open_terrain(name, side=312))
    t.set_goal("prt_in")
    door = (134, 221)
    t._route = [Hop("prontera", door[0], door[1], "prt_in", 131, 71)]
    t._terrain = open_terrain("prontera", side=312)
    t._warp_cell = door
    t._warp_since = clock.now
    t._warp_try = 0
    walker.state = "arrived"
    # 撐過三個 SETTLE 視窗：不管門本體還是周圍環，靠的都是 step_onto，不是繞路。
    for _ in range(3):
        clock.now += travel.WARP_SETTLE_SEC + 0.1
        t._push_warp((134, 219))
    assert door in walker.steps, "門本身一定要被直接踩過"
    assert not walker.paths, "踩傳點不該再用 set_path 繞（那正是 15 秒黑名單的成因）"


def test_an_npc_we_cannot_talk_to_makes_us_take_another_route(monkeypatch):
    """使用者的規矩：不要叫使用者持續配合。講不通就改走別條。"""
    walk = {"prt_fild06": [(200, 200, "prontera", 10, 10)],
            "prontera": [(50, 50, "prt_in", 20, 20)]}
    npc = {"prt_fild06": [(28, 188, "prt_in", 100, 100, "緊急傳送 職員", 700)]}
    monkeypatch.setattr(travel, "warps_on_map", lambda m: walk.get(m, []))
    monkeypatch.setattr(travel, "npc_links_on_map", lambda m: npc.get(m, []))
    t, _walker, _clock = make(loader=lambda name: open_terrain(name, side=312))
    t.set_goal("prt_in")
    hop = Hop("prt_fild06", 28, 188, "prt_in", 100, 100, npc="緊急傳送 職員")
    t._route = [hop]
    t._npc_wait = hop
    assert t.npc_impassable() is True
    assert hop.key in t._avoid
    assert t.npc_hop is None


def test_with_no_other_route_we_still_wait_for_the_user(monkeypatch):
    """真的沒有別條路的時候，等人至少還有救 —— 那時候才可以停下來等。"""
    npc = {"prt_fild06": [(28, 188, "prt_in", 100, 100, "緊急傳送 職員", 700)]}
    monkeypatch.setattr(travel, "warps_on_map", lambda m: [])
    monkeypatch.setattr(travel, "npc_links_on_map", lambda m: npc.get(m, []))
    t, _walker, _clock = make(loader=lambda name: open_terrain(name, side=312))
    t.set_goal("prt_in")
    hop = Hop("prt_fild06", 28, 188, "prt_in", 100, 100, npc="緊急傳送 職員")
    t._route = [hop]
    t._npc_wait = hop
    assert t.npc_impassable() is False
    assert t.npc_hop is hop, "還是停在他面前等人"
    assert hop.key not in t._avoid, "沒改走別條就不要把它記成走不通"


# ---- 出發前先把走法講清楚（使用者 2026-08-31 指定）------------------------


def test_the_route_is_described_in_chinese():
    """★ 使用者：「需要跳出一個視窗告訴我我們的路徑，用中文地圖名字和中文
    NPC 名字，告訴我要從哪張圖到哪張圖、NPC 要傳到哪張圖」。"""
    from ro_toolbox.services.travel import Hop, describe_route

    text = describe_route("izlude", [
        Hop("izlude", 128, 148, "geffen", 120, 39, npc="卡普拉職員", npc_id=117),
        Hop("geffen", 119, 213, "gef_fild04", 187, 42),
    ])
    assert "衛星都市 依斯魯得島" in text
    assert "魔法之都 吉芬" in text
    assert "卡普拉職員" in text, "NPC 的中文名要寫出來"
    assert "走傳點 (119,213)" in text, "走傳點那種也要說清楚是哪一格"
    assert "要換 2 張圖" in text


def test_two_maps_with_the_same_chinese_name_are_told_apart():
    """⚠ `mjolnir_06` 與 `mjolnir_07` 兩張都叫「妙勒妙勒尼山脈南區」。

    撞名的話兩段看起來一模一樣 —— 那正好是使用者想從這個視窗看懂的東西。
    """
    from ro_toolbox.services.travel import Hop, describe_route

    text = describe_route("mjolnir_06", [
        Hop("mjolnir_06", 382, 377, "mjolnir_07", 19, 377),
    ])
    assert "mjolnir_06" in text and "mjolnir_07" in text


def test_an_unknown_map_falls_back_to_the_internal_name():
    """查不到中文名就寫內部名 —— **不准留白**，留白就看不出是哪一段。"""
    from ro_toolbox.services.travel import Hop, describe_route

    text = describe_route("no_such_map", [
        Hop("no_such_map", 1, 1, "also_missing", 2, 2),
    ])
    assert "no_such_map" in text and "also_missing" in text


# ---- 有前置的傳送直接濾掉（使用者 2026-08-31 指定）------------------------


def test_gated_npc_warps_are_never_planned():
    """★ 使用者：「不可以猜，也不能學習。伊甸園入會、結婚…這不就是箝制嗎，
    我們直接濾掉就好」。

    導航資料**沒有「有沒有前置」這個欄位**，排進路線角色就會走到 NPC 面前
    卡住（對話根本不會給你傳送的選項）。
    """
    from ro_toolbox.services import travel as mod

    assert "moc_para01" in mod.GATED_MAPS, "伊甸園總部要先入會"
    assert "jawaii" in mod.GATED_MAPS, "加維要結婚"
    for map_name in ("prontera", "izlude", "geffen", "alberta"):
        dests = {row[2] for row in mod._npc_links(map_name)}
        assert not (dests & mod.GATED_MAPS), f"{map_name} 還排得到有前置的地圖"


def test_the_ordinary_npc_warps_are_still_there():
    """⛔ 反面：不准把好好的 NPC 傳送一起濾掉。"""
    from ro_toolbox.services import travel as mod

    dests = {row[2] for row in mod._npc_links("izlude")}
    assert "alberta" in dests, "船員通往艾爾貝塔，那條是好的"
