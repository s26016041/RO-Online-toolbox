"""socket 的身分要**問核心**，不能問 ws2_32（[PKT-097]，2026-09-05）。

使用者：「為何又來」—— 補水第二次撞到「send 失敗 10038 → 15 秒判定斷線」，
[PKT-096] 那次以為是「遊戲換地圖伺服器把我們的複本關掉」，修了 `socket_alive()`
（`getpeername`）也沒用。實機用本機 socket 量出真相：

    複本 A（連到 :57967）              getpeername → :57967   send → OK
    CloseHandle(A)                     getpeername → :57967   send → 10038
    複製 B（連到 :57968），拿到同一個值  getpeername → :57967（錯的！）

ws2_32 對「不是它自己建立的 handle」照**第一次**看到那個值時的快取回答，
而 handle 值一關掉就立刻回收發給下一個 `DuplicateHandle`。
所以 `find_game_socket()` 一輪「複製 → 問 → 關」拿到的都是同一個值，
整輪只有第一個 socket 的身分是真的 —— 綁錯 handle → 10038，
問「活著沒」→ 快取說活著 → 永遠不重綁。

修法：身分（本機埠）、活著沒、送封包三件事都改走核心
（`NtDeviceIoControlFile(AFD_GET_SOCK_NAME)`、`WriteFile`）。
這裡用真的本機 socket 釘住這三件事，另外用替身釘 `find_game_socket()` 的挑法
與 `GameLink.resync()` 的「本機埠變了就重綁」。
"""

from __future__ import annotations

import ctypes
import socket
from ctypes import wintypes

import pytest

from ro_toolbox.services import game_link, game_socket
from ro_toolbox.services.process_monitor import Connection

SERVER = ("219.84.200.100", 10001)


def _dup(fd: int) -> int:
    me = game_socket._k32.GetCurrentProcess()
    out = wintypes.HANDLE()
    assert game_socket._k32.DuplicateHandle(
        me, wintypes.HANDLE(fd), me, ctypes.byref(out),
        0, False, game_socket._DUPLICATE_SAME_ACCESS,
    )
    return int(out.value)


@pytest.fixture
def pair():
    """兩條連到本機的 TCP 連線：`(client, server_side)` × 2。"""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    made = []
    for _ in range(2):
        client = socket.create_connection(listener.getsockname())
        server_side, _ = listener.accept()
        server_side.settimeout(1.0)
        made.append((client, server_side))
    yield made
    for client, server_side in made:
        client.close()
        server_side.close()
    listener.close()


# ---- 真的 socket：三件事都要老實 -------------------------------------------------

def test_the_kernel_reports_the_true_local_port_of_our_copy(pair):
    (client, _), _ = pair
    handle = _dup(client.fileno())
    try:
        assert game_socket._local_port_of(handle) == client.getsockname()[1]
        assert game_socket.socket_local_port(handle) == client.getsockname()[1]
    finally:
        game_socket.close_socket(handle)


def test_a_closed_copy_is_reported_dead_and_a_reused_value_gets_the_new_identity(pair):
    """★★ 這就是 ws2_32 做不到的兩件事：關掉後說死、值被回收後說新的身分。"""
    (a, _), (b, _) = pair
    handle_a = _dup(a.fileno())
    assert game_socket._local_port_of(handle_a) == a.getsockname()[1]
    game_socket.close_socket(handle_a)
    assert game_socket._local_port_of(handle_a) is None, "被關掉的 handle 要老實說不是 socket"

    handle_b = _dup(b.fileno())
    try:
        # 值多半會被回收成同一個（實測連續三次都一樣）；不管有沒有重用，
        # 回答都必須是 **B 的**埠，不是 A 的。
        assert game_socket._local_port_of(handle_b) == b.getsockname()[1]
    finally:
        game_socket.close_socket(handle_b)


def test_writing_through_the_kernel_delivers_the_bytes(pair):
    (client, server_side), _ = pair
    handle = _dup(client.fileno())
    try:
        assert game_socket.send_on_socket(handle, b"\x9f\x03hello") == 7
        assert server_side.recv(16) == b"\x9f\x03hello"
    finally:
        game_socket.close_socket(handle)


def test_writing_to_a_closed_copy_fails_with_invalid_handle(pair, caplog):
    (client, _), _ = pair
    handle = _dup(client.fileno())
    game_socket.close_socket(handle)
    sent, err = game_socket._write(handle, b"x")
    assert (sent, err) == (-1, 6), "ERROR_INVALID_HANDLE —— 這是我們自己的 bug，不是連線"


def test_a_non_socket_handle_is_not_mistaken_for_one():
    """`find_game_socket()` 以前會把事件／檔案 handle 當成遊戲 socket 回來（快取害的）。"""
    event = game_socket._k32.CreateEventW(None, True, False, None)
    try:
        assert game_socket._local_port_of(event) is None
        assert game_socket._write(event, b"x")[0] == -1
    finally:
        game_socket._k32.CloseHandle(wintypes.HANDLE(event))


def test_socket_alive_is_a_kernel_question(pair, monkeypatch):
    """conftest 把 `socket_alive` 換成替身；這裡把真貨拿回來對真 socket 問一次。"""
    from ro_toolbox.services import game_socket as real

    monkeypatch.undo()          # 拿掉 autouse 的替身
    (client, _), _ = pair
    handle = _dup(client.fileno())
    assert real.socket_alive(handle) is True
    real.close_socket(handle)
    assert real.socket_alive(handle) is False


# ---- find_game_socket：挑「最新那條」的本機埠，逐個問核心 ------------------------

