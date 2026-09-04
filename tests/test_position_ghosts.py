"""好幾個「還沒走過」的物件都帶著我們的 AID —— 記住幽靈，換圖後剔掉（[DAT-074]）。

實機（白狐，2026-09-05 00:57~01:02，AID 24940572）跨了七張圖，每次換圖都是：

    有 2 個「還沒走過」的元件都像是角色本人（['0x1fbedda0', '0x1fbee8d0']）
    —— 分不出來，先用進圖座標
    ...（3 秒後角色走了一步）角色移動元件定位於 0x310c5db0

那兩個位址從頭到尾沒變過、tick 一直在跳、終點永遠沒填。它們**不是**這張圖新配的
元件（新元件每張圖位址都不同），但光看形狀跟剛換圖的本人一模一樣。

分辨的依據不是形狀，是**時間**：本人認出來的那一刻（它說得出位置），其他
「還沒走過」形狀的物件都不是本人。把它們記住，換圖之後直接剔掉。
"""

from __future__ import annotations

import logging
import struct

from ro_toolbox.services import actor
from ro_toolbox.services import player_position as pp

AID = 24940572
GHOST_A = 0x1FBEDDA0
GHOST_B = 0x1FBEE8D0
REAL = 0x310C5DB0            # 第一張圖上走過路的本人
NEW = 0x32B149B0             # 換圖後新配的本人（還沒走過）
ENTRY = (49, 75)


def _blob(*, state=1, dest=(0, 2), index=-1, begin=0, end=0) -> bytes:
    buf = bytearray(pp.SPAN)
    struct.pack_into("<I", buf, 0, AID)
    struct.pack_into("<I", buf, pp.OFF_STATE, state)
    struct.pack_into("<ii", buf, pp.OFF_DEST_X, *dest)
    struct.pack_into("<i", buf, pp.OFF_PATH_INDEX, index)
    struct.pack_into("<II", buf, pp.OFF_PATH_BEGIN, begin, end)
    return bytes(buf)


UNMOVED = _blob()                          # 「還沒走過」的形狀
PLACED = _blob(dest=(48, 8))               # 說得出位置的本人
RECYCLED = _blob(state=0)                  # 被客戶端回收了


class _Scanner:
    """`blobs`: 位址 → 內容。所有候選的動作 tick 都在跳。"""

    def __init__(self, blobs: dict[int, bytes]) -> None:
        self.blobs = blobs

    def read_region(self, addr: int, size: int):
        blob = self.blobs.get(addr)
        if blob is not None:
            return blob[:size]
        base = addr - actor.TICK
        if base in self.blobs:
            return struct.pack("<I", (actor.now_tick() - 20) & 0xFFFF_FFFF)
        return None

    def regions(self, writable_only=True):     # noqa: ARG002 - 測試替身
        return []


def _locator(blobs: dict[int, bytes]) -> pp.PlayerPosition:
    loc = pp.PlayerPosition(_Scanner(blobs))
    loc._aid = AID
    loc._candidates = list(blobs)
    loc._last_full = loc._now()
    loc._entry_pos = lambda: ENTRY
    loc._on_map = lambda pos, map_name: True
    return loc


def _change_map(loc: pp.PlayerPosition, blobs: dict[int, bytes]) -> None:
    """換圖：候選重掃（測試裡直接塞），幽靈記憶要留著。"""
    loc.invalidate()
    loc._scanner.blobs = blobs
    loc._candidates = list(blobs)
    loc._last_full = loc._now()


def test_the_ghosts_are_learned_the_moment_the_real_component_is_recognised():
    loc = _locator({GHOST_A: UNMOVED, GHOST_B: UNMOVED, REAL: PLACED})
    assert loc._locate_component("valkyrie") is True
    assert loc.address == REAL
    assert loc._ghosts == {GHOST_A, GHOST_B}


def test_after_a_map_change_the_ghosts_are_skipped_and_the_new_component_is_bound(caplog):
    """★ 實機那一幕：換圖後三個「還沒走過」—— 兩個幽靈 ＋ 一個真的新元件。"""
    loc = _locator({GHOST_A: UNMOVED, GHOST_B: UNMOVED, REAL: PLACED})
    assert loc._locate_component("valkyrie") is True

    _change_map(loc, {GHOST_A: UNMOVED, GHOST_B: UNMOVED, REAL: RECYCLED, NEW: UNMOVED})
    with caplog.at_level("ERROR"):
        assert loc._locate_component("prontera") is True
    assert loc.address == NEW
    shouted = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not shouted, "認得出來就不准印「分不出來」"
    assert loc.read("prontera") == ENTRY and loc.live is False


def test_without_a_learned_history_two_unmoved_candidates_are_still_ambiguous(caplog):
    """第一次就撞到兩個（還沒機會學）—— 照舊安全退化：進圖座標，走一步再說。"""
    loc = _locator({GHOST_A: UNMOVED, NEW: UNMOVED})
    with caplog.at_level("ERROR"):
        assert loc._locate_component("prontera") is False
    assert loc.address is None
    assert any("分不出來" in r.getMessage() for r in caplog.records)


def test_a_recycled_ghost_drops_out_of_the_memory():
    loc = _locator({GHOST_A: UNMOVED, GHOST_B: UNMOVED, REAL: PLACED})
    assert loc._locate_component("valkyrie") is True
    _change_map(loc, {GHOST_A: UNMOVED, GHOST_B: RECYCLED, REAL: RECYCLED, NEW: UNMOVED})
    assert loc._locate_component("prontera") is True
    assert loc.address == NEW
    assert loc._ghosts == {GHOST_A}


def test_only_ghosts_left_means_the_new_component_is_not_there_yet():
    """換圖後新元件還沒配出來：只剩幽靈 → 當「還沒找到」，不准綁幽靈。"""
    loc = _locator({GHOST_A: UNMOVED, GHOST_B: UNMOVED, REAL: PLACED})
    assert loc._locate_component("valkyrie") is True
    _change_map(loc, {GHOST_A: UNMOVED, GHOST_B: UNMOVED, REAL: RECYCLED})
    assert loc._locate_component("prontera") is False
    assert loc.address is None
    assert loc.read("prontera") == ENTRY, "退回進圖座標（伺服器剛講的落點）"


def test_a_ghost_is_never_bound_even_when_it_is_the_only_unmoved_candidate():
    """⛔ 「只有一個候選」不等於「這個候選是對的」（[DAT-072] 的教訓，換一個地方再犯）。"""
    loc = _locator({GHOST_A: UNMOVED, REAL: PLACED})
    assert loc._locate_component("valkyrie") is True
    _change_map(loc, {GHOST_A: UNMOVED, REAL: RECYCLED})
    assert loc._locate_component("prontera") is False
    assert loc.address is None


def test_invalidate_keeps_the_ghosts_but_forget_drops_them():
    loc = _locator({GHOST_A: UNMOVED, REAL: PLACED})
    assert loc._locate_component("valkyrie") is True
    loc.invalidate()
    assert loc._ghosts == {GHOST_A}, "幽靈就是要跨圖才有用"
    loc.forget()
    assert loc._ghosts == set(), "換行程了，位址全部作廢"
