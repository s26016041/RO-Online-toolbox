"""送封包失敗時的日誌節流（不需要真的 socket）。

釘的是使用者實測回報的那一幕：連線被伺服器 reset（WSA 10054）之後，
15 秒噴了 40 行**一模一樣**的「第 1 次」訊息 —— 節流寫了等於沒寫。

原因是計數只用錯誤碼當鍵，而且「任何一次成功就整個清空」。多開的時候
另外兩隻一直在成功送封包，於是壞掉那一隻的計數每一拍都被歸零。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import game_socket


class _FakeWs2:
    """假的 ws2_32：想讓哪一條 socket 失敗就把它放進 `broken`。"""

    def __init__(self, broken: set[int]) -> None:
        self._broken = broken

    def send(self, sock, _buf, length, _flags):  # noqa: ANN001
        return -1 if sock in self._broken else length

    @staticmethod
    def WSAGetLastError() -> int:  # noqa: N802 - 照 Win32 的名字
        return 10054


@pytest.fixture(autouse=True)
def _clean():
    game_socket._send_errors.clear()
    yield
    game_socket._send_errors.clear()


def _install(monkeypatch, broken: set[int]) -> None:
    monkeypatch.setattr(game_socket, "_ws2", _FakeWs2(broken))


def test_the_same_error_is_only_shouted_once(monkeypatch, caplog):
    _install(monkeypatch, {7})
    with caplog.at_level("ERROR"):
        for _ in range(20):
            game_socket.send_on_socket(7, b"x")
    assert len(caplog.records) == 1, "同一條 socket 的同一個錯誤只准吼一次"


def test_another_sockets_success_does_not_reset_the_count(monkeypatch, caplog):
    """⚠ 這就是實機那 40 行的成因：多開時別人一直送成功。"""
    _install(monkeypatch, {7})
    with caplog.at_level("ERROR"):
        for _ in range(20):
            game_socket.send_on_socket(7, b"x")     # 壞掉的那一條
            game_socket.send_on_socket(8, b"x")     # 隔壁那隻，好好的
    assert len(caplog.records) == 1, "別人送得出去不代表我這條好了"


def test_my_own_success_clears_my_count(monkeypatch, caplog):
    """自己這條接回來了就重新起算 —— 下一次真的斷線要再吼一次。"""
    ws2 = _FakeWs2({7})
    monkeypatch.setattr(game_socket, "_ws2", ws2)
    with caplog.at_level("ERROR"):
        game_socket.send_on_socket(7, b"x")
        ws2._broken.clear()
        game_socket.send_on_socket(7, b"x")         # 好了
        ws2._broken.add(7)
        game_socket.send_on_socket(7, b"x")         # 又壞了
    assert len(caplog.records) == 2


def test_a_long_outage_still_gets_a_summary(monkeypatch, caplog):
    """一直壞下去要定期補一行，不然看不出它還在壞。"""
    _install(monkeypatch, {7})
    with caplog.at_level("ERROR"):
        for _ in range(game_socket._SEND_ERROR_EVERY):
            game_socket.send_on_socket(7, b"x")
    assert len(caplog.records) == 2
    assert "已經連續" in caplog.records[-1].getMessage()


class _ExplodingWs2:
    """任何 ws2_32 呼叫都當場炸掉 —— 收尾時不准碰到它。"""

    def __getattr__(self, name: str):  # noqa: ANN204
        raise AssertionError(
            f"close_socket() 不准呼叫 ws2_32.{name}："
            "closesocket 會把遊戲的連線一起關掉"
        )


class _FakeK32:
    def __init__(self) -> None:
        self.closed: list[int] = []

    def CloseHandle(self, handle) -> int:  # noqa: ANN001, N802 - 照 Win32 的名字
        self.closed.append(handle.value if hasattr(handle, "value") else handle)
        return 1


def test_close_socket_only_releases_our_handle(monkeypatch):
    """⛔ 釘住 2026-09-03 那個 bug：關掉工具會把使用者的 RO 一起弄斷線。

    複製來的 handle 跟遊戲手上那個是同一條連線的兩個 handle。
    `CloseHandle` 只放掉我們這一份；`closesocket` 是「關掉這個 socket」，
    遊戲的連線會跟著收掉。
    """
    k32 = _FakeK32()
    monkeypatch.setattr(game_socket, "_k32", k32)
    monkeypatch.setattr(game_socket, "_ws2", _ExplodingWs2())

    game_socket.close_socket(0x29C)

    assert k32.closed == [0x29C]


def test_close_socket_forgets_that_sockets_error_count(monkeypatch):
    """handle 值會被系統回收再發給別條連線，計數不清會讓節流提早吞錯誤。"""
    monkeypatch.setattr(game_socket, "_ws2", _FakeWs2({7}))
    game_socket.send_on_socket(7, b"x")
    assert any(key[0] == 7 for key in game_socket._send_errors)

    monkeypatch.setattr(game_socket, "_k32", _FakeK32())
    monkeypatch.setattr(game_socket, "_ws2", _ExplodingWs2())
    game_socket.close_socket(7)

    assert not any(key[0] == 7 for key in game_socket._send_errors)
