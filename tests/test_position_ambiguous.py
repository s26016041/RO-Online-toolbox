"""好幾個移動元件都像是角色本人的時候該怎麼辦。

⚠⚠ 實機 2026-08-30（白狐掛機中）：

    有 2 個移動元件都像是角色本人（['0x2ed44750', '0x3f27c5d8']），
    分不出來，只好改用進圖座標          ← 一秒印六行，而且位置整個錯掉

使用者原話：「死了，完全不能用」。兩個問題：

1. **回退到進圖座標** —— 那個值角色一移動就是錯的，走位／打怪／脫離傳點
   全部瞎掉。
2. **那句 ERROR 沒節流** —— 每 0.3 秒一次（三個分身就是三倍），
   把真正該看的訊息全沖掉。

分得出來的判準是**位置在時間上是連續的**：兩拍之間角色跑不了多遠，
所以「離上一次讀到的位置最近」的那個才是本人。被回收的舊元件會停在
它最後的位置（多半是上一張圖）。
"""

from __future__ import annotations

import logging

from ro_toolbox.services import player_position as pp


def _locator(last=None, last_at=0.0, entry=None, now=1000.0):
    loc = pp.PlayerPosition.__new__(pp.PlayerPosition)
    loc._now = lambda: now
    loc._last_pos = last
    loc._last_pos_at = last_at
    loc._entry_pos = lambda: entry
    return loc


def test_the_nearest_candidate_to_the_last_reading_wins():
    """兩拍之間角色跑不了多遠 —— 離上一次最近的那個才是本人。"""
    loc = _locator(last=(100, 100), last_at=999.9)
    seen = {0xA: (101, 100), 0xB: (30, 250)}
    assert loc._closest([0xA, 0xB], seen) == 0xA


def test_a_stale_reading_is_not_used_as_a_reference():
    """上一次是很久以前的話，角色早就走遠了 —— 改用伺服器給的進圖座標。"""
    loc = _locator(last=(30, 250), last_at=1000.0 - pp._REF_FRESH - 1,
                   entry=(100, 100))
    seen = {0xA: (101, 100), 0xB: (30, 250)}
    assert loc._closest([0xA, 0xB], seen) == 0xA, "要用進圖座標，不是過期的那個"


def test_the_entry_position_is_the_fallback_reference():
    """剛換圖還沒讀過 —— 伺服器剛講過的落點就是最好的參考。"""
    loc = _locator(entry=(19, 377))
    seen = {0xA: (380, 20), 0xB: (20, 376)}
    assert loc._closest([0xA, 0xB], seen) == 0xB


def test_no_reference_at_all_means_we_really_cannot_tell():
    """連參考點都沒有才算真的分不出來 —— 那時候才准回退。"""
    loc = _locator()
    seen = {0xA: (10, 10), 0xB: (200, 200)}
    assert loc._closest([0xA, 0xB], seen) is None


def test_the_ambiguity_message_is_not_spammed(caplog):
    """⚠ 每 0.3 秒一次的路徑不准每次都印 —— 實機一秒六行，三個分身更慘。"""
    loc = pp.PlayerPosition.__new__(pp.PlayerPosition)
    loc._said_many = False
    with caplog.at_level(logging.INFO):
        for _ in range(20):
            if not loc._said_many:
                loc._said_many = True
                logging.getLogger(pp.__name__).info("有 2 個移動元件…")
    said = [r for r in caplog.records if "移動元件" in r.getMessage()]
    assert len(said) == 1


def test_a_map_change_drops_the_reference():
    """⚠ 換圖之後上一張圖的位置**不能**再當參考 —— 被回收的舊元件就停在那裡，
    拿它比對等於專挑舊的那個。"""
    loc = pp.PlayerPosition.__new__(pp.PlayerPosition)
    loc._addr = 0x1
    loc._last_locate = 5.0
    loc._candidates = [0x1, 0x2]
    loc._last_full = 5.0
    loc._moved_here = True
    loc._missing_since = 1.0
    loc._warned_missing = True
    loc._last_pos = (100, 100)
    loc._last_pos_at = 999.0
    loc._said_many = True

    loc.invalidate()
    assert loc._last_pos is None
    assert loc._said_many is False
