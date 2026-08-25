"""封包切包：把一段 TCP 位元組流切成一個個封包（[PKT-043]）。"""

from __future__ import annotations

from ro_toolbox.core.ro_packet import split_packets

# ---- 精確切包（[PKT-043]）------------------------------------------------

_LENGTHS = {
    0x0080: (7, 7),      # 固定長度
    0x0087: (12, 12),
    0x01C8: (15, 15),
    0x0B08: (-1, 5),     # 可變長度：真正的長度寫在 opcode 後面 2 bytes
}


def _fixed(opcode: int, size: int, filler: int = 0xAA) -> bytes:
    return opcode.to_bytes(2, "little") + bytes([filler]) * (size - 2)


def _variable(opcode: int, size: int) -> bytes:
    return opcode.to_bytes(2, "little") + size.to_bytes(2, "little") + b"\xbb" * (size - 4)


def test_without_a_table_the_whole_segment_is_one_packet():
    """沒有長度表時維持舊行為，呼叫端不必改。"""
    data = _fixed(0x0080, 7) + _fixed(0x0087, 12)
    assert split_packets(data) == [(0x0080, data)]


def test_glued_packets_are_split_exactly():
    """一個 TCP 分段裡黏著三個封包 —— 舊行為只看得到第一個。"""
    parts = [_fixed(0x0080, 7), _fixed(0x0087, 12), _fixed(0x01C8, 15)]
    got = split_packets(b"".join(parts), _LENGTHS)
    assert [op for op, _b in got] == [0x0080, 0x0087, 0x01C8]
    assert [b for _op, b in got] == parts


def test_the_use_item_reply_is_visible_behind_another_packet():
    """實測踩過的那個 bug：`0x01C8` 黏在別的封包後面就完全看不到。"""
    reply = _fixed(0x01C8, 15)
    got = split_packets(_fixed(0x0080, 7) + reply, _LENGTHS)
    assert (0x01C8, reply) in got


def test_variable_length_packet_uses_its_own_length_field():
    listing = _variable(0x0B08, 40)
    got = split_packets(listing + _fixed(0x0080, 7), _LENGTHS)
    assert [op for op, _b in got] == [0x0B08, 0x0080]
    assert got[0][1] == listing


def test_unknown_opcode_hands_back_the_rest_instead_of_guessing():
    """不認得的 opcode 無法安全往下切 —— 把剩下的整段交出去，不亂猜。"""
    rest = (0x9999).to_bytes(2, "little") + b"\x01\x02\x03"
    got = split_packets(_fixed(0x0080, 7) + rest, _LENGTHS)
    assert got == [(0x0080, _fixed(0x0080, 7)), (0x9999, rest)]


def test_packet_cut_at_the_segment_boundary_is_not_forced():
    """封包被切在分段邊界上時，不硬切成錯的長度。"""
    truncated = _fixed(0x01C8, 15)[:9]
    got = split_packets(_fixed(0x0080, 7) + truncated, _LENGTHS)
    assert got[-1] == (0x01C8, truncated)


def test_empty_and_tiny_input():
    assert split_packets(b"", _LENGTHS) == []
    assert split_packets(b"\x01", _LENGTHS) == []
