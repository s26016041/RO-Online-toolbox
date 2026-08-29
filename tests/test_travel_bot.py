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


# ---- 座標讀不到：不准安靜地空轉 --------------------------------------------


def test_an_unreadable_position_eventually_nudges(monkeypatch):
    """⚠ 實機踩過（2026-08-29，白狐）：換到 mjolnir_06 之後移動元件失效，

    而「走一步就會接上」的那一步**永遠不會發生** —— 要走路得先知道自己在哪。
    舊版每一拍靜靜 `continue`，日誌整整 42 秒一行都沒有，最後是使用者自己
    走一步再按一次尋路才救回來。出口跟換圖那條一樣：**推一步問位置**。
    """
    from ro_toolbox.services import travel_bot as mod

    bot = TravelBot(1234)
    terrain = _terrain(200, 200)
    monkeypatch.setattr(bot, "_terrain_for", lambda _m: terrain)
    nudged = []
    monkeypatch.setattr(bot, "_nudge", lambda t, m: nudged.append(m))

    clock = {"now": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])

    bot._no_position("mjolnir_06")
    assert nudged == [], "第一拍只記時間 —— 換圖那一兩拍讀不到是正常的"

    clock["now"] += mod._POS_LOST_SEC + 0.1
    bot._no_position("mjolnir_06")
    assert nudged == ["mjolnir_06"], "撐過去就要推一步，不能繼續空轉"


def test_no_terrain_still_says_something(monkeypatch):
    """沒有地形就推不動 —— 那也要**講出來**，不准安靜地停在那裡。"""
    from ro_toolbox.services import travel_bot as mod

    bot = TravelBot(1234)
    monkeypatch.setattr(bot, "_terrain_for", lambda _m: None)
    notes = []
    monkeypatch.setattr(bot, "_note", lambda text, level=0: notes.append(text))

    clock = {"now": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    bot._no_position("mjolnir_06")
    clock["now"] += mod._POS_LOST_SEC + 0.1
    bot._no_position("mjolnir_06")

    assert notes and "讀不到角色座標" in notes[-1]


def test_the_lost_position_warning_is_said_once_not_every_tick(monkeypatch, caplog):
    """⚠ 實機每秒噴兩行（使用者：「看起來好怪」）。

    `_note()` 只擋「跟上一句一樣」，而 `_nudge()` 自己也會講話 ——
    兩句輪流出現就等於兩句都沒被擋到。一次斷線只准講一句。
    """
    from ro_toolbox.services import travel_bot as mod

    bot = TravelBot(1234)
    terrain = _terrain(200, 200)
    monkeypatch.setattr(bot, "_terrain_for", lambda _m: terrain)
    monkeypatch.setattr(bot, "_nudge", lambda t, m: None)
    clock = {"now": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])

    with caplog.at_level("WARNING"):
        for _ in range(30):
            bot._no_position("mjolnir_06")
            clock["now"] += 1.0
            bot._stats.note = "別的訊息"     # 模擬 `_nudge` 插話
    said = [r for r in caplog.records if "讀不到角色座標" in r.getMessage()]
    assert len(said) == 1, f"只准講一次，實際 {len(said)} 次"
