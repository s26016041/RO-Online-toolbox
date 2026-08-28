"""用指令骨架定位全域：對得上才回答，有一點不一致就拒絕。

盯的是 CLAUDE.md 那兩條：**答案不准寫死在特徵裡**、**定位失敗要大聲**。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services.aob import CodeSignature, locate_global

MODULE = 0x400000
CODE = 0x401000
TARGET = 0x15D096A
OTHER = 0x15D2AC8

#: 實機骨架：movzx eax, byte [目標]; push eax; push 8; call [...]
CURSOR = CodeSignature(
    name="cursor",
    pattern="0F B6 05 ?? ?? ?? ?? 50 6A 08 FF 15",
    operands=(3,),
)
#: 另一條獨立骨架，指向同一個全域。
CURSOR2 = CodeSignature(
    name="cursor2",
    pattern="88 0D ?? ?? ?? ?? 8B 8E 34 01",
    operands=(2,),
)


def _site(prefix: bytes, addr: int, suffix: bytes = b"") -> bytes:
    return prefix + addr.to_bytes(4, "little") + suffix


def _code(*chunks: bytes) -> bytes:
    """把幾段指令包成一個「程式碼區段」。

    ⚠ 要墊到 0x1000 以上 —— `code_section` 會跳過太小的區段
    （那種尺寸不可能是主程式碼段，只會是解析錯了）。
    """
    body = bytearray(b"\xcc" * 32)
    for chunk in chunks:
        body += chunk + b"\xcc" * 32
    return bytes(body).ljust(0x2000, b"\xcc")


class FakeScanner:
    """只服務 `code_section` 需要的那幾個呼叫。"""

    def __init__(self, code: bytes) -> None:
        self.code = code

    def module_base(self, name):
        return MODULE if name.lower() == "ragexe.exe" else None

    def _read_bytes(self, addr: int, size: int):
        if addr == MODULE:                      # DOS 標頭
            head = bytearray(0x400)
            head[0x3C:0x40] = (0x80).to_bytes(4, "little")
            return bytes(head[:size])
        if addr == MODULE + 0x80:               # PE 標頭：1 個區段
            pe = bytearray(0x120)
            pe[6:8] = (1).to_bytes(2, "little")
            pe[20:22] = (0xE0).to_bytes(2, "little")
            # 可選標頭 +56 是 SizeOfImage —— 判斷「是不是模組自己的位址」要靠它
            pe[24 + 56:24 + 60] = (0x2000000).to_bytes(4, "little")
            return bytes(pe[:size])
        if addr == MODULE + 0x80 + 24 + 0xE0:   # 區段表
            row = bytearray(40)
            row[8:12] = len(self.code).to_bytes(4, "little")     # VirtualSize
            row[12:16] = (CODE - MODULE).to_bytes(4, "little")   # VirtualAddress
            row[36:40] = (0x20000000).to_bytes(4, "little")      # 可執行
            return bytes(row[:size])
        if addr == CODE:
            return self.code[:size]
        return b""


def test_it_reads_the_address_out_of_the_instruction():
    """答案是從立即值讀出來的 —— 特徵裡一個位址位元組都沒有。"""
    code = _code(_site(b"\x0f\xb6\x05", TARGET, b"\x50\x6a\x08\xff\x15"))
    assert locate_global(FakeScanner(code), [CURSOR]) == TARGET
    assert "??" in CURSOR.pattern


def test_many_hits_that_agree_are_fine():
    """同一條特徵命中很多處是正常的 —— 只要讀出來的位址一樣。"""
    site = _site(b"\x0f\xb6\x05", TARGET, b"\x50\x6a\x08\xff\x15")
    assert locate_global(FakeScanner(_code(site, site, site)), [CURSOR]) == TARGET


def test_two_independent_skeletons_cross_check_each_other():
    code = _code(
        _site(b"\x0f\xb6\x05", TARGET, b"\x50\x6a\x08\xff\x15"),
        _site(b"\x88\x0d", TARGET, b"\x8b\x8e\x34\x01"),
    )
    assert locate_global(FakeScanner(code), [CURSOR, CURSOR2]) == TARGET


def test_disagreement_is_refused_not_voted_on():
    """兩處讀出不同位址 → **拒絕作答**。挑一個用就是安靜地選錯。"""
    code = _code(
        _site(b"\x0f\xb6\x05", TARGET, b"\x50\x6a\x08\xff\x15"),
        _site(b"\x0f\xb6\x05", OTHER, b"\x50\x6a\x08\xff\x15"),
    )
    assert locate_global(FakeScanner(code), [CURSOR]) is None


def test_skeletons_that_disagree_with_each_other_are_refused():
    code = _code(
        _site(b"\x0f\xb6\x05", TARGET, b"\x50\x6a\x08\xff\x15"),
        _site(b"\x88\x0d", OTHER, b"\x8b\x8e\x34\x01"),
    )
    assert locate_global(FakeScanner(code), [CURSOR, CURSOR2]) is None


def test_no_hit_means_none():
    """改版之後那幾行指令不見了 —— 回 None，讓上層停用功能。"""
    assert locate_global(FakeScanner(_code(b"\x90" * 16)), [CURSOR]) is None


def test_an_address_outside_the_module_is_refused():
    """讀出來的值不在模組範圍內＝解錯了，不准拿去用。"""
    code = _code(_site(b"\x0f\xb6\x05", 0x7FFF0000, b"\x50\x6a\x08\xff\x15"))
    assert locate_global(FakeScanner(code), [CURSOR]) is None


def test_operands_inside_one_skeleton_must_agree():
    """骨架裡好幾個立即值指的是同一個全域 —— 那是它自帶的一致性檢查。"""
    loop = CodeSignature(
        name="loop", pattern="8A 8C 02 ?? ?? ?? ?? 30 88 ?? ?? ?? ?? C3",
        operands=(3, 9),
    )
    good = b"\x8a\x8c\x02" + TARGET.to_bytes(4, "little") + b"\x30\x88" \
        + TARGET.to_bytes(4, "little") + b"\xc3"
    bad = b"\x8a\x8c\x02" + TARGET.to_bytes(4, "little") + b"\x30\x88" \
        + OTHER.to_bytes(4, "little") + b"\xc3"
    assert locate_global(FakeScanner(_code(good)), [loop]) == TARGET
    assert locate_global(FakeScanner(_code(bad)), [loop]) is None


def test_neighbour_fields_are_normalised_before_comparing():
    """骨架一次碰 HP 與它旁邊的 MaxHP：讀出來先減掉 delta 再比對。

    這是 [MEM-052] 之後加的機制 —— 只驗一個立即值的特徵，指到別的欄位時
    完全看不出來；要求「相鄰欄位剛好差 4」就把長得像的骨架擋掉了。
    """
    mov_ecx = bytes.fromhex("8B0D")   # mov ecx, [imm32]
    mov_edx = bytes.fromhex("8B15")   # mov edx, [imm32]
    ret = bytes.fromhex("C3")
    pair = CodeSignature(
        name="pair", pattern="8B 0D ?? ?? ?? ?? 8B 15 ?? ?? ?? ?? C3",
        operands=(2, 8), operand_deltas=(0, 4),
    )

    def site(gap: int) -> bytes:
        return (mov_ecx + TARGET.to_bytes(4, "little")
                + mov_edx + (TARGET + gap).to_bytes(4, "little") + ret)

    assert locate_global(FakeScanner(_code(site(4))), [pair]) == TARGET
    # 差距不是 4 —— 這一組不是 HP/MaxHP，必須拒答而不是挑一個用。
    assert locate_global(FakeScanner(_code(site(8))), [pair]) is None


def test_delta_count_must_match_operands():
    """寫錯 delta 個數是程式錯誤，要當場炸，不能安靜地少驗一個。"""
    with pytest.raises(ValueError):
        CodeSignature(name="x", pattern="A1 ?? ?? ?? ??",
                      operands=(1,), operand_deltas=(0, 4))


@pytest.mark.parametrize("sig", [
    *__import__(
        "ro_toolbox.services.signatures", fromlist=["x"]
    ).CHAR_STATUS_SIGS,
    *__import__(
        "ro_toolbox.services.signatures", fromlist=["x"]
    ).SELECT_CURSOR_SIGS,
    *__import__(
        "ro_toolbox.services.signatures", fromlist=["x"]
    ).SELECT_NAME_SIGS,
])
def test_registered_signatures_hide_their_answer(sig):
    """登錄表裡的每一條都要遮掉答案，而且要寫清楚骨架是什麼。"""
    tokens = sig.pattern.split()
    for off in sig.operands:
        assert tokens[off:off + 4] == ["??"] * 4, f"{sig.name} 沒把答案遮掉"
    assert sig.why, f"{sig.name} 沒寫出處"
    assert sig.compiled().pattern
