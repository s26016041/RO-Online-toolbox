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

## ⚠⚠ 真正把訂單吃掉的是 `0x09D4` —— **客戶端買完會自己關店**（[PKT-092]）

2026-09-01 實機證明（狐狐狸 @ prt_fild05 道具商人，`close_race.py`）：
在 `0x00C6 商品清單` 與 `0x00C8 下單` 中間插一個 `0x09D4`，那一筆
**完全沒有回應** —— 沒有 `0x0B41`、沒有 `0x00CA`，跟使用者回報的
「等買賣結果逾時」一模一樣。對照組（不插）當場買到。

而那一包**不是我們送的**：客戶端每收到一次 `0x00CA` 就會自己送一個
`0x09D4` 把它的商店視窗關掉（實測落在結果後 **16~78 ms**，客戶端在背景時更久）。
我們一收到結果就重開店，於是那一包會**插進我們的開店流程中間**，
把伺服器那邊的交易狀態關掉，後面的 `0x00C8` 就被安靜地丟掉。
這解釋了為什麼同一段程式碼 06:33 買得到 192 個、14:09 卻停在「共買了 1 個」——
**它從頭到尾是個時序賽跑，不是設定或商人的問題。**

### 修法：**把一筆單的三包併成一次 `send()`**

`接觸 0x0090 ＋ 選買 0x00C5 ＋ 下單 0x00C8` 一次寫進 socket
（`shop.order_packet()`）。同一次寫入的位元組是連續的，客戶端**插不進來** ——
那個縫隙從此不存在，不是「比較不容易中」而是**沒有了**。

實機驗收（同一隻角色、同一個商人、客戶端當時正處在「每收到商品清單就馬上
送 `0x09D4`」的狀態）：**分開送 0/3 成交、併成一包 3/3 成交**。

順帶拿掉的東西：不必再等 `0x00C4`、不必「重開店」那一輪、不必等客戶端關店。
每一筆單自己帶著開店，所以 `一次開店只能下一筆單`（[PKT-079]）也自動滿足了。

還留著的保險（時序以外的原因也可能吃掉訂單）：

