"""EntityScanner 用合成的記憶體內容驗證（不需遊戲）。

結構偏移只有一份，在 `services/actor.py`（[MEM-058]）：class ID 在 GID-0x4、
vtable 在 GID-0x110、**現在站的格是 GID+0x5C/+0x60**（走路中讀路徑節點）、
「還在不在世界上」看動作 tick GID+0x134。這些測試把版面釘住 ——
偏移或判準被改錯的話這裡會直接紅。

⚠ 這裡特別釘住三個**曾經解錯**的欄位（見 `entities.py` 開頭）：
`+0x120/+0x124` 的 float 不是位置、`-0x24` 不是存活旗標、`+0x110` 不是繪圖指標。
"""

from __future__ import annotations

import struct

import numpy as np

from ro_toolbox.services import actor
from ro_toolbox.services import entities as ent
from ro_toolbox.services.entities import EntityScanner
from ro_toolbox.services.gamedata import mobs_on_map
from ro_toolbox.services.mapdata import MapTerrain

MAP = "prt_fild07"
CLASS_ID = min(mobs_on_map(MAP))  # 這張圖真的會出的怪
REGION_BYTES = 0x2000
#: 把 class 欄位放在第幾個 dword。要留得下 vtable（GID-0x110）。
_SLOT = 0x60
#: 假的 tick 時基：測試不看真的 GetTickCount，一律用這個。
NOW_TICK = 1_000_000


def terrain(walkable: bool = True) -> MapTerrain:
    types = np.zeros((400, 400), dtype=np.uint32) if walkable else np.ones(
        (400, 400), dtype=np.uint32
    )
    return MapTerrain(name=MAP, width=400, height=400, types=types)


def make_region(
    class_id: int,
    gid: int,
    x: int,
    y: int,
    slot: int = _SLOT,
    tick: int = NOW_TICK,
    lerp: tuple[float, float] = (0.0, 0.0),
    alive: int = 0,
    path_begin: int = 0,
) -> bytes:
    """組一段記憶體，在 slot 放一筆實體結構，其餘留白。

    預設就是**最容易被舊版誤殺**的那種怪：從沒走過路（`+0x110 == 0`、
    插值座標是 (0,0)）、`-0x24` 是 0。這種怪在實機上就站在角色旁邊。
    """
    buf = bytearray(REGION_BYTES)
    gid_at = slot * 4 + 4
    struct.pack_into("<I", buf, slot * 4, class_id)
    struct.pack_into("<I", buf, gid_at, gid)
    struct.pack_into("<I", buf, gid_at + actor.VTABLE, 0x0110_0000)  # 假 vtable
    struct.pack_into("<i", buf, gid_at - 0x24, alive)
    struct.pack_into("<ii", buf, gid_at + actor.DEST_X, x, y)
    struct.pack_into("<I", buf, gid_at + actor.PATH_BEGIN, path_begin)
    struct.pack_into("<i", buf, gid_at + actor.PATH_INDEX, -1)
    struct.pack_into("<ff", buf, gid_at + actor.MOVE_LERP_X, *lerp)
    struct.pack_into("<I", buf, gid_at + actor.TICK, tick)
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


def scan(scanner: EntityScanner, me, monkeypatch=None):
    """掃描時把 tick 時基固定住（不然會拿真的 GetTickCount 去比）。"""
    original = actor.now_tick
    ent.actor.now_tick = lambda: NOW_TICK
    try:
        return scanner.scan(me)
    finally:
        ent.actor.now_tick = original


def test_finds_a_monster_that_never_walked():
    """⚠ 這一條就是使用者回報的「明明有怪卻說沒怪」。

    站著沒動過的怪：`+0x110`（路徑陣列）是 0、插值座標是 (0,0)、`-0x24` 是 0。
    舊版三道過濾各自都會把牠丟掉；牠其實就站在角色旁邊。
    """
    found = scan(scanner_with(make_region(CLASS_ID, 2870, 221, 256)), (220, 255))
    assert len(found) == 1
    assert found[0].gid == 2870
    assert found[0].class_id == CLASS_ID
    assert found[0].pos == (221, 256), "位置要讀 +0x5C/+0x60，不是插值座標"


