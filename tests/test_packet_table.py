"""封包長度表的 AOB 抽取（用合成的程式碼，不需要遊戲）。"""

from __future__ import annotations

import struct

import pytest

from ro_toolbox.services import packet_table
from ro_toolbox.services.packet_table import extract

pytest.importorskip("capstone")

BASE = 0x400000
CODE_RVA = 0x1000
REGISTER_RVA = 0x9000       # 註冊函式（相對模組基底）
ENTRIES = [(0x0080, 7, 7), (0x0087, 12, 12), (0x0B08, -1, 5)]
FILLER = 250                # 要夠多次呼叫才會被認定成註冊函式


class FakeModule:
    def __init__(self, name: str, base: int) -> None:
        self.name = name
        self.base = base


def _call_site(opcode: int, length: int, header: int, here: int, target: int) -> bytes:
    """push flag/header/len/opcode ; mov ecx,esi ; call rel32"""
    body = (
        b"\x68" + struct.pack("<I", 1)
        + b"\x68" + struct.pack("<I", header & 0xFFFFFFFF)
        + b"\x68" + struct.pack("<I", length & 0xFFFFFFFF)
        + b"\x68" + struct.pack("<I", opcode)
    )
    call_at = here + len(body)
    rel = target - (call_at + 7)
    return body + b"\x8b\xce\xe8" + struct.pack("<i", rel)


def build_code() -> bytes:
    out = bytearray()
    target = BASE + REGISTER_RVA
    rows = ENTRIES + [(0x1000 + i, 4, 4) for i in range(FILLER)]
    for opcode, length, header in rows:
        here = BASE + CODE_RVA + len(out)
        out += _call_site(opcode, length, header, here, target)
        out += b"\x90" * 8
    return bytes(out)


class FakeScanner:
    """假的掃描器：提供一個最小可用的 PE 版面與一段合成程式碼。"""

    def __init__(self, code: bytes, *, executable: bool = True, module: str = "ragexe.exe"):
        self.code = code
        self.executable = executable
        self.module = module
        self.closed = False

    def open(self, pid):  # noqa: ARG002
        return None

    def list_modules(self):
        return [FakeModule(self.module, BASE)]

    def module_base(self, name):
        # 正式版走這條（模組列舉會被 GameGuard 擋，見 aob.code_section）。
        return BASE if name.lower() == self.module.lower() else None

    def _read_bytes(self, addr: int, size: int):
        if addr == BASE:                       # DOS 標頭：只有 e_lfanew 有意義
            head = bytearray(0x400)
            struct.pack_into("<I", head, 0x3C, 0x80)
            return bytes(head[:size])
        if addr == BASE + 0x80:                # PE 標頭：區段數與可選標頭大小
            pe = bytearray(0x120)
            struct.pack_into("<H", pe, 6, 1)
            struct.pack_into("<H", pe, 20, 0xE0)
            return bytes(pe[:size])
        if addr == BASE + 0x80 + 24 + 0xE0:     # 區段表
            row = bytearray(40)
            row[:8] = b".text\x00\x00\x00"
            struct.pack_into("<II", row, 8, len(self.code), CODE_RVA)
            struct.pack_into("<I", row, 36, 0x20000000 if self.executable else 0x40000000)
            return bytes(row[:size])
        if addr == BASE + CODE_RVA:
            return self.code[:size]
        return None

    def close(self):
        self.closed = True


@pytest.fixture
def scanner(monkeypatch):
    fake = FakeScanner(build_code())
    monkeypatch.setattr(packet_table, "MemoryScanner", lambda: fake)
    return fake


def test_extracts_the_registered_lengths(scanner):  # noqa: ARG001
    table = extract(1234)
    assert len(table) == len(ENTRIES) + FILLER
    assert table[0x0080].length == 7
    assert table[0x0087].length == 12


def test_variable_length_is_flagged(scanner):  # noqa: ARG001
    """長度 -1 代表可變長度，標頭大小另外給（實測 0x0B08 是 -1 / 標頭 5）。"""
    info = extract(1234)[0x0B08]
    assert info.variable is True
    assert info.length == -1
    assert info.header == 5


def test_fixed_length_is_not_flagged_variable(scanner):  # noqa: ARG001
    assert extract(1234)[0x0080].variable is False


def test_returns_empty_when_module_is_missing(monkeypatch):
    """定位不到就回空 —— 呼叫端安全退化，不准拿猜的長度用。"""
    fake = FakeScanner(build_code(), module="notepad.exe")
    monkeypatch.setattr(packet_table, "MemoryScanner", lambda: fake)
    assert extract(1234) == {}


def test_returns_empty_when_no_executable_section(monkeypatch):
    fake = FakeScanner(build_code(), executable=False)
    monkeypatch.setattr(packet_table, "MemoryScanner", lambda: fake)
    assert extract(1234) == {}


def test_refuses_when_the_call_is_not_hot_enough(monkeypatch):
    """只有幾次呼叫的目標不能當註冊函式 —— 那多半是碰巧的指令組合。"""
    out = bytearray()
    target = BASE + REGISTER_RVA
    for opcode, length, header in ENTRIES:
        here = BASE + CODE_RVA + len(out)
        out += _call_site(opcode, length, header, here, target) + b"\x90" * 8
    fake = FakeScanner(bytes(out))
    monkeypatch.setattr(packet_table, "MemoryScanner", lambda: fake)
    assert extract(1234) == {}


def test_closes_the_scanner_it_opened(scanner):
    extract(1234)
    assert scanner.closed is True
