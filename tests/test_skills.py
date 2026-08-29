"""從記憶體讀技能表（用合成的記憶體，不需要遊戲）。

真正的判別是「英文代號字串反查到的 ID 必須等於結構裡的 ID」，所以這裡的
假記憶體用的是 `assets/skills.json.gz` 裡**真的**代號與 MaxLv —— 用假資料
測不到那條交叉驗證。
"""

from __future__ import annotations

import logging
import struct

import pytest

from ro_toolbox.services import skills as skills_mod
from ro_toolbox.services.gamedata import skill_table
from ro_toolbox.services.skills import FULL_RESCAN_SEC, SkillReader

REGION = 0x10000000
REGION_SIZE = 0x1000
STRINGS = 0x20000000
#: 結構之間留空，確保掃描不是靠「剛好連在一起」才找得到。
STRIDE = 0x60
FIRST = 0x100


def _maxlv(skill_id: int) -> int:
    entry = skill_table()[skill_id]
    return int(entry["maxlv"])


def _key(skill_id: int) -> str:
    return skill_table()[skill_id]["key"]


def _node(skill_id: int, level: int, sp: int, name_ptr: int, *,
          maxlv: int | None = None, tail: int = 0, inf: int = 4) -> bytes:
    """一個技能結構（版面見 services/skills.py 開頭）。

    `tail` 塞進 `+0x14` —— 那個欄位意義未解，實機出現過 2 和 4。
    `inf` 是 `+0x04` 的目標型態，分類會用到。
    """
    out = bytearray(0x24)
    struct.pack_into("<I", out, 0x00, skill_id)
    struct.pack_into("<I", out, 0x04, inf)
    struct.pack_into("<I", out, 0x08, level)
    struct.pack_into("<I", out, 0x0C, sp)
    struct.pack_into("<I", out, 0x14, tail)
    struct.pack_into("<I", out, 0x18, name_ptr)
    struct.pack_into("<I", out, 0x20, _maxlv(skill_id) if maxlv is None else maxlv)
    return bytes(out)


class FakeScanner:
    """一塊可寫記憶體 ＋ 一批字串。只實作 SkillReader 會用到的方法。"""

    def __init__(self) -> None:
        self.region = bytearray(REGION_SIZE)
        self.strings: dict[int, bytes] = {}
        self._next_string = STRINGS
        self._slot = 0
        self.closed = False
        #: 掃過幾次整塊記憶體。快路徑不該讓這個數字增加。
        self.full_scans = 0

    # ---- 佈置 ----

    def put_string(self, text: str) -> int:
        addr = self._next_string
        self.strings[addr] = text.encode("ascii") + b"\0"
        self._next_string += 0x40      # 保持 4 對齊，跟真的字串池一樣
        return addr

    def add_skill(self, skill_id: int, level: int, sp: int, *,
                  key: str | None = None, maxlv: int | None = None,
                  tail: int = 0, inf: int = 4) -> int:
        """放一個技能結構，回傳它在記憶體裡的位址。"""
        pointer = self.put_string(_key(skill_id) if key is None else key)
        offset = FIRST + self._slot * STRIDE
        self._slot += 1
        blob = _node(skill_id, level, sp, pointer, maxlv=maxlv, tail=tail, inf=inf)
        self.region[offset:offset + len(blob)] = blob
        return REGION + offset

    # ---- MemoryScanner 介面 ----

    def open(self, pid):  # noqa: ARG002
        return None

    def close(self) -> None:
        self.closed = True

    def _iter_regions(self, writable_only=True):  # noqa: ARG002
        return [(REGION, REGION_SIZE)]

    def _read_region(self, base, size):
        self.full_scans += 1
        if base != REGION:
            return None
        return bytes(self.region[:size])

    def _read_bytes(self, addr, size):
        for start, data in self.strings.items():
            if start <= addr < start + len(data):
                return data[addr - start:addr - start + size]
        if REGION <= addr < REGION + REGION_SIZE:
            return bytes(self.region[addr - REGION:addr - REGION + size])
        return None


class OnlineCharacter:
    """假的角色狀態讀取器：有角色在場上。

    `attaches` 數的是「跑了幾次 AOB 定位」—— 真的那一個要 1~2 秒，
    5 秒一輪的刷新每次都重跑就等於白做快路徑。
    """

    attaches = 0

    def attach(self, pid):  # noqa: ARG002
        type(self).attaches += 1
        return True

    def read(self):
        return object()

    def close(self) -> None:
        return None


