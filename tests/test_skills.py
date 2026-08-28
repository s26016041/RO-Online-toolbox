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
from ro_toolbox.services.skills import SkillReader

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
          maxlv: int | None = None, tail: int = 0) -> bytes:
    """一個技能結構（版面見 services/skills.py 開頭）。

    `tail` 塞進 `+0x14` —— 那個欄位意義未解，實機出現過 2 和 4。
    """
    out = bytearray(0x24)
    struct.pack_into("<I", out, 0x00, skill_id)
    struct.pack_into("<I", out, 0x04, 4)
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

    # ---- 佈置 ----

    def put_string(self, text: str) -> int:
        addr = self._next_string
        self.strings[addr] = text.encode("ascii") + b"\0"
        self._next_string += 0x40      # 保持 4 對齊，跟真的字串池一樣
        return addr

    def add_skill(self, skill_id: int, level: int, sp: int, *,
                  key: str | None = None, maxlv: int | None = None,
                  tail: int = 0) -> int:
        """放一個技能結構，回傳它在記憶體裡的位址。"""
        pointer = self.put_string(_key(skill_id) if key is None else key)
        offset = FIRST + self._slot * STRIDE
        self._slot += 1
        blob = _node(skill_id, level, sp, pointer, maxlv=maxlv, tail=tail)
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
    """假的角色狀態讀取器：有角色在場上。"""

    def attach(self, pid):  # noqa: ARG002
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
    monkeypatch.setattr(skills_mod, "CharacterReader", OnlineCharacter)


@pytest.fixture
def reader():
    scanner = FakeScanner()
    reader = SkillReader(scanner)
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


def test_conflicting_copies_fail_loudly(reader, caplog):
    """兩份都通過交叉驗證卻不一樣 —— 不准挑一個用，賭錯就是看著別人的等級。"""
    r, scanner = reader
    scanner.add_skill(5, 10, 15)
    scanner.add_skill(5, 3, 8)

    with caplog.at_level(logging.ERROR):
        assert r.read() is None
    assert "矛盾" in caplog.text


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


def test_close_releases_only_owned_scanner():
    """外面傳進來的 scanner 不歸我們關 —— 關掉別人還在用的會很難查。"""
    scanner = FakeScanner()
    SkillReader(scanner).close()
    assert not scanner.closed

    owned = SkillReader()
    owned.close()          # 自己建的 scanner，關掉不該丟例外
