from __future__ import annotations

from ro_toolbox.core.packet import CapturedPacket
from ro_toolbox.utils.hexdump import (
    encryption_verdict,
    format_packet,
    format_packets,
    hexdump,
)


def make_packet(index: int = 1, outbound: bool = True, payload: bytes = b"AB") -> CapturedPacket:
    return CapturedPacket(
        index=index,
        timestamp=1_756_000_000.123,
        src_ip="192.168.1.20",
        src_port=52134,
        dst_ip="175.41.10.5",
        dst_port=6900,
        outbound=outbound,
        payload=payload,
    )


def test_hexdump_empty():
    assert hexdump(b"") == "(無資料)"


def test_hexdump_layout():
    lines = hexdump(bytes(range(20))).splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("0000  00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F")
    assert lines[0].endswith("|................|")
    assert lines[1].startswith("0010  10 11 12 13")


def test_hexdump_ascii_column_shows_printable():
    assert hexdump(b"RO\x00!").endswith("|RO.!|")


def test_format_packet_header_has_direction_and_length():
    text = format_packet(make_packet(payload=b"1234"))
    header = text.splitlines()[0]
    assert "#1" in header
    assert "送出" in header
    assert "192.168.1.20:52134 -> 175.41.10.5:6900" in header
    assert "len=4" in header


def test_format_packets_summary_counts_both_directions():
    packets = [
        make_packet(1, outbound=True, payload=b"ab"),
        make_packet(2, outbound=False, payload=b"cdef"),
    ]
    text = format_packets(packets, title="ro.exe")
    assert "# 筆數：2（送出 1 / 接收 1）" in text
    assert "# 位元組：6" in text
    assert "ro.exe" in text


def test_format_packets_empty():
    assert format_packets([]) == "(沒有封包)"


def test_entropy_zero_for_empty_payload():
    assert make_packet(payload=b"").entropy() == 0.0


def test_entropy_higher_for_random_than_text():
    text = make_packet(payload=b"A" * 200)
    random_bytes = make_packet(payload=bytes(range(256)))
    assert random_bytes.entropy() > text.entropy()


def test_verdict_needs_enough_data():
    assert "太少" in encryption_verdict([make_packet(payload=b"short")])


def test_verdict_calls_structured_data_plaintext():
    payload = b"GET /login HTTP/1.1\r\nHost: ro.example.com\r\n\r\n" + bytes(40)
    verdict = encryption_verdict([make_packet(payload=payload)])
    assert "明文" in verdict


def test_verdict_flags_high_entropy_as_encrypted():
    import os

    packets = [make_packet(i, payload=os.urandom(512)) for i in range(1, 5)]
    verdict = encryption_verdict(packets)
    assert "加密" in verdict
