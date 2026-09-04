"""換地圖伺服器之後，我們複製來的那份 socket 已經**不是 socket 了**（[PKT-096]）。

使用者 2026-09-04：「這個是怎樣為何會這樣我看我的角色正常在打怪物阿」——
他是對的，**角色沒事**：壞掉的不是遊戲那條連線，是我們手上那份複本。

換地圖時伺服器把角色搬到另一台 map server，客戶端對舊連線呼叫
`closesocket()`。那一刻我們 `DuplicateHandle` 來的 handle 就不再是 socket，
`send()` 回的是 **WSA 10038（WSAENOTSOCK）**，不是連線斷掉的 10054。

舊版只能**撞牆才知道**，而且 `resync()` 判斷「連線換了沒」是比對 (ip, port)：
遊戲重連到**同一台** map server 時那組值一模一樣，比對不出來 ——
只能等 `send()` 失敗把 `self.server` 清成 None 才會重綁。實機代價：

    13:35:03  send 失敗，WSA 錯誤 10038 → ⚠ 寄信送不出去（連線斷了？）
    13:35:11  send 失敗，WSA 錯誤 10038 → ⚠ 寄信送不出去（連線斷了？）
    13:35:22  send 失敗，WSA 錯誤 10038 → 20 秒後再試
    13:35:53  終於寄出                      ← 一趟換圖害寄信晚了 50 秒

修法：送之前先問一句 `getpeername`（微秒級、唯讀、跟 send 同一條判準）。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import game_link, game_socket

SERVER = ("219.84.200.101", 10009)


class _FakeCapture:
    def __init__(self, pid, on_packet):  # noqa: D107 - 測試替身
        self.on_packet = on_packet

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass


class _FakeReader:
    position_located = True

    def attach(self, pid, should_stop=None) -> bool:  # noqa: ARG002
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def link(monkeypatch):
    """一個已經接上的 `GameLink`，socket 是遞增的假 handle。"""
    handles = iter(range(100, 200))
    monkeypatch.setattr(game_link, "find_server", lambda _pid: SERVER)
    monkeypatch.setattr(game_link, "find_servers", lambda _pid: [SERVER])
    monkeypatch.setattr(game_link, "CharacterReader", _FakeReader)
    monkeypatch.setattr(game_link, "PacketCapture", _FakeCapture)
    monkeypatch.setattr(game_link.game_socket, "open_any_game_socket",
                        lambda pid, servers, **kw: (next(handles), servers[0]))
    monkeypatch.setattr(game_link.game_socket, "close_socket", lambda sock: None)
    obj = game_link.GameLink(1234)
    assert obj.open() is None
    return obj


def test_a_live_copy_is_left_alone(link):
    """正常情況：端點沒變、複本還活著 —— 不准重綁（重綁要 0.6 秒起跳）。"""
    sock = link.sock
    assert link.resync() is None
    assert link.sock == sock
    assert link.rebound is False


def test_the_same_endpoint_still_rebinds_when_our_copy_was_closed(
    link, monkeypatch
):
    """★★ 端點一模一樣，但遊戲把我們那份關掉了 —— 一定要重綁。

    這是實機那條路：換地圖伺服器後遊戲重連到**同一台**，
    `find_server()` 回一樣的 (ip, port)，舊版就說「沒變」然後繼續拿廢掉的
    handle 送封包，直到 `send()` 撞出 10038。
    """
    sock = link.sock
    monkeypatch.setattr(game_socket, "socket_alive", lambda _sock: False)
    assert link.resync() is None
    assert link.sock != sock, "要換一份新的複本"
    assert link.rebound is True


def test_the_probe_asks_the_os_not_our_own_bookkeeping():
    """`socket_alive()` 問的是作業系統 —— 不是 socket 就回 False。

    ⚠ 這裡直接測底下那一支（`_peer_of`）：conftest 有個 autouse 的替身把
    `socket_alive` 換掉了（不然每支測試的假整數 socket 都會被判成死的），
    所以透過名字拿到的是替身，不是真貨。
    """
    assert game_socket._peer_of(0x7FFF_FFF0) is None, "不是 socket 就要問不出 peer"


def test_a_reset_connection_is_not_confused_with_a_closed_copy(link, monkeypatch):
    """⚠ 連線被對方 reset（10054）時 socket 還在，`getpeername` 照樣成功 ——
    那條路歸 `dead` 管，兩者不可以混在一起。"""
    monkeypatch.setattr(game_link.game_socket, "send_on_socket",
                        lambda sock, data: -1)
    sock = link.sock
    assert link.send(b"\x9f\x03") is False
    # 送失敗會把 server 清成 None 逼重綁，但**不是**因為複本被關掉。
    assert link.alive() is True
    assert link.sock == sock
