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
    # 名字要有值：`_plausible` 要求非空（空名字＝選角畫面的殘留，不是真角色）。
    values = dict(
        name="測試角色", hp=100, max_hp=200, sp=30, max_sp=60,
        base_level=50, job_level=40,
    )
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
        dict(hp=-1),
        dict(hp=99_999_999),       # 離譜的值＝定位跑掉了
        dict(max_hp=0),
    ],
)
def test_plausible_rejects_bad_values(bad):
    assert _plausible(make(**bad)) is False


def test_plausible_accepts_normal():
    assert _plausible(make()) is True


def test_levelling_up_is_not_a_broken_signature():
    """升等那一拍客戶端先更新 HP、還沒更新 maxHP —— 會讀到 hp > max_hp。

    實測 `HP 1274/914`（狐狐狸升到 Base 40 的瞬間）。那是真的角色，
    以前會被判成「定位已失效」而噴警告（使用者實測回報）。
    `_plausible()` 要認的是**定位跑掉**，而定位跑掉給的是離譜數字，
    那由上下限擋住 —— 不該用 hp <= max_hp 去認。
    """
    assert _plausible(make(hp=1274, max_hp=914)) is True
    assert _plausible(make(sp=130, max_sp=100)) is True


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
    from ro_toolbox.services.ro_capture import find_server

    targets = [
        w for w in window_list.enumerate_windows()
        if w.process_name.lower() == "ragexe.exe"
    ]
    if not targets:
        pytest.skip("沒有執行中的 Ragexe.exe")

    # 「有遊戲在跑」不等於「已經登入」。停在登入畫面時角色結構還沒建立，
    # AOB 當然定位不到 —— 那不是特徵壞了，是還沒登入。有沒有登入一律問連線，
    # 不能看記憶體（GAMEDATA [MEM-029]、[PKT-044]）。
    targets = [w for w in targets if find_server(w.pid) is not None]
    if not targets:
        pytest.skip("Ragexe 在跑但都還沒登入（停在登入畫面）")

    reader = CharacterReader()
    try:
        if not reader.attach(targets[0].pid):
            # 「有連線」不等於「已經進到遊戲畫面」：停在**選角畫面**時
            # 連線是有的（char server），但角色結構還沒建立，AOB 當然定位不到。
            # 網路層分不出選角與遊戲中，所以這裡只能跳過，不能當成特徵壞了。
            pytest.skip("連線在但角色結構還沒建立（多半停在選角畫面）")
        status = reader.read()
        if status is None:
            # 定位到了但驗不過（多半是選角畫面的殘留：數值合理、名字空白）。
            pytest.skip("讀到的不是真的角色狀態（多半停在選角畫面）")
        assert status.name, "沒讀到角色名"
        assert status.base_level >= 1
        assert status.max_hp > 0
    finally:
        reader.close()


def test_position_is_not_a_hardcoded_offset_any_more():
    """座標**不准**再用「相對 HP 全域的固定距離」推導，見 GAMEDATA [MEM-039]。

    2026-08-26 改版時兩個全域移動幅度不同（+0x60D8 vs +0x60B8），那條推導就斷了，
    而且斷得很安靜：舊位址指到一片 0，(0,0) 通過了當時的合理性檢查。
    現在改用程式碼特徵定位（POSITION_X_SIGS / POSITION_Y_SIGS）。
    """
    assert not hasattr(STATUS_OFFSETS, "position")


def test_position_signatures_mask_the_answer_and_cross_check():
    """特徵不准把答案寫死；x 與 y 取的是同一個骨架裡的不同立即值。"""
    from ro_toolbox.services.signatures import (
        POSITION_X_SIGS,
        POSITION_XY_GAP,
        POSITION_Y_SIGS,
    )

    x_sig, y_sig = POSITION_X_SIGS[0], POSITION_Y_SIGS[0]
    assert x_sig.pattern == y_sig.pattern, "兩條要錨在同一段程式碼才算交叉驗證"
    assert "??" in x_sig.pattern
    # 每個立即值本身在骨架裡出現兩次 —— 那是這條特徵自帶的一致性檢查
    assert len(x_sig.operands) == 2
    assert len(y_sig.operands) == 2
    assert set(x_sig.operands).isdisjoint(y_sig.operands)
    assert POSITION_XY_GAP == 4


def test_position_rejects_out_of_range(monkeypatch):
    """超出任何 RO 地圖尺寸的值要當成定位失效。"""
    reader = CharacterReader()
    reader._position = 0x1000

    class FakeScanner:
        def _read_bytes(self, _addr, _size):
            return (999).to_bytes(4, "little") + (999).to_bytes(4, "little")

    reader._scanner = FakeScanner()
    assert reader.read_position() is None


def test_position_rejects_all_zero(monkeypatch):
    """(0,0) 是定位失效的樣子，不是合法座標 —— 見 GAMEDATA [MEM-039]。

    改版讓 position 偏移失效時讀到的就是一片 0。當時的檢查只擋 >=512，
    所以 (0,0) 一路傳給自動打怪，它拿去算 A* 找不到路就往地圖角落走，
    全程不報錯。0 是地圖邊界，任何地圖上都不可走。
    """
    reader = CharacterReader()
    reader._position = 0x1000

    class ZeroScanner:
        def _read_bytes(self, _addr, _size):
            return b"\x00" * 8

    reader._scanner = ZeroScanner()
    assert reader.read_position() is None


