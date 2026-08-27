"""伺服器對照表。

資料來源是 2026-08-25 的實機擷取（登入後伺服器推的那一包，164-byte 條目）。
名稱是 **cp950**，這一點被 `test_names_are_the_cp950_bytes_from_the_capture`
釘住 —— 抄錯編碼的話選單會顯示亂碼，而且送出去的識別值會對不上。
"""

from __future__ import annotations

from ro_toolbox.services import servers


def test_table_is_populated():
    assert servers.known()
    assert servers.names() == ["查爾斯", "波利"]


def test_names_are_the_cp950_bytes_from_the_capture():
    """封包裡實際看到的位元組：查爾斯 = ac64bab8b4b5、波利 = aa69a751。"""
    assert "查爾斯".encode("cp950").hex() == "ac64bab8b4b5"
    assert "波利".encode("cp950").hex() == "aa69a751"


def test_code_is_the_position_in_the_list():
    assert servers.code_of("查爾斯") == 0
    assert servers.code_of("波利") == 1


def test_unknown_name_returns_none_not_a_default():
    """查不到回 None —— 呼叫端要拒絕動作，不准拿 0 當預設值送出去。"""
    assert servers.code_of("不存在的伺服器") is None


def test_every_entry_records_where_it_came_from():
    """鐵則：抄進程式的遊戲資料一定要留出處。"""
    for server in servers.KNOWN:
        assert server.source, f"{server.name} 沒有註明來源"
        assert server.host.endswith(":10029")