- 下單之後沒等到 `0x00CA`：先看 **`0x0B41`（東西進背包了沒）**，
  進了就照成交算，沒進就**重下一次**，不再整趟放棄。

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
#: 同一筆訂單最多重下幾次。時序以外的原因（伺服器忙、封包掉了）也可能吃掉
#: 一筆單，重下一次很便宜；試完還是沒回應就大聲停。
ORDER_RETRIES = 2
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
    #: ★ 開了店之後**貨架上沒有**的道具編號。
    #:
    #: 開店那一包（`0x00C6`）把整個貨架送過來，所以這是**當場量到的事實**，
    #: 不是推論。呼叫端拿它記進 `services/shop_reach.py`，下一趟就不會再走
    #: 同一條路去撲空（實機 2026-09-03：連續好幾趟走到高級藥水商人，
    #: 每次都走到底才說「這家店沒有你設定的藥水」）。
    missing: list[int] = field(default_factory=list)
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
        self._step = ""                  # 現在在等什麼
        self._since = 0.0
        #: 商店視窗開著嗎。**開著的時候角色不能移動** —— 收尾一定要關掉。
        self._opened = False
        #: 這一筆已經有幾個進背包了（`0x0B41`）。逾時的時候用它分辨
        #: 「其實成交了」與「伺服器整包丟掉了」，不必猜。
        self._got = 0
        #: 這一筆重下過幾次。
        self._retries = 0
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
        self._got = 0
        self._retries = 0
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
        if opcode == shop.OP_CLOSE_SHOP:
            # 客戶端把它的商店視窗關掉了。**現在無所謂** —— 每一筆單都自己
            # 帶著開店（`shop.order_packet()`），它插不進去了。記一下就好，
            # 收尾才知道要不要補一個關閉。
            self._opened = False
            return
        if opcode == shop.OP_ITEM_ADDED and self._step == "等買賣結果":
            got = shop.parse_item_added(payload)
            if got is not None and got[0] == self._item:
                self._got += got[1]
            return
        if opcode == shop.OP_SHOP_LIST and self._step == "等商品清單":
            self._items = shop.parse_shop_list(payload)
            self._opened = True
            log.info("商店有 %d 項商品", len(self._items))
            self._next_item()
        elif opcode == shop.OP_BUY_RESULT and self._step == "等買賣結果":
            self._on_result(shop.parse_buy_result(payload))

    def update(self, now: float | None = None) -> str:
        """每拍呼叫。回 idle／working／done／blocked。"""
        if not self.stats.running:
            return "idle" if not self.stats.note else self._settled()
        moment = self._now() if now is None else now
        if self._step == "找商人":
            if self._gid is None:
                return self._maybe_timeout(
                    moment, f"⚠ 走到了卻認不出商人（外觀 {self._look} @ {self._cell}）"
                )
            self._send(shop.open_shop(self._gid))     # 接觸 ＋ 選買，一次送
            self._opened = True
            self._enter("等商品清單")
            return "working"
        if self._step == "等買賣結果":
            if moment - self._since <= STEP_TIMEOUT:
                return "working"
            return self._order_timed_out()
        if self._step == "等商品清單":
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

    def _order_timed_out(self) -> str:
        """`0x00CA` 沒回來。**先看東西有沒有進背包**，不要憑逾時下結論。

        伺服器把訂單安靜地丟掉是**時序賽跑**（客戶端的 `0x09D4` 插進中間），
        重下一次多半就過了 —— 舊版在這裡直接整趟放棄，於是使用者看到的是
        「補了 蝴蝶翅膀 1 個」然後一瓶藥水都沒有。
        """
        what = item_name(self._item or 0)
        if self._got:
            log.warning("買 %s 沒收到 0x00CA，但 %d 個已經進背包了 —— 照成交算",
                        what, self._got)
            return self._bought(self._got)
        if self._retries < ORDER_RETRIES:
            self._retries += 1
            log.warning("買 %s 這一筆伺服器沒有任何回應，再下一次（第 %d 次）",
                        what, self._retries)
            self._buy(self._pending)
            return "working"
        return self._fail(f"⚠ 買 {what} 送出去沒有任何回應（試了 {self._retries + 1} 次），已停止")

    def _close_shop(self) -> None:
        """把商店視窗關掉。**每一條收尾路徑都要走這裡。**

        商店開著的時候角色**不能移動** —— 買完不關就走不回練功點
        （使用者實測回報）。跟 [PKT-075] 的「最後那個『離開』不按掉，
        傳送永遠不會發生」是同一類問題。
        失敗的路徑也要關：那時候商店多半也開著。
        """
        if self._opened:
            self._opened = False
            self._send(shop.close_shop())

    def _fail(self, why: str) -> str:
        self._close_shop()
        self.stats.running = False
        self.stats.note = why
        self._step = ""
        log.warning("%s", why)
        return "blocked"

    def _finish(self, why: str) -> str:
        self._close_shop()
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

    def _next_item(self) -> str:
        """換下一個要買的道具。都買完了（或負重滿了）就結束。

        ⚠ 不必先重開店：每一筆單都**自己帶著開店**（`shop.order_packet()`），
        那是唯一擋得住客戶端 `0x09D4` 的做法（見檔頭 [PKT-092]）。
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
                if item_id not in self.stats.missing:
                    self.stats.missing.append(item_id)
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
                self._buy(amount)
                return "working"
            self._buy(PROBE_AMOUNT)
            return "working"
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
        """再買一筆（探路的第二瓶、或算完之後的大單）。"""
        if self._item is None or amount <= 0:
            return "working"
        self._buy(amount)
        return "working"

    def _buy(self, amount: int) -> None:
        """下一筆單：**接觸 ＋ 選買 ＋ 下單一次送出去**（[PKT-092]）。

        ⚠ 一次開店只能下一筆單（[PKT-079]），所以每一筆本來就要重開；
        而**分開送就會被客戶端的 `0x09D4` 插進來**，訂單被安靜地丟掉。
        併成一包之後那個縫隙就不存在了 —— 實機 0/3 → 3/3。
        """
        if self._item is None or amount <= 0:
            return
        self._pending = amount
        self._got = 0                    # 這一筆的答案卡（0x0B41）重新算
        self._opened = True
        self._send(shop.order_packet(self._gid or 0, [(self._item, amount)]))
        self._enter("等買賣結果")

    def _on_result(self, result: int | None) -> str:
        if self._item is None:
            return "working"
        if result != shop.RESULT_OK:
            return self._fail(f"⚠ 買 {item_name(self._item)} 被拒絕（結果 {result}）")
        return self._bought(self._pending)

    def _bought(self, amount: int) -> str:
        """這一筆成交了：記帳，再決定下一步。

        `amount` 是**真的買到幾個** —— 正常路徑是送出去的數量，
        「沒收到 0x00CA 但東西進背包了」那條路是 `0x0B41` 數出來的。
        """
        if self._item is None:
            return "working"
        self.stats.bought[self._item] = (
            self.stats.bought.get(self._item, 0) + amount
        )
        self._retries = 0
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
