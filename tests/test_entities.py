"""EntityScanner 用合成的記憶體內容驗證（不需遊戲）。

結構偏移來自實機（GAMEDATA [MEM-014]/[MEM-016]）：class ID 在 GID-0x4、
存活旗標在 GID-0x24、繪圖指標在 GID+0x110、座標 float 在 GID+0x120/+0x124。
這些測試把版面釘住 —— 偏移被改錯的話這裡會直接紅。
"""

from __future__ import annotations

import struct

import numpy as np

from ro_toolbox.services.entities import OFF_ALIVE, OFF_RENDER, EntityScanner
from ro_toolbox.services.gamedata import mobs_on_map
from ro_toolbox.services.mapdata import MapTerrain

MAP = "prt_fild07"
CLASS_ID = min(mobs_on_map(MAP))  # 這張圖真的會出的怪
REGION_BYTES = 0x2000
_DWORDS = REGION_BYTES // 4
_SLOT = 16  # 把 class 欄位放在第幾個 dword


def terrain(walkable: bool = True) -> MapTerrain:
    types = np.zeros((400, 400), dtype=np.uint32) if walkable else np.ones(
        (400, 400), dtype=np.uint32
    )
    return MapTerrain(name=MAP, width=400, height=400, types=types)


def make_region(
    class_id: int,
    gid: int,
    x: float,
    y: float,
    slot: int = _SLOT,
    alive: int = 1,
    render: int = 0x0BAD_F00D,
) -> bytes:
    """組一段記憶體，在 slot 放一筆實體結構，其餘留白。

    alive / render 預設是「活著」的值；測死掉的情況就傳 0。
    """
    buf = bytearray(REGION_BYTES)
    gid_at = slot * 4 + 4
    struct.pack_into("<I", buf, slot * 4, class_id)
    struct.pack_into("<I", buf, gid_at, gid)
    struct.pack_into("<i", buf, gid_at + OFF_ALIVE, alive)
    struct.pack_into("<I", buf, gid_at + OFF_RENDER, render)
    struct.pack_into("<f", buf, gid_at + 0x120, x)
    struct.pack_into("<f", buf, gid_at + 0x124, y)
    return bytes(buf)


class FakeMemory:
    """假的記憶體：只回一段我們自己組出來的內容。"""

    def __init__(self, blob: bytes) -> None:
        self.blob = blob

    def regions(self, writable_only: bool = True) -> list[tuple[int, int]]:
        return [(0x10000, len(self.blob))]

    def read_region(self, base: int, size: int):
        return memoryview(self.blob)

    def open(self, pid: int) -> None:
        pass

    def close(self) -> None:
        pass


def scanner_with(blob: bytes, walkable: bool = True, view: int = 30) -> EntityScanner:
    scanner = EntityScanner(terrain(walkable), MAP, view=view)
    scanner._scanner = FakeMemory(blob)  # noqa: SLF001 - 測試替身
    return scanner


def test_finds_entity():
    found = scanner_with(make_region(CLASS_ID, 2870, 221.2, 256.2)).scan((220, 255))
    assert len(found) == 1
    assert found[0].gid == 2870
    assert found[0].class_id == CLASS_ID
    assert found[0].pos == (221, 256)


def test_class_not_on_this_map_is_rejected():
    """沙漠的怪不會出現在草原 —— 地圖出沒表是最強的過濾條件。"""
    outsider = 1002 if 1002 not in mobs_on_map(MAP) else 1001
    assert not scanner_with(make_region(outsider, 2870, 221.0, 256.0)).scan((220, 255))


def test_unwalkable_cell_is_rejected():
    """怪不會站在不可走的格子上，用 .gat 擋掉剛好長得像座標的垃圾值。"""
    blob = make_region(CLASS_ID, 2870, 221.0, 256.0)
    assert not scanner_with(blob, walkable=False).scan((220, 255))


def test_far_entity_is_rejected():
    blob = make_region(CLASS_ID, 2870, 380.0, 380.0)
    assert not scanner_with(blob).scan((100, 100))


def test_zero_gid_is_rejected():
    assert not scanner_with(make_region(CLASS_ID, 0, 221.0, 256.0)).scan((220, 255))


def test_nan_coordinates_are_rejected():
    blob = bytearray(make_region(CLASS_ID, 2870, 0.0, 0.0))
    struct.pack_into("<I", blob, _SLOT * 4 + 4 + 0x120, 0x7FC00000)  # NaN
    assert not scanner_with(bytes(blob)).scan((220, 255))