def test_interpolated_coordinates_are_not_the_position():
    """`+0x120/+0x124` 是移動插值，不是「現在站哪」（實機中位差 181 格）。"""
    blob = make_region(CLASS_ID, 2870, 221, 256, lerp=(9.0, 9.0))
    found = scan(scanner_with(blob), (220, 255))
    assert [e.pos for e in found] == [(221, 256)]


def test_class_not_on_this_map_is_rejected():
    """沙漠的怪不會出現在草原 —— 地圖出沒表是最強的過濾條件。"""
    outsider = 1002 if 1002 not in mobs_on_map(MAP) else 1001
    assert not scan(scanner_with(make_region(outsider, 2870, 221, 256)), (220, 255))


def test_unwalkable_cell_is_rejected():
    """怪不會站在不可走的格子上，用 .gat 擋掉剛好長得像座標的垃圾值。"""
    blob = make_region(CLASS_ID, 2870, 221, 256)
    assert not scan(scanner_with(blob, walkable=False), (220, 255))


def test_far_entity_is_rejected():
    blob = make_region(CLASS_ID, 2870, 380, 380)
    assert not scan(scanner_with(blob), (100, 100))


def test_zero_gid_is_rejected():
    assert not scan(scanner_with(make_region(CLASS_ID, 0, 221, 256)), (220, 255))


def test_zero_cell_is_rejected():
    """(0,0) 一定要擋掉 —— [MEM-039] 就是被它通過驗證害的。"""
    assert not scan(scanner_with(make_region(CLASS_ID, 2870, 0, 0)), (0, 0))


def test_stale_actor_is_rejected():
    """殘留的實體：動作 tick 停住了（實機殘留物落後 5 秒~9 分鐘）。"""
    blob = make_region(CLASS_ID, 2870, 221, 256, tick=NOW_TICK - 30_000)
    assert not scan(scanner_with(blob), (220, 255))


def test_hot_region_is_remembered():
    """掃到怪的區段會被記成熱區段，之後每拍只掃那些（實測 4ms）。"""
    scanner = scanner_with(make_region(CLASS_ID, 2870, 221, 256))
    scan(scanner, (220, 255))
    assert scanner._hot  # noqa: SLF001


# ---- 快路徑：記住位址，之後只讀那個位址 ------------------------------------


def _one_struct(gid=777, class_id=1002, x=100, y=100, tick=NOW_TICK,
                vtable=0x0110_0000, path_begin=0, path_index=-1):
    """組一筆「一隻怪」的記憶體內容（vtable 一路到動作 tick）。"""
    buf = bytearray(actor.BLOCK)
    at = actor.HEAD
    struct.pack_into("<I", buf, at + actor.VTABLE, vtable)
    struct.pack_into("<i", buf, at + actor.CLASS, class_id)
    struct.pack_into("<I", buf, at + actor.GID, gid)
    struct.pack_into("<ii", buf, at + actor.DEST_X, x, y)
    struct.pack_into("<I", buf, at + actor.PATH_BEGIN, path_begin)
    struct.pack_into("<i", buf, at + actor.PATH_INDEX, path_index)
    struct.pack_into("<I", buf, at + actor.TICK, tick)
    return bytes(buf)


def _scanner_with(monkeypatch, payload):
    scanner = ent.EntityScanner(terrain(), MAP, view=30)
    monkeypatch.setattr(scanner._scanner, "read_region", lambda _a, _s: payload)
    monkeypatch.setattr(scanner, "_lut", _always_true_lut())
    monkeypatch.setattr(ent.actor, "now_tick", lambda: NOW_TICK)
    return scanner


class _always_true_lut:  # noqa: N801 - 假的查表物件，不是型別
    def __getitem__(self, _i):
        return True


