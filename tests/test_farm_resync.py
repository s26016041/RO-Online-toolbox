"""換地圖／換頻道之後要重新綁定（不需遊戲）。

踩過的坑：角色中途換圖或伺服器把連線移到別的地圖伺服器之後，
啟動時抓的 socket 與地形都失效 —— 封包石沉大海、A* 用舊地圖，
**bot 看起來在跑但什麼都沒做**。這幾個測試把「會偵測並重綁」釘住。
"""

from __future__ import annotations

import numpy as np
import pytest

from ro_toolbox.services import farm_bot as fb
from ro_toolbox.services import game_link
from ro_toolbox.services.entities import MemoryEntity
from ro_toolbox.services.farm_bot import _RESYNC_SEC, FarmBot, _Aim
from ro_toolbox.services.mapdata import MapTerrain

T0 = 5000.0


class FakeStatus:
    def __init__(self, map_name: str) -> None:
        self.map_name = map_name
        self.hp = 100
        self.max_hp = 100

    def read_position(self):  # pragma: no cover - 不會被呼叫
        return (10, 10)


class FakeReader:
    def __init__(self, map_name: str) -> None:
        self.map_name = map_name

    def read(self):
        return FakeStatus(self.map_name)

    def read_position(self):
        return (10, 10)


def make_bot(monkeypatch, map_name="prt_fild07", server=("1.2.3.4", 10000)) -> FarmBot:
    bot = FarmBot(pid=1234)
    bot._reader = FakeReader(map_name)
    bot._map = map_name
    bot._server = server
    bot._sock = 111
    types = np.zeros((400, 400), dtype=np.uint32)
    bot._terrain = MapTerrain(name=map_name, width=400, height=400, types=types)
    monkeypatch.setattr(fb, "load_terrain", lambda name: MapTerrain(
        name=name, width=300, height=300, types=np.zeros((300, 300), dtype=np.uint32)))
    return bot


def test_nothing_happens_when_unchanged(monkeypatch):
    bot = make_bot(monkeypatch)
    monkeypatch.setattr(fb, "find_server", lambda pid: ("1.2.3.4", 10000))
    assert bot._keep_in_sync(T0) is True
    assert bot._map == "prt_fild07"
    assert bot._sock == 111


def test_map_change_reloads_terrain_and_clears_state(monkeypatch):
    bot = make_bot(monkeypatch)
    monkeypatch.setattr(fb, "find_server", lambda pid: ("1.2.3.4", 10000))
    bot._reader.map_name = "moc_fild01"
    bot._aim = _Aim(gid=9, since=T0)
    bot._roam_goal = (1, 2)
    bot._skip[9] = T0 + 30
    bot._world.sync_from_memory([MemoryEntity(9, 1052, 10, 10, addr=0)])

    assert bot._keep_in_sync(T0) is True
    assert bot._map == "moc_fild01"
    assert bot._terrain.name == "moc_fild01"
    assert bot._terrain.width == 300, "地形要重載成新地圖的"
    assert bot._aim is None and bot._roam_goal is None
    assert not bot._skip and bot._world.monster_gids() == []


def test_channel_change_rebinds_socket(monkeypatch):
    bot = make_bot(monkeypatch)
    closed, found = [], []
    monkeypatch.setattr(fb, "find_server", lambda pid: ("9.9.9.9", 10004))
    monkeypatch.setattr(game_link.game_socket, "close_socket", closed.append)
    monkeypatch.setattr(game_link.game_socket, "find_game_socket",
                        lambda pid, ip, port: found.append((ip, port)) or 222)

    assert bot._keep_in_sync(T0) is True
    assert closed == [111], "舊 socket 要關掉"
    assert found == [("9.9.9.9", 10004)]
    assert bot._sock == 222 and bot._server == ("9.9.9.9", 10004)


def test_stops_loudly_when_new_socket_not_found(monkeypatch):
    bot = make_bot(monkeypatch)
    monkeypatch.setattr(fb, "find_server", lambda pid: ("9.9.9.9", 10004))
    monkeypatch.setattr(game_link.game_socket, "close_socket", lambda s: None)
    monkeypatch.setattr(game_link.game_socket, "find_game_socket", lambda pid, ip, port: 0)
    # ⚠ 重綁本來會重試 SOCKET_REBIND_SEC 秒（實機需要，剛換頻道複製不到）——
    # 測試要把它縮掉，不然這一條自己會跑十秒。
    monkeypatch.setattr(game_link.game_socket, "SOCKET_REBIND_SEC", 0.0)

    assert bot._keep_in_sync(T0) is False
    assert not bot._stats.running
    assert "socket" in bot._stats.note


def test_stops_loudly_when_connection_lost(monkeypatch):
    bot = make_bot(monkeypatch)
    monkeypatch.setattr(fb, "find_server", lambda pid: None)
    assert bot._keep_in_sync(T0) is False
    assert not bot._stats.running
    assert "連線" in bot._stats.note


def test_send_failure_forces_rebind(monkeypatch):
    """送封包失敗＝socket 已失效，下一拍必須重綁，不能安靜地繼續。"""
    bot = make_bot(monkeypatch)
    monkeypatch.setattr(game_link.game_socket, "send_on_socket", lambda sock, data: -1)
    bot._send(b"\x00\x01")
    assert bot._server is None
    assert bot._resync_at == 0.0


def test_check_is_throttled(monkeypatch):
    """不用每一拍都去列舉連線，太貴。"""
    calls = []
    bot = make_bot(monkeypatch)
    monkeypatch.setattr(fb, "find_server", lambda pid: calls.append(pid) or ("1.2.3.4", 10000))
    bot._keep_in_sync(T0)
    bot._keep_in_sync(T0 + _RESYNC_SEC / 2)
    assert len(calls) == 1
    bot._keep_in_sync(T0 + _RESYNC_SEC + 0.1)
    assert len(calls) == 2


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
