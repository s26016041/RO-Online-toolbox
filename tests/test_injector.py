from __future__ import annotations

import os

import pytest

from ro_toolbox.core.intercept import CodeRange, InterceptedPacket
from ro_toolbox.services.injector import (
    TargetUnsupportedError,
    build_stub_asm,
    inspect_target,
)


def test_stub_assembles_with_keystone():
    """stub 是手寫組語，語法錯了要在這裡就抓到，而不是注入時把遊戲弄崩。"""
    keystone = pytest.importorskip("keystone")

    asm = build_stub_asm(wcnt=0x10000000, ring=0x10000040, origp=0x10000004)
    ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_32)
    ks.syntax = keystone.KS_OPT_SYNTAX_INTEL

    encoding, _count = ks.asm(asm, addr=0x10005000)
    assert encoding, "stub 組譯結果不該是空的"
    assert len(encoding) > 100, "stub 明顯太短，可能有指令被吃掉"


def test_inspect_rejects_64bit_executable():
    """64 位元目標要在解析階段就擋下，附帶說明原因。"""
    pytest.importorskip("pefile")

    exe = r"C:\Windows\System32\notepad.exe"
    if not os.path.exists(exe):
        pytest.skip("找不到用來測試的 64 位元執行檔")

    with pytest.raises(TargetUnsupportedError) as info:
        inspect_target(exe)
    assert "32 位元" in str(info.value)


def _packet(frames: list[int], data: bytes = b"AB") -> InterceptedPacket:
    return InterceptedPacket(
        seq=1,
        timestamp=1_756_000_000.0,
        caller=0x401234,
        length=len(data),
        data=data,
        frames=frames,
        args=[(0, 0, 0, 0, 0)] * len(frames),
        code_range=CodeRange(low=0x401000, high=0x7D0000),
    )


def test_call_chain_keeps_only_game_code_addresses():
    packet = _packet([0x401500, 0x77001234, 0x402000, 0])
    assert packet.call_chain == [0x401500, 0x402000]


def test_call_chain_deduplicates():
    packet = _packet([0x401500, 0x401500, 0x402000])
    assert packet.call_chain == [0x401500, 0x402000]


def test_call_chain_empty_without_code_range():
    packet = _packet([0x401500])
    object.__setattr__(packet, "code_range", None)
    assert packet.call_chain == []


def test_truncated_flag():
    packet = _packet([0x401500], data=b"1234")
    packet.length = 900
    assert packet.truncated is True


def test_packed_detection_on_ro_executable():
    """RO 的執行檔有加殼，錯誤訊息要說明原因而不是只說『找不到 send』。"""
    pytest.importorskip("pefile")

    exe = r"D:\ro\RagnarokOnline\Ragexe.exe"
    if not os.path.exists(exe):
        pytest.skip("找不到 RO 執行檔")

    with pytest.raises(TargetUnsupportedError) as info:
        inspect_target(exe)
    message = str(info.value)
    assert "找不到 send" in message
    assert "加殼" in message, "應該要偵測出加殼並說明"


def test_looks_packed_heuristic():
    from ro_toolbox.services.injector import _looks_packed

    class Entry:
        def __init__(self, n):
            self.imports = [None] * n

    class Fake:
        def __init__(self, counts):
            self.DIRECTORY_ENTRY_IMPORT = [Entry(c) for c in counts]

    assert _looks_packed(Fake([1] * 37)) is True          # RO 的樣子
    assert _looks_packed(Fake([50, 30, 20, 40, 60])) is False   # 正常執行檔
    assert _looks_packed(Fake([1, 1])) is False           # DLL 太少不判定
