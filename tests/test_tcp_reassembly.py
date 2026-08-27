"""TCP 重組：一個 RO 封包可能跨分段。

實測 2026-08-25：伺服器的登入回應 `0x0B60` 宣告自己 392 bytes，
卻被切成 **64 + 328** 兩段送達。沒有重組的話：

- 第一段被當成一個 64 bytes 的 0x0B60 —— 內容被截斷
- 第二段的開頭兩個 byte 被當成新的 opcode —— 冒出 0xA8C0、0x5FF8、0x96E7
  這種不可能存在的 opcode，而且**內容整個錯位**

伺服器清單就是這樣被誤讀的（[PKT-050]）。
"""

from __future__ import annotations

from ro_toolbox.core.ro_packet import split_stream

#: opcode → (長度, 標頭)。長度 < 0 代表可變長度（真正長度在 opcode 後 2 bytes）。
#: 這幾筆是從客戶端抽出來的實際值。
LENGTHS = {
    0x0B60: (-1, 64),    # 可變長度，登入回應
    0x0064: (55, 55),    # 固定，帳號密碼
    0x0187: (6, 6),      # 固定，心跳
}


def _variable(opcode: int, total: int) -> bytes:
    """組一個宣告長度為 total 的可變長度封包。"""
    body = bytes(total - 4)
    return opcode.to_bytes(2, "little") + total.to_bytes(2, "little") + body


def _fixed(opcode: int, total: int) -> bytes:
    return opcode.to_bytes(2, "little") + bytes(total - 2)


def test_packet_split_across_segments_is_reassembled():
    """0x0B60 的真實情況：宣告 392，分成 64 + 328 送達。"""
    whole = _variable(0x0B60, 392)
    first, second = whole[:64], whole[64:]

    packets, leftover = split_stream(first, LENGTHS)
    assert packets == [], "還沒到齊就不該吐出來"
    assert leftover == first, "沒切完的要整段留著"

    packets, leftover = split_stream(leftover + second, LENGTHS)
    assert len(packets) == 1
    assert packets[0][0] == 0x0B60
    assert len(packets[0][1]) == 392, "重組後要是完整的 392 bytes"
    assert leftover == b""


def test_without_reassembly_the_next_segment_becomes_a_bogus_opcode():
    """這一條記錄「不重組會發生什麼」——第二段開頭被當成新 opcode。"""
    whole = _variable(0x0B60, 392)
    second = whole[64:]
    bogus = int.from_bytes(second[:2], "little")
    assert bogus not in LENGTHS, "第二段的開頭本來就不是合法 opcode"


def test_several_packets_in_one_segment_all_come_out():
    """黏在同一段裡的封包要全部切出來，不能只看到第一個（[PKT-043]）。"""
    data = _fixed(0x0187, 6) + _fixed(0x0064, 55) + _fixed(0x0187, 6)
    packets, leftover = split_stream(data, LENGTHS)
    assert [op for op, _ in packets] == [0x0187, 0x0064, 0x0187]
    assert leftover == b""


def test_trailing_partial_packet_is_kept_not_emitted():
    data = _fixed(0x0187, 6) + _fixed(0x0064, 55)[:20]
    packets, leftover = split_stream(data, LENGTHS)
    assert [op for op, _ in packets] == [0x0187]
    assert len(leftover) == 20


def test_declared_length_not_yet_arrived():
    """連宣告長度那 2 bytes 都還沒到齊，也要留著等下一段。"""
    packets, leftover = split_stream(b"\x60\x0b\x88", LENGTHS)
    assert packets == []
    assert leftover == b"\x60\x0b\x88"


def test_unknown_opcode_gives_up_instead_of_hoarding():
    """失去同步時把剩下的交出去（至少看得到），不要留成尾巴無限累積。"""
    packets, leftover = split_stream(b"\xff\xee" + bytes(10), LENGTHS)
    assert len(packets) == 1
    assert leftover == b"", "不認得就不要留著，否則緩衝會一直長"


def test_absurd_declared_length_gives_up():
    """宣告長度比標頭還小 → 一定是錯位了，不要照著切下去。"""
    bad = (0x0B60).to_bytes(2, "little") + (3).to_bytes(2, "little") + bytes(60)
    packets, leftover = split_stream(bad, LENGTHS)
    assert len(packets) == 1
    assert leftover == b""


def test_no_length_table_keeps_old_behaviour():
    """沒有長度表時維持舊行為：整段當一包，不留尾巴。"""
    packets, leftover = split_stream(b"\x60\x0b" + bytes(20), None)
    assert len(packets) == 1
    assert leftover == b""
