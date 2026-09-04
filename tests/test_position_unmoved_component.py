"""剛換圖、還沒走過的角色 —— 元件**一直都在**，是我們把它判掉了（[MEM-060]）。

使用者 2026-09-04：
「我不想再出現這個，最好是每次換地圖都會找不到座標」
「用 AOB 照理來說每次都查應該不會出現這問題」—— **他是對的。**

實機唯讀量了三隻角色（`GID == AID` 全掃，當場）：

    狐狐狸（走過路）  候選 139｜dest 合法 1 個  dest=(23,24)   tickAge=7ms
    狐狐狸2（走路中）候選 190｜dest 合法 1 個  dest=(245,52)  tickAge=16ms
    白狐（剛換圖）    候選  90｜dest 合法 0 個
                     0x2ef79b58 state=1 idx=-1 begin=0 **dest=(0,2)** tickAge=13ms

`+0x5C/+0x60` 是**移動終點**，角色在這張圖上還沒走過就沒被寫過。舊版拿它當
「是不是本人」的驗證條件，等於宣告「沒走過的角色一律不算本人」，症狀就是：

    16:06:08  伺服器說我被移到 aldebaran (197, 68)
    16:06:09  還沒找到角色的移動元件（49 個候選）
    16:06:39  已經 30 秒找不到角色的移動元件…一直出現代表遊戲改版

（另外量過：把元件前後 0x200 bytes 全掃，**(197,68) 一個欄位都沒有** ——
客戶端在角色走第一步之前真的不知道自己在哪。那時只有進圖座標算數。）

所以「這是不是我的元件」與「它知不知道自己在哪」是**兩個問題**。
認得出元件的好處是：不必每 0.3 秒重新全掃（0.6~0.8 秒一次），而且角色一走
第一步，**同一個位址**的終點欄位就有值了。
"""

from __future__ import annotations

import struct

from ro_toolbox.services import actor
from ro_toolbox.services import player_position as pp

AID = 24940572
ADDR = 0x2EF79B58
ENTRY = (197, 68)          # 伺服器說「你被移到 aldebaran (197,68)」


def _component(*, state=1, dest=(0, 2), index=-1, begin=0, end=0) -> bytes:
    """做一份移動元件的位元組，欄位照 `services/actor.py` 的偏移擺。"""
    buf = bytearray(pp.SPAN)
    struct.pack_into("<I", buf, 0, AID)
    struct.pack_into("<I", buf, pp.OFF_STATE, state)
    struct.pack_into("<ii", buf, pp.OFF_DEST_X, *dest)
    struct.pack_into("<i", buf, pp.OFF_PATH_INDEX, index)
    struct.pack_into("<II", buf, pp.OFF_PATH_BEGIN, begin, end)
    return bytes(buf)


class _Scanner:
    """只回一塊記憶體的假掃描器。`ticking` 決定動作 tick 新不新鮮。"""

    def __init__(self, blob: bytes, *, ticking: bool = True) -> None:
        self.blob = blob
        self.ticking = ticking

    def read_region(self, addr: int, size: int):
        if addr == ADDR:
            return self.blob[:size]
        if addr == ADDR + actor.TICK:          # `_ticking()` 自己去讀的那 4 bytes
            now = actor.now_tick()
            age = 20 if self.ticking else actor.FRESH_MS * 5
            return struct.pack("<I", (now - age) & 0xFFFF_FFFF)
        return None

    def regions(self, writable_only=True):     # noqa: ARG002 - 測試替身
        return []


def _locator(blob: bytes, *, ticking: bool = True) -> pp.PlayerPosition:
    loc = pp.PlayerPosition(_Scanner(blob, ticking=ticking))
    loc._aid = AID
    loc._candidates = [ADDR]
    loc._last_full = loc._now()                # 別重新全掃
    loc._entry_pos = lambda: ENTRY
    loc._on_map = lambda pos, map_name: True
    return loc


def test_a_component_that_has_not_moved_yet_is_still_recognised_as_ours():
    """★★ 這是實機那一顆：`state=1`、`idx=-1`、`begin=0`、tick 還在跳，
    但 `dest=(0,2)`。它**就是本人**，只是還說不出自己在哪。"""
    loc = _locator(_component())
    assert loc._look_at(ADDR) == (True, None)
    assert loc._locate_component("aldebaran") is True
    assert loc.address == ADDR, "要綁住它，不然每 0.3 秒就重新全掃一次"


def test_the_position_falls_back_to_the_landing_cell_and_says_it_is_not_live():
    """位置由**進圖座標**回答（伺服器剛講過的落點），而且要老實說不是即時的。"""
    loc = _locator(_component())
    assert loc.read("aldebaran") == ENTRY
    assert loc.live is False, "角色跑了它也不會變，呼叫端不可以拿它當「有在動」"
    assert loc._moved_here is False, "還沒走過，進圖座標仍然可信"


def test_it_does_not_cry_that_the_game_was_patched():
    """⚠ 綁著元件就**不准**喊「30 秒找不到、可能是遊戲改版」——

    使用者看到那句話的意思是「工具壞了」，但這是換圖後的**正常狀態**。
    """
    loc = _locator(_component())
    for _ in range(5):
        assert loc.read("aldebaran") == ENTRY
    assert loc._missing_since is None
    assert loc._warned_missing is False


def test_the_first_step_turns_the_same_address_into_live_coordinates():
    """走第一步之後終點欄位就有值 —— **同一個位址**直接變即時座標，不必重掃。"""
    loc = _locator(_component())
    assert loc.read("aldebaran") == ENTRY and loc.live is False
    loc._scanner.blob = _component(dest=(199, 70))      # 角色動了
    assert loc.read("aldebaran") == (199, 70)
    assert loc.live is True
    assert loc.address == ADDR, "位址沒變過"
    assert loc._moved_here is True, "動過了 —— 從現在起不准再退回進圖座標"


def test_a_recycled_component_is_still_rejected():
    """⛔ 被回收的元件 `state` 是 0 —— 放寬的是「終點沒填」，不是「狀態是 0」。"""
    loc = _locator(_component(state=0))
    assert loc._look_at(ADDR) == (False, None)


def test_a_leftover_with_a_stopped_tick_is_still_rejected():
    """⛔ 「還沒走過」這個形狀**一定要配 tick 還在跳** ——

    不然任何一塊 `state` 剛好合法、終點是垃圾的殘留物都會被當成本人。
    """
    loc = _locator(_component(), ticking=False)
    assert loc._look_at(ADDR) == (False, None)


def test_a_half_written_path_is_not_the_unmoved_shape():
    """⛔ 有路徑陣列就不是「還沒走過」—— 那是別的東西，照舊拒絕。"""
    loc = _locator(_component(index=0, begin=0x1000, end=0x1010))
    assert loc._look_at(ADDR) == (False, None)