class _FakeK32:
    def __init__(self) -> None:
        self.closed: list[int] = []

    def OpenProcess(self, *_args) -> int:  # noqa: N802 - 照 Win32 的名字
        return 0x999

    def CloseHandle(self, handle) -> int:  # noqa: ANN001, N802
        self.closed.append(handle.value if hasattr(handle, "value") else handle)
        return 1


def test_find_game_socket_matches_by_kernel_local_port(monkeypatch):
    k32 = _FakeK32()
    duplicated: list[int] = []
    ports = {0x101: 5000, 0x102: 6000, 0x103: 6000}
    monkeypatch.setattr(game_socket, "_k32", k32)
    monkeypatch.setattr(game_socket, "_enum_handles", lambda pid: [1, 2, 3])
    monkeypatch.setattr(game_socket, "_dup_from",
                        lambda source, value: duplicated.append(value) or value + 0x100)
    monkeypatch.setattr(game_socket, "_local_port_of", ports.get)
    monkeypatch.setattr(game_socket, "_newest_local_port", lambda pid, ip, port: 6000)

    assert game_socket.find_game_socket(1234, *SERVER) == 0x102
    assert duplicated == [1, 2], "找到就停，不要把剩下的也複製一遍"
    assert k32.closed == [0x101, 0x999], "不是的那份要放掉，行程 handle 也要放掉"


def test_find_game_socket_gives_up_when_the_tcp_table_has_no_such_connection(monkeypatch):
    monkeypatch.setattr(game_socket, "_newest_local_port", lambda pid, ip, port: None)
    monkeypatch.setattr(game_socket, "_enum_handles",
                        lambda pid: pytest.fail("TCP 表裡沒有就不該去列舉 handle"))
    assert game_socket.find_game_socket(1234, *SERVER) is None


def test_the_newest_connection_wins_when_two_go_to_the_same_server(monkeypatch):
    """舊連線在我們握著複本的期間不會消失（[PKT-063] 的 11 分鐘），
    兩條端點一模一樣 —— 只准挑最新建立的那條。"""
    rows = [
        Connection(ip=SERVER[0], port=SERVER[1], created=200, established=True, local_port=52000),
        Connection(ip=SERVER[0], port=SERVER[1], created=100, established=True, local_port=51000),
        Connection(ip="1.2.3.4", port=443, created=300, established=True, local_port=53000),
    ]
    monkeypatch.setattr(game_socket, "connections_of", lambda pid: rows)
    assert game_socket._newest_local_port(1234, *SERVER) == 52000


# ---- GameLink：端點沒變，但本機埠變了 = 遊戲換了一條 ----------------------------

class _FakeCapture:
    def __init__(self, pid, on_packet):  # noqa: D107 - 測試替身
        pass

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
    handles = iter(range(100, 200))
    monkeypatch.setattr(game_link, "find_server", lambda _pid: SERVER)
    monkeypatch.setattr(game_link, "find_servers", lambda _pid: [SERVER])
    monkeypatch.setattr(game_link, "CharacterReader", _FakeReader)
    monkeypatch.setattr(game_link, "PacketCapture", _FakeCapture)
    monkeypatch.setattr(game_link.game_socket, "open_any_game_socket",
                        lambda pid, servers, **kw: (next(handles), servers[0]))
    monkeypatch.setattr(game_link.game_socket, "close_socket", lambda sock: None)
    monkeypatch.setattr(game_link.game_socket, "socket_local_port", lambda sock: 51000)
    obj = game_link.GameLink(1234)
    assert obj.open() is None
    assert obj.local_port == 51000
    return obj


def _newest(local_port: int) -> Connection:
    return Connection(ip=SERVER[0], port=SERVER[1], created=1, established=True,
                      local_port=local_port)


def test_same_endpoint_same_local_port_is_left_alone(link, monkeypatch):
    monkeypatch.setattr(game_link, "find_connection", lambda _pid: _newest(51000))
    sock = link.sock
    assert link.resync() is None
    assert link.sock == sock and link.rebound is False


def test_same_endpoint_but_a_new_local_port_rebinds(link, monkeypatch):
    """★ 遊戲重連到同一台伺服器：(ip, port) 一樣、我們的複本還活著（核心物件
    被我們握著），但遊戲早就在用另一條 —— 只有本機埠分得出來。"""
    monkeypatch.setattr(game_link, "find_connection", lambda _pid: _newest(52000))
    sock = link.sock
    assert link.resync() is None
    assert link.sock != sock and link.rebound is True


def test_the_caller_supplied_endpoint_is_also_checked_against_the_local_port(link, monkeypatch):
    """`farm_bot` 會把自己讀到的端點傳進來 —— 那條路也要比本機埠。"""
    monkeypatch.setattr(game_link, "find_connection", lambda _pid: _newest(52000))
    assert link.resync(SERVER) is None
    assert link.rebound is True


def test_an_unknown_local_port_falls_back_to_the_old_behaviour(link, monkeypatch):
    """問不到本機埠（舊系統、TCP 表拿不到）就當沒變 —— 寧可少重綁也不要一直重綁。"""
    link.local_port = 0
    monkeypatch.setattr(game_link, "find_connection", lambda _pid: _newest(52000))
    assert link.resync() is None
    assert link.rebound is False


def test_closing_clears_the_field_before_releasing_the_handle(monkeypatch):
    """handle 值一關掉就會被回收發給別人 —— 先清欄位再關，兩個執行緒才不會各關一次。"""
    order: list[str] = []
    obj = game_link.GameLink(1234)
    obj.sock, obj.local_port = 0x2A, 51000

    def close(sock):
        order.append(f"close:{sock:#x} field={obj.sock!r}")

    monkeypatch.setattr(game_link.game_socket, "close_socket", close)
    obj.close()
    assert order == ["close:0x2a field=None"]
    assert obj.local_port == 0