def test_read_one_parses_a_known_address(monkeypatch):
    """快路徑只讀 0x248 bytes，不掃記憶體 —— 這才是每一拍該做的事。"""
    scanner = _scanner_with(monkeypatch, _one_struct(x=100, y=100))
    got = scanner.read_one(0xDEAD0000, (100, 100))
    assert got is not None
    assert (got.gid, got.class_id, got.pos, got.addr) == (777, 1002, (100, 100), 0xDEAD0000)


def test_read_one_rejects_a_stale_actor(monkeypatch):
    """動作 tick 停了＝這塊記憶體已經不是世界上的東西了。"""
    scanner = _scanner_with(monkeypatch, _one_struct(tick=NOW_TICK - 30_000))
    assert scanner.read_one(0xDEAD0000, (100, 100)) is None


def test_read_one_rejects_a_recycled_block(monkeypatch):
    """vtable 不見了＝那塊記憶體被拿去放別的東西。"""
    scanner = _scanner_with(monkeypatch, _one_struct(vtable=0x1234))
    scanner._module = (0x0100_0000, 0x0200_0000)  # noqa: SLF001
    assert scanner.read_one(0xDEAD0000, (100, 100)) is None


def test_read_one_rejects_a_monster_that_walked_out_of_view(monkeypatch):
    scanner = _scanner_with(monkeypatch, _one_struct(x=100, y=100))
    assert scanner.read_one(0xDEAD0000, (200, 200)) is None


def test_read_known_keeps_addresses_of_monsters_that_walked_away(monkeypatch):
    """走出視野**不是**忘掉牠的理由 —— 位址還是好的，牠走回來就又看得到。

    舊版一走遠就從清單移除，於是要等背景輪掃好幾秒才找得回來。
    """
    scanner = _scanner_with(monkeypatch, _one_struct(gid=777, x=100, y=100))
    scanner._known = {777: 0xDEAD0000}  # noqa: SLF001
    assert scanner.read_known((200, 200)) == []
    assert scanner.known_count == 1, "只是走遠了，位址沒壞"


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


# ---- 屍體不算怪（封包對照：11 次死亡全中）----------------------------------


def _kill(blob: bytes, slot: int = _SLOT) -> bytes:
    """把一段記憶體裡那隻怪標成死的（`GID+0x1A0` = 1）。"""
    buf = bytearray(blob)
    struct.pack_into("<I", buf, slot * 4 + 4 + actor.DEAD, 1)
    return bytes(buf)


def test_a_corpse_is_rejected_by_the_scan():
    """⚠⚠ 屍體的 **GID 還在、座標也還在**，只有 `+0x1A0` 變成 1。

    伺服器的 `0x0080 type=1` 同一拍記憶體就翻好了（比封包還快）。
    少了這道就是走過去對著屍體送攻擊 —— 使用者說的「打空氣」。
    """
    blob = _kill(make_region(CLASS_ID, 2870, 221, 256))
    assert not scan(scanner_with(blob), (220, 255))


def test_a_corpse_is_rejected_on_the_fast_path(monkeypatch):
    dead = bytearray(_one_struct(gid=777))
    struct.pack_into("<I", dead, actor.HEAD + actor.DEAD, 1)
    scanner = _scanner_with(monkeypatch, bytes(dead))
    assert scanner.read_one(0xDEAD0000, (100, 100)) is None


def test_a_monster_that_left_view_is_rejected(monkeypatch):
    """離開視野時客戶端把 GID 寫成 0xFFFFFFFF（實測 8 次全部如此）——
    「GID 在合理範圍」那道驗證本來就擋得掉，這裡把它釘住。"""
    gone = bytearray(_one_struct(gid=777))
    struct.pack_into("<I", gone, actor.HEAD + actor.GID, actor.GID_GONE)
    scanner = _scanner_with(monkeypatch, bytes(gone))
    assert scanner.read_actor(0xDEAD0000) is None
