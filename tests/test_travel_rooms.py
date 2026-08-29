"""「一張地圖裡好幾個互不相連的房間」—— 用**真的遊戲資料**釘住兩個實機案例。

這兩個都是使用者實測回報的，而且都是**安靜地失敗**：算同一條算不出來的路
到放棄為止，人留在原地。

⚠ 這裡刻意用真實的 `assets/`（warps ＋ terrain），不用假地圖：
壞掉的是「資料長這樣的時候我們的判斷不夠」，假資料重現不出來。
"""

from __future__ import annotations

import time

import pytest

from ro_toolbox.services import travel
from ro_toolbox.services.gamedata import warps_on_map
from ro_toolbox.services.mapdata import GatError, load_terrain
from ro_toolbox.services.travel import Traveler, nearest_walkable


class _Walker:
    def set_path(self, *a, **k) -> None: ...
    def update(self, *a, **k) -> str:
        return "walking"
    def clear(self) -> None: ...


def _traveler() -> Traveler:
    return Traveler(walker=_Walker(), terrain_loader=load_terrain,
                    now=time.monotonic)


def _terrain(name: str):
    try:
        return load_terrain(name)
    except (GatError, FileNotFoundError) as exc:      # pragma: no cover
        pytest.skip(f"沒有 {name} 的地形資料：{exc}")


# ---- 吉芬塔：出口在別的房間，要先爬樓 --------------------------------------


def test_geffen_tower_climbs_an_inner_warp_to_reach_the_exit():
    """使用者實測 2026-08-29：

        gef_tower 上從 (52, 177) 到 gef_dun00 傳點 (153, 28) 的路徑…
        ⚠ gef_tower 上走不到任何一道通往 gef_dun00 的傳點

    使用者說「明明傳點就在正前方」—— 兩件事都對。吉芬塔有 **21 個傳點，
    其中 20 個是塔內部的**（爬樓用），出去 `gef_dun00` 的只有 (153,28)，
    跟角色站的那一塊不連通。要先踩內部傳點換房間。
    """
    terrain = _terrain("gef_tower")
    inner = [w for w in warps_on_map("gef_tower") if w[2] == "gef_tower"]
    assert len(inner) >= 10, "吉芬塔應該有一堆爬樓用的內部傳點"

    exits = [(x, y) for x, y, dest, _dx, _dy in warps_on_map("gef_tower")
             if dest == "gef_dun00"]
    assert exits, "應該有一個通往 gef_dun00 的出口"

    start = nearest_walkable(terrain, (52, 177))
    assert nearest_walkable(terrain, exits[0]) not in terrain.reachable_from(start), \
        "前提：出口跟角色站的那一塊本來就不連通"

    step = _traveler()._inner_hop(terrain, "gef_tower", (52, 177), exits)
    assert step is not None, "要找得出「先踩哪個內部傳點」"
    assert step in {nearest_walkable(terrain, (w[0], w[1])) for w in inner}, \
        "第一步要是一個內部傳點"


def test_no_inner_hop_when_the_target_is_already_reachable():
    """走得到就不要多繞 —— 不然每一段都先去踩個傳點。"""
    terrain = _terrain("gef_tower")
    start = (52, 177)
    reachable = next(iter(terrain.reachable_from(nearest_walkable(terrain, start))))
    assert _traveler()._inner_hop(terrain, "gef_tower", start, [reachable]) is None


# ---- 普隆德拉內部：15 道門，只有 1 道通到道具商人那間 ----------------------


def test_only_one_prontera_door_reaches_the_potion_shop_room():
    """前提：這不是我們算錯，是那張圖真的長這樣。"""
    terrain = _terrain("prt_in")
    shop = nearest_walkable(terrain, (126, 76))
    doors = [(x, y, dx, dy) for x, y, dest, dx, dy in warps_on_map("prontera")
             if dest == "prt_in"]
    assert len(doors) >= 10, "prontera 通往 prt_in 的門很多道"

    good = []
    for x, y, dx, dy in doors:
        land = nearest_walkable(terrain, (dx, dy), radius=3)
        if land is not None and shop in terrain.reachable_from(land):
            good.append((x, y))
    assert len(good) == 1, f"只有一道門走得到商人那間，實際 {good}"


def test_planning_picks_the_door_that_actually_reaches_the_shop():
    """⚠⚠ 實機 2026-08-29（狐狐狸回城補水）：挑到走不到商人的門，
    進去之後同一條路重算 40 次然後放棄，藥水一瓶都沒補。

    兩個上限都太小：`FILL_BUDGET`(4) 讓真正被採用的那道門**一次都沒驗到**，
    `ROUTE_TRIES`(8) 讓它在找到唯一正確的那道之前就放棄、而且**把學到的
    黑名單整個丟掉**照原路走。
    """
    terrain = _terrain("prt_in")
    shop = nearest_walkable(terrain, (126, 76))

    traveler = _traveler()
    traveler.set_goal("prt_in", (126, 76))
    assert traveler._replan("prontera") is True
    assert traveler._route, "要算得出路線"

    hop = traveler._route[0]
    land = nearest_walkable(terrain, (hop.to_x, hop.to_y), radius=3)
    assert land is not None
    assert shop in terrain.reachable_from(land), (
        f"挑到的門 ({hop.x},{hop.y}) 落在 {land}，走不到商人 {shop}"
    )


def test_the_budgets_are_big_enough_for_a_real_indoor_map():
    """釘住「為什麼是這個數字」：門有幾道，額度就得夠驗幾道。"""
    doors = [w for w in warps_on_map("prontera") if w[2] == "prt_in"]
    assert travel.FILL_BUDGET >= len(doors), "每一道門都要驗得到"
    assert travel.ROUTE_TRIES >= len(doors), "每一道門都要試得到"
