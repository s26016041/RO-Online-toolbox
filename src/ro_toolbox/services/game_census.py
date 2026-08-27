"""盤點「現在有哪些遊戲實例、各自是什麼狀態」。

多帳號自動登入的地基：登入之前要先知道哪些已經在跑、哪些該關掉。

## 判斷規則（只用讀得到的事實，不猜狀態）

| 實例狀態 | 怎麼判斷 | 該怎麼處置 |
|---|---|---|
| 使用中 | `find_server(pid)` 有連線 | **不碰**，並認出它是哪個帳號 |
| 狀態不明 | 沒有連線 | **關掉** —— 沒登入、或登入後斷線了 |

⚠ **斷線也算「沒連線」，一樣關掉。** 我們無法知道它斷在哪一步（錯誤框？選角？
被踢？），接續是賭博、重開是確定的。與其猜，不如回到已知狀態。

⚠ **絕對不強制登入。** 已經連著的實例一律不動 —— 硬登會把使用者正在玩的
那個踢下線。

⚠ **「有連線但認不出帳號」不是一種正常狀態。**
連著就代表它一定登入過，客戶端一定把帳號記在那個位址了。讀不到只可能是
我們自己的問題（多半是映像基底一時掃不到）—— 所以要**重試**，重試完還是
讀不到就當**錯誤大聲報出來**，不要默默歸成一類然後照樣往下做。

## 「這個實例是哪個帳號」怎麼讀

客戶端把**上次送出去的帳號**記在 `Ragexe.exe + 0x11D2ACC`
（見 `input_helper.submitted_account`，今天實測驗證過多次）。
連著的實例一定登入過，所以那裡一定有值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: 遊戲的行程名稱。外殼（Ragnarok.exe）不算實例，但殘留的外殼會擋住新的啟動。
GAME_PROCESS = "ragexe.exe"
LAUNCHER_PROCESS = "ragnarok.exe"


@dataclass(frozen=True, slots=True)
class Instance:
    """一個在跑的遊戲實例。"""

    pid: int
    server: tuple[str, int] | None      # 連到哪；None = 沒連線
    account: str | None                 # 讀得到的話，它送出去的帳號

    @property
    def in_use(self) -> bool:
        """有連線就算使用中 —— 登入畫面、選角、遊戲中都算。"""
        return self.server is not None

    @property
    def label(self) -> str:
        if not self.in_use:
            return f"PID {self.pid}：沒有連線（沒登入或已斷線）"
        who = self.account or "認不出帳號"
        return f"PID {self.pid}：{who} @ {self.server[0]}:{self.server[1]}"


def take() -> list[Instance]:
    """盤點目前所有遊戲實例。"""
    try:
        import psutil
    except ImportError:
        log.warning("沒有 psutil，無法盤點遊戲實例")
        return []

    from ro_toolbox.services.ro_capture import find_server

    found: list[Instance] = []
    for process in psutil.process_iter(["pid", "name"]):
        if (process.info["name"] or "").lower() != GAME_PROCESS:
            continue
        pid = process.info["pid"]
        try:
            server = find_server(pid)
        except Exception as exc:  # noqa: BLE001 - 讀不到就當沒連線
            log.debug("PID %s 讀不到連線：%s", pid, exc)
            server = None
        account = _account_of(pid) if server else None
        if server is not None and account is None:
            log.error(
                "PID %s 連著伺服器卻讀不到它登入的帳號 —— 這不該發生（連著就一定"
                "登入過）。多半是映像基底一時掃不到；這個實例先不動。", pid,
            )
        found.append(Instance(pid=pid, server=server, account=account))
    return found


def _account_of(pid: int, attempts: int = 3) -> str | None:
    """讀出這個實例登入的帳號，讀不到就重試。

    連著的實例一定有值（見模組開頭）；讀不到通常是映像基底那一下沒掃到，
    再試一次多半就好。
    """
    import time

    from ro_toolbox.services import input_helper

    for _ in range(attempts):
        account = input_helper.submitted_account(pid)
        if account:
            return account
        time.sleep(0.3)
    return None


def close_idle(instances: list[Instance] | None = None) -> list[int]:
    """關掉所有**沒有連線**的實例。回傳關掉的 PID。

    ⚠ 有連線的一個都不動。
    """
    import psutil

    victims = [x for x in (instances if instances is not None else take()) if not x.in_use]
    closed = []
    for target in victims:
        try:
            psutil.Process(target.pid).terminate()
            closed.append(target.pid)
            log.info("關掉沒有連線的實例 PID %s", target.pid)
        except Exception as exc:  # noqa: BLE001
            log.warning("關不掉 PID %s：%s", target.pid, exc)
    return closed


def close_stale_launchers() -> list[int]:
    """關掉殘留的啟動器（`Ragnarok.exe`）。

    ⚠ 殘留的外殼會讓新的啟動起不來（實測：外殼還在時新開的遊戲不會出現），
    而它本身不是遊戲實例，關掉沒有副作用。
    """
    import psutil

    closed = []
    for process in psutil.process_iter(["pid", "name"]):
        if (process.info["name"] or "").lower() != LAUNCHER_PROCESS:
            continue
        try:
            process.terminate()
            closed.append(process.info["pid"])
        except Exception as exc:  # noqa: BLE001
            log.debug("關不掉啟動器 %s：%s", process.info["pid"], exc)
    return closed


def account_in_use(username: str, instances: list[Instance] | None = None) -> bool:
    """這個帳號是不是已經有實例在跑。

    ⚠ 認不出帳號的連線實例**也算佔用**（回 True）。那是不該出現的狀態
    （`take()` 已經大聲報過），但既然出現了，寧可跳過這一輪，
    也不要冒著把使用者正在玩的那個踢下線的風險。
    """
    for target in instances if instances is not None else take():
        if not target.in_use:
            continue
        if target.account is None or target.account == username:
            return True
    return False