class OfflineCharacter(OnlineCharacter):
    """停在選角／登入畫面：連線還在，但角色狀態定位不到。"""

    def attach(self, pid):  # noqa: ARG002
        return False


@pytest.fixture(autouse=True)
def _character_online(monkeypatch):
    """預設「有角色在場上」，否則每條測試都會卡在線上檢查。"""
    OnlineCharacter.attaches = 0
    monkeypatch.setattr(skills_mod, "CharacterReader", OnlineCharacter)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def reader(clock):
    scanner = FakeScanner()
    reader = SkillReader(scanner, now=clock)
    assert reader.attach(1234)
    return reader, scanner


def test_reads_skills_with_levels(reader):
    r, scanner = reader
    scanner.add_skill(5, 10, 15)      # SM_BASH
    scanner.add_skill(60, 7, 38)      # KN_TWOHANDQUICKEN
    scanner.add_skill(61, 0, 3)       # KN_AUTOCOUNTER（學得起但沒點）

    found = r.read()
    assert found is not None
    assert [s.id for s in found] == [5, 60, 61]
    bash, quicken, counter = found
    assert (bash.key, bash.level, bash.sp) == ("SM_BASH", 10, 15)
    assert bash.name and not bash.name.startswith("#")
    assert bash.max_level == _maxlv(5)
    assert quicken.learned and not counter.learned


def test_classifies_active_buff_and_passive(reader):
    """分類要跟遊戲自己的說明一致，不能靠猜。

    - SM_BASH「類型 : 近距離物理」→ 打怪型
    - SM_MAGNUM「類型 : 近距離物理，Buff」→ **打怪型**（怒爆是攻擊技能，
      比對順序要讓攻擊字樣先中，否則會被 buff 搶走）
    - KN_TWOHANDQUICKEN「類型 : Buff」→ 補助型
    - `inf == 0` → 被動，比資料表的分類更權威
    """
    r, scanner = reader
    scanner.add_skill(5, 10, 15)                 # SM_BASH
    scanner.add_skill(7, 5, 30)                  # SM_MAGNUM
    scanner.add_skill(60, 7, 38)                 # KN_TWOHANDQUICKEN
    scanner.add_skill(3, 10, 0, inf=0)           # SM_TWOHAND（被動熟練度）

    found = {s.id: s for s in r.read() or []}
    assert found[5].kind == skills_mod.ACTIVE
    assert found[7].kind == skills_mod.ACTIVE
    assert found[60].kind == skills_mod.BUFF
    assert found[3].kind == skills_mod.PASSIVE
    assert found[60].castable and not found[3].castable


def test_garbage_inf_is_not_forced_into_a_class(reader):
    """未學的被動技能那一欄是垃圾值（實機 6322451）—— 不准硬塞進打怪／補助。"""
    r, scanner = reader
    # SM_MOVINGRECOVERY 沒有「類型」欄位，只能靠 inf；inf 是垃圾就該收手。
    scanner.add_skill(144, 0, 0, inf=6322451)

    found = r.read()
    assert found is not None
    assert found[0].kind == skills_mod.UNKNOWN
    assert not found[0].castable


def test_description_comes_from_the_game(reader):
    """tooltip 用的說明是遊戲自己的字串，顏色碼原樣留著。"""
    r, scanner = reader
    scanner.add_skill(60, 7, 38)

    lines = (r.read() or [])[0].description()
    assert lines and "雙手劍攻擊速度增加" in lines[0]
    assert any("^" in line for line in lines)


def test_ignores_struct_whose_name_does_not_match_id(reader):
    """字串說是 SM_BASH、ID 欄位卻寫 60 —— 堆積垃圾就長這樣，必須丟掉。"""
    r, scanner = reader
    scanner.add_skill(60, 7, 38, key="SM_BASH", maxlv=_maxlv(60))
    assert r.read() is None


def test_ignores_struct_with_wrong_max_level(reader):
    """MaxLv 跟 skillinfolist.lub 對不上就不是技能結構。"""
    r, scanner = reader
    scanner.add_skill(5, 10, 15, maxlv=_maxlv(5) + 3)
    assert r.read() is None


