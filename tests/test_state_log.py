"""背景輪詢的失敗訊息要降噪，但不能消音。

背景：角色狀態每 12 秒讀一次、背包每一秒多讀一次。定位不到就照實記的話，
幾分鐘後日誌是幾百行一模一樣的字，真正的錯誤全被洗掉 —— 使用者實際回報過。
"""

from __future__ import annotations

import logging

from ro_toolbox.utils.logging import StateLog


class _Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _log_with_recorder(name: str):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    recorder = _Recorder()
    logger.addHandler(recorder)
    return StateLog(logger), recorder


def test_the_same_problem_is_only_shouted_once():
    notes, recorder = _log_with_recorder("test.state.repeat")
    for _ in range(50):
        notes.problem("gone", logging.ERROR, "找不到東西")

    levels = [r.levelno for r in recorder.records]
    assert levels[0] == logging.ERROR
    # 其餘的降到 DEBUG —— **還看得到**，只是不洗版
    assert set(levels[1:]) == {logging.DEBUG}
    assert len(levels) == 50


def test_a_different_problem_is_shouted_again():
    """換了一件事就要重新說 —— 不然真的改版壞掉會被前一件事蓋住。"""
    notes, recorder = _log_with_recorder("test.state.switch")
    notes.problem("not-in-game", logging.INFO, "還沒進遊戲")
    notes.problem("ambiguous", logging.ERROR, "特徵命中太多個")

    assert [r.levelno for r in recorder.records] == [logging.INFO, logging.ERROR]


def test_recovering_says_so_once():
    notes, recorder = _log_with_recorder("test.state.recover")
    notes.problem("gone", logging.ERROR, "找不到東西")
    notes.ok("又找到了")
    notes.ok("又找到了")          # 已經正常了就不用再說

    assert [r.getMessage() for r in recorder.records] == ["找不到東西", "又找到了"]


def test_ok_without_a_prior_problem_is_silent():
    """一直都正常的時候不要講話。"""
    notes, recorder = _log_with_recorder("test.state.quiet")
    for _ in range(10):
        notes.ok("正常")
    assert recorder.records == []


def test_the_problem_can_be_shouted_again_after_recovery():
    """壞 → 好 → 又壞，第二次還是要大聲 —— 這是新的一次故障。"""
    notes, recorder = _log_with_recorder("test.state.again")
    notes.problem("gone", logging.ERROR, "找不到東西")
    notes.ok("又找到了")
    notes.problem("gone", logging.ERROR, "找不到東西")

    assert [r.levelno for r in recorder.records] == [
        logging.ERROR, logging.INFO, logging.ERROR,
    ]
