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
