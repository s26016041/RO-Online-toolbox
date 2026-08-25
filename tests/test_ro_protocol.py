"""RO 封包協定的編碼／解碼測試。"""

from __future__ import annotations

import pytest

from ro_toolbox.core.ro_protocol import (
    CZ_REQUEST_MOVE,
    build_move,
    opcode_of,
    pack_position,
    unpack_position,
)


@pytest.mark.parametrize(
    "hexstr, expected",
    [
        ("28D020", (163, 258, 0)),  # 狐狐狸實際擷取
        ("3283C0", (202, 60, 0)),   # 白雪狐實際擷取
    ],
)
def test_unpack_matches_captured(hexstr, expected):
    assert unpack_position(bytes.fromhex(hexstr)) == expected


@pytest.mark.parametrize("hexstr", ["28D020", "3283C0"])
def test_pack_is_inverse_of_unpack(hexstr):
    raw = bytes.fromhex(hexstr)
    x, y, direction = unpack_position(raw)
    assert pack_position(x, y, direction) == raw


def test_build_move_has_opcode_and_length():
    packet = build_move(166, 258)
    assert opcode_of(packet) == CZ_REQUEST_MOVE
    assert len(packet) == 5  # 2 opcode + 3 座標
    assert unpack_position(packet[2:])[:2] == (166, 258)


def test_roundtrip_across_map_range():
    for x, y in [(1, 1), (399, 399), (200, 60), (0, 0), (511, 511)]:
        assert unpack_position(pack_position(x, y))[:2] == (x, y)


def test_build_attack_matches_captured():
    from ro_toolbox.core.ro_protocol import (
        ACT_ATTACK_CONT,
        CZ_REQUEST_ACT,
        build_attack,
        parse_target_id,
    )

    # 實際擷取：0x0437 + F6 0D 00 00 07（攻擊 ID 3574，連續）
    packet = build_attack(3574, ACT_ATTACK_CONT)
    assert packet == bytes.fromhex("3704") + bytes.fromhex("F60D000007")
    assert parse_target_id(packet[2:]) == 3574
    assert CZ_REQUEST_ACT == 0x0437


def test_build_pickup_matches_captured():
    from ro_toolbox.core.ro_protocol import CZ_ITEM_PICKUP, build_pickup, parse_target_id

    # 實際擷取：0x0362 + 6E D7 00 00（撿 ID 55150）
    packet = build_pickup(55150)
    assert packet == bytes.fromhex("6203") + bytes.fromhex("6ED70000")
    assert parse_target_id(packet[2:]) == 55150
    assert CZ_ITEM_PICKUP == 0x0362
