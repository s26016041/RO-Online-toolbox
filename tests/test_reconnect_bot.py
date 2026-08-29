"""自動回連的執行層。全部用假的相依，不會開遊戲也不會送封包。

釘住的是使用者實際講過的三件事：
  1. 「斷線還有分是我網路斷線還是遊戲斷線」—— 網路斷了**不准動遊戲**
  2. 「無腦嘗試很糟糕」—— 失敗要退避
  3. 「回連後要保持我斷線之前的一切功能跟選項」，而且「換地圖斷線要繼續跑圖」
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import reconnect
from ro_toolbox.services.reconnect_bot import (
    BACKOFF,
    NO_NETWORK,
    OK,
    WATCHING,
    ReconnectSupervisor,
    Snapshot,
)


class World:
    """假的世界：一個遊戲行程、一條連線、一份「在跑什麼」。"""

    def __init__(self) -> None:
        self.pid = 100
        self.online = True
        self.network = True
        self.closed: list[int] = []
        self.launched = 0
        self.logins = 0
        self.login_ok = True
        self.launch_ok = True
        self.restored: list[tuple[int, Snapshot]] = []
        self.state = Snapshot(
            farming=True, potion="紅色藥水 60%", destination="geffen",
            labels=["自動打怪", "自動補水", "前往 geffen"],
        )

    def build(self) -> ReconnectSupervisor:
        return ReconnectSupervisor(
            "狐狐狸",
            find_pid=lambda: self.pid,
            connected=lambda _pid: self.online,
            network_up=lambda: self.network,
            close_game=self._close,
            relaunch=self._relaunch,
            login=self._login,
            snapshot=lambda _pid: self.state,
            restore=lambda pid, snap: self.restored.append((pid, snap)),
        )

    def _close(self, pid: int) -> None:
        self.closed.append(pid)
        self.pid = None

    def _relaunch(self):
        self.launched += 1
        if not self.launch_ok:
            return None
        self.pid = 200 + self.launched
        return self.pid

    def _login(self, _pid: int) -> bool:
        self.logins += 1
        self.online = self.login_ok
        return self.login_ok


@pytest.fixture
def world() -> World:
    return World()


def _drop(world: World) -> None:
    world.online = False


# ---- 三種「沒有連線」---------------------------------------------------


def test_all_quiet_when_the_connection_is_fine(world):
    bot = world.build()
    assert bot.tick(0.0) == OK
    assert world.closed == [] and world.launched == 0


def test_my_network_being_down_never_touches_the_game(world):
    """⚠ 你自己的網路斷了 —— 關遊戲重開是幫倒忙：重開照樣連不上，
    而且原本還在線上的角色被登出了。"""
    bot = world.build()
    bot.tick(0.0)
    _drop(world)
    world.network = False
    for t in range(0, 600, 30):
        assert bot.tick(float(t)) == NO_NETWORK
    assert world.closed == [], "網路斷線期間不准關遊戲"
    assert world.launched == 0


def test_a_map_change_blip_is_not_a_disconnect(world):
    """換地圖時伺服器會把連線移到另一台 map server，那一瞬間就是沒有連線。
    看到一次就重開＝每次換圖都把自己踢掉。"""
    bot = world.build()
    bot.tick(0.0)
    _drop(world)
    assert bot.tick(1.0) == WATCHING
    assert bot.tick(5.0) == WATCHING
    world.online = True                      # 換圖完成
    assert bot.tick(6.0) == OK
    assert world.closed == []


def test_a_real_disconnect_reconnects_after_the_grace(world):
    bot = world.build()
    bot.tick(0.0)
    _drop(world)
    bot.tick(1.0)
    assert bot.tick(1.0 + reconnect.GRACE_SEC + 1) == OK
    assert world.closed == [100], "要先關掉斷在半途的那個"
    assert world.launched == 1 and world.logins == 1


# ---- 接回斷線前在跑的東西 ----------------------------------------------


def test_it_puts_everything_back(world):
    """使用者：「回連後要保持我斷線之前的一切功能跟選項」。"""
    bot = world.build()
    bot.tick(0.0)
    _drop(world)
    bot.tick(1.0)
    bot.tick(1.0 + reconnect.GRACE_SEC + 1)
    assert len(world.restored) == 1
    pid, snap = world.restored[0]
    assert pid == world.pid, "要接到**新開的**那個行程上"
    assert snap.farming is True
    assert snap.potion == "紅色藥水 60%"
    assert snap.destination == "geffen", "換地圖斷線要繼續跑圖"


def test_the_snapshot_is_taken_while_still_online(world):
    """⚠ 斷線當下的狀態是「什麼都停了」—— 那時候拍等於把要接回去的忘光。"""
    bot = world.build()
    bot.tick(0.0)                              # 這時候拍到「有東西在跑」
    _drop(world)
    world.state = Snapshot()                   # 斷線後畫面上什麼都沒在跑
    bot.tick(1.0)
    bot.tick(1.0 + reconnect.GRACE_SEC + 1)
    _pid, snap = world.restored[0]
    assert snap.farming is True, "接回去的要是斷線**前**那一份"


def test_nothing_running_means_nothing_to_restore(world):
    world.state = Snapshot()
    bot = world.build()
    bot.tick(0.0)
    _drop(world)
    bot.tick(1.0)
    assert bot.tick(1.0 + reconnect.GRACE_SEC + 1) == OK
    assert world.restored == [], "本來就沒在跑就不要亂開東西"


# ---- 失敗要退避，不准無腦重試 ------------------------------------------


def test_a_failed_launch_retries_at_once_then_backs_off(world):
    """兩條使用者指令在這裡碰頭，順序是：**先馬上重試，撐不住才退避**。

    「登入到一半出錯或登入失敗都不等待，馬上關閉重開」（2026-08-29）——
    所以前幾次失敗**下一拍就再開一次**，不必再等 30 秒觀察期。
    「無腦嘗試很糟糕」（更早）—— 所以連續失敗幾次之後開始等，
    伺服器維修我們分不出來，退避是唯一保護。
    """
    world.launch_ok = False
    bot = world.build()
    bot.tick(0.0)
    _drop(world)
    bot.tick(1.0)

    now = 1.0 + reconnect.GRACE_SEC + 1
    bot.tick(now)
    assert world.launched == 1
    # 前幾次：下一拍就再開一次，沒有等待
    immediate = sum(1 for w in reconnect.BACKOFF_SEC if w == 0)
    for _ in range(immediate):
        now += 1
        bot.tick(now)
    assert world.launched == immediate + 1, "前幾次失敗要馬上再試"

    # 撐不住了就開始退避 —— 這一拍之後就不准再開遊戲
    now += 1
    assert bot.tick(now) == BACKOFF
    assert bot.tick(now + 5) == BACKOFF
    assert world.launched == immediate + 1


def test_a_failed_login_also_backs_off(world):
    world.login_ok = False
    bot = world.build()
    bot.tick(0.0)
    _drop(world)
    bot.tick(1.0)
    assert bot.tick(1.0 + reconnect.GRACE_SEC + 1) == BACKOFF
    assert world.logins == 1
    assert world.restored == [], "沒登進去就不准把東西接回去"


def test_backoff_grows_so_it_does_not_hammer(world):
    """開始退避之後，間隔只准越等越久 —— 伺服器維修時不該一直狂開遊戲。"""
    world.launch_ok = False
    bot = world.build()
    bot.tick(0.0)
    _drop(world)
    now = 1.0
    bot.tick(now)
    now += reconnect.GRACE_SEC + 1

    waits = []
    for _ in range(len(reconnect.BACKOFF_SEC)):
        bot.tick(now)                      # 這一拍會嘗試並失敗
        wait = bot._decider._next_try - now
        waits.append(wait)
        if wait <= 0:
            now += 1
            continue
        # 要等的時候 `_lost_at` 會被清掉 —— 等完還得重走一次觀察期才會再試
        now += wait + 1
        bot.tick(now)
        now += reconnect.GRACE_SEC + 1
    assert waits == sorted(waits), f"間隔只准越等越久，實際 {waits}"
    assert waits[0] == 0.0, "前幾次馬上重試（使用者指定）"
    assert waits[-1] > 0.0, "最後總要開始等"
