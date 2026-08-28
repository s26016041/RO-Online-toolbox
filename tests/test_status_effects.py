"""身上的狀態清單：算得出來才回答，內容不對就整批拒絕。

盯的是 CLAUDE.md 那幾條：**定位失敗要大聲**、**驗不過退安全預設**、
「安靜地做錯事」一律當 bug —— 特別是「沒有 buff」與「讀不到」不能混成同一種答案。
"""

from __future__ import annotations

import struct

import pytest

pytest.importorskip("numpy")

from ro_toolbox.services.aob import locate_global  # noqa: E402
from ro_toolbox.services.signatures import (  # noqa: E402
    STATUS_MAX_ENTRIES,
    STATUS_NO_TIME_LIMIT,
    STATUS_VEC_OFFSETS,
    STATUS_VEC_SIGS,
)
from ro_toolbox.services.status_effects import (  # noqa: E402
    ActiveStatus,
    StatusEffects,
    _elapsed,
)

VECTOR = 0x1357D74
BODY = 0x16000000
STRIDE = STATUS_VEC_OFFSETS.stride
NOW = 500_000_000


def entry(efst: int, expire: int, total: int, val1: int = 0) -> bytes:
    # tick 是 32 位元無號、會回繞，所以負的到期時刻要照回繞的方式塞回去。
    return struct.pack(
        "<IIiiiii", efst, expire & 0xFFFFFFFF, val1, 0, 0, total, total
    )


class FakeScanner:
    """只回答 `read_region`，就是 StatusEffects 用到的全部。"""

    def __init__(self, head: bytes | None, body: bytes = b"") -> None:
        self.head = head
        self.body = body

    def read_region(self, addr: int, size: int):
        if addr == VECTOR:
            return None if self.head is None else memoryview(self.head[:size])
        if addr == BODY:
            return memoryview(self.body[:size]) if len(self.body) >= size else None
        return None


def make(entries: list[bytes], capacity: int | None = None) -> StatusEffects:
    span = len(entries) * STRIDE
    cap = BODY + (span if capacity is None else capacity)
    head = struct.pack("<III", BODY, BODY + span, cap)
    effects = StatusEffects(FakeScanner(head, b"".join(entries)), now=lambda: NOW)
    effects._addr = VECTOR
    return effects


# ---- 讀得到的情況 -------------------------------------------------------


def test_reads_the_buff_with_its_chinese_name_and_countdown():
    rows = make([entry(2, NOW + 61_200, 210_000, val1=15)]).read()
    assert len(rows) == 1
    row = rows[0]
    assert row.efst == 2
    # 名稱來自 assets/efst.json.gz，不是寫死的字串
    assert row.name == "雙手劍攻擊速度增加"
    assert row.remaining_ms == 61_200
    assert row.total_ms == 210_000
    assert row.val1 == 15
    assert not row.permanent


def test_no_time_limit_is_not_a_countdown_of_zero():
    """永久狀態（馴鷹術／手推車）的到期欄位是垃圾 —— 不准拿去算倒數。"""
    rows = make([entry(28, NOW - 562_238_500, STATUS_NO_TIME_LIMIT)]).read()
    assert rows[0].name == "馴鷹術"
    assert rows[0].remaining_ms is None
    assert rows[0].total_ms is None
    assert rows[0].permanent


def test_nonsense_expire_falls_back_to_no_countdown():
    """有 total 但到期時刻離譜（六天前）→ 只顯示名稱，不顯示 0 秒。"""
    rows = make([entry(695, NOW - 999_999_999, 1_982_693_187)]).read()
    assert rows[0].remaining_ms is None
    assert rows[0].total_ms is None


def test_expired_entry_clamps_to_zero_not_negative():
    rows = make([entry(2, NOW - 1_000, 210_000)]).read()
    assert rows[0].remaining_ms == 0


def test_unknown_efst_falls_back_to_code_then_number():
    """查不到名字就退代號／編號 —— 不准假裝知道，也不准整批不給。"""
    rows = make([entry(46, NOW + 500, 500), entry(65535, NOW + 500, 500)]).read()
    assert rows[0].name == "EFST_POSTDELAY"      # 有代號沒中文名
    assert rows[1].name == "#65535"              # 表裡根本沒有


def test_empty_vector_means_no_buffs_not_a_failure():
    """`[]` 與 `None` 是兩件事：確定沒有 vs 問不出來。"""
    never_used = StatusEffects(FakeScanner(struct.pack("<III", 0, 0, 0)), now=lambda: NOW)
    never_used._addr = VECTOR
    assert never_used.read() == []
    assert make([]).read() == []


def test_reads_several_entries():
    rows = make([
        entry(2, NOW + 1_000, 210_000),
        entry(10, NOW + 2_000, 240_000),
        entry(673, 0, STATUS_NO_TIME_LIMIT),
    ]).read()
    assert [row.efst for row in rows] == [2, 10, 673]


# ---- 內容不可信 → 整批拒絕 ----------------------------------------------


def test_not_located_reads_nothing():
    effects = StatusEffects(FakeScanner(None), now=lambda: NOW)
    assert effects.located is False
    assert effects.read() is None


def test_unreadable_header_is_a_failure_not_an_empty_list():
    effects = StatusEffects(FakeScanner(None), now=lambda: NOW)
    effects._addr = VECTOR
    assert effects.read() is None


@pytest.mark.parametrize("head", [
    struct.pack("<III", BODY, BODY - STRIDE, BODY + 0x70),   # end < begin
    struct.pack("<III", BODY, BODY + 0x70, BODY),            # cap < end
    struct.pack("<III", BODY, BODY + STRIDE + 3, BODY + 0x70),  # 長度不是整數筆
    struct.pack("<III", 0, BODY + STRIDE, BODY + 0x70),      # begin 是 0 但 end 不是
])
def test_broken_vector_is_refused(head):
    effects = StatusEffects(FakeScanner(head, b"\x00" * 0x200), now=lambda: NOW)
    effects._addr = VECTOR
    assert effects.read() is None


