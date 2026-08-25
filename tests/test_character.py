"""角色狀態讀取測試。

不需要遊戲的部分測資料模型與驗證邏輯；需要遊戲的部分在找不到
Ragexe.exe 時自動跳過。
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("numpy")

from ro_toolbox.services.character import (  # noqa: E402
    CharacterReader,
    CharacterStatus,
    _plausible,
    _until_null,
)
from ro_toolbox.services.signatures import CHAR_STATUS, STATUS_OFFSETS  # noqa: E402


def make(**kwargs) -> CharacterStatus:
    values = dict(hp=100, max_hp=200, sp=30, max_sp=60, base_level=50, job_level=40)
    values.update(kwargs)
    return CharacterStatus(**values)


def test_percentages():
    status = make(hp=100, max_hp=200, sp=30, max_sp=60)
    assert status.hp_percent == 50.0
    assert status.sp_percent == 50.0


def test_percentages_handle_zero_max():
    assert make(max_hp=0).hp_percent == 0.0
    assert make(max_sp=0).sp_percent == 0.0


def test_exp_percent_matches_game_display():
    """實機對照：8961/12986 = 69.01%，遊戲畫面無條件捨去顯示 69.0%。"""
    status = make(base_exp=8961, base_exp_next=12986, job_exp=2907, job_exp_next=8920)
    assert status.has_exp
    assert round(status.base_percent, 2) == 69.01
    assert round(status.job_percent, 2) == 32.59


def test_exp_missing_is_reported_not_faked():
    """讀不到經驗值就說讀不到，不要回 0% 讓人以為真的是 0。"""
    assert not make().has_exp
    assert make().base_percent == 0.0


def test_max_level_sentinel():
    """滿級時伺服器塞哨兵大數當門檻（實測商狐 Job 50 讀到 999999999999999999）。"""
    status = make(job_exp=1426, job_exp_next=999_999_999_999_999_999,
                  base_exp=1, base_exp_next=100)
    assert status.job_maxed
    assert status.job_percent == 100.0
    assert not status.base_maxed


def test_plausible_rejects_absurd_exp():
    assert not _plausible(make(base_exp=10**12, base_exp_next=100))


def test_exp_offsets_are_int64_and_ordered():
    """四個經驗欄位是連續的 int64，順序：Base經驗, Base門檻, Job門檻, Job經驗。"""
    offsets = [
        STATUS_OFFSETS.base_exp,
        STATUS_OFFSETS.base_exp_next,
        STATUS_OFFSETS.job_exp_next,
        STATUS_OFFSETS.job_exp,
    ]
    assert offsets == sorted(offsets)
    assert all(b - a == 8 for a, b in zip(offsets, offsets[1:], strict=False))
    assert STATUS_OFFSETS.job_exp + 8 == STATUS_OFFSETS.base_level


def test_until_null_truncates():
    assert _until_null(bytes([65, 66, 0, 67])) == b"AB"


def test_until_null_without_null():
    assert _until_null(b"AB") == b"AB"


@pytest.mark.parametrize(
    "bad",
    [
        dict(base_level=0),
        dict(job_level=0),
        dict(base_level=1000),
        dict(hp=300, max_hp=200),  # hp 不能大於 max
        dict(sp=100, max_sp=60),
        dict(max_hp=0),
    ],
)
def test_plausible_rejects_bad_values(bad):
    assert _plausible(make(**bad)) is False


def test_plausible_accepts_normal():
    assert _plausible(make()) is True


def test_signature_offsets_are_documented():
    """偏移是三角色交叉比對出來的，改動要同步更新 GAMEDATA [MEM-003]。"""
    assert STATUS_OFFSETS.hp == 0x00
    assert STATUS_OFFSETS.max_hp == 0x04
    assert STATUS_OFFSETS.sp == 0x08
    assert STATUS_OFFSETS.max_sp == 0x0C
    assert STATUS_OFFSETS.base_level == -0x3B58
    assert STATUS_OFFSETS.job_level == -0x3B50
    assert STATUS_OFFSETS.name == 0x2800


def test_signature_has_wildcards():
    """特徵不准把答案寫死，必須留萬用字元（見 CLAUDE.md）。"""
    _pattern, mask = CHAR_STATUS.parse()
    assert 0 in mask, "特徵沒有任何 ?? 位元組，可能把變動值寫死了"
    assert CHAR_STATUS.value_offset == 0x20


@pytest.mark.skipif(sys.platform != "win32", reason="只支援 Windows")
def test_reads_running_game_if_present():
    from ro_toolbox.services import window_list

    targets = [
        w for w in window_list.enumerate_windows()
        if w.process_name.lower() == "ragexe.exe"
    ]
    if not targets:
        pytest.skip("沒有執行中的 Ragexe.exe")

    reader = CharacterReader()
    try:
        assert reader.attach(targets[0].pid), "AOB 定位失敗"
        status = reader.read()
        assert status is not None
        assert status.name, "沒讀到角色名"
        assert status.base_level >= 1
        assert status.max_hp > 0
    finally:
        reader.close()


def test_position_offset_is_documented():
    """座標偏移是移動驗證 + 三角色交叉比對出來的，見 GAMEDATA [MEM-006]。"""
    assert STATUS_OFFSETS.position == -0x3AD5FC


def test_position_rejects_out_of_range(monkeypatch):
    """超出任何 RO 地圖尺寸的值要當成定位失效。"""
    reader = CharacterReader()
    reader._base = 0x1000

    class FakeScanner:
        def _read_bytes(self, _addr, _size):
            return (999).to_bytes(4, "little") + (999).to_bytes(4, "little")

    reader._scanner = FakeScanner()
    assert reader.read_position() is None
