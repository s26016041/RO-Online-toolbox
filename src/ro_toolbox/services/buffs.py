"""看著身上的狀態，勾選的補助技能**沒有或快過期就補上**。

## 怎麼知道「這個技能對應身上的哪個狀態」

技能編號（`KN_TWOHANDQUICKEN` = 60）跟狀態編號（`EFST_TWOHANDQUICKEN` = 2）是
**兩套不同的編號**，客戶端沒有現成的對照表。名字長得像不算證據
（CLAUDE.md：不准用「看起來像」）。

所以這裡**當場學**：施放前記下身上的狀態集合，施放後看多出來哪一個 ——
多出來的那個就是它。只有「恰好多一個」才採信；多好幾個（同時被怪上了 debuff）
或一個都沒多，這一次就不學，下次再說。

學到之後存進設定，下次直接用（`learned` 進出都由呼叫端負責，這裡不碰檔案）。

## 施放後怎麼確認

**不睡覺等**。送出去之後每一拍去讀身上的狀態，看到它出現才算成功
（CLAUDE.md：做 → 讀 → 確認）。逾時只當**放棄的上限**，不是成功的依據。

連續失敗三次就停用那個技能並大聲說 —— 常見原因是 SP 不夠、還在冷卻、
或者那根本不是會上狀態的技能（「物品鑑定」也被分類成補助型）。
安靜地每 0.5 秒重送一次是最糟的結果。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ro_toolbox.core.ro_protocol import build_use_skill
from ro_toolbox.services.gamedata import skill_name

log = logging.getLogger(__name__)

#: 剩下不到這麼久就先補起來（使用者指定 10 秒）。
REFRESH_BELOW_MS = 10_000
#: 送出之後等這麼久還沒看到狀態上身，就當這一次失敗。**只是放棄的上限**。
CONFIRM_TIMEOUT = 5.0
#: 同一個技能連續失敗幾次就停用它。
MAX_FAILURES = 3
#: 兩次施放之間至少隔多久 —— 一拍只做一件事，免得整排 buff 一起噴出去。
MIN_GAP = 0.6


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
    before: frozenset[int]


@dataclass
class BuffStats:
    """給介面看的狀態。"""

    cast: int = 0
    failed: int = 0
    note: str = ""
    #: 已停用的技能編號 → 為什麼。
    disabled: dict[int, str] = field(default_factory=dict)


class BuffKeeper:
    """`tick()` 每拍做**一件事**：確認上一次的結果，或補一個 buff。

    不自己開連線、不自己讀記憶體 —— 送封包與讀狀態都由呼叫端注入，
    這樣 farm_bot 可以沿用它已經建好的那一份（[PKT-072]：同一條規則抄很多份
    就會有人漏掉）。
    """

    def __init__(
        self,
        send,
        aid: int,
        read_statuses,
        now,
        learned: dict[int, int] | None = None,
    ) -> None:
        #: `send(data) -> bool`：把封包送出去。
        self._send = send
        #: 自己的 AID。對自己放補助技能就是把目標填自己（[PKT-041]）。
        self._aid = aid
        #: `read_statuses() -> list[ActiveStatus] | None`
        self._read = read_statuses
        self._now = now
        #: 技能編號 → 它會上的狀態編號（EFST）。學到就留著。
        self.learned: dict[int, int] = dict(learned or {})
        self._plans: list[BuffPlan] = []
        self._pending: _Pending | None = None
        self._failures: dict[int, int] = {}
        self._last_cast = 0.0
        self.stats = BuffStats()

    def set_plans(self, plans) -> None:
        """換掉勾選清單。**已經學到的對應不清掉** —— 那是知識不是設定。"""
        self._plans = [p for p in plans if p.level > 0]
        alive = {p.skill_id for p in self._plans}
        # 取消勾選再勾回來，等於使用者說「再試一次」：把失敗計數與停用清掉。
        for skill_id in list(self._failures):
            if skill_id not in alive:
                self._failures.pop(skill_id, None)
                self.stats.disabled.pop(skill_id, None)

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
            done = self._settle(present)
            if done is not None:
                return done

        return self._cast_next(present, sp)

    # ---- 確認上一次 -------------------------------------------------

    def _settle(self, present: dict[int, object]) -> str | None:
        """看上一次送出去的技能成功了沒。還在等就回 None（這一拍不做別的）。"""
        pending = self._pending
        assert pending is not None
        now = self._now()
        appeared = frozenset(present) - pending.before
        known = self.learned.get(pending.skill_id)

        if known is not None and known in present:
            self._succeed(pending.skill_id)
            return self._note(f"{skill_name(pending.skill_id)} 補上了")

        if known is None and len(appeared) == 1:
            efst = next(iter(appeared))
            self.learned[pending.skill_id] = efst
            self._succeed(pending.skill_id)
            return self._note(
                f"{skill_name(pending.skill_id)} 補上了（學到它對應狀態 {efst}）"
            )

        if now - pending.sent_at < CONFIRM_TIMEOUT:
            return None       # 還在等，這一拍不做別的事

        self._pending = None
        self.stats.failed += 1
        count = self._failures.get(pending.skill_id, 0) + 1
        self._failures[pending.skill_id] = count
        name = skill_name(pending.skill_id)
        if count >= MAX_FAILURES:
            why = (
                "施放後身上沒出現對應的狀態 —— 可能 SP 不夠、還在冷卻，"
                "或者它根本不是會上狀態的技能"
            )
            self.stats.disabled[pending.skill_id] = why
            log.warning("補助技能「%s」連續失敗 %d 次，停用它：%s", name, count, why)
            return self._note(f"{name} 連續失敗 {count} 次，停用")
        log.info("補助技能「%s」這次沒成功（第 %d 次），等下再試", name, count)
        return self._note(f"{name} 沒成功，等下再試")

    def _succeed(self, skill_id: int) -> None:
        self._pending = None
        self._failures.pop(skill_id, None)
        self.stats.cast += 1

    # ---- 挑一個來補 -------------------------------------------------

    def _cast_next(self, present: dict, sp: int | None) -> str | None:
        now = self._now()
        if now - self._last_cast < MIN_GAP:
            return None
        for plan in self._plans:
            if plan.skill_id in self.stats.disabled:
                continue
            if not self._needs(plan, present):
                continue
            if sp is not None and sp <= 0:
                return self._note("SP 用完了，先不補 buff")
            data = build_use_skill(plan.level, plan.skill_id, self._aid)
            if not self._send(data):
                return self._note(f"{skill_name(plan.skill_id)} 送不出去")
            self._last_cast = now
            self._pending = _Pending(plan.skill_id, now, frozenset(present))
            return self._note(f"補 {skill_name(plan.skill_id)} Lv{plan.level}")
        return None

    def _needs(self, plan: BuffPlan, present: dict) -> bool:
        """這個 buff 該不該補。"""
        efst = self.learned.get(plan.skill_id)
        if efst is None:
            return True             # 還不知道它上什麼狀態 —— 放一次才學得到
        row = present.get(efst)
        if row is None:
            return True             # 身上沒有
        remaining = getattr(row, "remaining_ms", None)
        if remaining is None:
            return False            # 無時限（或算不出可信值）→ 別重放
        return remaining < REFRESH_BELOW_MS

    def _note(self, text: str) -> str:
        self.stats.note = text
        return text