def test_empty_name_is_not_a_real_character():
    """停在選角畫面時會讀到殘留結構：數值都合理，只有名字是空的。

    實測 2026-08-25：Base 54 / Job 54 / HP 54/54 / SP 0/0、名字空白 ——
    全部通過範圍檢查。少了名字這一條，自動掛機頁會拿它建出一個分頁，
    然後照著垃圾值算血量百分比（安靜地做錯事）。
    """
    assert _plausible(make(name="狐狐狸")) is True
    assert _plausible(make(name="")) is False
    assert _plausible(make(name="   ")) is False


def test_residual_char_select_values_are_rejected():
    """把實測到的那一組殘留值原封不動釘住。"""
    residual = make(
        name="", base_level=54, job_level=54, hp=54, max_hp=54, sp=0, max_sp=0
    )
    assert _plausible(residual) is False


# ---- 定位：命中多個時要先驗合理性，不是直接放棄 ---------------------------


def _fake_position(monkeypatch, x=0x122A67C, y=0x122A680):
    """把座標的程式碼特徵定位換掉（單元測試沒有真的行程可以掃）。"""
    from ro_toolbox.services import character as mod
    from ro_toolbox.services.signatures import POSITION_X_SIGS

    monkeypatch.setattr(
        mod, "locate_global", lambda _sc, sigs: x if sigs is POSITION_X_SIGS else y
    )


def _attachable(monkeypatch, hits, plausible_at):
    """準備一個只差 scan 結果的 reader（不真的開行程）。"""
    from ro_toolbox.services import character as mod

    reader = CharacterReader()
    monkeypatch.setattr(reader._scanner, "open", lambda _pid: None)
    monkeypatch.setattr(reader._scanner, "close", lambda: None)
    monkeypatch.setattr(mod, "scan", lambda *a, **k: list(hits))
    monkeypatch.setattr(
        CharacterReader,
        "probe",
        lambda self, base: make(name="狐狐狸") if base in plausible_at else None,
    )
    _fake_position(monkeypatch)
    return reader


def test_junk_hits_are_filtered_instead_of_giving_up(monkeypatch):
    """實測 2026-08-26：玩久了堆積裡會出現 5 個同樣位元組樣式的垃圾
    （HP 15、max_hp 42 億、名字與地圖都空的），真的角色也在裡面。

    舊版看到「不只一個」就直接放棄，症狀是「遊戲明明開著卻讀不到角色」。
    AOB 只是錨，分辨「這是不是角色」要靠數值本身。
    """
    reader = _attachable(
        monkeypatch,
        hits=[0x39871000, 0x15D7C98, 0x39872000, 0x39873000],
        plausible_at={0x15D7C98},
    )
    assert reader.attach(4321) is True
    assert reader._base == 0x15D7C98


def test_two_believable_characters_still_fail_loudly(monkeypatch):
    """驗證之後還是不只一個＝真的分不出來。不准賭，賭錯就是照別人的血量決策。"""
    reader = _attachable(
        monkeypatch,
        hits=[0x15D7C98, 0x16D7C98],
        plausible_at={0x15D7C98, 0x16D7C98},
    )
    assert reader.attach(4321) is False
    assert reader._base is None


def test_no_believable_hit_is_treated_as_not_in_game(monkeypatch):
    reader = _attachable(monkeypatch, hits=[0x39871000, 0x39872000], plausible_at=set())
    assert reader.attach(4321) is False
    assert reader._base is None


def test_single_hit_skips_the_probe(monkeypatch):
    """只有一個命中時不必額外驗 —— read() 本來就會驗，不要多掃一次記憶體。"""
    from ro_toolbox.services import character as mod

    reader = CharacterReader()
    monkeypatch.setattr(reader._scanner, "open", lambda _pid: None)
    monkeypatch.setattr(mod, "scan", lambda *a, **k: [0x15D7C98])
    _fake_position(monkeypatch)

    def boom(self, base):
        raise AssertionError("只有一個命中時不該呼叫 probe")

    monkeypatch.setattr(CharacterReader, "probe", boom)
    assert reader.attach(4321) is True
    assert reader._base == 0x15D7C98
    assert reader.position_located is True


def test_position_is_disabled_when_the_two_globals_disagree(monkeypatch):
    """x 與 y 不相鄰＝骨架解錯了。不准將就用，走路類功能要停用。"""
    from ro_toolbox.services import character as mod

    reader = CharacterReader()
    monkeypatch.setattr(reader._scanner, "open", lambda _pid: None)
    monkeypatch.setattr(mod, "scan", lambda *a, **k: [0x15D7C98])
    _fake_position(monkeypatch, x=0x122A67C, y=0x1999999)
    assert reader.attach(4321) is True  # HP／等級還是讀得到
    assert reader.position_located is False
    assert reader.read_position() is None