def test_hot_region_is_remembered():
    """掃到怪的區段會被記成熱區段，之後每拍只掃那些（實測 4ms）。"""
    scanner = scanner_with(make_region(CLASS_ID, 2870, 221.0, 256.0))
    scanner.scan((220, 255))
    assert scanner._hot  # noqa: SLF001


def test_dead_entity_is_rejected():
    """死掉的怪結構會留在記憶體裡沒被回收，但存活旗標會被清成 0。

    這就是「畫面不殘留、記憶體卻殘留」的分界 —— 少了這一關就會去打空氣。
    """
    blob = make_region(CLASS_ID, 2870, 221.0, 256.0, alive=0)
    assert not scanner_with(blob).scan((220, 255))


def test_entity_without_render_object_is_rejected():
    """繪圖物件指標被清成 0 = 畫面上已經看不到它了。"""
    blob = make_region(CLASS_ID, 2870, 221.0, 256.0, render=0)
    assert not scanner_with(blob).scan((220, 255))


# ---- 快路徑：記住位址，之後只讀那個位址 ------------------------------------


import struct  # noqa: E402

from ro_toolbox.services import entities as ent  # noqa: E402


def _one_struct(gid=777, class_id=1002, x=100.0, y=100.0, alive=1, render=0x1234):
    """組一筆「一隻怪」的記憶體內容（從存活旗標到 y）。"""
    buf = bytearray(ent._ONE_SIZE)
    struct.pack_into("<I", buf, ent._B_ALIVE, alive)
    struct.pack_into("<I", buf, ent._B_CLASS, class_id)
    struct.pack_into("<I", buf, ent._B_GID, gid)
    struct.pack_into("<I", buf, ent._B_RENDER, render)
    struct.pack_into("<f", buf, ent._B_X, x)
    struct.pack_into("<f", buf, ent._B_Y, y)
    return bytes(buf)


def _scanner_with(monkeypatch, payload):
    scanner = ent.EntityScanner(terrain(), MAP, view=30)
    monkeypatch.setattr(scanner._scanner, "read_region", lambda _a, _s: payload)
    monkeypatch.setattr(scanner, "_lut", _always_true_lut())
    return scanner


class _always_true_lut:
    def __getitem__(self, _i):
        return True


def test_read_one_parses_a_known_address(monkeypatch):
    """快路徑只讀 0x14C bytes，不掃記憶體 —— 這才是每一拍該做的事。"""
    scanner = _scanner_with(monkeypatch, _one_struct(x=100.0, y=100.0))
    got = scanner.read_one(0xDEAD0000, (100, 100))
    assert got is not None
    assert (got.gid, got.class_id, got.pos, got.addr) == (777, 1002, (100, 100), 0xDEAD0000)


def test_read_one_rejects_a_dead_structure(monkeypatch):
    """死掉的結構還在記憶體裡，但存活旗標與繪圖指標會被清掉（[MEM-016]）。"""
    scanner = _scanner_with(monkeypatch, _one_struct(alive=0))
    assert scanner.read_one(0xDEAD0000, (100, 100)) is None
    scanner = _scanner_with(monkeypatch, _one_struct(render=0))
    assert scanner.read_one(0xDEAD0000, (100, 100)) is None


def test_read_one_rejects_a_monster_that_walked_out_of_view(monkeypatch):
    scanner = _scanner_with(monkeypatch, _one_struct(x=100.0, y=100.0))
    assert scanner.read_one(0xDEAD0000, (200, 200)) is None


def test_read_known_drops_addresses_that_stopped_matching(monkeypatch):
    """位址上的 GID 換人了＝那塊記憶體被回收給別的東西，要從清單移除。"""
    scanner = _scanner_with(monkeypatch, _one_struct(gid=999))
    scanner._known = {777: 0xDEAD0000}      # 我們以為 777 在那裡
    assert scanner.read_known((100, 100)) == []
    assert scanner.known_count == 0, "對不上的位址要丟掉，不然會一直讀到別人的資料"


def test_read_known_returns_live_entries(monkeypatch):
    scanner = _scanner_with(monkeypatch, _one_struct(gid=777))
    scanner._known = {777: 0xDEAD0000}
    found = scanner.read_known((100, 100))
    assert [e.gid for e in found] == [777]
    assert scanner.known_count == 1