def test_absurd_count_is_refused():
    count = STATUS_MAX_ENTRIES + 1
    head = struct.pack("<III", BODY, BODY + count * STRIDE, BODY + count * STRIDE)
    effects = StatusEffects(FakeScanner(head, b"\x00" * count * STRIDE), now=lambda: NOW)
    effects._addr = VECTOR
    assert effects.read() is None


def test_body_that_cannot_be_read_is_refused():
    head = struct.pack("<III", BODY, BODY + STRIDE, BODY + STRIDE)
    effects = StatusEffects(FakeScanner(head, b""), now=lambda: NOW)
    effects._addr = VECTOR
    assert effects.read() is None


def test_impossible_efst_number_refuses_the_whole_batch():
    """一筆解錯就代表版面對不上 —— 不准只丟掉那筆、其餘照用。"""
    assert make([entry(2, NOW + 1000, 210_000), entry(0x12345678, 0, 0)]).read() is None


def test_has_says_none_when_it_cannot_tell():
    """`has()` 問不出來要回 None —— 回 False 會讓呼叫端以為 buff 掉了。"""
    effects = StatusEffects(FakeScanner(None), now=lambda: NOW)
    effects._addr = VECTOR
    assert effects.has(2) is None
    assert make([entry(2, NOW + 1000, 210_000)]).has(2) is True
    assert make([entry(2, NOW + 1000, 210_000)]).has(10) is False


# ---- 時基 ---------------------------------------------------------------


def test_tick_wraparound_does_not_produce_49_days():
    """GetTickCount 每 49.7 天回繞一次；直接相減會算出 ±49 天。"""
    almost = 0xFFFFFFFF - 1_000
    assert _elapsed(500, almost) == 1_501
    assert _elapsed(almost, 500) == -1_501
    assert _elapsed(NOW + 5_000, NOW) == 5_000


def test_reads_correctly_across_the_wrap():
    head = struct.pack("<III", BODY, BODY + STRIDE, BODY + STRIDE)
    body = entry(2, 500, 210_000)
    effects = StatusEffects(FakeScanner(head, body), now=lambda: 0xFFFFFFFF - 1_000)
    effects._addr = VECTOR
    assert effects.read()[0].remaining_ms == 1_501


# ---- 顯示 ---------------------------------------------------------------


@pytest.mark.parametrize("remaining,expected", [
    (None, "加速術"),
    (5_400, "加速術 5s"),
    (61_200, "加速術 1:01"),
    (210_000, "加速術 3:30"),
])
def test_describe(remaining, expected):
    row = ActiveStatus(efst=12, name="加速術", remaining_ms=remaining, total_ms=None)
    assert row.describe() == expected


# ---- 特徵本身 -----------------------------------------------------------


def _bytes_for(sig, address: int) -> bytes:
    """照特徵的骨架生一段假程式碼，答案填在 operands 指定的位移上。

    這同時驗了兩件事：樣式打得對不對，以及 operands 的位移對不對 ——
    位移填錯的話讀出來就不是 `address`。
    """
    tokens = sig.pattern.split()
    out = bytearray(int(t, 16) if t != "??" else 0x90 for t in tokens)
    for offset in sig.operands:
        out[offset:offset + 4] = address.to_bytes(4, "little")
    return bytes(out)


MODULE = 0x400000
CODE = 0x401000


class FakeImage:
    """`code_section` 需要的最小 PE（照 tests/test_code_signature.py 的做法）。"""

    def __init__(self, code: bytes) -> None:
        self.code = code.ljust(0x2000, b"\xcc")

    def module_base(self, name):
        return MODULE if name.lower() == "ragexe.exe" else None

    def _read_bytes(self, addr: int, size: int):
        if addr == MODULE:
            head = bytearray(0x400)
            head[0x3C:0x40] = (0x80).to_bytes(4, "little")
            return bytes(head[:size])
        if addr == MODULE + 0x80:
            pe = bytearray(0x120)
            pe[6:8] = (1).to_bytes(2, "little")
            pe[20:22] = (0xE0).to_bytes(2, "little")
            pe[24 + 56:24 + 60] = (0x2000000).to_bytes(4, "little")
            return bytes(pe[:size])
        if addr == MODULE + 0x80 + 24 + 0xE0:
            row = bytearray(40)
            row[8:12] = len(self.code).to_bytes(4, "little")
            row[12:16] = (CODE - MODULE).to_bytes(4, "little")
            row[36:40] = (0x20000000).to_bytes(4, "little")
            return bytes(row[:size])
        if addr == CODE:
            return self.code[:size]
        return b""


def test_every_skeleton_finds_the_same_global():
    code = bytearray(b"\xcc" * 32)
    for sig in STATUS_VEC_SIGS:
        code += _bytes_for(sig, VECTOR) + b"\xcc" * 32
    assert locate_global(FakeImage(bytes(code)), STATUS_VEC_SIGS) == VECTOR


def test_the_answer_is_not_written_into_the_pattern():
    """CLAUDE.md：特徵裡不准把答案寫死。位址是從立即值讀出來的。"""
    for sig in STATUS_VEC_SIGS:
        assert "??" in sig.pattern
        assert sig.why, f"{sig.name} 沒寫骨架出處"
        for offset in sig.operands:
            tokens = sig.pattern.split()
            assert tokens[offset:offset + 4] == ["??"] * 4, (
                f"{sig.name} 的 operand {offset} 沒有被遮掉"
            )
