"""送輸入的子行程：**被整批擋掉就換一個再送**。

## 為什麼需要重送（2026-08-30 實機量的）

GameGuard 會隨機把**整個子行程**的輸入擋掉，而且是「這個行程能不能送」
一次決定：能送的行程連送 20 次都進得去（8 回裡 7 回整包成功），
被擋的行程第一個動作就失敗。同一個視窗、同一時間交錯量：

    打包版 exe 子行程   PostMessage 6 成功 / 4 失敗、SendInput 3 / 7
    venv python 子行程  PostMessage 10 / 0、        SendInput 10 / 0

所以「換一個子行程」＝重擲一次骰子。詳見 GAMEDATA [INP-022]。

⚠ 但**只有一個動作都沒做的時候才准重送** —— 做到一半重來會打兩次字。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import input_helper
from ro_toolbox.services.input_helper import InputHelperError


def _blow_up(times: int, done: int):
    """前 `times` 次丟「被擋掉」，之後成功。回傳 (假的 _run, 呼叫次數)。"""
    calls = {"n": 0}

    def fake_run(actions, hwnd):
        calls["n"] += 1
        if calls["n"] <= times:
            raise InputHelperError("輸入沒送出去：畫面不吃訊息", done)
        return ""

    return fake_run, calls


def test_a_fully_blocked_batch_is_retried_with_a_fresh_process(monkeypatch):
    """整批被擋掉（做完 0 個）→ 換一個子行程重送，呼叫端不必知道。"""
    fake, calls = _blow_up(times=3, done=0)
    monkeypatch.setattr(input_helper, "_run", fake)
    input_helper.send(0x1234, [{"key": 0x0D}])
    assert calls["n"] == 4, "應該重送到成功為止"


def test_a_half_done_batch_is_never_retried(monkeypatch):
    """做到一半才被擋 → **不准重送**（欄位裡已經有字，重來會打兩次）。"""
    fake, calls = _blow_up(times=1, done=2)
    monkeypatch.setattr(input_helper, "_run", fake)
    with pytest.raises(InputHelperError):
        input_helper.send(0x1234, [{"key": 0x0D}])
    assert calls["n"] == 1, "做到一半就不該再送"


def test_an_unknown_progress_is_treated_as_half_done(monkeypatch):
    """問不出做了幾個（子行程沒回報）＝不知道 → 當作做過事，不重送。"""
    fake, calls = _blow_up(times=1, done=None)
    monkeypatch.setattr(input_helper, "_run", fake)
    with pytest.raises(InputHelperError):
        input_helper.send(0x1234, [{"key": 0x0D}])
    assert calls["n"] == 1


def test_it_eventually_gives_up(monkeypatch):
    """一直被擋就要**大聲失敗**，不能無限重送。"""
    fake, calls = _blow_up(times=99, done=0)
    monkeypatch.setattr(input_helper, "_run", fake)
    with pytest.raises(InputHelperError):
        input_helper.send(0x1234, [{"key": 0x0D}], tries=3)
    assert calls["n"] == 3


def test_the_child_reports_how_far_it_got():
    """`DONE n` 是重送安不安全的唯一依據 —— 解析要對。"""
    assert input_helper._actions_done("STAGE LOGIN\nDONE 5\n") == 5
    assert input_helper._actions_done("DONE 0") == 0
    assert input_helper._actions_done("沒有這一行") is None
    assert input_helper._actions_done(None) is None


# ---- 送輸入要走「小 exe」，看畫面才走主 exe（[INP-023]）-------------------


def test_input_goes_to_the_small_exe_when_frozen(monkeypatch, tmp_path):
    """打包版送輸入一律走小 exe —— 大的那顆會被 GameGuard 隨機整批擋掉。"""
    worker = tmp_path / input_helper.INPUT_WORKER_EXE
    worker.write_bytes(b"")
    monkeypatch.setattr(input_helper.sys, "frozen", True, raising=False)
    monkeypatch.setattr(input_helper.sys, "_MEIPASS", str(tmp_path), raising=False)
    cmd = input_helper._command(0x1234, "s.json", [{"key": 0x0D}])
    assert cmd[0] == str(worker), cmd


def test_looking_at_the_screen_still_goes_to_the_main_exe(monkeypatch, tmp_path):
    """看畫面要 Qt（樣板比對），小 exe 沒有 —— 那件事只讀不送，不會被擋。"""
    (tmp_path / input_helper.INPUT_WORKER_EXE).write_bytes(b"")
    monkeypatch.setattr(input_helper.sys, "frozen", True, raising=False)
    monkeypatch.setattr(input_helper.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(input_helper.sys, "executable", r"C:\big\RO-Online-toolbox.exe")
    cmd = input_helper._command(0x1234, "s.json", [{"look": True}])
    assert cmd[0] == r"C:\big\RO-Online-toolbox.exe", cmd


def test_without_the_small_exe_it_falls_back_to_the_main_one(monkeypatch, tmp_path):
    """小 exe 漏收就退回主 exe：會被擋、會慢，但至少還會動（`--selftest` 會抓）。"""
    monkeypatch.setattr(input_helper.sys, "frozen", True, raising=False)
    monkeypatch.setattr(input_helper.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(input_helper.sys, "executable", r"C:\big\RO-Online-toolbox.exe")
    assert input_helper.input_worker() is None
    cmd = input_helper._command(0x1234, "s.json", [{"key": 0x0D}])
    assert cmd[0] == r"C:\big\RO-Online-toolbox.exe", cmd


def test_the_small_exe_never_imports_qt():
    """⚠ 小 exe 的進入點**不准 import** 任何會拉到 Qt／numpy 的東西。

    PyInstaller 連函式裡面的 import 都會跟著收 —— 一不小心這顆就從 7 MB
    變回 83 MB，然後照樣被 GameGuard 擋掉，而且沒有任何徵兆。
    所以這條用 AST 檢查真正的 import（文件字串裡提到名字不算）。
    """
    import ast
    from pathlib import Path

    banned = ("PySide6", "numpy", "game_screen", "input_helper", "ro_toolbox.app")
    source = Path(__file__).resolve().parents[1] / "src" / "ro_toolbox"
    for name in ("input_worker.py", "services/input_actions.py"):
        tree = ast.parse((source / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"{node.module or ''}.{a.name}" for a in node.names]
            else:
                continue
            for imported in names:
                assert not any(b in imported for b in banned), f"{name} import 了 {imported}"
