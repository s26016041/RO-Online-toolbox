"""地形資產：**沒有 RODATA 也要能走路**。

為什麼要有這組測試：地形以前只從 `RODATA/data/**.gat` 讀，而那個資料夾只有
開發機有（1082 張、1800 MB，不可能打包）。所以「在我這台好好的、換一台
裝 exe 就整個不會走路」——而且症狀是「讀不到地形」這種看起來像資料壞掉的訊息。

現在改成先讀 `assets/terrain.bin.gz`（只存每格能不能站，1 bit/格 → 1.5 MB）。
這裡守的就是「使用者那台」的情境：資產本身可用、內容與 .gat 一致、
而且不靠任何 RODATA 路徑。
"""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")

import numpy as np  # noqa: E402

from ro_toolbox.services.mapdata import (  # noqa: E402
    TERRAIN_ASSET,
    available_maps,
    gat_path,
    has_terrain,
    load_terrain,
)

#: 幾張各種尺寸的地圖：城鎮、野外、洞窟都有。
SAMPLE = ("prontera", "prt_fild08", "payon", "geffen", "pay_dun00")


def test_asset_is_shipped_and_small_enough_to_bundle():
    assert TERRAIN_ASSET.is_file(), f"{TERRAIN_ASSET} 不見了 —— 跑 tools/build_terrain.py"
    size_mb = TERRAIN_ASSET.stat().st_size / 1024 / 1024
    assert size_mb < 8, f"地形資產 {size_mb:.1f} MB，太大了不該打包進 exe"


def test_asset_covers_the_whole_world():
    """313 張是「從普隆德拉純靠走路可達」的數量（[PKT-062]），要遠多於那個。"""
    assert available_maps() > 900


@pytest.mark.parametrize("name", SAMPLE)
def test_maps_load_from_the_asset_not_from_rodata(name):
    terrain = load_terrain(name)
    assert terrain.source == "asset"
    assert terrain.width > 0 and terrain.height > 0
    assert 0.0 < terrain.walkable_ratio() < 1.0


@pytest.mark.parametrize("name", SAMPLE)
def test_asset_matches_the_original_gat_exactly(name):
    """資產是從 .gat 抽的，可走格必須一格不差。開發機才跑得到這條。"""
    path = gat_path(name)
    if not path.is_file():
        pytest.skip("這台沒有 RODATA，無法比對原始 .gat")
    from_asset = load_terrain(name)
    from_gat = load_terrain(name, data_dir=path.parent)
    assert from_gat.source == "gat"
    assert (from_asset.width, from_asset.height) == (from_gat.width, from_gat.height)
    assert np.array_equal(from_asset.walkable, from_gat.walkable)


def test_has_terrain_answers_without_touching_rodata():
    """`navigation` 用它決定要不要接受一個目的地。改回去問 .gat 檔在不在，
    在使用者的電腦上等於**全部拒絕**。"""
    assert has_terrain("prontera") is True
    assert has_terrain("prontera.rsw") is True
    assert has_terrain("PRONTERA") is True
    assert has_terrain("no_such_map") is False


def test_pathfinding_works_on_asset_terrain():
    """用**實機量過的真座標**：2026-08-26 角色站在 prt_fild05 (309, 231)
    （玩家 `/where` 回報），自動尋路就是從那裡走去普隆德拉的。
    隨便挑兩個可走格不行 —— 地圖角落常常是互不相通的孤島。
    """
    terrain = load_terrain("prt_fild05")
    start, goal = (309, 231), (150, 150)
    assert terrain.is_walkable(*start)
    path = terrain.find_path(start, goal)
    assert path, f"{start} → {goal} 應該走得到"
    assert path[-1] == goal
    assert all(terrain.is_walkable(*cell) for cell in path)
    # 逐格路徑：每一步只能走到相鄰格（Walker 靠這個切段）
    for a, b in zip([start, *path], path, strict=False):
        assert max(abs(a[0] - b[0]), abs(a[1] - b[1])) == 1


# ---- 道具圖示資產 ---------------------------------------------------


def test_icon_asset_is_shipped():
    from ro_toolbox.services.icons import ICON_ASSET

    assert ICON_ASSET.is_file(), f"{ICON_ASSET} 不見了 —— 跑 tools/build_icons.py"
    size_mb = ICON_ASSET.stat().st_size / 1024 / 1024
    assert size_mb < 8, f"圖示資產 {size_mb:.1f} MB，太大了"


@pytest.mark.parametrize("item_id", [501, 502, 601, 909, 12629])
def test_icons_come_from_the_asset_not_rodata(item_id, monkeypatch):
    """把解包目錄關掉（模擬使用者的電腦），圖示還是要拿得到。"""
    from ro_toolbox.services import icons

    monkeypatch.setattr(icons, "_ui_root", lambda: None)
    icons.icon_path.cache_clear()
    try:
        data = icons.icon_bytes(item_id)
        assert data, f"道具 {item_id} 在沒有 RODATA 時取不到圖示"
        assert data[:2] == b"BM", "應該是 BMP"
        assert icons.available() is True
    finally:
        icons.icon_path.cache_clear()


def test_unknown_item_has_no_icon_instead_of_a_wrong_one():
    """查不到就回 None —— 介面顯示沒有圖示，不拿別的圖來頂。"""
    from ro_toolbox.services import icons

    assert icons.icon_bytes(999_999) is None