def test_unknown_field_does_not_hide_skills(reader):
    """`+0x14` 意義未解，實機出現過 2 和 4。

    這是回歸測試：曾經把它當 `upgradable` 拿去粗篩（要求 <= 1），結果**安靜漏掉**
    4 個技能，其中 SM_PROVOKE 還是已經點到 Lv5 的。
    """
    r, scanner = reader
    scanner.add_skill(6, 5, 8, tail=2)     # SM_PROVOKE
    scanner.add_skill(58, 0, 9, tail=4)    # KN_SPEARSTAB

    found = r.read()
    assert found is not None
    assert [s.id for s in found] == [6, 58]


def test_duplicate_copies_are_merged(reader):
    """實機每個技能都有**兩份一模一樣**的結構，那不是衝突。"""
    r, scanner = reader
    scanner.add_skill(5, 10, 15)
    scanner.add_skill(5, 10, 15)

    found = r.read()
    assert found is not None
    assert [s.id for s in found] == [5]


def test_the_sp_table_settles_a_disagreement(reader):
    """兩份不一樣時用**第三份獨立資料**仲裁：SP 要對得上 lub 的 SpAmount。

    實機白狐的 `AL_WARP` 就是這樣：`lv=0 sp=38` 的殘留與 `lv=1 sp=35` 的真貨，
    而表是 `[35, 32, 29, 26]` —— 38 根本不在裡面。
    """
    r, scanner = reader
    scanner.add_skill(27, 0, 38)         # 殘留：SP 對不上表
    scanner.add_skill(27, 1, 35)         # 真的：sp[0] == 35

    found = r.read()
    assert found is not None
    assert [(s.id, s.level, s.sp) for s in found] == [(27, 1, 35)]


def test_an_unsettled_skill_is_dropped_not_the_whole_table(reader, caplog):
    """仲裁不出來就**只丟那一個**，其他技能照常。

    回歸測試：舊版因為一個技能矛盾就把整張表判定為失敗 —— 技能面板全空、
    而且每 5 秒噴一行 ERROR 把日誌洗掉，別的問題全被沖走（使用者實測回報）。
    """
    r, scanner = reader
    scanner.add_skill(5, 10, 15)         # SM_BASH，正常
    scanner.add_skill(27, 2, 99)         # 兩份 SP 都對不上表 → 分不出來
    scanner.add_skill(27, 3, 98)

    with caplog.at_level(logging.WARNING):
        found = r.read()
    assert [s.id for s in found or []] == [5], "分不出來的丟掉，其餘照常"
    assert "矛盾" in caplog.text


def test_the_conflict_warning_is_not_repeated_every_tick(reader, clock, caplog):
    """5 秒一輪，同一組矛盾不該每輪都喊一次。"""
    r, scanner = reader
    scanner.add_skill(5, 10, 15)
    scanner.add_skill(27, 2, 99)
    scanner.add_skill(27, 3, 98)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            clock.t += FULL_RESCAN_SEC + 1        # 每次都全掃
            r.read()
    assert caplog.text.count("矛盾") == 1


def test_no_skills_is_not_a_crash(reader):
    """還沒進到遊戲裡就是讀不到 —— 回 None，不回空清單充數。"""
    r, _ = reader
    assert r.read() is None


def test_missing_table_disables_the_feature(reader, monkeypatch, caplog):
    """資料表載不到就大聲停用，不准拿空表繼續算。"""
    r, scanner = reader
    scanner.add_skill(5, 10, 15)
    monkeypatch.setattr(skills_mod, "skill_codes", dict)

    with caplog.at_level(logging.WARNING):
        assert r.read() is None
    assert "停用" in caplog.text


def test_leftovers_are_not_reported_when_nobody_is_in_game(reader, monkeypatch, caplog):
    """停在選角畫面時，記憶體裡的技能表是上一次登入的殘留 —— 不准當成答案。

    實機遇過：PID 4116 連著 char server（`find_server()` 回得出伺服器），
    角色狀態定位失敗，技能表卻照樣讀得出 18 個。「有沒有連線」判不出這件事。
    """
    r, scanner = reader
    scanner.add_skill(28, 1, 13)      # AL_HEAL，上一隻角色留下來的
    monkeypatch.setattr(skills_mod, "CharacterReader", OfflineCharacter)

    with caplog.at_level(logging.INFO):
        assert r.read() is None
    assert "殘留" in caplog.text

    # 呼叫端自己確認過在場上時才准跳過檢查。
    assert r.read(require_online=False) is not None


