"""導航目標讀取端的驗證（不需遊戲）。

這條特徵錨在 CRT 靜態建構鏈上、只有 1 處命中，改版時建構順序一變就可能
安靜地指到**別的**全域。所以真正的安全網不是特徵，是讀出來之後的驗證：
不像地圖名、或我們沒有那張圖的地形檔，一律回 None 讓呼叫端大聲停用。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import navigation
from ro_toolbox.services.navigation import NavigationReader, clean_map_name


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("prt_fild08.rsw", "prt_fild08"),  # 全域裡就是帶副檔名的
        ("prontera.gat", "prontera"),
        ("payon", "payon"),
        ("1@mjo1.rsw", "1@mjo1"),  # 副本地圖有 @
        ("MOC_FILD01.RSW", "moc_fild01"),
        ("prt_fild08.rsw\x00\x00garbage", "prt_fild08"),
    ],
)
def test_clean_map_name_accepts_real_shapes(raw, expect):
    assert clean_map_name(raw) == expect


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ab",  # 太短
        "這不是地圖名",
        "some path\\with\\slashes",
        "prt fild08",  # 有空白
        "x" * 40,  # 太長
    ],
)
def test_clean_map_name_rejects_junk(raw):
    """特徵指錯全域時讀到的就是這種東西 —— 一律不採用，不猜。"""
    assert clean_map_name(raw) is None


class FakeScanner:
    def __init__(self, text: str) -> None:
        self.text = text

    def read_string(self, _addr, _len, _enc):
        return self.text

    def close(self) -> None:
        pass


def make(text: str) -> NavigationReader:
    reader = NavigationReader()
    reader._scanner = FakeScanner(text)
    reader._address = 0x123CD58
    return reader


def test_destination_returns_map_when_terrain_exists(monkeypatch):
    monkeypatch.setattr(navigation, "has_terrain", lambda name: name == "prt_fild08")
    assert make("prt_fild08.rsw").destination() == "prt_fild08"


def test_destination_refuses_map_we_cannot_walk(monkeypatch):
    """讀對了名字但拿不到地形＝走不過去。回 None 比回一個走不到的目標好。"""
    monkeypatch.setattr(navigation, "has_terrain", lambda _name: False)
    assert make("prt_fild08.rsw").destination() is None


def test_destination_refuses_junk_before_asking_about_terrain(monkeypatch):
    """先驗名字再問地形 —— 垃圾字串不該有機會走到後面那一步。"""

    def boom(_name):
        raise AssertionError("名字都不合法了，不該去問地形")

    monkeypatch.setattr(navigation, "has_terrain", boom)
    assert make("\x01\x02\x03").destination() is None


def test_real_asset_knows_the_maps_we_ship():
    """用真的 `assets/terrain.bin.gz` 驗一次 ——
    換一台沒有 `RODATA/` 的電腦也要認得出這些地圖。"""
    assert make("prontera.rsw").destination() == "prontera"
    assert make("prt_fild08.rsw").destination() == "prt_fild08"
    assert make("no_such_map.rsw").destination() is None


def test_destination_is_none_before_attach():
    assert NavigationReader().destination() is None
    assert NavigationReader().located is False


# ---- 「空的」跟「讀不到」不是同一件事 --------------------------------------


def test_blank_target_is_flagged_separately(monkeypatch):
    """⚠ 客戶端把目標**清空**跟我們讀不到，訊息要講得不一樣。

    實測（2026-08-27，[MEM-045]）：目的地選**地城**（iz_dun02）時，這個
    std::string 的 size 被設成 0（buffer 裡還留著上一個目標的殘骸）。
    對一個明明設好目的地的人說「請先設定目的地」是最惹人火的錯誤訊息。
    """
    monkeypatch.setattr(navigation, "has_terrain", lambda _name: True)
    reader = make("")
    assert reader.destination() is None
    assert reader.blank is True


def test_junk_is_not_reported_as_blank(monkeypatch):
    """讀到垃圾是「特徵可能指錯了」，不是「使用者沒設定」—— 別混為一談。"""
    monkeypatch.setattr(navigation, "has_terrain", lambda _name: True)
    reader = make("rubbish!!")
    assert reader.destination() is None
    assert reader.blank is False


def test_blank_clears_once_a_real_target_appears(monkeypatch):
    """旗標不能黏著 —— 讀到真的目標之後要放掉。"""
    monkeypatch.setattr(navigation, "has_terrain", lambda _name: True)
    reader = make("")
    reader.destination()
    assert reader.blank is True
    reader._scanner.text = "prt_fild08.rsw"
    assert reader.destination() == "prt_fild08"
    assert reader.blank is False

