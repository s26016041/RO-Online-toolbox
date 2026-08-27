"""開遊戲：路徑檢查與「哪個是開始遊戲按鈕」的判斷規則。

按鈕**不准寫死座標**（視窗會被拖動、版面會改），所以用規則現找。
規則挑不出唯一候選時必須回 None 讓呼叫端停手 —— 亂按可能按到設定或關閉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ro_toolbox.services import game_launcher
from ro_toolbox.services.game_launcher import GAME_ARG, GamePaths


def _game_dir(tmp_path: Path, *, with_game: bool = True) -> Path:
    launcher = tmp_path / "Ragnarok.exe"
    launcher.write_bytes(b"MZ")
    if with_game:
        (tmp_path / "Ragexe.exe").write_bytes(b"MZ")
    return launcher


def test_launch_argument_is_the_measured_one():
    """實機攔截到的命令列是 `Ragexe.exe 1rag1`（GAMEDATA [PKT-047]）。

    不帶參數會跳 Error 對話框，所以這個值不是可有可無的。
    """
    assert GAME_ARG == "1rag1"


def test_paths_derive_the_game_from_the_launcher(tmp_path):
    paths = GamePaths(_game_dir(tmp_path))
    assert paths.directory == tmp_path
    assert paths.game == tmp_path / "Ragexe.exe"
    assert paths.problem() == ""


@pytest.mark.parametrize(
    ("make", "expect"),
    [
        (lambda tmp: GamePaths(Path("")), "還沒設定"),
        (lambda tmp: GamePaths(tmp / "不存在.exe"), "找不到檔案"),
    ],
)
def test_paths_report_problems_in_plain_words(tmp_path, make, expect):
    assert expect in make(tmp_path).problem()


def test_missing_game_exe_is_caught_before_launching(tmp_path):
    """啟動器在、遊戲本體不在 —— 多半是路徑選錯了，要在開之前就講。"""
    paths = GamePaths(_game_dir(tmp_path, with_game=False))
    assert "Ragexe.exe" in paths.problem()


def test_not_an_exe_is_rejected(tmp_path):
    bad = tmp_path / "readme.txt"
    bad.write_text("x", encoding="utf-8")
    assert "不是執行檔" in GamePaths(bad).problem()


# ---- 按鈕判斷規則 ----------------------------------------------------------
#
# find_start_button 要 win32 才跑得動，所以這裡測的是抽出來的挑選規則本身：
# 「位在右下半部、面積最大、而且次大者不能太接近」。


def _pick(kids, width=608, height=367, ratio=game_launcher._AMBIGUOUS_RATIO):
    """複製 find_start_button 的挑選規則（同一份條件，見該函式）。"""
    candidates = [k for k in kids if k[1] > width * 0.5 and k[2] > height * 0.5]
    candidates.sort(key=lambda k: -k[3])
    if not candidates:
        return None
    if len(candidates) > 1 and candidates[1][3] > candidates[0][3] * ratio:
        return None
    return candidates[0][0]


def test_real_launcher_layout_picks_the_start_button():
    """實測到的版面（GAMEDATA [PKT-047]）：四個控制項，開始遊戲是 0x5200FA。"""
    kids = [
        (0x5200FA, 475, 253, 109 * 61),   # 開始遊戲
        (0xA40500, 312, 289, 144 * 21),   # 進度條
        (0x5309C8, 312, 253, 56 * 25),    # 設定
        (0x290D82, 575, 35, 28 * 27),     # 關閉
    ]
    assert _pick(kids) == 0x5200FA


def test_two_similar_candidates_stop_instead_of_guessing():
    """大小相近就分不出來 —— 按錯可能按到別的功能，寧可停手。"""
    kids = [
        (1, 475, 253, 100 * 60),
        (2, 500, 300, 95 * 58),
    ]
    assert _pick(kids) is None


def test_nothing_in_the_lower_right_returns_none():
    kids = [(1, 10, 10, 100 * 60)]
    assert _pick(kids) is None
