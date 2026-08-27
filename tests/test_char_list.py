"""角色清單（0x0B72）解析。

版面是 2026-08-25 實機擷取來的，**兩份獨立擷取交叉驗證過**：
清單說「格號 4 = 狐狐狸、地圖 prontera.gat」，另一份擷取裡選角送出
0x0066 格號 4、伺服器回 0x0AC5 說地圖 prontera.gat。
"""

from __future__ import annotations

from ro_toolbox.services import char_list


def _entry(name: str, slot: int, game_map: str = "prontera.gat") -> bytes:
    e = bytearray(char_list.ENTRY_SIZE)
    e[108:108 + len(name.encode("cp950"))] = name.encode("cp950")
    e[132:138] = bytes([31, 1, 1, 1, 40, 1])          # 六圍
    e[138:140] = slot.to_bytes(2, "little")
    e[142:142 + len(game_map)] = game_map.encode()
    return bytes(e)


def _packet(*entries: bytes) -> bytes:
    body = b"".join(entries)
    return (len(body) + 4).to_bytes(2, "little") + body


def test_entry_size_is_the_measured_one():
    """177 - 2 = 175，一筆；527 - 2 = 525 = 3 筆。都整除才對得上。"""
    assert char_list.ENTRY_SIZE == 175
    assert (177 - 2) % char_list.ENTRY_SIZE == 0
    assert (527 - 2) // char_list.ENTRY_SIZE == 3


def test_parses_the_real_layout():
    rows = char_list.parse(_packet(_entry("狐狐狸", 4)))
    assert [(c.name, c.slot) for c in rows] == [("狐狐狸", 4)]


def test_parses_multiple_entries_in_one_packet():
    """實測一包 3 筆（雪狐u / 雪色狐狸 / 光狐），順序不是格號順序。"""
    packet = _packet(_entry("雪狐u", 2), _entry("雪色狐狸", 1), _entry("光狐", 3))
    rows = char_list.parse(packet)
    assert [(c.name, c.slot) for c in rows] == [("雪狐u", 2), ("雪色狐狸", 1), ("光狐", 3)]


def test_name_is_cp950_not_utf8():
    """抄錯編碼會變亂碼，而且之後拿角色名比對就永遠對不上。"""
    rows = char_list.parse(_packet(_entry("狐狐狸", 4)))
    assert rows[0].name == "狐狐狸"
    assert "狐狐狸".encode("cp950").hex() == "aab0aab0af57"


def test_map_is_readable():
    packet = _packet(_entry("雪色狐狸", 1, "moc_fild01.gat"))
    assert char_list.map_of(packet, 0) == "moc_fild01.gat"


def test_bad_size_is_refused_not_guessed():
    """長度不是 175 的整數倍 → 版面變了。整包不採用，不要硬解出錯位的格號。"""
    assert char_list.parse(b"\x10\x00" + b"\x00" * 100) == []


def test_absurd_slot_refuses_the_whole_packet():
    """格號超出範圍代表欄位錯位了 —— 回一個錯的格號會安靜地選到別隻角色。"""
    assert char_list.parse(_packet(_entry("怪怪", 300))) == []


def test_short_payload_is_empty_not_an_error():
    assert char_list.parse(b"") == []
    assert char_list.parse(b"\x02") == []
