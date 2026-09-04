"""換圖之後多久才讀得到座標。

使用者 2026-09-03：「常常換圖抓不到座標，這問題很嚴重要修好，
右上角地圖明明都及時會換，不能這樣對使用者體感太差」。

換圖那一刻有三件事各自發生、**順序不保證**：

1. 客戶端回收舊的移動元件      → `_component_pos()` 開始回 None
2. 伺服器 `0x0091` 寫進圖座標  → 右上角小地圖也是這一刻換的
3. 角色結構裡的地圖名變成新的

舊版把「換圖了」完全外包給第 3 步（`character._note_map()`）。只要 1 早於 3，
中間那段 `_moved_here` 還記著上一張圖，`read()` 就回 **None** ——
呼叫端看到「讀不到角色座標」，開始送移動去逼位置出來（實機 22:06:26 起
連送 657 個目標，而且那時候連線正在重綁、封包一個都沒接到）。
"""

from __future__ import annotations

from ro_toolbox.services import player_position as pp

OLD_MAP, NEW_MAP = "aldebaran", "mjolnir_12"
OLD_CELL, NEW_CELL = (140, 60), (199, 375)


class _Fake(pp.PlayerPosition):
    """只換掉「跟遊戲講話」的那幾支，決策邏輯全部照真的跑。"""

    def __init__(self) -> None:  # noqa: D107 - 測試替身
        super().__init__(scanner=None, now=lambda: self.clock)
        self.clock = 1000.0
        self.entry = OLD_CELL
        self.component = OLD_CELL      # None = 客戶端把元件回收了
        self._aid = 4242
        self._entry = 0x15D6C80        # 「進圖座標全域找到了」

    # --- 對遊戲的三個窗口 ---
    def _entry_pos(self):
        return self.entry

    def _component_pos(self):
        if self.component is None:
            self._addr = None
            return None
        self._addr = 0xB0000
        return self.component

    def _locate_component(self, map_name: str = "") -> None:
        self._last_locate = self.clock

    def _on_map(self, pos, map_name) -> bool:
        # 兩張圖各自只認自己的那一格（＝真的「站得住」驗證的效果）
        return pos == (NEW_CELL if map_name == NEW_MAP else OLD_CELL)


def _walked_around_on_the_old_map() -> _Fake:
    loc = _Fake()
    assert loc.read(OLD_MAP) == OLD_CELL
    assert loc.live is True
    assert loc._moved_here is True, "在這張圖上讀到過元件"
    return loc


def test_the_entry_position_covers_the_gap_before_the_map_name_flips():
    """★★ 元件先被回收、地圖名還沒翻 —— 這一段不准回 None。

    偵測靠的是**進圖座標全域變了**：那個全域只有 `0x0091` 會寫，
    也就是右上角小地圖換掉的同一刻。不需要等角色結構、也不需要接到封包。
    """
    loc = _walked_around_on_the_old_map()

    # 換圖了：伺服器寫了新的進圖座標、客戶端回收了舊元件，
    # 但角色結構裡的地圖名**還是舊的那張**。
    loc.entry = NEW_CELL
    loc.component = None
    loc.clock += 0.1

    # 地圖名還沒翻的那一拍：新的落點在**舊圖**上站不住 → 回 None 是對的
    # （不准把新圖的格子當成舊圖的位置回出去）。
    assert loc.read(OLD_MAP) is None
    # 但「動過了」必須在這一拍就被清掉 —— 不然會一路卡到地圖名翻為止，
    # 那段時間呼叫端只能看到「讀不到角色座標」。
    assert loc._moved_here is False, "進圖座標一變就該把「動過了」清掉"

    loc.clock += 0.1
    assert loc.read(NEW_MAP) == NEW_CELL, "地圖名一翻就要馬上有座標"
    assert loc.live is False, "這是進圖座標，不是即時的"


def test_it_does_not_wait_for_the_caller_to_say_the_map_changed():
    """⚠ 呼叫端沒呼叫 `invalidate()` 也要自己發現。"""
    loc = _walked_around_on_the_old_map()
    loc.entry = NEW_CELL
    loc.component = None
    loc.clock += 0.1

    assert loc.read(NEW_MAP) == NEW_CELL


def test_a_same_map_teleport_also_counts_as_being_moved():
    """同圖傳點也會寫進圖座標 —— 那**也**是「被移動了」，該重找元件。"""
    loc = _walked_around_on_the_old_map()
    moved_to = (10, 10)
    loc.entry = moved_to
    loc.component = None
    loc._on_map = lambda pos, m: pos in (OLD_CELL, moved_to)
    loc.clock += 0.1

    assert loc.read(OLD_MAP) == moved_to
    assert loc._moved_here is False


def test_a_lost_component_on_the_same_map_still_returns_none():
    """⛔ 保險不可以變成「換一個地方重演 [MEM-047]」。

    進圖座標**沒變**（沒有被移動過）而元件不見了 —— 那是角色在這張圖上走過、
    元件被回收，進圖座標早就過期。這種一律回 None，不准拿它頂替。
    """
    loc = _walked_around_on_the_old_map()
    loc.component = None
    loc.clock += 0.1

    assert loc.read(OLD_MAP) is None


def test_the_live_component_always_wins():
    """元件讀得到就用元件 —— 進圖座標只是空窗期的替補。"""
    loc = _walked_around_on_the_old_map()
    loc.entry = NEW_CELL
    loc.component = NEW_CELL
    loc.clock += 0.1

    assert loc.read(NEW_MAP) == NEW_CELL
    assert loc.live is True
