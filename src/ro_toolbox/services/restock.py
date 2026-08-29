"""回程之後自動補藥水：認人 → 開店 → **量出單位重量** → 買到負重 65%。

**這支不碰 socket、不碰記憶體、不開執行緒**：封包進來用 `feed()`，要送出去的
封包交給建構子給的 `send`。所以整段測得起來（見 `tests/test_restock.py`）。

## 使用者指定的規則

- **只補 HP**（補水設定的 `hp_item`）。使用者 2026-08-29 指定：回程補給**完全
  不碰 SP** —— 所以這裡連 SP 的欄位都沒有，不是「有欄位但預設不填」。
- 買到「現在負重 ＋ 買下去的重量」達到**上限的 65%** 為止（硬上限 70%，見
  `shop.fill_target()`），**沒有數量上限**。
- 出發前就已經到 65% 了就**一瓶都不買**（連探路那兩瓶都不買）——
  探路是為了算「還能買幾個」，答案已經是 0 的時候買它就是買過頭。
- **錢不夠**就結束自動打怪並跳畫面最前面的通知（這裡回報 `broke=True`，
  真正的停用與通知由介面做）。

## 為什麼要先買兩瓶

單位重量不能猜，也不能解道具說明裡的「重量 : 10」（CLAUDE.md：那是很有自信
的錯）。而負重是**只在變動時**才由伺服器送過來的（[PKT-074]），剛接上很可能
一次都沒看過。

所以一律**先買 1 瓶、再買 1 瓶**：兩次之間的負重差就是**量出來的**單位重量，
而第二次的絕對值就是「現在的負重」—— 兩個未知數一次解決，不必事先知道任何
東西。代價是兩瓶藥水，換到的是「絕不算錯十倍」（負重原始值是畫面的十倍）。

## ⚠ 一次開店只能下一筆單 —— 每買一次都要重新接觸 NPC

實機兩次驗證：第一筆 `0x00C8` 回了 `0x00CA`（成功），**第二筆送出去石沉大海**，
一路等到逾時。手上唯一那份真人擷取（`封包/購買藥水.txt`，一口氣買 300 瓶）
從頭到尾也**只有一個 `0x00C8`** —— 所以「同一次開店買第二筆」本來就沒被驗過，
是我們自己假設出來的。

所以每買一次就重來一輪 `0x0090 接觸 → 0x00C4 → 0x00C5 → 0x00C6 商品清單`，
拿到清單才送下一筆 `0x00C8`。多花幾個來回，換到「不會安靜地停在半路」。

## 每一步都等讀得到的訊號

送出去就等對應的回封包（`0x00C4` → `0x00C6` → `0x00CA`），逾時只是**放棄的
上限**，不是「等到了就當成功」。等不到就大聲停用，不會默默往下買。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from ro_toolbox.services import shop
from ro_toolbox.services.gamedata import item_name

log = logging.getLogger(__name__)

#: 認 NPC 時座標容許差幾格（navi 給的是他站的格，實際可能差一點）。
NPC_SNAP = 3
#: 每一步等回應的上限。**只是放棄的上限**，不是成功的依據。
STEP_TIMEOUT = 8.0
#: 探路各買幾個。買 1 個就量得出單位重量，不必多花錢。
PROBE_AMOUNT = 1
#: 探路要做幾次（兩次才有差值）。
PROBE_ROUNDS = 2


#: 回程道具要補到背包有幾個（使用者 2026-08-29 指定）。
HOME_TARGET = 20


@dataclass
class RestockOrder:
    """要補什麼。**藥水只補 HP**（使用者 2026-08-29 指定：回程補給不補 SP）。

    回程道具（蝴蝶翼之類）是**固定數量**：補到背包有 `HOME_TARGET` 個為止。
    跟藥水的「買到負重幾成」是兩套規則 —— 回程道具買太多只是佔重量，
    而少了一個就回不了城。
    """

    hp_item: int | None = None
    ratio: float = shop.FILL_RATIO
    #: 回程道具的編號（None = 不補）。
    home_item: int | None = None
    #: 背包現在有幾個回程道具（呼叫端查好傳進來 —— 這裡不碰背包）。
    home_have: int = 0

    def home_needed(self) -> int:
        """回程道具還差幾個。已經夠了就是 0。"""
        if not self.home_item:
            return 0
        return max(0, HOME_TARGET - self.home_have)

    def wanted(self) -> list[int]:
        """買的順序：**回程道具先買**。

        它只要幾個、又是「沒有就回不了城」的東西；藥水是買到負重滿為止，
        排在後面才不會把額度吃光。
        """
        out: list[int] = []
        if self.home_item and self.home_needed() > 0:
            out.append(self.home_item)
        if self.hp_item:
            out.append(self.hp_item)
        return out


@dataclass
class RestockStats:
    running: bool = False
    bought: dict[int, int] = field(default_factory=dict)
    #: 錢不夠。⚠ 介面看到這個要**停掉自動打怪並跳通知**（使用者指定）。
    broke: bool = False
    note: str = ""


class Restocker:
    """站到商人旁邊之後的那一段。呼叫端每拍餵 `update()`，看回傳狀態。

    狀態：``idle`` / ``working`` / ``done`` / ``blocked``。
    """

    def __init__(
        self,
        send: Callable[[bytes], None],
        now: Callable[[], float],
        order: RestockOrder,
    ) -> None:
        self._send = send
        self._now = now
        self._order = order
        self._gid: int | None = None
        self._look = 0
        self._cell = (0, 0)
        self._queue: list[int] = []      # 還沒買的道具編號
        self._item: int | None = None    # 現在在買哪一個
        self._price = 0
        self._pending = 0                # 這一筆送出去買了幾個
        self._items: list[shop.ShopItem] = []
        self._weight: int | None = None
        self._max_weight: int | None = None
        self._zeny: int | None = None
        self._probe: list[int] = []      # 每次探路量到的負重
        self._probing = True             # 這個道具還在探單位重量嗎
        #: 重新開店之後要買幾個（None = 開店後照 `_next_item()` 走）。
        #: 一次開店只能下一筆單，所以每一筆都要先把店重開一次。
        self._want: int | None = None
        self._step = ""                  # 現在在等什麼
        self._since = 0.0
        self.stats = RestockStats()

    # ---- 控制 -------------------------------------------------------

    def start(self, look: int, cell: tuple[int, int]) -> None:
        """開始跟這個外觀編號、站在這一格的商人買東西。

        ⚠ 認人要**外觀編號 ＋ 座標兩個都對上**（[DAT-027]），不是猜一個 GID。
        """
        self._look = look
        self._cell = cell
        self._gid = None
        self._queue = list(self._order.wanted())
        self._item = None
        self._items = []
        self._probe = []
        self._probing = True
        self._want = None
        self.stats = RestockStats(running=True)
        self._enter("找商人")

    @property
    def active(self) -> bool:
        return self.stats.running

    # ---- 呼叫端每拍餵進來 -------------------------------------------

    def note_entity(self, gid: int, look: int, x: int, y: int) -> None:
        """看到一個實體。外觀與座標**都**對得上才認它 —— 對不上就當沒看到。"""
        if self._gid is not None or look != self._look:
            return
        if max(abs(x - self._cell[0]), abs(y - self._cell[1])) > NPC_SNAP:
            return
        self._gid = gid
        log.info("認出商人 GID %s（外觀 %s @ %s）", gid, look, self._cell)

    def feed(self, opcode: int, payload: bytes) -> None:
        """收到一個封包。**只處理認得懂的**，其餘一律忽略。"""
        pair = shop.parse_par_change(opcode, payload)
        if pair is not None:
            kind, value = pair
            if kind == shop.SP_WEIGHT:
                self._weight = value
            elif kind == shop.SP_MAX_WEIGHT:
                self._max_weight = value
            elif kind == shop.SP_ZENY:
                self._zeny = value
            return
        if not self.stats.running:
            return
        if opcode == shop.OP_DEAL_TYPE and self._step == "等商店回應":
            self._send(shop.choose_buy(self._gid or 0))
            self._enter("等商品清單")
        elif opcode == shop.OP_SHOP_LIST and self._step == "等商品清單":
            self._items = shop.parse_shop_list(payload)
            log.info("商店有 %d 項商品", len(self._items))
            if self._want is not None:
                amount, self._want = self._want, None
                self._buy(amount)       # 這是「重開店來下一筆」
            else:
                self._next_item(fresh=True)
        elif opcode == shop.OP_BUY_RESULT and self._step == "等買賣結果":
            self._on_result(shop.parse_buy_result(payload))

    def update(self, now: float | None = None) -> str:
        """每拍呼叫。回 idle／working／done／blocked。"""
        if not self.stats.running:
            return "idle" if not self.stats.note else self._settled()
        moment = self._now() if now is None else now
        if self._step in ("找商人", "重新開店"):
            if self._gid is None:
                return self._maybe_timeout(
                    moment, f"⚠ 走到了卻認不出商人（外觀 {self._look} @ {self._cell}）"
                )
            self._send(shop.contact_npc(self._gid))
            self._enter("等商店回應")
            return "working"
        if self._step in ("等商店回應", "等商品清單", "等買賣結果"):
            return self._maybe_timeout(moment, f"⚠ {self._step}逾時，補藥水已停止")
        return "working"

    # ---- 內部 -------------------------------------------------------

    def _settled(self) -> str:
        return "blocked" if self.stats.note.startswith("⚠") else "done"

    def _enter(self, step: str) -> None:
        self._step = step
        self._since = self._now()
        if step:
            self.stats.note = step

    def _maybe_timeout(self, now: float, why: str) -> str:
        if now - self._since <= STEP_TIMEOUT:
            return "working"
        return self._fail(why)

    def _fail(self, why: str) -> str:
        self.stats.running = False
        self.stats.note = why
        self._step = ""
        log.warning("%s", why)
        return "blocked"

    def _finish(self, why: str) -> str:
        self.stats.running = False
        self.stats.note = why
        self._step = ""
        log.info("%s", why)
        return "done"

    def _full(self) -> bool:
        """負重已經到目標了嗎（到了就連探路那兩瓶都不買）。

        ⚠ 讀不到負重就當作**不知道**（False）：負重只在變動時才送過來
        （[PKT-074]），剛接上很可能一次都沒看過。那種情況照樣去探路 ——
        探路會把負重問出來，`_buy_the_rest()` 算出 0 就自然不會多買。
        """
        if self._weight is None or self._max_weight is None:
            return False
        return self._weight >= shop.fill_target(self._max_weight, self._order.ratio)

    def _next_item(self, fresh: bool = False) -> str:
        """換下一個要買的道具。都買完了（或負重滿了）就結束。

        `fresh=True` 代表**剛收到商品清單**（店是開著的），可以直接下單；
        否則要先把店重開一次（一次開店只能下一筆單，見檔頭）。
        """
        self._probe = []
        self._probing = True
        while self._queue:
            item_id = self._queue.pop(0)
            # ⚠ 負重滿了只擋**藥水**：回程道具沒有就回不了城，
            #   而 20 個蝴蝶翼的重量微不足道。
            if item_id != self._order.home_item and self._full():
                continue
            found = shop.find_item(self._items, item_id)
            if found is None:
                # ⚠ 清單裡沒有就是沒有 —— 不准挑一個「看起來像」的來買。
                log.warning("這家店沒有 %s（%s），跳過", item_name(item_id), item_id)
                continue
            self._item = item_id
            self._price = found.price
            if item_id == self._order.home_item:
                # 回程道具是**固定數量**，不探路也不看負重比例：
                # 要幾個是算出來的（補到 HOME_TARGET），不是量出來的。
                amount = self._order.home_needed()
                if amount <= 0:
                    continue
                self._probing = False
                if fresh:
                    self._buy(amount)
                    return "working"
                return self._order_more(amount)
            if fresh:
                self._buy(PROBE_AMOUNT)
                return "working"
            return self._order_more(PROBE_AMOUNT)
        self._item = None
        self._queue = []
        total = sum(self.stats.bought.values())
        if total:
            return self._finish(f"補貨完成，共買了 {total} 個")
        if self._full():
            return self._finish(
                f"負重已達上限的 {self._order.ratio:.0%}，不用補"
            )
        return self._fail("⚠ 這家店沒有你設定的藥水，什麼都沒買")

    def _order_more(self, amount: int) -> str:
        """再買一筆。**一次開店只能下一筆單**，所以先把店重開一次（見檔頭）。"""
        if self._item is None or amount <= 0:
            return "working"
        self._want = amount
        self._enter("重新開店")
        return "working"

    def _buy(self, amount: int) -> None:
        """送出下單封包。⚠ 只有在**剛拿到商品清單**之後呼叫才有用。"""
        if self._item is None or amount <= 0:
            return
        self._pending = amount
        self._send(shop.buy_packet([(self._item, amount)]))
        self._enter("等買賣結果")

    def _on_result(self, result: int | None) -> str:
        if self._item is None:
            return "working"
        if result != shop.RESULT_OK:
            return self._fail(f"⚠ 買 {item_name(self._item)} 被拒絕（結果 {result}）")
        self.stats.bought[self._item] = (
            self.stats.bought.get(self._item, 0) + self._pending
        )
        if not self._probing:
            return self._next_item()
        # 探路：兩次之間的負重差就是**量出來的**單位重量
        if self._weight is None:
            return self._fail("⚠ 買了卻讀不到負重（伺服器沒送 0x00B0），已停止")
        self._probe.append(self._weight)
        if len(self._probe) < PROBE_ROUNDS:
            return self._order_more(PROBE_AMOUNT)
        self._probing = False
        unit = (self._probe[-1] - self._probe[-2]) // PROBE_AMOUNT
        return self._buy_the_rest(unit)

    def _buy_the_rest(self, unit: int) -> str:
        if unit <= 0:
            return self._fail("⚠ 量不出單位重量（買下去負重沒變），已停止")
        if self._max_weight is None or self._weight is None or self._zeny is None:
            return self._fail("⚠ 讀不到負重上限或所持金錢，不敢亂買")
        plan = shop.plan_purchase(
            self._weight, self._max_weight, unit, self._zeny, self._price,
            ratio=self._order.ratio,
        )
        log.info(
            "%s：單位重 %s、還能買 %d 個（卡在%s）",
            item_name(self._item or 0), unit, plan.amount,
            "錢" if plan.limited_by == "zeny" else "負重",
        )
        if plan.limited_by == "zeny":
            # ⚠ 錢不夠要**講出來**：使用者要求那時候停掉自動打怪並跳通知。
            self.stats.broke = True
        if plan.amount <= 0:
            return self._next_item()
        return self._order_more(plan.amount)
