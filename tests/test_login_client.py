"""不開遊戲直接跟伺服器要角色清單：封包組裝與版面解析。

這一支不連網路 —— 版面全部照實機擷取（GAMEDATA [PKT-059]）組出來再解回去。
"""

from __future__ import annotations

import struct

import pytest

from ro_toolbox.services import login_client
from ro_toolbox.services.accounts import Account
from ro_toolbox.services.totp import parse as parse_secret

SECRET = "otpauth://totp/GRAVITY:demo?secret=JBSWY3DPEHPK3PXP&issuer=GRAVITY"
BLOB = "288a6e486c3a6ee8fc17aa7be70b101c4e4b0b10df56a97c"


def _account(**kwargs) -> Account:
    base = {
        "name": "測試", "username": "s26016041", "password": "pw",
        "secret": parse_secret(SECRET)[0], "password_blob": BLOB,
    }
    base.update(kwargs)
    return Account(**base)


def test_login_packet_matches_the_real_capture():
    """實機 #14：version 01 00 00 00 + 帳號[24] + 密文[24] + clienttype 05。"""
    payload = login_client._login_packet(_account())
    assert len(payload) == 53                       # 加上 opcode 才是 55
    assert payload[:4] == bytes([1, 0, 0, 0])
    assert payload[4:28].split(b"\x00")[0] == b"s26016041"
    assert payload[28:52].hex() == BLOB
    assert payload[52] == 5


def test_a_wrong_sized_blob_is_refused():
    """密文長度不對就不要送 —— 送出去只會被伺服器打回票，還多一次登入紀錄。"""
    with pytest.raises(login_client.LoginClientError):
        login_client._login_packet(_account(password_blob="00" * 10))
    with pytest.raises(login_client.LoginClientError):
        login_client._login_packet(_account(password_blob=""))


def _accept_packet(servers: list[tuple[str, str, int]]) -> bytes:
    """照實機 #19 的版面組一包 0x0B60（payload，不含 opcode）。"""
    body = bytearray(login_client._ACCEPT_SERVERS)
    struct.pack_into("<I", body, login_client._ACCEPT_LOGIN_ID1, 0x00000401)
    struct.pack_into("<I", body, login_client._ACCEPT_AID, 0x016B510B)
    struct.pack_into("<I", body, login_client._ACCEPT_LOGIN_ID2, 0x31203332)
    body[login_client._ACCEPT_SEX] = 1
    for name, host, port in servers:
        entry = bytearray(login_client._SERVER_ENTRY)
        # ⚠ IP 欄位刻意填**內網位址** —— 實機就是這樣，解析時必須忽略它。
        entry[0:4] = bytes([192, 168, 204, 82])
        struct.pack_into("<H", entry, login_client._ENTRY_PORT, port)
        raw = name.encode("cp950")
        entry[login_client._ENTRY_NAME:login_client._ENTRY_NAME + len(raw)] = raw
        text = f"{host}:{port}".encode("ascii")
        entry[login_client._ENTRY_HOST:login_client._ENTRY_HOST + len(text)] = text
        body += entry
    struct.pack_into("<H", body, 0, len(body) + 2)
    return bytes(body)


def test_server_list_uses_the_hostname_not_the_ip():
    """清單裡的 IP 是伺服器的**內網位址**（實機讀到 192.168.204.82）——
    要連的是主機名字串，靠 DNS 解析。用 IP 會連到自己的區網。"""
    payload = _accept_packet([
        ("查爾斯", "twro-char2.gnjoy.com.tw", 10029),
        ("波利", "twro-char3.gnjoy.com.tw", 10029),
    ])
    servers = login_client._parse_servers(payload)
    assert servers == [
        ("查爾斯", "twro-char2.gnjoy.com.tw", 10029),
        ("波利", "twro-char3.gnjoy.com.tw", 10029),
    ]


def test_the_ticket_fields_come_out_right():
    payload = _accept_packet([("查爾斯", "twro-char2.gnjoy.com.tw", 10029)])
    assert struct.unpack_from("<I", payload, login_client._ACCEPT_AID)[0] == 0x016B510B
    assert struct.unpack_from(
        "<I", payload, login_client._ACCEPT_LOGIN_ID1
    )[0] == 0x00000401
    assert payload[login_client._ACCEPT_SEX] == 1


def test_a_truncated_server_list_is_skipped_not_guessed():
    """半截的條目直接跳過 —— 硬湊出來的主機名會連到不存在的地方。"""
    payload = _accept_packet([("查爾斯", "twro-char2.gnjoy.com.tw", 10029)])
    assert login_client._parse_servers(payload[:-40]) == []


def test_no_length_table_says_what_to_do(monkeypatch):
    """沒有封包長度表就切不了包 —— 要講出「先用遊戲登入一次」，不是丟一句失敗。"""
    monkeypatch.setattr(
        login_client.packet_table, "load_any_cached", lambda: None
    )
    with pytest.raises(login_client.LoginClientError) as caught:
        login_client._lengths()
    assert "先用遊戲" in str(caught.value)
