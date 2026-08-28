"""角色狀態讀取測試。

不需要遊戲的部分測資料模型與驗證邏輯；需要遊戲的部分在找不到
Ragexe.exe 時自動跳過。
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("numpy")

from ro_toolbox.services.character import (  # noqa: E402
    CharacterReader,
    CharacterStatus,
    _plausible,
    _until_null,
)
from ro_toolbox.services.signatures import CHAR_STATUS, STATUS_OFFSETS  # noqa: E402


def make(**kwargs) -> CharacterStatus:
    # 名字要有值：`_plausible` 要求非空（空名字＝選角畫面的殘留，不是真角色）。
    values = dict(
        name="測試角色", hp=100, max_hp=200, sp=30, max_sp=60,
        base_level=50, job_level=40,
    )
    values.update(kwargs)
    return CharacterStatus(**values)


def test_percentages():
    status = make(hp=100, max_hp=200, sp=30, max_sp=60)
    assert status.hp_percent == 50.0
    assert status.sp_percent == 50.0


def test_percentages_handle_zero_max():
    assert make(max_hp=0).hp_percent == 0.0
    assert make(max_sp=0).sp_percent == 0.0


def test_exp_percent_matches_game_display():
    """實機對照：8961/12986 = 69.01%，遊戲畫面無條件捨去顯示 69.0%。"""
    status = make(base_exp=8961, base_exp_next=12986, job_exp=2907, job_exp_next=8920)
    assert status.has_exp
    assert round(status.base_percent, 2) == 69.01
    assert round(status.job_percent, 2) == 32.59


def test_exp_missing_is_reported_not_faked():
    """讀不到經驗值就說讀不到，不要回 0% 讓人以為真的是 0。"""
    assert not make().has_exp
    assert make().base_percent == 0.0


def test_max_level_sentinel():
    """滿級時伺服器塞哨兵大數當門檻（實測商狐 Job 50 讀到 999999999999999999）。"""
    status = make(job_exp=1426, job_exp_next=999_999_999_999_999_999,
                  base_exp=1, base_exp_next=100)
    assert status.job_maxed
    assert status.job_percent == 100.0
    assert not status.base_maxed


def test_plausible_rejects_absurd_exp():
    assert not _plausible(make(base_exp=10**12, base_exp_next=100))


def test_exp_offsets_are_int64_and_ordered():
    """四個經驗欄位是連續的 int64，順序：Base經驗, Base門檻, Job門檻, Job經驗。"""
    offsets = [
        STATUS_OFFSETS.base_exp,
        STATUS_OFFSETS.base_exp_next,
        STATUS_OFFSETS.job_exp_next,
        STATUS_OFFSETS.job_exp,
    ]
    assert offsets == sorted(offsets)
    assert all(b - a == 8 for a, b in zip(offsets, offsets[1:], strict=False))
    assert STATUS_OFFSETS.job_exp + 8 == STATUS_OFFSETS.base_level


def test_until_null_truncates():
    assert _until_null(bytes([65, 66, 0, 67])) == b"AB"


def test_until_null_without_null():
    assert _until_null(b"AB") == b"AB"


@pytest.mark.parametrize(
    "bad",
    [
        dict(base_level=0),
        dict(job_level=0),
        dict(base_level=1000),
        dict(hp=-1),
        dict(hp=99_999_999),       # 離譜的值＝定位跑掉了
        dict(max_hp=0),
    ],
)
def test_plausible_rejects_bad_values(bad):
    assert _plausible(make(**bad)) is False


def test_plausible_accepts_normal():
    assert _plausible(make()) is True


def test_levelling_up_is_not_a_broken_signature():
    """升等那一拍客戶端先更新 HP、還沒更新 maxHP —— 會讀到 hp > max_hp。

    實測 `HP 1274/914`（狐狐狸升到 Base 40 的瞬間）。那是真的角色，
    以前會被判成「定位已失效」而噴警告（使用者實測回報）。
    `_plausible()` 要認的是**定位跑掉**，而定位跑掉給的是離譜數字，
    那由上下限擋住 —— 不該用 hp <= max_hp 去認。
    """
    assert _plausible(make(hp=1274, max_hp=914)) is True
    assert _plausible(make(sp=130, max_sp=100)) is True


def test_signature_offsets_are_documented():
    """偏移是三角色交叉比對出來的，改動要同步更新 GAMEDATA [MEM-003]。"""
    assert STATUS_OFFSETS.hp == 0x00
    assert STATUS_OFFSETS.max_hp == 0x04
    assert STATUS_OFFSETS.sp == 0x08
    assert STATUS_OFFSETS.max_sp == 0x0C
    assert STATUS_OFFSETS.base_level == -0x3B58
    assert STATUS_OFFSETS.job_level == -0x3B50
    assert STATUS_OFFSETS.name == 0x2800


def test_signature_has_wildcards():
    """特徵不准把答案寫死，必須留萬用字元（見 CLAUDE.md）。"""
    _pattern, mask = CHAR_STATUS.parse()
    assert 0 in mask, "特徵沒有任何 ?? 位元組，可能把變動值寫死了"
    assert CHAR_STATUS.value_offset == 0x20


@pytest.mark.skipif(sys.platform != "win32", reason="只支援 Windows")
def test_reads_running_game_if_present():
    from ro_toolbox.services import window_list
    from ro_toolbox.services.ro_capture import find_server

    targets = [
        w for w in window_list.enumerate_windows()
        if w.process_name.lower() == "ragexe.exe"
    ]
    if not targets:
        pytest.skip("沒有執行中的 Ragexe.exe")

    # 「有遊戲在跑」不等於「已經登入」。停在登入畫面時角色結構還沒建立，
    # AOB 當然定位不到 —— 那不是特徵壞了，是還沒登入。有沒有登入一律問連線，
    # 不能看記憶體（GAMEDATA [MEM-029]、[PKT-044]）。
    targets = [w for w in targets if find_server(w.pid) is not None]
    if not targets:
        pytest.skip("Ragexe 在跑但都還沒登入（停在登入畫面）")

    reader = CharacterReader()
    try:
        if not reader.attach(targets[0].pid):
            # 「有連線」不等於「已經進到遊戲畫面」：停在**選角畫面**時
            # 連線是有的（char server），但角色結構還沒建立，AOB 當然定位不到。
            # 網路層分不出選角與遊戲中，所以這裡只能跳過，不能當成特徵壞了。
            pytest.skip("連線在但角色結構還沒建立（多半停在選角畫面）")
        status = reader.read()
        if status is None:
            # 定位到了但驗不過（多半是選角畫面的殘留：數值合理、名字空白）。
            pytest.skip("讀到的不是真的角色狀態（多半停在選角畫面）")
        assert status.name, "沒讀到角色名"
        assert status.base_level >= 1
        assert status.max_hp > 0
    finally:
        reader.close()


def test_position_is_not_a_hardcoded_offset_any_more():
    """座標**不准**再用「相對 HP 全域的固定距離」推導，見 GAMEDATA [MEM-039]。

    2026-08-26 改版時兩個全域移動幅度不同（+0x60D8 vs +0x60B8），那條推導就斷了，
    而且斷得很安靜：舊位址指到一片 0，(0,0) 通過了當時的合理性檢查。
    現在改用程式碼特徵定位（POSITION_X_SIGS / POSITION_Y_SIGS）。
    """
    assert not hasattr(STATUS_OFFSETS, "position")


def test_position_signatures_are_gone_on_purpose():
    """座標**不再**用任何程式碼特徵定位 —— 見 GAMEDATA [MEM-047]。

    舊的 `POSITION_X/Y_SIGS` 技術上完全正確（1 處命中、四個立即值互驗、y=x+4），
    但它錨到的是**小地圖標記**的全域：沒有小地圖圖檔的地圖（1082 張裡有 396 張，
    室內圖幾乎全中）上那段程式碼根本不會執行，於是全域停在上一張圖的座標，
    `position_located` 卻還是 True。留著它就等於留著一個「很有自信的錯值」。
    """
    from ro_toolbox.services import signatures

    for name in ("POSITION_X_SIGS", "POSITION_Y_SIGS", "POSITION_XY_GAP"):
        assert not hasattr(signatures, name), f"{name} 應該已經刪掉"


# ---- 座標：移動元件（`GID == AID`）＋ 進圖座標全域 -----------------------


class FakeMemory:
    """假的記憶體：位址 → bytes。只實作 PlayerPosition 用得到的兩個方法。"""

    def __init__(self, blocks=None, regions=((0x1000, 0x1000),)):
        self.blocks = dict(blocks or {})
        self._regions = list(regions)

    def regions(self, writable_only=True):  # noqa: ARG002
        return list(self._regions)

    def read_region(self, addr, size):
        for base, blob in self.blocks.items():
            if base <= addr and addr + size <= base + len(blob):
                return blob[addr - base : addr - base + size]
        return None


#: 進圖座標全域放在假記憶體的這個位址（真的位址是程式碼特徵定位出來的）
ENTRY_ADDR = 0xE000
#: 重找的冷卻時間過了多久（比 RELOCATE_COOLDOWN 大就好）
RELOCATE_GAP = 5.0
#: ⚠ 這比 FULL_RESCAN_SEC 還大，會觸發全掃 —— 只想測「重驗候選」的用 0.5


def _component(aid, dest, path=(), index=-1, path_at=0x9000, state=1):
    """組一塊移動元件：GID、狀態、終點、路徑陣列指標、路徑索引。

    `state=0` 代表被回收的殘留元件（實測四塊殘留的這個欄位全是 0）。
    """
    from ro_toolbox.services import player_position as pp

    buf = bytearray(pp.SPAN)
    buf[0:4] = aid.to_bytes(4, "little")
    buf[pp.OFF_STATE : pp.OFF_STATE + 4] = state.to_bytes(4, "little")
    buf[pp.OFF_DEST_X : pp.OFF_DEST_X + 4] = dest[0].to_bytes(4, "little", signed=True)
    buf[pp.OFF_DEST_Y : pp.OFF_DEST_Y + 4] = dest[1].to_bytes(4, "little", signed=True)
    begin = path_at if path else 0
    end = begin + len(path) * pp.PATH_STRIDE
    buf[pp.OFF_PATH_BEGIN : pp.OFF_PATH_BEGIN + 4] = begin.to_bytes(4, "little")
    buf[pp.OFF_PATH_END : pp.OFF_PATH_END + 4] = end.to_bytes(4, "little")
    buf[pp.OFF_PATH_INDEX : pp.OFF_PATH_INDEX + 4] = index.to_bytes(
        4, "little", signed=True
    )
    nodes = bytearray()
    for x, y in path:
        node = bytearray(pp.PATH_STRIDE)
        node[0:4] = x.to_bytes(4, "little", signed=True)
        node[4:8] = y.to_bytes(4, "little", signed=True)
        nodes += node
    return bytes(buf), bytes(nodes)


def _entry_blob(cell):
    return cell[0].to_bytes(4, "little", signed=True) + cell[1].to_bytes(
        4, "little", signed=True
    )


def _position_at(addr, aid=777, entry=None, **kw):
    """做一個已經定位好的 PlayerPosition（跳過掃描與程式碼特徵）。"""
    from ro_toolbox.services.player_position import PlayerPosition

    body, nodes = _component(aid, **kw)
    blocks = {addr: body, kw.get("path_at", 0x9000): nodes}
    if entry is not None:
        blocks[ENTRY_ADDR] = _entry_blob(entry)
    mem = FakeMemory(blocks)
    pos = PlayerPosition(mem)
    pos._aid = aid
    pos._addr = addr
    pos._entry = ENTRY_ADDR if entry is not None else None
    return pos, mem


def test_standing_still_reads_the_destination_field():
    """沒在走的時候（索引 -1）終點欄位就是目前所在格。"""
    pos, _mem = _position_at(0x5000, dest=(65, 99))
    assert pos.read() == (65, 99)


def test_walking_reads_the_path_node_not_the_destination():
    """走路途中終點欄位是**要去哪**，目前在哪要看路徑索引。

    實機 2026-08-28：送出 (65,92)→(65,104) 之後終點欄位 0.03 秒就變成 (65,104)，
    而角色還在 (65,92)。拿終點當位置的話，A* 會從一個還沒到的地方起算。
    """
    path = [(65, 92), (65, 93), (65, 94), (65, 95)]
    pos, _mem = _position_at(0x5000, dest=(65, 95), path=path, index=1, state=2)
    assert pos.read() == (65, 93)


def test_recycled_component_is_rejected():
    """殘留的舊元件：GID 還在、座標也還在，只有狀態欄位被清成 0。

    實測四塊殘留元件（(110,182)／(249,42)／(198,205)／(65,84)，第一個就是
    換圖前 izlude 的位置）全部 `+0x38 == 0`，活的是 1（站著）或 2（走路）。
    這正是 [MEM-047] 那個「很有自信的錯值」的來源。
    """
    pos, _mem = _position_at(0x5000, dest=(112, 181), state=0)
    assert pos.read() is None


def test_falls_back_to_the_map_entry_position():
    """剛換圖、還沒走過路：移動元件根本不存在，答案在進圖座標全域。

    實機驗證過：換圖之後 60+ 個 `GID == AID` 的候選沒有一個通得過驗證
    （狀態是 0、路徑指標是 0、終點欄位是垃圾）。少了這個退化，
    走路類功能會在每次換圖之後整個停用。
    """
    pos, _mem = _position_at(0x5000, dest=(0, 0), state=0, entry=(112, 179))
    assert pos.read() == (112, 179)


def test_component_wins_over_the_map_entry_position():
    """走過之後就以移動元件為準 —— 進圖座標不會跟著走路更新。"""
    pos, _mem = _position_at(0x5000, dest=(65, 99), entry=(65, 87))
    assert pos.read() == (65, 99)


def test_zero_cell_is_rejected():
    """(0,0) 是定位失效的樣子，不是合法座標 —— 見 GAMEDATA [MEM-039]。

    0 是地圖邊界，任何地圖上都不可走。當年就是 (0,0) 通過了檢查，
    自動打怪拿它當 A* 起點走去地圖角落，全程不報錯。
    """
    pos, _mem = _position_at(0x5000, dest=(0, 0))
    assert pos.read() is None


def test_out_of_range_cell_is_rejected():
    """RO 沒有超過 512x512 的地圖。"""
    pos, _mem = _position_at(0x5000, dest=(999, 999))
    assert pos.read() is None


def test_state_field_out_of_range_is_rejected():
    """堆積垃圾在這個位置常常是指標或很大的數字。"""
    pos, _mem = _position_at(0x5000, dest=(65, 99), state=1_000_000)
    assert pos.read() is None


def test_path_index_out_of_range_is_rejected():
    """索引超出路徑陣列＝解錯了，不准硬讀那塊記憶體。"""
    pos, _mem = _position_at(0x5000, dest=(65, 99), path=[(65, 92)], index=7)
    assert pos.read() is None


def test_gid_change_means_the_component_was_recycled():
    """那塊記憶體換人住了 —— 立刻回 None，不准繼續回舊座標。"""
    pos, _mem = _position_at(0x5000, dest=(65, 99))
    pos._aid = 888  # 元件上的 GID 還是 777
    assert pos.read() is None


def test_a_cell_that_is_not_on_this_map_is_rejected():
    """殘留座標最有效的一關：**這一格在這張圖上站得住嗎**。

    實機 izlude → izlude_in：兩張都是 200×200，殘留的 (112,181) 範圍內合法，
    卻落在牆裡（izlude_in 只有 7.9% 可走）。判準跟 `Traveler._settle()` 一樣，
    兩層用不同判準的縫就是 [PKT-078] 卡住的地方。
    """
    from ro_toolbox.services import player_position as pp

    class Wall:
        width = height = 200

        @staticmethod
        def is_walkable(x, y):
            return (x, y) == (65, 87)

    pos, _mem = _position_at(0x5000, dest=(112, 181), entry=(65, 87))
    pos._terrain_map = "izlude_in"
    pos._terrain = Wall()
    assert pp.START_SNAP < 10, "這個測試假設 START_SNAP 遠小於兩點的距離"
    assert pos.read("izlude_in") == (65, 87)


def test_locate_picks_the_live_component_out_of_the_ghosts():
    """殘留元件 GID 一樣，靠狀態欄位分辨（[MEM-041]：命中多個 ≠ 方法壞了）。"""
    from ro_toolbox.services.player_position import PlayerPosition

    aid = 777
    live, nodes = _component(aid, dest=(65, 99))
    ghost, _ = _component(aid, dest=(112, 181), state=0)
    key = aid.to_bytes(4, "little")
    region = bytearray(0x1000)
    region[0x100 : 0x100 + len(ghost)] = ghost
    region[0x600 : 0x600 + len(live)] = live
    mem = FakeMemory({0x1000: bytes(region), 0x9000: nodes}, regions=[(0x1000, 0x1000)])
    assert region.count(key) >= 2, "兩塊都要含有 GID，才是真的在考驗驗證"

    pos = PlayerPosition(mem)
    pos._aid = aid
    assert pos._locate_component() is True
    assert pos.address == 0x1600
    assert pos.read() == (65, 99)


def test_two_live_components_fall_back_instead_of_guessing():
    """驗完還是不只一個＝真的分不出來。不准賭 —— 改用進圖座標。"""
    from ro_toolbox.services.player_position import PlayerPosition

    aid = 777
    one, nodes = _component(aid, dest=(65, 99))
    two, _ = _component(aid, dest=(30, 40))
    region = bytearray(0x1000)
    region[0x100 : 0x100 + len(one)] = one
    region[0x600 : 0x600 + len(two)] = two
    mem = FakeMemory(
        {0x1000: bytes(region), 0x9000: nodes, ENTRY_ADDR: _entry_blob((7, 8))},
        regions=[(0x1000, 0x1000)],
    )
    pos = PlayerPosition(mem)
    pos._aid = aid
    pos._entry = ENTRY_ADDR
    assert pos._locate_component() is False
    assert pos.address is None
    assert pos.read() == (7, 8)


def _one_region_memory(aid=777, dest=(65, 99)):
    live, nodes = _component(aid, dest=dest)
    region = bytearray(0x1000)
    region[0x600 : 0x600 + len(live)] = live
    return FakeMemory(
        {0x1000: bytes(region), 0x9000: nodes}, regions=[(0x1000, 0x1000)]
    )


def test_read_relocates_after_the_component_dies_but_not_every_tick():
    """位址失效要重找，但**不准每一拍都重找**（全掃一趟 0.7~0.8 秒，bot 會定格）。"""
    from ro_toolbox.services.player_position import PlayerPosition

    mem = _one_region_memory()
    clock = [100.0]
    pos = PlayerPosition(mem, now=lambda: clock[0])
    pos._aid = 777
    assert pos._locate_component() is True

    tries = []
    real = PlayerPosition._locate_component
    pos._locate_component = lambda: (tries.append(1), real(pos))[1]

    pos._addr = 0x4000  # 假裝元件被回收了（那個位址什麼都沒有）
    assert pos.read() is None          # 冷卻中：連找都不找
    assert tries == []

    clock[0] += 100
    assert pos.read() == (65, 99)      # 冷卻過了：重找並找回來
    assert len(tries) == 1


def test_relocating_only_revalidates_the_cached_candidates():
    """全掃只做一次：元件在**走第一步之前**就已經帶著 `GID == AID` 了。

    ⚠ 第一版是「只掃上次有命中的區段、全掃 15 秒一次」，實機一換圖就中招：
      新元件配在冷區段裡 → 整整 15 秒都讀進圖座標 → 角色明明在走，
      travel_bot 卻判定「一步都沒動、可能是背包太重」把趕路停掉。
      便宜的快取不能拿正確性去換。
    """
    from ro_toolbox.services.player_position import PlayerPosition

    aid = 777
    # 還沒走過路的元件：GID 在，但狀態欄位是 0（驗不過）
    asleep, nodes = _component(aid, dest=(0, 0), state=0)
    region = bytearray(0x1000)
    region[0x600 : 0x600 + len(asleep)] = asleep
    mem = FakeMemory({0x1000: bytes(region), 0x9000: nodes}, regions=[(0x1000, 0x1000)])

    clock = [1000.0]
    pos = PlayerPosition(mem, now=lambda: clock[0])
    pos._aid = aid
    assert pos._locate_component() is False, "狀態是 0，還驗不過"
    assert pos._candidates == [0x1600], "候選要記起來（GID 已經在那裡了）"

    full = []
    real_regions = mem.regions
    mem.regions = lambda writable_only=True: (full.append(1), real_regions())[1]

    # 角色走了第一步 → 同一塊記憶體填上狀態與終點，重驗候選就接上了
    live, _ = _component(aid, dest=(65, 99), state=1)
    region[0x600 : 0x600 + len(live)] = live
    mem.blocks[0x1000] = bytes(region)
    pos._addr = None
    clock[0] += 0.5          # 過了重驗的冷卻，但遠不到全掃的間隔
    assert pos.read() == (65, 99)
    assert full == [], "不必再全掃一次記憶體"


def test_changing_map_throws_the_candidates_away_too():
    """換圖之後客戶端會另外配一個新元件，舊的候選清單裡沒有它。"""
    pos, _mem = _position_at(0x5000, dest=(65, 99))
    pos._candidates = [0x5000]
    pos.invalidate()
    assert pos._candidates == [], "候選也要丟，不然新元件永遠找不到"


def test_invalidate_forces_a_relocate():
    """換地圖一定要重找：舊元件會被回收，而**回收不等於清乾淨**。"""
    pos, _mem = _position_at(0x5000, dest=(65, 99))
    assert pos.read() == (65, 99)
    pos.invalidate()
    assert pos.address is None


def test_map_entry_signatures_cross_check_each_other():
    """兩條骨架必須互相獨立，而且都不准把答案寫死。"""
    from ro_toolbox.services.signatures import (
        MAP_ENTRY_X_SIGS,
        MAP_ENTRY_XY_GAP,
        MAP_ENTRY_Y_SIGS,
    )

    assert len(MAP_ENTRY_X_SIGS) == 2 and len(MAP_ENTRY_Y_SIGS) == 2
    patterns = {sig.pattern for sig in MAP_ENTRY_X_SIGS}
    assert len(patterns) == 2, "兩條要是不同的骨架才算互相驗證"
    assert patterns == {sig.pattern for sig in MAP_ENTRY_Y_SIGS}
    for sig in MAP_ENTRY_X_SIGS + MAP_ENTRY_Y_SIGS:
        assert "??" in sig.pattern, f"{sig.name} 把答案寫死了"
    # x 與 y 取的是**同一段程式碼裡不同的立即值**
    for xs, ys in zip(MAP_ENTRY_X_SIGS, MAP_ENTRY_Y_SIGS):
        assert xs.pattern == ys.pattern
        assert set(xs.operands).isdisjoint(ys.operands)
    assert MAP_ENTRY_XY_GAP == 4


def test_empty_name_is_not_a_real_character():
    """停在選角畫面時會讀到殘留結構：數值都合理，只有名字是空的。

    實測 2026-08-25：Base 54 / Job 54 / HP 54/54 / SP 0/0、名字空白 ——
    全部通過範圍檢查。少了名字這一條，自動掛機頁會拿它建出一個分頁，
    然後照著垃圾值算血量百分比（安靜地做錯事）。
    """
    assert _plausible(make(name="狐狐狸")) is True
    assert _plausible(make(name="")) is False
    assert _plausible(make(name="   ")) is False


def test_residual_char_select_values_are_rejected():
    """把實測到的那一組殘留值原封不動釘住。"""
    residual = make(
        name="", base_level=54, job_level=54, hp=54, max_hp=54, sp=0, max_sp=0
    )
    assert _plausible(residual) is False


# ---- 定位：命中多個時要先驗合理性，不是直接放棄 ---------------------------


def _fake_position(monkeypatch, ok=True, addr=0x5078D228, map_name="izlude"):
    """把「找角色實體」那一趟全掃換掉（單元測試沒有真的行程可以掃）。

    順便把 `_collect()` 與 `attached` 也換掉 —— 定位座標要先知道 AID，
    而 AID 是從角色結構讀出來的，沒有真的行程就讀不到。
    """
    from ro_toolbox.services.memory_scan import MemoryScanner
    from ro_toolbox.services.player_position import PlayerPosition

    def fake_locate(self, aid):
        self._aid = aid
        self._addr = addr if ok else None
        return ok

    monkeypatch.setattr(PlayerPosition, "locate", fake_locate)
    monkeypatch.setattr(
        PlayerPosition, "read", lambda self, m="": (65, 99) if ok else None,
    )
    monkeypatch.setattr(
        CharacterReader, "_collect",
        lambda self: make(name="狐狐狸", map_name=map_name),
    )
    monkeypatch.setattr(
        CharacterReader, "_read_text", lambda self, *a, **k: map_name,
    )
    monkeypatch.setattr(MemoryScanner, "attached", property(lambda self: True))


def _attachable(monkeypatch, hits, plausible_at):
    """準備一個只差 scan 結果的 reader（不真的開行程）。"""
    from ro_toolbox.services import character as mod

    reader = CharacterReader()
    monkeypatch.setattr(reader._scanner, "open", lambda _pid: None)
    monkeypatch.setattr(reader._scanner, "close", lambda: None)
    monkeypatch.setattr(mod, "scan", lambda *a, **k: list(hits))
    monkeypatch.setattr(
        CharacterReader,
        "probe",
        lambda self, base: make(name="狐狐狸") if base in plausible_at else None,
    )
    _fake_position(monkeypatch)
    return reader


def test_junk_hits_are_filtered_instead_of_giving_up(monkeypatch):
    """實測 2026-08-26：玩久了堆積裡會出現 5 個同樣位元組樣式的垃圾
    （HP 15、max_hp 42 億、名字與地圖都空的），真的角色也在裡面。

    舊版看到「不只一個」就直接放棄，症狀是「遊戲明明開著卻讀不到角色」。
    AOB 只是錨，分辨「這是不是角色」要靠數值本身。
    """
    reader = _attachable(
        monkeypatch,
        hits=[0x39871000, 0x15D7C98, 0x39872000, 0x39873000],
        plausible_at={0x15D7C98},
    )
    assert reader.attach(4321) is True
    assert reader._base == 0x15D7C98


def test_two_believable_characters_still_fail_loudly(monkeypatch):
    """驗證之後還是不只一個＝真的分不出來。不准賭，賭錯就是照別人的血量決策。"""
    reader = _attachable(
        monkeypatch,
        hits=[0x15D7C98, 0x16D7C98],
        plausible_at={0x15D7C98, 0x16D7C98},
    )
    assert reader.attach(4321) is False
    assert reader._base is None


def test_no_believable_hit_is_treated_as_not_in_game(monkeypatch):
    reader = _attachable(monkeypatch, hits=[0x39871000, 0x39872000], plausible_at=set())
    assert reader.attach(4321) is False
    assert reader._base is None


def test_single_hit_skips_the_probe(monkeypatch):
    """只有一個命中時不必額外驗 —— read() 本來就會驗，不要多掃一次記憶體。"""
    from ro_toolbox.services import character as mod

    reader = CharacterReader()
    monkeypatch.setattr(reader._scanner, "open", lambda _pid: None)
    monkeypatch.setattr(mod, "scan", lambda *a, **k: [0x15D7C98])
    _fake_position(monkeypatch)

    def boom(self, base):
        raise AssertionError("只有一個命中時不該呼叫 probe")

    monkeypatch.setattr(CharacterReader, "probe", boom)
    assert reader.attach(4321) is True
    assert reader._base == 0x15D7C98
    assert reader.position_located is True


def test_position_is_disabled_when_no_source_works(monkeypatch):
    """兩個來源都問不出來＝不知道人在哪。走路類功能要停用，不准空轉。

    ⚠ HP／等級照樣讀得到（那是另一個結構）—— attach 不該整個失敗。
    """
    from ro_toolbox.services import character as mod

    reader = CharacterReader()
    monkeypatch.setattr(reader._scanner, "open", lambda _pid: None)
    monkeypatch.setattr(mod, "scan", lambda *a, **k: [0x15D7C98])
    _fake_position(monkeypatch, ok=False)
    assert reader.attach(4321) is True  # HP／等級還是讀得到
    assert reader.position_located is False
    assert reader.read_position() is None


def test_changing_map_throws_the_component_address_away(monkeypatch):
    """換圖時記著的實體位址不能再信 —— [MEM-047] 那個「很有自信的錯值」
    就是舊實體被回收之後座標停在上一張圖造成的。"""
    from ro_toolbox.services import character as mod

    reader = CharacterReader()
    monkeypatch.setattr(reader._scanner, "open", lambda _pid: None)
    monkeypatch.setattr(mod, "scan", lambda *a, **k: [0x15D7C98])
    _fake_position(monkeypatch)
    maps = ["izlude", "izlude", "izlude_in"]
    monkeypatch.setattr(
        CharacterReader,
        "_collect",
        lambda self: make(name="狐狐狸", map_name=maps.pop(0) if maps else "izlude_in"),
    )
    assert reader.attach(4321) is True

    dropped = []
    monkeypatch.setattr(
        type(reader._position), "invalidate", lambda self: dropped.append(1)
    )
    reader.read()          # 還在 izlude
    assert dropped == []
    reader.read()          # 換到 izlude_in
    assert dropped == [1]
