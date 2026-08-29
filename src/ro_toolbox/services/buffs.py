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

from ro_toolbox.core.ro_protocol import build_query, build_use_skill
from ro_toolbox.services import cast_lock
from ro_toolbox.services.game_link import GameLink
from ro_toolbox.services.gamedata import skill_name, skill_table
from ro_toolbox.services.party import PartyWatch

log = logging.getLogger(__name__)

#: 剩下不到這麼久就先補起來（使用者指定 10 秒）。**這是給自己的**。
REFRESH_BELOW_MS = 10_000
#: 幫隊友補的門檻：剩不到總時長的這個比例就補（使用者指定 50%）。
#:
#: 為什麼跟自己不一樣：自己的狀態讀得到**精確的剩餘毫秒**（記憶體），
#: 隊友的只能從封包裡的 total 自己倒數 —— 誤差比較大，用比例比較穩，
#: 而且幫別人放本來就該早一點（他不會等你）。
MATE_REFRESH_RATIO = 0.5
#: 詠唱期間叫走路與打怪讓路的秒數。
#:
#: **移動與攻擊會打斷詠唱** —— 而自動打怪一路在送走路封包，所以不讓路的話
#: 幫隊友放的每一發都會被自己人打斷（實機日誌連續三次「沒上身」，
#: 使用者回報「反應很慢」）。使用者指定：幫隊友放 buff **最高優先**。
#:
#: 抓 1.2 秒：一般補助技能的詠唱都在一秒內，上身之後 `_settle_mates()`
#: 會馬上 `release()`，所以真正讓路的時間通常更短。
CAST_HOLD = 1.2
#: 查不到技能射程時，幫隊友放的距離上限（格）。
#:
#: **判準是技能自己的射程**（`assets/skills.json.gz` 的 `range`：天使之賜福
#: 與加速術是 9、塗毒是 1）—— 使用者 2026-08-29 指定：
#: 「隊友在我施放範圍、又沒有該 BUFF 或剩下時間不到一半，就要馬上幫他放」。
#:
#: ⚠ 同一天稍早先做過「一律夾在 5 格」，理由是使用者怕「無腦一直空放」。
#: 那個顧慮現在由**退避**接住（`_mate_retry`：放不上去就 1→2→4…秒），
#: 用不著再犧牲射程 —— 夾在 5 格的副作用是「隊友明明放得到卻不放」。
#: 這個常數只在**查不到射程**時當保守預設。
MATE_MAX_CELLS = 5
#: 送出之後等這麼久還沒看到狀態上身，就當這一次沒成功。**只是放棄的上限**。
CONFIRM_TIMEOUT = 5.0
#: 沒成功時的退避：第一次等 1 秒，之後翻倍，最多 30 秒。
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
#: 兩次施放之間至少隔多久 —— 免得整排 buff 一起噴出去。
#:
#: 🔬 2026-08-29 從 0.6 降到 0.3（使用者回報「幫隊友放有點慢」）。
#: 真正的大頭不在這裡（見 `PartyWatch` 的說明：隊友**動一下**我們才認得出他），
#: 但這一段是我們控制得了的部分。
MIN_GAP = 0.3
#: 主迴圈多久跑一拍。
#:
#: 🔬 同上，從 0.4 降到 0.2。一拍的成本只是讀一次記憶體狀態（微秒級）。
TICK = 0.2
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


#: 「對象」欄位裡代表**指名一個人**的寫法。看完整份技能表（1605 個）之後，
#: 補助技能只出現這幾種：`目標1個`(32)、`1個目標`(4)、`自己和隊友1名`(1)、
#: `自己以外的一名隊員`(1)。⚠ `地面1格` 是「格」不是「個」，不會誤中。
_ONE_PERSON = ("1個", "1名", "一名")


