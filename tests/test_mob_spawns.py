"""怪物出沒表：哪張圖有這隻怪、大概幾隻。

資料在 `assets/mobs.json.gz` 的 `maps` 欄（`{地圖: 數量}`），來源是客戶端自己的
`navi_mob_tw.lub` —— 遊戲的地圖資訊視窗用的是同一份（[DAT-016]）。
**數量不是我們估的，是遊戲自己的數字。**
"""

from __future__ import annotations

from ro_toolbox.services import gamedata
from ro_toolbox.services.mapdata import has_terrain

PORING = 1002


def test_poring_spawns_match_the_client_data():
    """實際資料：波利在 prt_fild08 有 100 隻，是牠最多的一張圖。"""
    spots = gamedata.mob_maps(PORING)
    assert spots, "波利應該查得到出沒地圖"
    assert spots[0] == ("prt_fild08", 100)
    assert [count for _map, count in spots] == sorted(
        (c for _m, c in spots), reverse=True
    ), "要由多到少排，人才會先看到最好的那張"


def test_unknown_mob_is_empty_not_an_error():
    assert gamedata.mob_maps(999_999) == []


def test_density_label_boundaries():
    """⚠ 分界是**我們切的**（客戶端只有數字），所以要釘住，不要有人隨手改。"""
    assert gamedata.density_label(1) == "很少"
    assert gamedata.density_label(5) == "很少"
    assert gamedata.density_label(6) == "普通"
    assert gamedata.density_label(20) == "普通"
    assert gamedata.density_label(21) == "多"
    assert gamedata.density_label(50) == "多"
    assert gamedata.density_label(51) == "超多"
    assert gamedata.density_label(230) == "超多"


def test_find_mobs_matches_part_of_the_name():
    hits = dict(gamedata.find_mobs("波利"))
    assert PORING in hits and hits[PORING] == "波利"
    assert len(hits) > 1, "「波利」是很多怪名字的一部分（克拉波利…）"


def test_find_mobs_with_nothing_typed_returns_nothing():
    """空字串不要把整份倒出來 —— 那不是搜尋，是洗版。"""
    assert gamedata.find_mobs("") == []
    assert gamedata.find_mobs("   ") == []


def test_spawn_rows_only_list_places_we_can_actually_walk_to():
    """⚠ 沒有地形的地圖列出來，等於讓人選到一個註定失敗的目的地。"""
    rows = gamedata.mob_spawn_rows()
    assert len(rows) > 2000, f"出沒列太少了，資料可能沒載到（{len(rows)}）"
    assert all(name for name, _map, _count in rows), "沒中文名的怪沒得搜，不該收"
    assert all(has_terrain(where) for _name, where, _count in rows)


def test_spawn_rows_put_the_best_map_first_for_each_mob():
    rows = [row for row in gamedata.mob_spawn_rows() if row[0] == "波利"]
    assert rows[0][1] == "prt_fild08"
    assert [c for _n, _m, c in rows] == sorted((c for _n, _m, c in rows), reverse=True)
