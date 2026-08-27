"""記憶體掃描核心的自我測試。

用本行程當目標：建立一個已知值的 ctypes 變數，透過 ReadProcessMemory /
WriteProcessMemory 讀寫它，確認整條 Win32 路徑是通的。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

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


# ---- 模組表：不准卡死呼叫端 ------------------------------------------------
#
# 實際災情（2026-08-26）：在記憶體分頁選了 RO 之後整個工具箱凍住。
# py-spy 抓到主執行緒停在 list_modules → EnumProcessModulesEx ——
# 那個 Win32 呼叫對掛 GameGuard 的行程**會卡住不回來**，而它當時是被
# `open()` 直接在 UI 執行緒上呼叫的。


def test_open_does_not_enumerate_modules(monkeypatch):
    """`open()` 不准列舉模組 —— 那一步會把呼叫端（UI 執行緒）卡死。"""
    from ro_toolbox.services import memory_scan

    called = []
    monkeypatch.setattr(
        memory_scan.MemoryScanner, "list_modules", lambda self: called.append(1) or []
    )
    monkeypatch.setattr(memory_scan.kernel32, "OpenProcess", lambda *a: 0x1234)
    monkeypatch.setattr(memory_scan.MemoryScanner, "_is_wow64", staticmethod(lambda h: True))

    scanner = memory_scan.MemoryScanner()
    scanner.open(4242)
    scanner._handle = None                      # 別讓 close() 去關假 handle
    assert not called, "open() 不該列舉模組（會卡死 UI）"


def test_list_modules_gives_up_instead_of_hanging(monkeypatch):
    """底層卡住時要在逾時內放棄並回空清單，不是陪它一起卡住。"""
    import time

    from ro_toolbox.services import memory_scan

    monkeypatch.setattr(memory_scan, "_MODULE_TIMEOUT", 0.2)
    monkeypatch.setattr(
        memory_scan.MemoryScanner,
        "_list_modules_blocking",
        lambda self: time.sleep(30) or [],      # 模擬 GameGuard 讓它回不來
    )
    scanner = memory_scan.MemoryScanner()
    started = time.monotonic()
    assert scanner.list_modules() == []
    assert time.monotonic() - started < 5, "卡住的模組查詢沒有在逾時內放棄"


def test_module_base_looks_it_up_on_demand(monkeypatch):
    """模組表改成要用到才查；查過一次就不再重查。"""
    from ro_toolbox.services import memory_scan

    calls = []

    def fake_list(self):
        calls.append(1)
        return [memory_scan.ModuleInfo(name="Ragexe.exe", base=0x400000, size=0x1000, path="")]

    monkeypatch.setattr(memory_scan.MemoryScanner, "list_modules", fake_list)
    scanner = memory_scan.MemoryScanner()
    assert scanner.module_base("ragexe.exe") == 0x400000
    assert scanner.module_base("ragexe.exe") == 0x400000
    assert len(calls) == 1, "模組表應該只查一次"


def test_blocked_module_query_is_remembered_across_scanners(monkeypatch, caplog):
    """被 GameGuard 擋住之後，**冷卻期內不再重試也不再記一次**。

    以前「查過就不再查」是記在 scanner 實例上，但呼叫端到處都在開新的
    （`submitted_account()` 每次一顆、`game_census.take()` 每個實例一顆、
    帳號頁每三秒查一次連線）—— 每一顆都要重付一次 3 秒逾時、各噴一行 WARNING。
    使用者實測看到的就是每隔幾秒洗一行同樣的訊息，而且每次逾時還會留下
    一條卡死在 `EnumProcessModulesEx` 裡的執行緒。
    """
    import logging

    from ro_toolbox.services import memory_scan

    tries = []
    monkeypatch.setattr(memory_scan, "_MODULE_TIMEOUT", 0.05)
    monkeypatch.setattr(memory_scan, "_module_blocked", {})
    monkeypatch.setattr(
        memory_scan.MemoryScanner,
        "_list_modules_blocking",
        lambda self: (tries.append(1), time.sleep(30))[0] or [],   # 真的卡住
    )

    def make():
        scanner = memory_scan.MemoryScanner()
        scanner._pid = 4321          # 假裝附加在同一個行程上
        return scanner

    with caplog.at_level(logging.WARNING, logger="ro_toolbox.services.memory_scan"):
        assert make().list_modules() == []
        assert make().list_modules() == []
        assert make().list_modules() == []

    assert len(tries) == 1, "冷卻期內不該再去問一次（每次都會卡住並留下執行緒）"
    said = [r for r in caplog.records if "列舉模組" in r.message]
    assert len(said) == 1, "同一個行程被擋著，不該每隔幾秒洗一行"


def test_exe_base_prefers_scanning_over_the_blocked_module_table(monkeypatch):
    """主程式的基底一律**先掃描**，不要先問模組表。

    GameGuard 會擋模組列舉（[MEM-031]），被擋時 `list_modules()` 要卡滿逾時
    才回空清單。`submitted_account()` 原本是直接叫 `image_base_by_scan()`
    （全程不碰列舉），改用 `locate_global()` 之後就走進 `module_base()` ——
    而它掛在帳號頁每三秒一次的連線查詢上，於是每三秒卡一次、噴一行警告
    （使用者實際回報）。
    """
    from ro_toolbox.services import memory_scan

    listed = []

    def fake_list(self):
        listed.append(1)
        return [memory_scan.ModuleInfo(name="Ragexe.exe", base=0xBAD, size=0x10, path="")]

    monkeypatch.setattr(memory_scan.MemoryScanner, "list_modules", fake_list)
    monkeypatch.setattr(memory_scan.MemoryScanner, "attached", property(lambda self: True))
    monkeypatch.setattr(
        memory_scan.MemoryScanner, "image_base_by_scan", lambda self: 0x400000
    )
    scanner = memory_scan.MemoryScanner()
    assert scanner.module_base("ragexe.exe") == 0x400000
    assert listed == [], "掃得到就不該去碰會被擋的模組列舉"


def test_exe_base_falls_back_to_the_module_table_when_scan_fails(monkeypatch):
    """掃不到才回頭問模組表 —— 兩條路都要留著。"""
    from ro_toolbox.services import memory_scan

    monkeypatch.setattr(
        memory_scan.MemoryScanner,
        "list_modules",
        lambda self: [
            memory_scan.ModuleInfo(name="Ragexe.exe", base=0x500000, size=0x10, path="")
        ],
    )
    monkeypatch.setattr(memory_scan.MemoryScanner, "attached", property(lambda self: True))
    monkeypatch.setattr(memory_scan.MemoryScanner, "image_base_by_scan", lambda self: None)
    scanner = memory_scan.MemoryScanner()
    assert scanner.module_base("ragexe.exe") == 0x500000