def can_target_others(skill_id: int) -> bool:
    """這個技能**送得出「指定某個隊友」的封包**嗎。判不出來一律 False（安全退化）。

    判準是遊戲自己的技能說明裡的「對象」欄位（`assets/skills.json.gz`），
    而且**要它指名「一個」對象**：

        對象 : 目標1個            → 可以（加速術、天使之賜福，射程 9）
        對象 : 自己和隊友1名      → 可以（塗毒，說明寫「在指定目標的武器上…」）
        對象 : 自己以外的一名隊員 → 可以
        對象 : 自己               → 不行（霸體、雙手劍攻擊速度增加）
        對象 : 立即施展           → 不行（以自己為中心）
        對象 : 地面1格            → 地面技能，不是對人

    ## ⚠⚠ 「自己和隊員」是**範圍技**，不是「可以指定隊友」

    舊版看到「隊員」兩個字就回 True，於是速度激發(111)、無視體型攻擊(112)、
    凶砍(113) 被判成可以指定隊友放。**它們其實是以自己為中心的範圍技** ——
    遊戲自己的說明講得很清楚（凶砍：「增加自己及**周圍**隊員的物理傷害」，
    天使之障壁：「提升自己和**畫面內**隊員的…」）。送「目標＝隊友 GID」的
    `0x0438` 出去伺服器不會理，於是那顆技能永遠在重試然後退避，
    **安靜地什麼都沒發生**。

    這類技能正確的用法是**當成自己的 buff 勾起來**：放在自己身上，
    範圍效果本來就會罩到旁邊的隊友。

    分辨的方法就是「對象有沒有指名一個」：`自己和隊員`／`自己與我軍`
    是一群人（範圍），`自己和隊友1名`／`目標1個` 是一個人（可指定）。
    射程也對得上（可指定的是 9 格，範圍技都是 1）—— 但射程 1 的塗毒
    也是可指定的，所以**射程不能當判準**，只能當旁證。
    """
    target = (skill_table().get(skill_id) or {}).get("target") or ""
    if not target:
        return False
    if "格" in target:
        return False                      # 地面1格：對地不對人
    return any(word in target for word in _ONE_PERSON)


def skill_range(skill_id: int, level: int) -> int | None:
    """這一級的射程（格）。查不到回 None —— 呼叫端要自己決定安全預設。

    來源是遊戲自己的技能說明（`assets/skills.json.gz` 的 `range`）：
    天使之賜福／加速術是 9，塗毒是 1，以自己為中心的範圍技也是 1。
    """
    ranges = (skill_table().get(skill_id) or {}).get("range")
    if not ranges or not 1 <= level <= len(ranges):
        return None
    return ranges[level - 1]


def cells_between(a: tuple[int, int], b: tuple[int, int]) -> int:
    """RO 的距離是**正方形**的（八方向各算一格），所以取兩軸差的最大值。"""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


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
    #: 幫隊友補了幾次。
    mate_cast: int = 0


