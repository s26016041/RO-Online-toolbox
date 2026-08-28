"""`GameLink`：socket ／ 角色定位 ／ 封包擷取三條線的**唯一**一份規則。

為什麼要獨立測：這段以前是 farm_bot 抄一份、travel_bot 抄一份，
[PKT-072] 就是因為「剛連上複製不到 socket 要重試」抄了四份、漏了兩份才炸的。
現在只有一份，那一份就要有自己的網。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import game_link
from ro_toolbox.services.game_link import GameLink

SERVER = ("1.2.3.4", 10004)


class FakeReader:
    def __init__(self, ok: bool = True, position: bool = True) -> None:
        self.ok = ok
        self.position_located = position
        self.closed = False

    def attach(self, pid, should_stop=None) -> bool:  # noqa: ANN001, ARG002
        return self.ok

    def close(self) -> None:
        self.closed = True


class FakeCapture:
    started = True

    def __init__(self, pid, on_packet) -> None:  # noqa: ANN001, ARG002
        self.stopped = False

    def start(self) -> bool:
        return FakeCapture.started

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def wired(monkeypatch):
    """把外部世界全部換成假的：不碰行程、不碰網路、不碰記憶體。"""
    reader = FakeReader()
    monkeypatch.setattr(game_link, "find_server", lambda _pid: SERVER)
    monkeypatch.setattr(game_link, "CharacterReader", lambda: reader)
    monkeypatch.setattr(game_link, "PacketCapture", FakeCapture)
    monkeypatch.setattr(game_link.game_socket, "find_game_socket",
                        lambda pid, ip, port: 111)
    monkeypatch.setattr(game_link.game_socket, "close_socket", lambda sock: None)
    monkeypatch.setattr(game_link.game_socket, "send_on_socket", lambda sock, data: 8)
    return reader


def test_open_gets_all_three_lines(wired):
    link = GameLink(1234, on_packet=lambda p: None)
    assert link.open() is None
    assert link.sock == 111
    assert link.server == SERVER
    assert link.reader is wired
    assert link.capture is not None


def test_not_logged_in_says_so_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(game_link, "find_server", lambda _pid: None)
    link = GameLink(1234)
    assert link.open() == "找不到伺服器連線（還沒登入？）"
    assert link.sock is None


def test_a_socket_we_cannot_duplicate_is_a_named_failure(wired, monkeypatch):
    # ⚠ 直接換掉 `open_game_socket`：它內建 20 秒重試（實機需要，剛連上複製不到），
    # 從 `find_game_socket` 那一層擋的話這條測試自己會跑二十秒。
    monkeypatch.setattr(game_link.game_socket, "open_game_socket",
                        lambda *a, **k: 0)
    link = GameLink(1234)
    assert "socket" in (link.open() or "")


def test_position_is_only_demanded_when_the_caller_needs_it(monkeypatch, wired):
    """⚠ 走路類功能沒有座標就整個不成立，要**當場**講清楚而不是每拍空轉；
    但不走路的呼叫端不該被這條擋下來。"""
    wired.position_located = False

    walking = GameLink(1234, need_position=True)
    assert "座標定位失敗" in (walking.open() or "")
    assert wired.closed is True, "擋下來的時候要把 reader 收掉"

    wired.closed = False
    not_walking = GameLink(1234, need_position=False)
    assert not_walking.open() is None


def test_capture_is_only_started_when_someone_wants_packets(wired):
    link = GameLink(1234)          # 沒給 on_packet
    assert link.open() is None
    assert link.capture is None


def test_capture_failure_is_reported(wired, monkeypatch):
    monkeypatch.setattr(FakeCapture, "started", False)
    link = GameLink(1234, on_packet=lambda p: None)
    assert "擷取" in (link.open() or "")


# ---- 重綁：只管連線，不管地圖 ---------------------------------------------


def test_resync_does_nothing_when_the_connection_is_the_same(wired):
    link = GameLink(1234)
    link.open()
    assert link.resync() is None
    assert link.rebound is False


def test_resync_rebinds_when_the_channel_changes(wired, monkeypatch):
    link = GameLink(1234)
    link.open()
    closed: list[int] = []
    monkeypatch.setattr(game_link.game_socket, "close_socket", closed.append)
    monkeypatch.setattr(game_link.game_socket, "find_game_socket",
                        lambda pid, ip, port: 222)

    assert link.resync(("9.9.9.9", 10010)) is None
    assert closed == [111], "舊 socket 要關掉，不然會漏一個 handle"
    assert link.sock == 222
    assert link.server == ("9.9.9.9", 10010)
    assert link.rebound is True


def test_a_caller_supplied_server_is_used_instead_of_reading_again(wired, monkeypatch):
    """⚠ 呼叫端常常已經讀過一次連線。這裡再讀一次不只浪費 ——
    TCP 表是快照，兩次可能不一樣，於是判斷與動作對不起來。"""
    link = GameLink(1234)
    link.open()
    monkeypatch.setattr(game_link, "find_server",
                        lambda _pid: pytest.fail("不該再讀一次"))
    assert link.resync(SERVER) is None


def test_losing_the_connection_is_reported_once_we_had_one(wired, monkeypatch):
    link = GameLink(1234)
    link.open()
    monkeypatch.setattr(game_link, "find_server", lambda _pid: None)
    assert link.resync() == "⚠ 遊戲連線已中斷"


def test_no_connection_before_we_ever_had_one_is_not_an_error(monkeypatch):
    monkeypatch.setattr(game_link, "find_server", lambda _pid: None)
    link = GameLink(1234)
    assert link.resync() is None


# ---- 送封包與收攤 ---------------------------------------------------------


def test_a_failed_send_forces_a_rebind_next_tick(wired, monkeypatch):
    """⚠ 送不出去要**主動重綁**。舊 socket 送出去的東西不會報錯、只是沒人收
    —— 那正是「安靜地做錯事」。"""
    link = GameLink(1234)
    link.open()
    monkeypatch.setattr(game_link.game_socket, "send_on_socket",
                        lambda sock, data: -1)
    assert link.send(b"\x00\x01") is False
    assert link.server is None, "清掉才會在下一次 resync 重綁"


def test_sending_without_a_socket_is_false_not_a_crash():
    assert GameLink(1234).send(b"\x00") is False


def test_close_shuts_everything_and_can_be_called_twice(wired):
    link = GameLink(1234, on_packet=lambda p: None)
    link.open()
    capture = link.capture
    link.close()
    assert capture.stopped is True
    assert wired.closed is True
    assert link.sock is None and link.reader is None and link.capture is None
    link.close()   # 再關一次不該炸


def test_one_broken_shutdown_does_not_leave_the_others_open(wired, monkeypatch):
    """⚠ 收尾要**各自 try**：其中一項炸掉不該讓其他項留著不關。"""
    link = GameLink(1234, on_packet=lambda p: None)
    link.open()

    def boom() -> None:
        raise RuntimeError("擷取關不掉")

    monkeypatch.setattr(link.capture, "stop", boom)
    link.close()
    assert wired.closed is True
    assert link.sock is None
