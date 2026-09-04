"""送封包失敗時的日誌節流（不需要真的 socket）。

釘的是使用者實測回報的那一幕：連線被伺服器 reset 之後，
15 秒噴了 40 行**一模一樣**的「第 1 次」訊息 —— 節流寫了等於沒寫。

原因是計數只用錯誤碼當鍵，而且「任何一次成功就整個清空」。多開的時候
另外兩隻一直在成功送封包，於是壞掉那一隻的計數每一拍都被歸零。

⚠ 2026-09-05 起送封包走核心（`WriteFile`），這裡換掉的是 `_write`
（回 `(送出位元組數, Win32 錯誤碼)`），不再有 ws2_32（[PKT-097]）。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import game_socket


@pytest.fixture(autouse=True)
def _clean():
    game_socket._send_errors.clear()
    yield
    game_socket._send_errors.clear()


def _install(monkeypatch, broken: set[int]) -> set[int]:
    """假的核心寫入：想讓哪一條 socket 失敗就把它放進 `broken`。"""
    monkeypatch.setattr(
        game_socket, "_write",
        lambda sock, data: (-1, 64) if sock in broken else (len(data), 0),
    )
    return broken


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
    broken = _install(monkeypatch, {7})
    with caplog.at_level("ERROR"):
        game_socket.send_on_socket(7, b"x")
        broken.clear()
        game_socket.send_on_socket(7, b"x")         # 好了
        broken.add(7)
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


def test_an_empty_socket_is_reported_as_our_own_bug(monkeypatch, caplog):
    """`send(None)` 跟「遊戲把那條關掉了」以前長得一模一樣（都是 10038）——要分開講。"""
    _install(monkeypatch, set())
    with caplog.at_level("ERROR"):
        assert game_socket.send_on_socket(None, b"x") == -1
    assert "呼叫端的錯" in caplog.records[0].getMessage()


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

    game_socket.close_socket(0x29C)

    assert k32.closed == [0x29C]


def test_the_module_never_loads_ws2_32():
    """⛔⛔ 這一支不准碰 ws2_32（[PKT-097]）。

    `getpeername` 對複製來的 handle 照**第一次**的快取回答（被關掉、值被回收
    發給別的物件都照舊），`send` 的 10038 分不出「遊戲關的」還是「我們綁錯的」，
    `closesocket` 會把遊戲的連線一起關掉（[PKT-094]）。三件事都改走核心。
    """
    import inspect

    assert not hasattr(game_socket, "_ws2")
    source = inspect.getsource(game_socket)
    assert "ws2_32\"" not in source and "windll.ws2_32" not in source


def test_close_socket_forgets_that_sockets_error_count(monkeypatch):
    """handle 值會被系統回收再發給別條連線，計數不清會讓節流提早吞錯誤。"""
    _install(monkeypatch, {7})
    game_socket.send_on_socket(7, b"x")
    assert any(key[0] == 7 for key in game_socket._send_errors)

    monkeypatch.setattr(game_socket, "_k32", _FakeK32())
    game_socket.close_socket(7)

    assert not any(key[0] == 7 for key in game_socket._send_errors)
