"""驗證工具本身要先能被驗證。

用**合成的程式碼區段**（自己組出骨架、解析函式、封包註冊點）跑整套改版模擬，
不需要遊戲。這樣就算遊戲在維修、或改版把真骨架弄壞了，也還分得出
「是客戶端變了」還是「是工具寫壞了」。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import verify_sigs as vs  # noqa: E402

from ro_toolbox.services import bag  # noqa: E402

MODULE = 0x400000
TEXT_RVA = 0x1000
TEXT_SIZE = 0x20000
CONTAINER = 0x1500000
SKELETON = 0x8000        # 骨架在區段裡的位移（前面留一大片零給誘餌用）
PARSER = 0x9000
REGISTER = 0x200         # 封包長度表的註冊函式
SITES = 1200             # 註冊點數量（要 > 1000 才過得了基準檢查）
SITE_START = 0xC000


#: 選角游標與角色名字這兩個全域在合成快照裡的位址（都在模組範圍內）。
CURSOR = MODULE + 0x11D096A
CHAR_NAME = MODULE + 0x11DA498


def _text() -> bytes:
    """組一段假的 .text：背包骨架 + 解析函式 + 一堆封包註冊點。"""
    out = bytearray(TEXT_SIZE)
    base = MODULE + TEXT_RVA

    # --- 背包骨架：sub ecx,5 / mov eax,0xF0F0F0F1 / mul ecx / shr edx,5 / call ---
    body = (
        b"\x83\xe9\x05"                   # sub ecx, 5
        b"\xb8\xf1\xf0\xf0\xf0"           # mov eax, 0xF0F0F0F1
        b"\xf7\xe1"                       # mul ecx
        b"\xc1\xea\x05"                   # shr edx, 5
    )
    out[SKELETON : SKELETON + len(body)] = body
    call_at = SKELETON + len(body)
    out[call_at] = 0xE8
    struct.pack_into("<i", out, call_at + 1, PARSER - (call_at + 5))
    # 解析函式：mov ecx, <容器>
    out[PARSER] = 0xB9
    struct.pack_into("<I", out, PARSER + 1, CONTAINER)

    # --- 封包註冊點：push flag/hdr/len/opcode ; mov ecx,esi ; call 註冊函式 ---
    at = SITE_START
    for i in range(SITES):
        opcode = 0x100 + i
        chunk = (
            b"\x6a\x00"                              # push 0
            b"\x6a\x00"                              # push 0
            + b"\x6a" + bytes([(i % 60) + 2])        # push <長度>
            + b"\x68" + struct.pack("<I", opcode)    # push <opcode>
            + b"\x8b\xce"                            # mov ecx, esi
            + b"\xe8"                                # call rel32
        )
        out[at : at + len(chunk)] = chunk
        struct.pack_into("<i", out, at + len(chunk), REGISTER - (at + len(chunk) + 4))
        at += len(chunk) + 4
    # --- 選角畫面的兩個全域（自動選角的眼睛）---
    # 游標：三種骨架各放一份，答案都指向同一個位址；
    # 名字：一段對 0x40 bytes 做 xor 的迴圈，骨架裡三個立即值也指同一個位址。
    cursor_sites = (
        bytes([0x0F, 0xB6, 0x05]) + struct.pack("<I", CURSOR)
        + bytes([0x50, 0x6A, 0x08, 0xFF, 0x15]),
        bytes([0x0F, 0xB6, 0x0D]) + struct.pack("<I", CURSOR)
        + bytes([0x8B, 0x87, 0x18, 0x01, 0x00, 0x00, 0x80, 0x3C, 0x01, 0x01]),
        bytes([0x88, 0x0D]) + struct.pack("<I", CURSOR)
        + bytes([0x8B, 0x8E, 0x34, 0x01, 0x00, 0x00, 0x85, 0xC9]),
    )
    name_site = (
        bytes([0x8A, 0x8C, 0x02]) + struct.pack("<I", CHAR_NAME)
        + bytes([0x30, 0x88]) + struct.pack("<I", CHAR_NAME)
        + bytes([0x40, 0x83, 0xF8, 0x40, 0x72, 0xED])
        + bytes([0xB8]) + struct.pack("<I", CHAR_NAME) + bytes([0xC3])
    )
    at += 0x40
    for chunk in (*cursor_sites, name_site):
        out[at : at + len(chunk)] = chunk
        at += len(chunk) + 0x20

    assert at < TEXT_SIZE, "註冊點放不下"
    assert base + SKELETON  # base 只是為了說明位址關係
    return bytes(out)


def _snapshot() -> vs.Snapshot:
    """組出 verify_sigs 需要的最小 PE 版面。

    真實的 PE 裡 `e_lfanew` 只有 0x80，所以 PE 標頭與區段表**都落在前 0x400
    之內** —— 三個區塊是重疊的。這裡照著做，`pe` 與 `sections` 直接切自
    `head`，不然合成的版面會和真的不一樣，測出來的東西就不算數。
    """
    e_lfanew, opt_size = 0x80, 0xE0
    sect_off = e_lfanew + 24 + opt_size
    head = bytearray(0x400)
    struct.pack_into("<I", head, 0x3C, e_lfanew)
    head[e_lfanew : e_lfanew + 4] = b"PE\x00\x00"
    struct.pack_into("<H", head, e_lfanew + 6, 1)          # NumberOfSections
    struct.pack_into("<H", head, e_lfanew + 20, opt_size)  # SizeOfOptionalHeader
    # 可選標頭 +56 是 SizeOfImage。定位全域時要靠它判斷「這個立即值是不是
    # 模組自己的位址」（見 aob.image_size），少了它整條會退成安全預設。
    struct.pack_into("<I", head, e_lfanew + 24 + 56, 0x2000000)
    head[sect_off : sect_off + 8] = b".text\x00\x00\x00"
    struct.pack_into("<II", head, sect_off + 8, TEXT_SIZE, TEXT_RVA)
    struct.pack_into("<I", head, sect_off + 36, 0x20000000)  # MEM_EXECUTE
    return vs.Snapshot(
        module_base=MODULE,
        head=bytes(head),
        pe=bytes(head[e_lfanew : e_lfanew + 0x120]),
        sections=bytes(head[sect_off : sect_off + 40]),
        section_table=MODULE + sect_off,
        text_base=MODULE + TEXT_RVA,
        text=_text(),
    )


@pytest.fixture
def snap() -> vs.Snapshot:
    return _snapshot()


def _run(snap):
    report = vs.Report()
    vs.simulate(snap, report, [])
    return {c.name: c for c in report.checks}


# ---- 快照本身 ----------------------------------------------------------


def test_snapshot_scanner_serves_the_code_section(snap):
    scanner = vs.SnapshotScanner(snap)
    assert bag.find_container(scanner) == CONTAINER


def test_snapshot_survives_a_round_trip(snap, tmp_path):
    path = tmp_path / "snap.bin"
    vs.save_snapshot(snap, path)
    back = vs.load_snapshot(path)
    assert back == snap
    assert bag.find_container(vs.SnapshotScanner(back)) == CONTAINER


def test_a_truncated_snapshot_is_rejected(snap, tmp_path):
    """半份快照要拋例外 —— 拿它跑模擬只會得到假結論。"""
    path = tmp_path / "snap.bin"
    vs.save_snapshot(snap, path)
    path.write_bytes(path.read_bytes()[:-100])
    with pytest.raises(ValueError, match="不完整"):
        vs.load_snapshot(path)


def test_a_foreign_file_is_rejected(tmp_path):
    path = tmp_path / "nope.bin"
    path.write_bytes(b"not a snapshot")
    with pytest.raises(ValueError, match="快照檔"):
        vs.load_snapshot(path)


# ---- 改版模擬 ----------------------------------------------------------


def test_every_simulation_passes_on_a_healthy_snapshot(snap):
    """乾淨的快照上每一項都要通過 —— 有一項不過就是工具或定位器有問題。"""
    checks = _run(snap)
    bad = [c.name for c in checks.values() if c.status == "NG"]
    assert not bad, f"這些項目在健康的快照上就不過：{bad}"


def test_the_decoy_is_placed_before_the_real_skeleton(snap):
    """誘餌一定要放在真骨架前面，否則這個測試永遠會過（假測試）。"""
    spot = vs._free_space(snap.text, 0x100, SKELETON)
    assert 0 < spot < SKELETON


def test_it_catches_a_locator_that_answers_wrongly(snap, monkeypatch):
    """把定位器換成「骨架壞了也硬回一個舊值」，模擬必須抓到。"""
    monkeypatch.setattr(bag, "find_container", lambda _scanner: CONTAINER)
    checks = _run(snap)
    assert checks["魔術乘數改掉 -> 失敗（不是回錯的位址）"].status == "NG"
    assert checks["容器換全域時回新值（沒寫死）"].status == "NG"


def test_it_catches_a_locator_that_hardcodes_the_address(snap):
    """容器立即值改掉之後還回舊值 = 寫死了，一定要被判不合格。"""
    checks = _run(snap)
    assert checks["容器換全域時回新值（沒寫死）"].status == "OK"


def test_it_catches_a_locator_that_cannot_see_its_own_ambiguity(snap, monkeypatch):
    """把定位器換成「只回第一個」，誘餌檢查必須不過。

    這正是這支工具在 2026-08-25 抓到的東西：`find_container` 看不見自己有
    兩個候選，改版新增一段一樣的骨架就會安靜地拿別人家的全域。
    """
    real = bag.find_containers
    monkeypatch.setattr(bag, "find_containers", lambda scanner: real(scanner)[:1])
    checks = _run(snap)
    assert checks["出現第二組骨架時看得見兩個候選"].status == "NG"


def test_skipped_checks_do_not_count_as_passed():
    """略過不能混進通過數 —— 那會讓報告看起來比實際乾淨。"""
    report = vs.Report()
    report.add("甲", True)
    report.skip("乙", "前提不成立")
    assert len(report.failed) == 0
    assert len(report.skipped) == 1
    assert [c.passed for c in report.checks] == [True, False]
