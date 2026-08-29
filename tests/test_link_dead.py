"""連線被伺服器 reset 之後要**認輸**，不要每拍重試到天亮。

回歸測試，症狀是使用者掛了一小時之後回報的：

    ERROR   | game_socket | send 失敗，WSA 錯誤 10054
    WARNING | game_link   | 送封包失敗，socket 可能已失效，強制重新綁定
    …（同樣兩行 5,185 次）

10054 是「連線被對方重設」。麻煩的是那條連線**還留在 TCP 表裡**，
所以 `find_server()` 照樣查得到、`resync()` 每次都「重綁成功」——
綁回同一條死的，然後再失敗一次。沒有人喊停，日誌也被沖光。
"""

from __future__ import annotations

import logging

import pytest

from ro_toolbox.services import game_link, game_socket
from ro_toolbox.services.game_link import DEAD_AFTER_SEC, GameLink

SERVER = ("219.84.200.101", 10009)
OTHER = ("219.84.200.102", 10022)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def link(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(game_link.time, "monotonic", clock)
    made = GameLink(1234)
    made.sock = 42
    made.server = SERVER
    return made, clock


def _fails(monkeypatch, yes: bool = True) -> None:
    monkeypatch.setattr(game_link.game_socket, "send_on_socket",
                        lambda _s, d: -1 if yes else len(d))


def test_one_failure_is_not_a_dead_link(link, monkeypatch):
    """換頻道的那一兩拍也會送失敗 —— 那是正常的，重綁一下就好。"""
    made, _clock = link
    _fails(monkeypatch)
    assert made.send(b"\x01\x02") is False
    assert not made.dead


def test_failing_for_long_enough_is_a_dead_link(link, monkeypatch, caplog):
    made, clock = link
    _fails(monkeypatch)
    with caplog.at_level(logging.WARNING):
        made.send(b"\x01\x02")
        clock.t += DEAD_AFTER_SEC + 1
        made.send(b"\x01\x02")
    assert made.dead
    assert "已經斷了" in caplog.text


def test_a_successful_send_clears_it(link, monkeypatch):
    made, clock = link
    _fails(monkeypatch)
    made.send(b"\x01")
    clock.t += DEAD_AFTER_SEC + 1
    made.send(b"\x01")
    assert made.dead

    _fails(monkeypatch, yes=False)
    made.sock = 42
    assert made.send(b"\x01") is True
    assert not made.dead


def test_rebinding_to_the_same_dead_connection_does_not_count(link, monkeypatch):
    """⚠ 這就是那 5,185 行的根因：綁回同一條死連線也算「重綁成功」。"""
    made, clock = link
    _fails(monkeypatch)
    made.send(b"\x01")
    clock.t += DEAD_AFTER_SEC + 1
    made.send(b"\x01")
    assert made.dead

    monkeypatch.setattr(game_link, "find_server", lambda _pid: SERVER)
    monkeypatch.setattr(game_link.game_socket, "open_game_socket",
                        lambda *a, **k: 99)
    problem = made.resync()
    assert problem and "已中斷" in problem


def test_a_genuinely_new_connection_still_rebinds(link, monkeypatch):
    """換頻道是真的換了一條 —— 那時候當然要重綁，而且重綁完就不算死。"""
    made, clock = link
    _fails(monkeypatch)
    made.send(b"\x01")
    clock.t += DEAD_AFTER_SEC + 1
    made.send(b"\x01")
    assert made.dead

    monkeypatch.setattr(game_link, "find_server", lambda _pid: OTHER)
    monkeypatch.setattr(game_link.game_socket, "open_game_socket",
                        lambda *a, **k: 99)
    monkeypatch.setattr(game_link.game_socket, "close_socket", lambda _s: None)
    assert made.resync() is None
    assert not made.dead
    assert made.server == OTHER


def test_the_same_wsa_error_is_not_logged_every_time(monkeypatch, caplog):
    """一小時 5,185 行會把真正的原因沖掉 —— 同一個錯誤只吼一次。"""
    monkeypatch.setattr(game_socket, "_send_errors", {})
    monkeypatch.setattr(game_socket._ws2, "send", lambda *a: -1)
    monkeypatch.setattr(game_socket._ws2, "WSAGetLastError", lambda: 10054)

    with caplog.at_level(logging.ERROR):
        for _ in range(50):
            game_socket.send_on_socket(42, b"\x01")
    assert caplog.text.count("10054") == 1