def test_should_stop_aborts(reader):
    r, scanner = reader
    scanner.add_skill(5, 10, 15)
    assert r.read(should_stop=lambda: True) is None


def test_skill_icons_ship_with_the_program():
    """使用者的電腦沒有 RODATA —— 圖示只能從打包資產來（CLAUDE.md）。"""
    from ro_toolbox.services.icons import skill_icon_bytes

    data = skill_icon_bytes("SM_BASH")
    assert data and data[:2] == b"BM", "技能圖示應該是 BMP"
    assert skill_icon_bytes("NOT_A_REAL_SKILL") is None
    assert skill_icon_bytes("") is None


def test_close_releases_only_owned_scanner():
    """外面傳進來的 scanner 不歸我們關 —— 關掉別人還在用的會很難查。"""
    scanner = FakeScanner()
    SkillReader(scanner).close()
    assert not scanner.closed

    owned = SkillReader()
    owned.close()          # 自己建的 scanner，關掉不該丟例外


# ---- 快路徑（5 秒刷新撐得住的原因）----------------------------------------


def test_second_read_does_not_rescan_memory(reader, clock):
    """第二次讀只重驗記住的結構位址 —— 不然 5 秒一輪等於每 5 秒掃 507 MB。"""
    r, scanner = reader
    scanner.add_skill(5, 10, 15)
    scanner.add_skill(60, 7, 38)

    assert r.read() is not None
    after_full = scanner.full_scans
    assert after_full > 0

    clock.t += 5
    again = r.read()
    assert [s.id for s in again] == [5, 60]
    assert scanner.full_scans == after_full, "快路徑不該再掃整塊記憶體"


def test_the_fast_path_sees_a_skill_point_being_spent(reader, clock):
    """加點不會換結構位址，只是把 level 從 0 改成 1 —— 快路徑要看得到。"""
    r, scanner = reader
    addr = scanner.add_skill(56, 0, 7)          # KN_PIERCE，學得起但沒點
    assert (r.read() or [])[0].level == 0

    offset = addr - REGION
    struct.pack_into("<I", scanner.region, offset + 0x08, 3)   # 點到 Lv3
    clock.t += 5
    assert (r.read() or [])[0].level == 3


def test_a_broken_cache_falls_back_to_a_full_scan(reader, clock, caplog):
    """結構動了（換角色／轉職）就整份不採用，重新全掃認一次。"""
    r, scanner = reader
    addr = scanner.add_skill(5, 10, 15)
    assert r.read() is not None
    before = scanner.full_scans

    offset = addr - REGION
    struct.pack_into("<I", scanner.region, offset, 999999)     # 把 ID 弄壞
    clock.t += 1
    with caplog.at_level(logging.INFO):
        assert r.read() is None                # 這塊記憶體裡沒有別的技能了
    assert scanner.full_scans > before
    assert "快取失效" in caplog.text


def test_a_full_scan_still_happens_now_and_then(reader, clock):
    """轉職會多出一整批新技能，那些結構不在快取裡 —— 靠定期全掃接住。"""
    r, scanner = reader
    scanner.add_skill(5, 10, 15)
    assert [s.id for s in r.read() or []] == [5]
    before = scanner.full_scans

    scanner.add_skill(60, 7, 38)               # 轉職後新增的技能
    clock.t += 5
    assert [s.id for s in r.read() or []] == [5], "還在快取期間，看不到新的"
    assert scanner.full_scans == before

    clock.t += skills_mod.FULL_RESCAN_SEC
    assert [s.id for s in r.read() or []] == [5, 60]
    assert scanner.full_scans > before


def test_the_online_check_reuses_one_character_reader(reader, clock):
    """`_online()` 每次都重新 attach 的話，5 秒一輪等於每 5 秒跑一次 AOB 定位。"""
    r, scanner = reader
    scanner.add_skill(5, 10, 15)
    r.read()
    r.read()
    assert OnlineCharacter.attaches == 1
