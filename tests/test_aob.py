"""AOB 特徵掃描測試。

掃描部分用本行程當目標：放一段獨特位元組樣式，確認 AOB 真的找得到它。
"""

from __future__ import annotations

import ctypes
import os
import sys

import pytest

pytest.importorskip("numpy")

from ro_toolbox.services.aob import AOBSignature, scan  # noqa: E402
from ro_toolbox.services.memory_scan import VALUE_TYPES, MemoryScanner  # noqa: E402


def test_parse_marks_wildcards():
    sig = AOBSignature(pattern="01 ?? 03 FF", value_offset=0)
    pattern, mask = sig.parse()

    assert pattern == bytes([0x01, 0x00, 0x03, 0xFF])
    assert list(mask) == [1, 0, 1, 1]  # ?? 的位置 mask 為 0


def test_parse_accepts_single_question_mark():
    _pattern, mask = AOBSignature(pattern="AA ? BB", value_offset=0).parse()
    assert list(mask) == [1, 0, 1]


def test_value_type_lookup():
    assert AOBSignature(pattern="00", value_offset=0, vt_key="float").vt.size == 4


@pytest.mark.skipif(sys.platform != "win32", reason="只支援 Windows")
def test_scan_finds_known_pattern_in_own_process():
    marker = bytes.fromhex("DEADBEEF") + b"\x11\x22\x33\x44" + bytes.fromhex("CAFEBABE")
    payload = marker + (4242).to_bytes(4, "little")
    buffer = ctypes.create_string_buffer(payload, len(payload))
    expected = ctypes.addressof(buffer) + len(marker)

    sig = AOBSignature(
        pattern="DE AD BE EF ?? ?? ?? ?? CA FE BA BE",
        value_offset=len(marker),
        vt_key="int32",
    )

    scanner = MemoryScanner()
    scanner.open(os.getpid())
    try:
        hits = scan(scanner, sig, writable_only=True, limit=64)
        assert expected in hits, "AOB 沒找到我們自己放進去的樣式"
        assert scanner.read_value(expected, VALUE_TYPES["int32"]) == 4242
    finally:
        scanner.close()


@pytest.mark.skipif(sys.platform != "win32", reason="只支援 Windows")
def test_scan_respects_should_stop():
    """should_stop 要能中止掃描，否則關程式時得等它整趟跑完。"""
    sig = AOBSignature(pattern="DE AD BE EF CA FE BA BE", value_offset=0)

    scanner = MemoryScanner()
    scanner.open(os.getpid())
    try:
        hits = scan(scanner, sig, should_stop=lambda: True)
        assert hits == []
    finally:
        scanner.close()
