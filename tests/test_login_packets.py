"""登入用的封包：二次密碼的亂序表、狀態碼、選角。

這一支盯的是**實機對照**：所有數字都來自真人登入的擷取檔，不是推的。
演算法改壞了會在這裡當場爆掉，而不是等到登入時把角色卡在線上。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import login_packets as lp

#: 三組實機樣本 —— 使用者的二次密碼固定是 8291，每次送出去的四位數都不一樣，
#: 因為那是「按鍵在那一輪亂序鍵盤上的位置」。
#:
#:     查爾斯.txt   seed 0x05FCC4B1 → 送出 "3289"
#:     全整登入.txt seed 0x064C7C55 → 送出 "3547"
#:
#: 前兩組是當初反推常數用的，第三組（0x064C7C55）是**事後另一次登入**，
#: 拿來驗證那組常數不是硬湊出來的。
PIN = "8291"
SAMPLES = [
    (0x05760EA1, "5367"),
    (0x05796F02, "8623"),
    (0x05FCC4B1, "3289"),
    (0x064C7C55, "3547"),
]


@pytest.mark.parametrize(("seed", "expected"), SAMPLES)
def test_pin_matches_the_real_captures(seed, expected):
    assert lp.encode_pin(seed, PIN) == expected


def test_the_keypad_is_a_permutation():
    """亂序表必須是 0–9 的重排 —— 少一個數字就會送出錯的位置。"""
    for seed, _ in SAMPLES:
        assert sorted(lp.shuffled_keypad(seed)) == list(range(10))


def test_different_seeds_give_different_layouts():
    """每一輪都要重算。拿上一輪的表去編這一輪＝送出錯的密碼。"""
    layouts = {tuple(lp.shuffled_keypad(seed)) for seed, _ in SAMPLES}
    assert len(layouts) == len(SAMPLES)


def test_pin_state_all_zero_means_ok():
    """實機對照：輸入正確時伺服器回一包全零的 0x08B9。"""
    assert lp.pin_state(bytes(10)) == lp.PIN_STATE_OK


def test_pin_state_one_means_please_type_it():
    """要求輸入那一包：seed(4) + AID(4) + 01 00。"""
    payload = bytes.fromhex("557C4C060B516B010100")
    assert lp.pin_state(payload) == lp.PIN_STATE_ASK
    assert lp.pin_seed(payload) == 0x064C7C55


def test_short_payload_is_unknown_not_zero():
    """讀不出來要回 None。當成 0（＝通過）就會在沒過的時候往下選角。"""
    assert lp.pin_state(b"\x00\x00") is None


def test_the_ok_reply_is_not_mistaken_for_a_seed():
    """全零那包的 seed 是 0 —— 不能拿它去算亂序表（踩過）。"""
    assert lp.pin_seed(bytes(10)) is None


def test_select_character_packet_is_opcode_plus_one_byte():
    """實機對照：0x0066 整包 3 bytes，payload 就一個格號。"""
    packet = lp.select_character_packet(4)
    assert packet == bytes.fromhex("660004")
    assert len(packet) == 3


def test_pin_packet_layout():
    """實機對照：0x08B8 = AID(4) + 四個 ASCII 數字。"""
    packet = lp.pin_packet(0x016B510B, "3547")
    assert packet == bytes.fromhex("B808") + bytes.fromhex("0B516B01") + b"3547"
