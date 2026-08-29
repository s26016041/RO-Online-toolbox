"""「我正在詠唱，大家別動」—— 跨 bot 的一小片共用狀態。

## 為什麼需要

RO 裡**移動與攻擊都會打斷詠唱**。而這個工具的每個功能各跑各的執行緒、
各自送封包：自動打怪一路在送走路與攻擊，自動補助技能同時在送 `0x0438`。
兩邊都不知道對方在做什麼，結果是 buff **每一次都被自己的走路打斷**。

實機證據（2026-08-29，白狐掛機中）：

    幫「#23810315」放的「加速術」沒上身，等下再試
    幫「#23810315」放的「天使之賜福」沒上身，等下再試
    幫「#25065963」放的「天使之賜福」沒上身，等下再試

封包送得出去、隊友也在旁邊，就是**沒上身** —— 然後退避、再試、再被打斷。
使用者看到的是「幫隊友放 BUFF 反應很慢」。

使用者指定的解法：**幫隊友放 buff 是最高優先，高於打怪跟尋路。**
所以詠唱的那幾百毫秒，走路與攻擊要讓路。

## 為什麼是這種形狀

一個 `{pid: 到期時刻}` 的字典，不是鎖也不是佇列：

- **不會卡死。** 每一筆都有到期時間，持有者掛掉最多影響 `MAX_HOLD` 秒。
  用真的鎖的話，補 buff 那條執行緒一崩就把打怪永遠鎖住。
- **不必等。** 讓路的一方只是「這一拍不動」，下一拍再問一次，
  不會有人被 block 住（CLAUDE.md：不准用「等幾秒」當機制 ——
  這裡等的是一個讀得到的狀態，而且有上限）。
- **跨行程不必同步。** 鍵是 pid，一台機器開三個遊戲互不干擾。
"""

from __future__ import annotations

import threading
import time

#: 一次詠唱最多讓路這麼久。
#:
#: ⚠ 這是**上限不是等待時間**：確認上身就馬上 `release()`。留這個上限是因為
#: 「確認」有可能永遠不來（封包漏收、隊友走掉），沒有上限的話打怪就停在那裡。
#: 值取得比 `buffs.CONFIRM_TIMEOUT`(5) 小 —— 讓路是為了讓那一發打得出去，
#: 不是為了等結果。
MAX_HOLD = 2.5

_lock = threading.Lock()
_until: dict[int, float] = {}


def hold(pid: int, seconds: float = MAX_HOLD, now=time.monotonic) -> None:
    """接下來這幾秒請大家別動（要詠唱了）。秒數會被夾在 `MAX_HOLD`。"""
    seconds = max(0.0, min(seconds, MAX_HOLD))
    deadline = now() + seconds
    with _lock:
        _until[pid] = max(_until.get(pid, 0.0), deadline)


def release(pid: int) -> None:
    """詠唱有結果了（上身或放棄），把路讓回去。"""
    with _lock:
        _until.pop(pid, None)


def held(pid: int, now=time.monotonic) -> bool:
    """現在該讓路嗎。**每拍問一次就好，不要拿來等。**"""
    with _lock:
        deadline = _until.get(pid)
        if deadline is None:
            return False
        if deadline > now():
            return True
        del _until[pid]
        return False


def clear() -> None:
    """測試用：把所有的讓路狀態清掉。"""
    with _lock:
        _until.clear()
