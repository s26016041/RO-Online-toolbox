"""記憶體掃描核心的自我測試。

用本行程當目標：建立一個已知值的 ctypes 變數，透過 ReadProcessMemory /
WriteProcessMemory 讀寫它，確認整條 Win32 路徑是通的。
"""

from __future__ import annotations

import ctypes
import os
import sys

import pytest

pytest.importorskip("numpy")

from ro_toolbox.services.memory_scan import (  # noqa: E402
    VALUE_TYPES,
    MemoryScanner,
    working_set_mb,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="只支援 Windows")


@pytest.fixture
def scanner():
    instance = MemoryScanner()
    instance.open(os.getpid())
    yield instance
    instance.close()


def test_open_self_reports_attached(scanner):
    assert scanner.attached is True
    assert scanner.pid == os.getpid()
    assert scanner.pointer_size in (4, 8)


def test_read_value_matches_known_variable(scanner):
    holder = ctypes.c_int32(1234567)
    address = ctypes.addressof(holder)

    assert scanner.read_value(address, VALUE_TYPES["int32"]) == 1234567


def test_write_value_changes_the_variable(scanner):
    holder = ctypes.c_int32(1000)
    address = ctypes.addressof(holder)

    scanner.write_value(address, VALUE_TYPES["int32"], 4242)

    # 從我們自己的變數看得到改動，代表 WriteProcessMemory 真的寫進去了
    assert holder.value == 4242
    assert scanner.read_value(address, VALUE_TYPES["int32"]) == 4242


def test_read_float_and_double(scanner):
    single = ctypes.c_float(1.5)
    double = ctypes.c_double(2.25)

    assert scanner.read_value(ctypes.addressof(single), VALUE_TYPES["float"]) == 1.5
    assert scanner.read_value(ctypes.addressof(double), VALUE_TYPES["double"]) == 2.25


def test_read_value_on_bad_address_returns_none(scanner):
    assert scanner.read_value(0x10, VALUE_TYPES["int32"]) is None


def test_string_roundtrip(scanner):
    buffer = ctypes.create_unicode_buffer("Prontera", 32)
    address = ctypes.addressof(buffer)
    byte_length = len("Prontera") * 2

    assert scanner.read_string(address, byte_length, "utf-16-le") == "Prontera"


def test_list_modules_includes_python(scanner):
    names = {module.name.lower() for module in scanner.list_modules()}
    assert any("python" in name for name in names)


def test_working_set_is_positive():
    assert (working_set_mb(os.getpid()) or 0) > 0


def test_reset_clears_results(scanner):
    scanner.reset()
    assert scanner.has_results is False
