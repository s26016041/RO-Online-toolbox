"""自動尋路的座標來源（不需遊戲）。

只釘一件事，但那件事讓補水整個卡死過：**換圖之後該相信誰**。
"""

from __future__ import annotations

import struct

import numpy as np

from ro_toolbox.core.ro_packet import RoPacket
from ro_toolbox.services.mapdata import MapTerrain
from ro_toolbox.services.travel_bot import TravelBot


def _terrain(width: int, height: int) -> MapTerrain:
    """一張全部可走的圖（type 0 = 可走） —— 重點是「殘留座標照樣站得住」那個情境。"""
    return MapTerrain(name="test", width=width, height=height,
                      types=np.zeros((height, width), dtype=np.uint32),
                      source="asset")


def _packet(opcode: int, payload: bytes) -> RoPacket:
    return RoPacket(seq=1, timestamp=0.0, outbound=False,
                    opcode=opcode, payload=payload)


def _move(x0: int, y0: int, x1: int, y1: int) -> bytes:
    """0x0087 的 6-byte「從哪走到哪」（版面見 [PKT-064]）。"""
    return bytes([
        x0 >> 2,
        ((x0 & 0x03) << 6) | (y0 >> 4),
        ((y0 & 0x0F) << 4) | (x1 >> 6),
        ((x1 & 0x3F) << 2) | (y1 >> 8),
        y1 & 0xFF,
        0,
    ])




# ---- 換圖之後：伺服器說的座標優先（不然會卡在門口）------------------------


def test_the_servers_word_wins_right_after_a_map_change(monkeypatch):
    """換圖那幾拍，記憶體的座標是上一張圖的殘留 —— 而且**照樣站得住**。

    實機踩過：從 izlude 走進 izlude_in，記憶體停在 (114,177)，伺服器
    `0x0091` 明明說了 (65,87)。舊版因為 (114,177) 在 izlude_in 上也站得住
    就直接採用，A* 從錯的起點算 → 「走不到目的地」→ **卡在門口，
    要人手動走一步才會動**（使用者實測回報）。
    """
    bot = TravelBot(1234)
    terrain = _terrain(200, 200)
    monkeypatch.setattr(bot, "_terrain_for", lambda _m: terrain)

    bot._server_pos = (65, 87)
    bot._server_pos_map = "izlude_in"
    bot._entry_fresh = True
    assert bot._trusted_position("izlude_in", (114, 177)) == (65, 87)


def test_memory_wins_again_once_the_character_has_moved(monkeypatch):
    """走過一步之後記憶體會逐格更新，比 `0x0087` 的「這一段起點」新。"""
    bot = TravelBot(1234)
    terrain = _terrain(200, 200)
    monkeypatch.setattr(bot, "_terrain_for", lambda _m: terrain)

    bot._server_pos = (65, 87)
    bot._server_pos_map = "izlude_in"
    bot._entry_fresh = False
    assert bot._trusted_position("izlude_in", (114, 177)) == (114, 177)


def test_a_map_move_packet_marks_the_position_fresh():
    bot = TravelBot(1234)
    payload = b"izlude_in.gat\x00\x00\x00" + struct.pack("<HH", 65, 87)
    bot._on_packet(_packet(0x0091, payload))
    assert bot._entry_fresh
    assert bot._server_pos == (65, 87)
    assert bot._server_pos_map == "izlude_in"


def test_a_move_ack_clears_it():
    """角色動了 —— 之後以記憶體為準。"""
    bot = TravelBot(1234)
    bot._entry_fresh = True
    bot._on_packet(_packet(0x0087, b"\x00" * 4 + _move(65, 87, 70, 90)))
    assert not bot._entry_fresh