class BuffKeeper:
    """`tick()` 每拍做**一件事**：確認上一次的結果，或補一個 buff。

    不自己開連線、不自己讀記憶體 —— 送封包與讀狀態都由呼叫端注入
    （[PKT-072]：同一條規則抄很多份就會有人漏掉）。
    """

    def __init__(self, send, aid: int, read_statuses, now, party=None,
                 read_position=None, hold=None, release=None) -> None:
        #: `send(data) -> bool`：把封包送出去。
        self._send = send
        #: 自己的 AID。對自己放補助技能就是把目標填自己（[PKT-041]）。
        self._aid = aid
        #: `read_statuses() -> list[ActiveStatus] | None`
        self._read = read_statuses
        self._now = now
        #: `services/party.PartyWatch`（None = 不幫隊友放）。
        self._party = party
        #: `read_position() -> (x, y) | None`：我現在在哪一格。
        #: 只有「幫隊友放」用得到（要量距離）。讀不到就不幫隊友放 ——
        #: 不知道距離而硬放就是使用者說的「無腦一直放」。
        self._read_position = read_position
        #: 「不知道自己在哪」講過了沒（不擋就是每拍一行）。
        self._said_no_pos = False
        #: `hold(seconds)` / `release()`：叫走路與打怪讓路。
        #:
        #: **移動與攻擊會打斷詠唱**，而自動打怪一路在送走路封包 —— 不讓路的話
        #: buff 每一次都被自己人打斷（實機：連續三次「沒上身」）。
        #: 使用者指定「幫隊友放 buff 最高優先，高於打怪跟尋路」。
        #: 見 `services/cast_lock.py`。
        self._hold = hold or (lambda _seconds: None)
        self._release = release or (lambda: None)
        #: 要不要幫隊友放（使用者在介面上勾）。
        self.help_mates = False
        self._plans: list[BuffPlan] = []
        self._pending: _Pending | None = None
        #: 技能編號 → (下次可以再試的時間, 目前的退避秒數)
        self._retry: dict[int, tuple[float, float]] = {}
        self._last_cast = 0.0
        #: 幫隊友放的節流跟自己的分開 —— 兩邊互相卡住只會讓兩邊都慢。
        self._last_mate_cast = 0.0
        #: (技能, 隊友AID) → 送出時刻。同一個目標的同一個技能一次只送一發。
        self._mate_pending: dict[tuple[int, int], float] = {}
        #: (技能, 隊友AID) → (下次可以再試的時間, 目前的退避秒數)
        #:
        #: ⚠⚠ **一定要有。** 幫隊友放會叫打怪讓路（`cast_lock`），
        #: 而放不上去的時候如果每 `MIN_GAP` 秒就重試一次，等於**一直占著路**，
        #: 打怪永遠不動 —— 45 秒之後還會被「毫無進展」判定成卡住。
        #: 放不上去的原因多半是短時間內好不了的（隊友換圖了、伺服器判定
        #: 距離不夠、技能對他無效），退避才是對的。
        self._mate_retry: dict[tuple[int, int], tuple[float, float]] = {}
        self.stats = BuffStats()

    def set_plans(self, plans) -> None:
        """換掉勾選清單。取消再勾回來等於「再試一次」：退避歸零。"""
        self._plans = [p for p in plans if p.level > 0]
        alive = {p.skill_id for p in self._plans}
        for skill_id in list(self._retry):
            if skill_id not in alive:
                self._retry.pop(skill_id, None)
        for key in list(self._mate_retry):
            if key[0] not in alive:
                self._mate_retry.pop(key, None)
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

        done = self._settle_mates()
        if done is not None:
            return done
        waiting = False
        if self._pending is not None:
            settled = self._settle(present)
            if settled is not None:
                return settled     # 這一拍的工作就是「確認上一個的結果」
            waiting = True
        if not waiting:
            mine = self._cast_next(present, sp)
            if mine is not None:
                return mine
        # 自己的都補好了才輪到隊友 —— 自己倒了就沒人補得成（使用者指定的
        # 是「多一個按鈕幫隊友放」，不是「改成幫隊友放」）。
        #
        # ⚠ **等自己那一發確認的時候照樣可以幫隊友放**：目標不同、狀態也分開
        # 確認，序列化沒有好處，只會讓「幫隊友」慢上好幾秒
        # （使用者回報「等 10 秒才放」）。同一個目標仍然一次一發。
        return self._cast_for_mates(sp)

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

    def _settle_mates(self) -> str | None:
        """幫隊友放的那幾發成功了沒。看的是**他**身上有沒有那個狀態。

        ⚠ 每個 `(技能, 隊友)` 各自獨立 —— 序列化只會讓「三個隊友」變成
        三倍的等待時間（使用者回報「等 10 秒才放」）。
        """
        if not self._mate_pending:
            return None
        now = self._now()
        for (skill_id, aid), sent_at in list(self._mate_pending.items()):
            efst = buff_efst(skill_id)
            mate = self._mate(aid)
            name = skill_name(skill_id)
            key = (skill_id, aid)
            if efst is not None and mate is not None and mate.has(efst, now):
                del self._mate_pending[key]
                self._mate_retry.pop(key, None)      # 成功了就把退避歸零
                self.stats.mate_cast += 1
                self._release()          # 上身了，馬上把路讓回去
                return self._note(f"幫 {mate.label()} 補上 {name}")
            if now - sent_at < CONFIRM_TIMEOUT:
                continue
            del self._mate_pending[key]
            self._release()
            _, backoff = self._mate_retry.get(key, (0.0, 0.0))
            backoff = min(BACKOFF_MAX, backoff * 2 if backoff else BACKOFF_START)
            self._mate_retry[key] = (now + backoff, backoff)
            who = mate.label() if mate is not None else f"#{aid}"
            log.info("幫「%s」放的「%s」沒上身，%.0f 秒後再試", who, name, backoff)
            return self._note(f"{who} 的 {name} 沒上身，{backoff:.0f} 秒後再試")
        return None

    def _mate(self, aid: int | None):
        if self._party is None or aid is None:
            return None
        for mate in self._party.mates():
            if mate.aid == aid:
                return mate
        return None

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

    def _cast_for_mates(self, sp: int | None) -> str | None:
        """幫隊友補一個。**一拍只補一個人的一個技能**。

        規則（使用者指定）：
        - 身上沒有那個 buff，**或**剩不到總時長的 50% 就補。
        - **放不到別人身上的技能直接跳過**（`can_target_others()`）——
          「自己」類與「以自己為中心的範圍技」送出去只會被伺服器丟掉。
        - **隊友太遠就不放**：門檻是技能自己的射程，再夾一個 `MATE_MAX_CELLS`
          的上限（使用者指定 5 格）。量不出距離（讀不到自己的座標）也不放。
        - SP 不夠**安靜跳過**，跟自己那條一樣。
        """
        if not self.help_mates or self._party is None:
            return None
        now = self._now()
        if now - self._last_mate_cast < MIN_GAP:
            return None
        mates = self._party.mates()
        if not mates:
            return None
        me = self._read_position() if self._read_position is not None else None
        if me is None:
            # 不知道自己在哪就量不出距離 —— **不放**（安全退化）。
            # 硬放的話放不到的那幾個會一直重試，正是使用者說的「無腦一直放」。
            if not self._said_no_pos:
                self._said_no_pos = True
                log.info("讀不到自己的座標，這段時間不幫隊友放（量不出距離）")
            return None
        self._said_no_pos = False
        for plan in self._plans:
            efst = buff_efst(plan.skill_id)
            if efst is None or not can_target_others(plan.skill_id):
                continue
            until, _ = self._retry.get(plan.skill_id, (0.0, 0.0))
            if now < until:
                continue
            cost = sp_cost(plan.skill_id, plan.level)
            if sp is not None and cost is not None and sp < cost:
                self.stats.waiting_sp += 1
                continue
            # 用**技能自己的射程**（查不到才退回保守值）。夾小的話會變成
            # 「隊友明明放得到卻不放」—— 那是使用者這次指出的問題。
            reach = skill_range(plan.skill_id, plan.level)
            if reach is None:
                reach = MATE_MAX_CELLS
            for mate in mates:
                key = (plan.skill_id, mate.aid)
                if key in self._mate_pending:
                    continue        # 這一發還在等結果，別重送
                until, _ = self._mate_retry.get(key, (0.0, 0.0))
                if now < until:
                    continue        # 上一發沒上身，等退避時間到
                if cells_between(me, mate.cell) > reach:
                    # 太遠 —— 伺服器只會安靜地丟掉，放了等於白放（見 MATE_MAX_CELLS）。
                    continue
                if not self._party.needs(mate, efst, MATE_REFRESH_RATIO):
                    # 他身上已經有了 —— 上一發其實成功了（只是確認來得晚），
                    # 或別人幫他放了。退避沒有意義了，清掉：下次掉了要馬上補。
                    self._mate_retry.pop(key, None)
                    continue
                # ⚠ **先要求讓路再送。** 反過來的話走路那一拍已經送出去了，
                # 詠唱一開始就被自己人打斷。
                self._hold(CAST_HOLD)
                data = build_use_skill(plan.level, plan.skill_id, mate.aid)
                if not self._send(data):
                    self._release()
                    return self._note(f"{skill_name(plan.skill_id)} 送不出去")
                self._last_mate_cast = now
                self._mate_pending[key] = now
                return self._note(
                    f"幫 {mate.label()} 補 {skill_name(plan.skill_id)} Lv{plan.level}"
                )
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

    def __init__(self, pid: int, plans=None, on_update=None,
                 help_mates: bool = False) -> None:
        self._pid = pid
        self._on_update = on_update
        self._plans = list(plans or [])
        self._help_mates = help_mates
        #: 隊友追蹤（要收封包才有東西，見 `services/party.py`）。
        self._party: PartyWatch | None = None
        self._asked_names: set[int] = set()
        self._link = GameLink(pid, on_packet=self._on_packet,
                              should_stop=lambda: self._stop.is_set(),
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

    def set_plans(self, plans, help_mates: bool | None = None) -> None:
        self._plans = list(plans)
        if help_mates is not None:
            self._help_mates = help_mates
        if self._keeper is not None:
            self._keeper.set_plans(self._plans)
            self._keeper.help_mates = self._help_mates

    def _on_packet(self, packet) -> None:  # noqa: ANN001 - RoPacket
        """把封包餵給隊友追蹤。**只收進來的**（自己送出去的不算資訊）。"""
        if packet.outbound or self._party is None:
            return
        self._party.feed(packet.opcode, packet.payload)

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
        # ⚠ 收攤一定要放行：不放的話打怪會空等到 `MAX_HOLD` 才動。
        cast_lock.release(self._pid)
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
        self._party = PartyWatch(status.aid, time.monotonic)
        self._keeper = BuffKeeper(
            self._link.send, status.aid, reader.status_effects, time.monotonic,
            party=self._party, read_position=reader.read_position,
            hold=lambda seconds: cast_lock.hold(self._pid, seconds),
            release=lambda: cast_lock.release(self._pid),
        )
        self._keeper.set_plans(self._plans)
        self._keeper.help_mates = self._help_mates
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
            self._ask_names()
            before = keeper.stats.note
            keeper.tick(sp=status.sp)
            if keeper.stats.note != before:
                self._emit()
            self._stop.wait(TICK)

    def _ask_names(self) -> None:
        """幫還沒有名字的隊友送一次 `0x0368` 查名字（回應是 `0x0095`）。

        ⚠ 一個 AID 只問一次 —— 問不到就顯示 AID，不要每拍再問一次。
        """
        if self._party is None:
            return
        for aid in self._party.unnamed():
            if aid in self._asked_names:
                continue
            self._asked_names.add(aid)
            self._link.send(build_query(aid))

    def _fail(self, message: str) -> None:
        self._stats.running = False
        self._stats.note = message
        log.warning("自動補 buff：%s", message)
        self._emit()

    def _emit(self) -> None:
        if self._on_update is not None:
            self._on_update(self._stats)
