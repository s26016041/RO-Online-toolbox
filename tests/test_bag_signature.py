"""背包骨架要分得出「除以 34」與「除以 17」。

實機上這兩段程式碼**共用同一個魔術乘數**（0xF0F0F0F1），差別只在最後一步
`shr edx, 5`（/34）還是 `shr edx, 4`（/17）。少了這個條件就會多出一個假容器
（GAMEDATA [MEM-037]）。
"""

from __future__ import annotations

import struct

from ro_toolbox.services import bag

MODULE = 0x400000
CODE = 0x401000
REAL = 0x15D2AC8
DECOY = 0x11F0758


def _parser(container: int) -> bytes:
    """解析函式：mov ecx, <容器> ; ret"""
    return bytes([0xB9]) + struct.pack("<I", container) + bytes([0xC3])


def _site(shr: int, call_to: int, at: int) -> bytes:
    """一段「乘魔術數再右移」的骨架，後面接一個 call 到解析函式。

    `shr` 是右移幾位：5 = 除以 34（背包），4 = 除以 17（別人家的）。
    """
    head = (
        bytes([0x83, 0xE9, 0x05])              # sub ecx, 5
        + bytes([0xB8, 0xF1, 0xF0, 0xF0, 0xF0])  # mov eax, 0xF0F0F0F1
        + bytes([0xF7, 0xE1])                   # mul ecx
        + bytes([0xC1, 0xEA, shr])              # shr edx, n
    )
    call_at = at + len(head)
    rel = call_to - (call_at + 5)
    return head + bytes([0xE8]) + struct.pack("<i", rel)


class FakeScanner:
    def __init__(self, code: bytes) -> None:
        self.code = code

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


def _code(*, with_decoy: bool) -> bytes:
    """真骨架（/34）在前，可選的誘餌（/17）在後。"""
    out = bytearray(b"\xcc" * 0x2000)
    real_parser_at = 0x800
    decoy_parser_at = 0x900
    out[real_parser_at:real_parser_at + 6] = _parser(REAL)
    out[decoy_parser_at:decoy_parser_at + 6] = _parser(DECOY)

    site_at = 0x100
    out[site_at:site_at + 16] = _site(5, CODE + real_parser_at, CODE + site_at)
    if with_decoy:
        decoy_at = 0x200
        out[decoy_at:decoy_at + 16] = _site(4, CODE + decoy_parser_at, CODE + decoy_at)
    return bytes(out)


def test_it_finds_the_divide_by_34_site():
    assert bag.find_containers(FakeScanner(_code(with_decoy=False))) == [REAL]


def test_the_divide_by_17_site_is_not_a_candidate():
    """同一個魔術乘數、只差 `shr edx,4` —— 那是除以 17，不是背包。

    實機上這個誘餌讀出來 0 筆，所以以前是靠「資料讀不出來」被裁掉的；
    但只要它哪天剛好讀得出像樣的東西，整個背包功能就會被判定為歧義而停用。
    """
    assert bag.find_containers(FakeScanner(_code(with_decoy=True))) == [REAL]


def test_sites_carry_the_skeleton_and_parser_for_the_verifier():
    """`find_container_sites` 要連骨架位址與解析函式一起給 —— 驗證工具靠它做改版模擬。

    這是為了**不要有第二份實作**：工具以前自己重寫一份，正式版收緊骨架時
    它沒跟上，報告就開始說謊。
    """
    sites = bag.find_container_sites(FakeScanner(_code(with_decoy=True)))
    assert len(sites) == 1
    site, parser, container = sites[0]
    assert container == REAL
    assert site == CODE + 0x100 + 3      # 骨架位址從 `mov eax, magic` 那一行算起
    assert parser == CODE + 0x800
