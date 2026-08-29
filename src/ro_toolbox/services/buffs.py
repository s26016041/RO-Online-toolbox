"""自動補補助技能：勾起來的，**身上沒有或剩不到 10 秒就放**。

跟自動打怪**完全獨立**：自己一條連線、自己一個開關（使用者指定）。
交戰中照放 —— 觸發條件到了就放，不看在不在打架。

## 怎麼知道「這個技能對應身上的哪個狀態」

查表。技能編號與狀態編號（EFST）是兩套編號，對照表在
`tools/build_skill_table.py` 生成時就算好了（`assets/skills.json.gz` 的 `efst` 欄），
用兩條獨立線索交叉驗證：**技能代號去掉職業前綴**（`SM_ENDURE` → `EFST_ENDURE`）
與**中文名完全相同**（「霸體」→「霸體」）。兩條都有就要一致才採用，
不一致的 4 個留空。實機驗證過 `SM_ENDURE` → 1、`KN_TWOHANDQUICKEN` → 2。

**對不到的技能不能自動補**（多半本來就不上狀態：瞬間移動、物品鑑定、偷竊…）。
介面上那些格子不給勾，這裡再擋一次。

## SP 不夠就跳過，不是失敗

SP 不夠是**很正常的暫時狀態**，等回滿了自然就補得上。所以那時候什麼都不做、
不記失敗、不跳訊息（使用者指定）—— 把它當錯誤只會洗版，而且會讓退避越拖越久。

## 送出去之後

每一拍去讀身上的狀態，看到它出現才算成功（CLAUDE.md：做 → 讀 → 確認）。
沒出現就**退避重試**（1 → 2 → 4 → … 最多 30 秒），不是連發也不是永久停用：
交戰中詠唱被打斷很常見，停用它反而是錯的。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from ro_toolbox.core.ro_protocol import build_use_skill
from ro_toolbox.services.game_link import GameLink
from ro_toolbox.services.gamedata import skill_name, skill_table

log = logging.getLogger(__name__)

#: 剩下不到這麼久就先補起來（使用者指定 10 秒）。
REFRESH_BELOW_MS = 10_000
#: 送出之後等這麼久還沒看到狀態上身，就當這一次沒成功。**只是放棄的上限**。
CONFIRM_TIMEOUT = 5.0
#: 沒成功時的退避：第一次等 1 秒，之後翻倍，最多 30 秒。
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
#: 兩次施放之間至少隔多久 —— 一拍只做一件事，免得整排 buff 一起噴出去。
MIN_GAP = 0.6
#: 主迴圈多久跑一拍。
TICK = 0.4
#: 多久檢查一次「連線有沒有換掉」。
#:
#: ⚠ 沒有這一條的代價是實測出來的：換地圖時伺服器會把連線移到另一台地圖伺服器
#: （[PKT-038]），舊 socket 就送不出去了。`BuffBot` 一開始沒有重綁，於是換一次圖
#: 就被自己的「連線已中斷」判斷停掉（使用者日誌：14:44:46 換圖 → 14:45:00 送失敗
#: → 14:45:15 停止）。**換圖不是斷線**，要重綁不是要收攤。
RESYNC_SEC = 2.0


def buff_efst(skill_id: int) -> int | None:
    """這個技能會上哪個狀態。查不到回 None（那就不能自動補）。"""
    return (skill_table().get(skill_id) or {}).get("efst")


def sp_cost(skill_id: int, level: int) -> int | None:
    """這一級要花多少 SP。查不到回 None（那就不做 SP 檢查，送出去讓伺服器決定）。"""
    costs = (skill_table().get(skill_id) or {}).get("sp")
    if not costs or not 1 <= level <= len(costs):
        return None
    return costs[level - 1]


@dataclass(frozen=True)
class BuffPlan:
    """使用者勾選的一個補助技能。

    存的是**技能編號**（穩定的身分），不是清單第幾列 —— 技能列表會因為
    學了新技能而重排（CLAUDE.md：存身分，不存位置）。
    """

    skill_id: int
    #: 要用第幾級施放。介面上可以調低（省 SP）。
    level: int


@dataclass
class _Pending:
    skill_id: int
    sent_at: float


@dataclass
class BuffStats:
    """給介面看的狀態。"""

    running: bool = False
    cast: int = 0
    #: 因為 SP 不夠而跳過幾次。**只是計數，不是錯誤** —— 給人看「它在等」。
    waiting_sp: int = 0
    note: str = ""
    #: 勾了但查不到對應狀態、補不了的技能編號。
    unusable: set[int] = field(default_factory=set)


class BuffKeeper:
    """`tick()` 每拍做**一件事**：確認上一次的結果，或補一個 buff。

    不自己開連線、不自己讀記憶體 —— 送封包與讀狀態都由呼叫端注入
    （[PKT-072]：同一條規則抄很多份就會有人漏掉）。
    """

    def __init__(self, send, aid: int, read_statuses, now) -> None:
        #: `send(data) -> bool`：把封包送出去。
        self._send = send
        #: 自己的 AID。對自己放補助技能就是把目標填自己（[PKT-041]）。
        self._aid = aid
        #: `read_statuses() -> list[ActiveStatus] | None`
        self._read = read_statuses
        self._now = now
        self._plans: list[BuffPlan] = []
        self._pending: _Pending | None = None
        #: 技能編號 → (下次可以再試的時間, 目前的退避秒數)
        self._retry: dict[int, tuple[float, float]] = {}
        self._last_cast = 0.0
        self.stats = BuffStats()

    def set_plans(self, plans) -> None:
        """換掉勾選清單。取消再勾回來等於「再試一次」：退避歸零。"""
        self._plans = [p for p in plans if p.level > 0]
        alive = {p.skill_id for p in self._plans}
        for skill_id in list(self._retry):
            if skill_id not in alive:
                self._retry.pop(skill_id, None)
        self.stats.unusable = {
            p.skill_id for p in self._plans if buff_efst(p.skill_id) is None
        }
        for skill_id in self.stats.unusable:
            log.warning(
                "「%s」查不到它會上哪個狀態，沒辦法確認補上了沒 —— 不自動補",
                skill_name(skill_id),
            )

    @property
    def active(self) -> bool:
        return bool(self._plans)

    def tick(self, sp: int | None = None) -> str | None:
        """做一件事。回傳這一拍的說明（沒事做就回 None）。"""
        if not self._plans:
            return None
        statuses = self._read()
        if statuses is None:
            # 讀不到就**什麼都不做**。把「問不出來」當成「身上沒有」的話，
            # 會對著一個其實還在的 buff 一直重放。
            return self._note("讀不到身上的狀態，這一拍先不補")
        present = {row.efst: row for row in statuses}

        if self._pending is not None:
            settled = self._settle(present)
            if settled is not None:
                return settled     # 這一拍的工作就是「確認上一個的結果」
            return None            # 還在等，先不做別的事
        return self._cast_next(present, sp)

    # ---- 確認上一次 -------------------------------------------------

    def _settle(self, present: dict) -> str | None:
        """看上一次送出去的技能成功了沒。還在等就回 None。"""
        pending = self._pending
        assert pending is not None
        efst = buff_efst(pending.skill_id)
        name = skill_name(pending.skill_id)

        if efst is not None and efst in present:
            self._pending = None
            self._retry.pop(pending.skill_id, None)
            self.stats.cast += 1
            return self._note(f"{name} 補上了")

        now = self._now()
        if now - pending.sent_at < CONFIRM_TIMEOUT:
            return None

        self._pending = None
        _, backoff = self._retry.get(pending.skill_id, (0.0, 0.0))
        backoff = min(BACKOFF_MAX, backoff * 2 if backoff else BACKOFF_START)
        self._retry[pending.skill_id] = (now + backoff, backoff)
        # 交戰中詠唱被打斷很常見 —— 退避重試就好，不必大聲，也不要停用。
        log.info("「%s」放了沒上身，%.0f 秒後再試", name, backoff)
        return self._note(f"{name} 沒上身，{backoff:.0f} 秒後再試")

    # ---- 挑一個來補 -------------------------------------------------

    def _cast_next(self, present: dict, sp: int | None) -> str | None:
        now = self._now()
        if now - self._last_cast < MIN_GAP:
            return None
        for plan in self._plans:
            efst = buff_efst(plan.skill_id)
            if efst is None:
                continue                      # 補不了（已經在 set_plans 說過一次）
            until, _ = self._retry.get(plan.skill_id, (0.0, 0.0))
            if now < until:
                continue                      # 還在退避
            if not self._needs(efst, present):
                continue
            cost = sp_cost(plan.skill_id, plan.level)
            if sp is not None and cost is not None and sp < cost:
                # SP 不夠是暫時狀態，等回滿了自然補得上 —— 安靜跳過（使用者指定）。
                self.stats.waiting_sp += 1
                log.debug("SP %d 不夠放「%s」（要 %d），先跳過",
                          sp, skill_name(plan.skill_id), cost)
                continue
            if not self._send(build_use_skill(plan.level, plan.skill_id, self._aid)):
                return self._note(f"{skill_name(plan.skill_id)} 送不出去")
            self._last_cast = now
            self._pending = _Pending(plan.skill_id, now)
            return self._note(f"補 {skill_name(plan.skill_id)} Lv{plan.level}")
        return None

    @staticmethod
    def _needs(efst: int, present: dict) -> bool:
        """這個 buff 該不該補：身上沒有，或剩不到 10 秒。"""
        row = present.get(efst)
        if row is None:
            return True
        remaining = getattr(row, "remaining_ms", None)
        if remaining is None:
            return False              # 無時限（或算不出可信值）→ 別重放
        return remaining < REFRESH_BELOW_MS

    def _note(self, text: str) -> str:
        self.stats.note = text
        return text


class BuffBot:
    """自己一條連線、自己一個執行緒，跟自動打怪互不相干（使用者指定）。

    ⚠ 跟 `PotionBot` 一樣是**獨立的送封包來源**。兩個 bot 同時對同一隻角色
    送東西是可以的（喝水與放 buff 不互斥），但走路類的動作不要混進來。
    """

    def __init__(self, pid: int, plans=None, on_update=None) -> None:
        self._pid = pid
        self._on_update = on_update
        self._plans = list(plans or [])
        self._link = GameLink(pid, should_stop=lambda: self._stop.is_set(),
                              need_position=False)
        self._keeper: BuffKeeper | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = BuffStats()

    # ---- 對外 -------------------------------------------------------

    @property
    def stats(self) -> BuffStats:
        return self._stats

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_plans(self, plans) -> None:
        self._plans = list(plans)
        if self._keeper is not None:
            self._keeper.set_plans(self._plans)

    def start(self) -> bool:
        if self.running:
            return True
        self._stop.clear()
        self._stats = BuffStats(running=True, note="連線中…")
        self._emit()
        self._thread = threading.Thread(
            target=self._run, name=f"buffs-{self._pid}", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        self._stats.running = False
        self._emit()

    # ---- 執行緒 -----------------------------------------------------

    def _run(self) -> None:
        try:
            if self._setup():
                self._loop()
        except Exception as exc:  # noqa: BLE001 - 背景執行緒不能讓例外逸出
            log.exception("自動補 buff 停了：%s", exc)
            self._fail(f"停了：{exc}")
        finally:
            self._link.close()
            self._stats.running = False
            self._emit()

    def _setup(self) -> bool:
        problem = self._link.open()
        if problem:
            self._fail(problem)
            return False
        reader = self._link.reader
        status = reader.read() if reader is not None else None
        if status is None or not status.aid:
            self._fail("讀不到角色（還沒進到遊戲裡？）")
            return False
        self._keeper = BuffKeeper(
            self._link.send, status.aid, reader.status_effects, time.monotonic
        )
        self._keeper.set_plans(self._plans)
        self._stats = self._keeper.stats
        self._stats.running = True
        self._stats.note = "看著身上的狀態…"
        self._emit()
        return True

    def _loop(self) -> None:
        keeper = self._keeper
        assert keeper is not None
        reader = self._link.reader
        resync_at = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - resync_at >= RESYNC_SEC:
                resync_at = now
                # ⚠ 換圖／換頻道會把連線移走（[PKT-038]）—— 先重綁再說。
                # 少了這一步，換一次圖就會被下面的 `dead` 判斷停掉。
                problem = self._link.resync()
                if problem:
                    self._fail(problem)
                    return
            if self._link.dead:
                self._fail("遊戲連線已中斷，先停下來")
                return
            status = reader.read() if reader is not None else None
            if status is None:
                # 角色不見了（登出／回到選角）—— 停下來，不要空轉送封包。
                self._fail("讀不到角色狀態，先停下來")
                return
            before = keeper.stats.note
            keeper.tick(sp=status.sp)
            if keeper.stats.note != before:
                self._emit()
            self._stop.wait(TICK)

    def _fail(self, message: str) -> None:
        self._stats.running = False
        self._stats.note = message
        log.warning("自動補 buff：%s", message)
        self._emit()

    def _emit(self) -> None:
        if self._on_update is not None:
            self._on_update(self._stats)
