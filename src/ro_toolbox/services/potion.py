"""自動補水：獨立執行緒，血量低於設定百分比就一直喝到補滿為止。

為什麼要獨立執行緒：這個服的藥水一次補很少、而怪打很痛，補血必須跟
自動打怪的節奏脫鉤 —— 打怪那邊一拍 0.2 秒還要算路徑，補血等不了。
這裡一拍 0.05 秒，而且**喝完立刻重新判斷**，不夠就再喝，直到過線。

三件事都不是猜的：
- 血量／魔量：記憶體即時讀（[MEM-003]）。
- 喝哪一格：送 `0x00A7` 帶格號與 AID（[PKT-036]、[MEM-017]）。
- **喝哪一格**：設定存的是**道具編號**，格號每次從記憶體現查（`services/bag.py`，
  [MEM-028]）。格號會挪動（丟東西、賣東西都會變），存格號遲早會喝錯。
- **有沒有真的喝到**：以伺服器回應 `0x01C8` 為準（[PKT-036]），
  它同時給「索引 / 道具編號 / 剩餘數量 / 結果」。回包說的道具跟預期
  對不起來就**立刻大聲停用** —— 喝錯東西比不喝糟得多。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ro_toolbox.core.ro_protocol import build_use_item
from ro_toolbox.services import bag, game_socket
from ro_toolbox.services.character import CharacterReader
from ro_toolbox.services.gamedata import item_name
from ro_toolbox.services.packet_capture import PacketCapture
from ro_toolbox.services.ro_capture import find_server

log = logging.getLogger(__name__)

_TICK = 0.05          # 主迴圈間隔
_ACK_TIMEOUT = 0.7    # 送出後等「數量少一個」最久多久
_ACK_POLL = 0.01      # 確認數量的輪詢間隔
#: 找遊戲 socket 最多重試多久。剛登入／剛換地圖的那幾秒複製不到是**正常過渡**，
#: 不是故障 —— `auto_login` 早就是這樣等的（見它的 `_PIN_SOCKET_TIMEOUT`）。
_SOCKET_WAIT_SEC = 10.0
_SOCKET_POLL = 0.3
#: 連喝時，等「那一格數量真的少一個」最多等多久。
#:
#: 這遊戲的藥水**沒有冷卻**（[MEM-021] 實測 12 秒喝掉 58 瓶），而且一瓶可能只補
#: 一點點 —— 低於門檻時要**連續喝到過線**。但「不等確認就狂送」會多灌：
#: 背包數量還沒更新，就一直看到「還有 N 瓶」（測試抓到：背包 2 瓶送了 27 次）。
#:
#: 所以還是等，只是**改等更便宜的訊號**：記憶體裡那一格的數量少一個
#: （讀一格 0.007 ms），而不是等封包的使用回應（那條路要等到 0.7 秒逾時）。
_BURST_ACK_SEC = 0.5
_BURST_POLL = 0.01
#: 一輪連喝最多幾瓶。純粹是保險 —— 正常情況幾瓶就過線了。
_BURST_MAX = 25
_MAX_MISS = 3         # 連續幾次沒喝到就重找格號
#: 重找格號之後先等一下再試。
#:
#: ⚠ 為什麼要等：喝不到的時候**不准停掉補水**（那會害死角色，見
#: `_maybe_give_up`），但也不能變成一條悶著狂送的暗路 —— 實測不節流的話
#: 一秒送四發。等一下下就好：真正的問題（格號挪了）重找就解決了，
#: 解決不了的話送再多也沒用。上限抓 5 秒 —— HP 掉得再快也還救得回來。
_MISS_COOLDOWN = 1.0
_MISS_COOLDOWN_MAX = 5.0
_RESYNC_SEC = 2.0     # 多久檢查一次換地圖／換頻道
#: 藥水剩幾瓶就該回去補（使用者 2026-08-29 指定：5 瓶，**不要等到 0**）。
#:
#: 為什麼不是 0：喝到最後一瓶才想回城，那一路上就已經沒水可喝了 ——
#: 回程道具要現找、路上還會被打。留幾瓶當回家的本錢。
LOW_STOCK = 5
#: 背包串列走不通時，最快多久才重新定位一次。
#: 重新定位要跑一次 AOB 掃描（實測 22 ms），而確認「有沒有喝到」是 10 ms 輪詢 ——
#: 沒有這條限流，綁定壞掉的當下會變成一串重掃把輪詢整個塞住。
_RELOCATE_SEC = 1.0
_BAG_SEC = 1.0        # 多久重讀一次背包（走 BagWatch 快路徑，一次 0.5 ms）
#: 使用道具的伺服器回應。payload = 索引(2)+道具ID(4)+AID(4)+剩餘數量(2)+結果(1)
#: 出處 GAMEDATA [PKT-036]（實機擷取核對過）。這是**權威**確認來源：
#: 記憶體那份背包只是 UI 鏡像，不是每個角色、每個時候都有。
_OP_USE_ACK = 0x01C8
_USE_ACK_MIN = 13
# 門檻上限 100。判斷式是 `hp_percent < 門檻`，所以設 100 在滿血時是 False，
# **不會**一直喝。之前誤以為 100 會失控，那次實測（12 秒灌掉 58 瓶）我設的是 101 ——
# 超過 100 才會變成「永遠低於門檻」，所以只要夾在 100 以內就是安全的。
_MAX_PERCENT = 100


@dataclass
class PotionConfig:
    """百分比設 0 代表關閉那一項。"""

    hp_item: int | None = None      # 道具編號，不是格號
    hp_percent: int = 0
    sp_item: int | None = None
    sp_percent: int = 0
    #: 水快沒了就回程：HP 或 SP 的藥水**任何一種**剩不到 `LOW_STOCK` 瓶，
    #: 就用 `home_item` 回程。
    #: 道具由使用者自己從整個背包挑 —— 道具表裡認不出「哪個是回程道具」
    #: （蝴蝶翅膀寫「移動至儲存的位置」、蒼蠅翅膀寫「移動至任意的位置」，
    #: 差別只在描述文字），靠關鍵字猜就是 CLAUDE.md 禁止的「很有自信的錯」。
    home_item: int | None = None

    def __post_init__(self) -> None:
        self.hp_percent = min(max(int(self.hp_percent), 0), _MAX_PERCENT)
        self.sp_percent = min(max(int(self.sp_percent), 0), _MAX_PERCENT)

    def wants_hp(self) -> bool:
        return self.hp_item is not None and self.hp_percent > 0

    def wants_sp(self) -> bool:
        return self.sp_item is not None and self.sp_percent > 0

    def wants_home(self) -> bool:
        return self.home_item is not None


@dataclass
class PotionStats:
    running: bool = False
    hp_used: int = 0
    sp_used: int = 0
    hp_left: int | None = None
    sp_left: int | None = None
    hp_percent: float = 0.0
    sp_percent: float = 0.0
    note: str = ""
    failed: bool = False
    #: 已經用回程道具回去了。UI 看到這個要把自動打怪也關掉 ——
    #: 人已經在城裡，繼續掛著打怪只會站在原地耗。
    went_home: bool = False
    #: 回程的原因是**沒水了**（不是外面叫它回去的）。
    #:
    #: ⚠ 兩種回程要分開：藥水用完是「回去補給，補完再回來繼續掛」；
    #: 負重滿了是使用者指定的「回程關掉自動打怪，**掛機不補給**」。
    #: 混在一起的話，負重滿的那次會自己跑去買一堆東西然後更重。
    needs_supplies: bool = False
    counts: dict[int, int] = field(default_factory=dict)


class PotionBot:
    def __init__(
        self,
        pid: int,
        config: PotionConfig,
        on_update: Callable[[PotionStats], None] | None = None,
    ) -> None:
        self._pid = pid
        self._cfg = config
        self._on_update = on_update
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = PotionStats()
        self._reader: CharacterReader | None = None
        self._bag: dict[int, tuple[int, int]] = {}   # 格號 → (道具編號, 數量)
        self._bag_at = 0.0
        self._watch: bag.BagWatch | None = None      # 綁定過的背包串列（快路徑）
        self._locate_at = 0.0                       # 上次重新定位背包的時間
        self._burst_t0 = 0.0                        # 這一輪連喝的實測用計時
        self._burst_n = 0
        self._sock: int | None = None
        self._server: tuple[str, int] | None = None
        self._aid = 0
        self._resync_at = 0.0
        self._miss = 0
        self._capture: PacketCapture | None = None
        self._character = ""
        #: 外面要求回程的理由（空字串 = 沒有）。見 `request_home()`。
        self._home_why = ""
        #: 重找格號之前要等多久（見 `_MISS_COOLDOWN`）。
        self._miss_wait = _MISS_COOLDOWN
        self._ack_lock = threading.Lock()
        # {格號: (序號, 道具編號, 剩餘數量, 結果)}
        # ⚠ 用遞增序號而不是時間戳：Windows 的 time.monotonic() 解析度約 15 ms，
        # 送出與回包會拿到**相同**的時間，回包就會被誤判成「送出前的舊資料」而丟掉。
        self._acks: dict[int, tuple[int, int, int, int]] = {}
        self._ack_seq = 0


    # ---- 對外 -------------------------------------------------------

    @property
    def stats(self) -> PotionStats:
        return self._stats

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def configure(self, config: PotionConfig) -> None:
        """執行中也可以改設定（改百分比不必重開）。"""
        self._cfg = config

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._stats = PotionStats(running=True, note="啟動中…")
        self._miss = 0
        self._thread = threading.Thread(target=self._run, name="potion", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """停掉並**確認執行緒真的結束**。

        join 逾時就默默放生執行緒是很糟的失效方式：那條執行緒還握著遊戲 socket，
        還會繼續送封包，而呼叫端以為已經停了。所以逾時要大聲講出來。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.error("⚠ 自動補水執行緒 %.0f 秒內沒有結束，它可能還在送封包", timeout)
        self._thread = None
        self._stats.running = False

    # ---- 主流程 -----------------------------------------------------

    def _run(self) -> None:
        try:
            if not self._setup():
                return
            self._loop()
        except Exception as exc:  # noqa: BLE001 - 背景執行緒不能讓例外炸掉程式
            log.exception("自動補水執行緒發生例外")
            self._fail(f"發生錯誤已停止：{exc}")
        finally:
            self._cleanup()

    def _setup(self) -> bool:
        # ⚠ **先定位角色，再找 socket。** 順序刻意反過來：角色一定位好就有名字，
        # 之後每一條失敗訊息才講得出是哪一隻 —— 多開的時候，
        # 一行「找不到遊戲 socket」根本分不出是誰失敗（使用者實際回報）。
        reader = CharacterReader()
        if not reader.attach(self._pid, should_stop=self._stop.is_set):
            self._fail("角色定位失敗")
            return False
        self._reader = reader
        status = reader.read()
        if status is None or not status.aid:
            self._fail("讀不到角色 AID，無法送使用道具封包")
            return False
        self._aid = status.aid
        self._character = status.name or ""

        sock, server = self._wait_for_socket()
        if sock is None:
            self._fail(
                f"{_SOCKET_WAIT_SEC:.0f} 秒內找不到遊戲 socket，無法送封包"
                if server is not None else "找不到伺服器連線（還沒登入？）"
            )
            return False
        self._sock, self._server = sock, server

        self._refresh_bag(force=True)
        if not self._bag:
            self._fail("讀不到背包（AOB 定位失敗），自動補水停用")
            return False

        capture = PacketCapture(self._pid, self._on_packet)
        if not capture.start():
            self._fail("開不了封包擷取（需要系統管理員），無法確認有沒有喝到")
            return False
        self._capture = capture
        self._note("待命中")
        return True

    # ---- 封包 -------------------------------------------------------

    def _on_packet(self, packet) -> None:  # noqa: ANN001 - RoPacket，避免循環匯入
        """擷取執行緒：只收**使用道具的回包**（`0x01C8`）。

        這不是「學背包」——它是伺服器對**我們剛送出的那個動作**的回覆，
        告訴我們有沒有成功、剩幾個、以及**剛才喝掉的是哪個道具**。
        最後那項是安全檢查：跟使用者選的對不起來就立刻停用。
        道具身分一律查 `assets/items.json.gz`（客戶端自己的 iteminfo，[DAT-020]）。
        """
        if packet.outbound:
            return
        payload = packet.payload
        if packet.opcode == _OP_USE_ACK and len(payload) >= _USE_ACK_MIN:
            aid = int.from_bytes(payload[6:10], "little")
            if aid != self._aid:
                return
            index = int.from_bytes(payload[0:2], "little")
            item_id = int.from_bytes(payload[2:6], "little")
            left = int.from_bytes(payload[10:12], "little")
            result = payload[12]
            with self._ack_lock:
                self._ack_seq += 1
                self._acks[index] = (self._ack_seq, item_id, left, result)
            return

    def _ack_mark(self) -> int:
        """送出前先記下目前的序號，之後只採信序號比它大的回包。"""
        with self._ack_lock:
            return self._ack_seq

    def _take_ack(self, index: int, after: int) -> tuple[int, int, int] | None:
        """拿 `after` 之後收到的那一格回包。回 (道具編號, 剩餘, 結果)。"""
        with self._ack_lock:
            got = self._acks.get(index)
            if got is None or got[0] <= after:
                return None
            del self._acks[index]
            return got[1], got[2], got[3]

    def _refresh_bag(self, force: bool = False) -> None:
        """重讀背包。走的是 `BagWatch` 的快路徑（一次不到 1 ms）。"""
        now = time.monotonic()
        if not force and now - self._bag_at < _BAG_SEC:
            return
        self._bag_at = now
        fresh = self._read_bag()
        if fresh:
            self._bag = fresh

    def _read_bag(self) -> dict[int, tuple[int, int]]:
        """讀背包，優先走已綁定的串列。

        為什麼不直接用 `bag.as_dict()`：它每次都重跑 AOB 掃描加 2048 個偏移的
        試走（實測 22 ms）。喝水每一瓶都要現查格號（[MEM-028]），用它等於
        每瓶白花這 22 ms。綁定過的串列只重走幾十個節點（實測 0.48 ms）。

        ⚠ 綁定會過期（換地圖、背包重新配置），所以走不通就**重新定位一次**，
        不是拿舊資料硬撐。兩條都失敗才回空的 —— 呼叫端會大聲停用。
        """
        if self._watch is not None:
            rows = self._watch.snapshot()
            if rows:
                return rows
            log.info("背包串列走不通了，重新定位")
            self._watch.close()
            self._watch = None
        now = time.monotonic()
        if now - self._locate_at < _RELOCATE_SEC:
            return {}
        self._locate_at = now
        watch = bag.BagWatch(self._pid)
        if not watch.open():
            return {}
        self._watch = watch
        return watch.snapshot()

    def _slot_of(self, item_id: int | None) -> int | None:
        """那個道具現在在第幾格。**每次都現查** —— 格號會挪動（[MEM-028]）。"""
        if item_id is None:
            return None
        for slot, (iid, _amount) in self._bag.items():
            if iid == item_id:
                return slot
        return None

    def _left(self, item_id: int | None) -> int | None:
        slot = self._slot_of(item_id)
        return self._bag[slot][1] if slot is not None else None

    def request_home(self, why: str) -> None:
        """外面叫它回程（例如自動打怪回報負重滿了）。**下一拍才動手**。

        ⚠ 不在呼叫端的執行緒直接回程：回程要送封包＋確認那一格真的少一個，
        那是這個 bot 自己的迴圈在做的事，兩邊同時碰背包會互相打架。
        """
        self._home_why = why

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            if not self._keep_in_sync(now):
                return
            if self._home_why:
                why, self._home_why = self._home_why, ""
                if self._cfg.wants_home():
                    # 外面叫的（負重滿了）—— 使用者指定「掛機不補給」。
                    self._go_home(why, supplies=False)
                else:
                    self._fail(f"{why}，但沒有設定回程道具")
                return
            status = self._reader.read() if self._reader else None
            if status is None:
                self._note("讀不到角色狀態，等待中…")
                self._stop.wait(0.3)
                continue

            self._refresh_bag()
            self._stats.hp_percent = status.hp_percent
            self._stats.sp_percent = status.sp_percent
            self._stats.counts = {s: a for s, (_i, a) in self._bag.items()}
            self._stats.hp_left = self._left(self._cfg.hp_item)
            self._stats.sp_left = self._left(self._cfg.sp_item)

            if self._cfg.wants_hp() and status.hp_percent < self._cfg.hp_percent:
                if not self._drink(self._cfg.hp_item, "HP", status.hp_percent):
                    return
                # 第一瓶已經確認喝到「對的道具」了，後面就用連喝快路徑衝到過線
                if not self._burst(self._cfg.hp_item, "HP"):
                    return
                continue  # 立刻重新判斷，不等下一拍
            if self._cfg.wants_sp() and status.sp_percent < self._cfg.sp_percent:
                if not self._drink(self._cfg.sp_item, "SP", status.sp_percent):
                    return
                if not self._burst(self._cfg.sp_item, "SP"):
                    return
                continue

            self._push()
            self._stop.wait(_TICK)

    # ---- 喝 ---------------------------------------------------------

    def _drink(self, item_id: int, kind: str, percent: float) -> bool:
        """喝一個並確認真的喝到。回 False 代表已經停用，呼叫端要結束迴圈。

        **格號每次現查**：設定存的是道具編號，而格號會挪動（賣東西、丟東西、
        用完一整疊都會讓後面的往前遞補，見 [MEM-028]）。存格號遲早會喝錯東西。
        """
        self._refresh_bag(force=True)
        slot = self._slot_of(item_id)
        if slot is None:
            return self._exhausted(kind, item_id)
        before = self._bag[slot][1]
        if before <= 0:
            return self._exhausted(kind, item_id)

        mark = self._ack_mark()
        self._send(build_use_item(slot, self._aid))
        deadline = time.monotonic() + _ACK_TIMEOUT
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            ack = self._take_ack(slot, mark)
            if ack is not None:
                got_id, left, result = ack
                if result != 1:
                    self._miss += 1
                    self._note(f"⚠ 伺服器拒絕使用第 {slot} 格（結果碼 {result}）")
                    return self._maybe_give_up(slot)
                if got_id != item_id:
                    # 現查過還對不上 = 送出到伺服器處理之間背包又變了。
                    self._fail(
                        f"⚠ 喝到的是 {item_name(got_id)}，不是你選的 "
                        f"{item_name(item_id)}，自動補水停用"
                    )
                    return False
                self._used(kind, slot, percent, left)
                # 下一拍 _drink 會強制重讀，這裡只是讓數量顯示立刻跟上
                if left > 0:
                    self._bag[slot] = (item_id, left)
                    low = self._low_stock(item_id, left)
                    return True if low is None else low
                self._bag.pop(slot, None)
                return self._exhausted(kind, item_id)
            self._stop.wait(_ACK_POLL)

        self._miss += 1
        self._note(f"⚠ 送了使用道具但伺服器沒回應（第 {self._miss} 次）")
        return self._maybe_give_up(slot)

    def _burst(self, item_id: int | None, kind: str) -> bool:
        """連續喝到過線。**不走封包確認那條路，改看記憶體數量。**

        為什麼可以：這一輪的**第一瓶已經被 `_drink()` 同步驗證過**
        （喝到的是不是你選的道具、伺服器有沒有拒絕）。同一輪裡道具沒變，
        剩下的只要確認「真的喝掉了」就好 —— 而那件事記憶體看得到，
        不必等封包（[MEM-021] 的手法：讀那一格的數量有沒有 -1）。

        為什麼要這樣：藥水沒有冷卻，一瓶可能只補一點點。一瓶等一次封包來回
        是每秒約 5 次（使用者回報「反應有點慢」）。

        ⚠ **每一瓶都要等數量真的少一個才送下一瓶。** 不等的話會多灌 ——
        背包數量還沒更新就一直看到「還有 N 瓶」（測試抓到：2 瓶送了 27 次）。
        血量也每一瓶之前重讀，過線立刻停。
        """
        if item_id is None:
            return True
        if self._miss:
            # ⚠ 第一瓶其實**沒喝到**（伺服器拒絕或沒回應，`_drink()` 還在容忍
            # 次數內所以回 True）。這種時候繼續連喝，等於繞過失敗計數狂送。
            return True
        want = self._cfg.hp_percent if kind == "HP" else self._cfg.sp_percent
        try:
            return self._burst_loop(item_id, kind, want)
        finally:
            self._report_rate()

    def _report_rate(self) -> None:
        """把實測的「每瓶幾毫秒」記進日誌 —— 快不快要看數字，不是看感覺。"""
        if self._burst_n < 2:
            return
        span = time.monotonic() - self._burst_t0
        log.info("連喝 %d 瓶，共 %.2f 秒（每瓶 %.0f ms）",
                 self._burst_n, span, span / self._burst_n * 1000)

    def _burst_loop(self, item_id: int, kind: str, want: float) -> bool:
        self._burst_t0 = time.monotonic()
        self._burst_n = 0
        for _ in range(_BURST_MAX):
            if self._stop.is_set():
                return True
            status = self._reader.read() if self._reader else None
            if status is None:
                return True
            percent = status.hp_percent if kind == "HP" else status.sp_percent
            if percent >= want:
                return True                      # 過線了，立刻停

            self._refresh_bag(force=True)        # 格號會挪動，每次現查
            slot = self._slot_of(item_id)
            if slot is None:
                return self._exhausted(kind, item_id)
            before = self._bag[slot][1]
            if before <= 0:
                return self._exhausted(kind, item_id)

            self._send(build_use_item(slot, self._aid))
            if not self._wait_used(slot, before):
                # 送了卻沒少 —— 跟「送了伺服器沒回應」是同一件事，要計入
                # 失敗次數。不計的話連喝會變成一條悶著狂送的暗路。
                self._miss += 1
                self._note(f"⚠ 連喝時第 {slot} 格的數量沒有減少（第 {self._miss} 次）")
                return self._maybe_give_up(slot)
            # ⚠ 剩幾個要**用道具編號重算**，不能看原本那一格 ——
            # 背包會重排，那格可能只是換了位置（[MEM-028]），不是用完了。
            self._refresh_bag(force=True)
            left = self._left(item_id) or 0
            self._burst_n += 1
            self._used(kind, slot, percent, left)
            if left <= 0:
                return self._exhausted(kind, item_id)
            low = self._low_stock(item_id, left)
            if low is not None:
                return low
        return True

    def _wait_used(self, slot: int, before: int) -> bool:
        """等那一格的數量真的少一個。等到回 True，逾時回 False。

        讀記憶體一格只要 0.007 ms，所以可以用很密的輪詢 —— 這正是它比
        「等封包回應」快的原因。喝完最後一瓶那一格會整個消失，那也算成功。
        """
        deadline = time.monotonic() + _BURST_ACK_SEC
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            self._refresh_bag(force=True)
            now = self._bag.get(slot)
            if now is None or now[1] < before:
                return True
            self._stop.wait(_BURST_POLL)
        return False

    def _maybe_give_up(self, index: int) -> bool:
        """喝不到的時候該怎麼辦。**幾乎永遠是「重找一次格號，繼續」。**

        ## ⚠⚠ 停掉補水 = 讓角色死掉

        實機 2026-08-30（白狐掛機中）：

            01:06:18  ⚠ 送了使用道具但伺服器沒回應（第 1 次）
            01:06:37  ⚠ 送了使用道具但伺服器沒回應（第 2 次）
            01:06:53  ⚠ 連續 3 次喝不到（第 13 格），自動補水停用
            …十分鐘後…
            01:17:42  HP 15% → 喝了第 13 格   ← 使用者自己發現、手動開回來

        使用者原話：「我的白狐自動喝水怎麼被關閉導致我死了」。
        **背包還有 150 瓶藥水**，格號那一格只是暫時對不上。

        CLAUDE.md 的「失效模式只允許大聲停用或安全退化」在這裡有個例外：
        補水停掉不是「安全」退化，是**致命**退化。人不在電腦前的時候，
        停掉補水跟關掉遊戲沒兩樣。

        ## 正確的做法：把格號重找一次

        這個 repo 早就有這條規矩（[MEM-028]：**存編號，格號現查**）——
        `_slot_of()` 就是幹這個的。喝不到最常見的原因就是格號挪了
        （丟東西、賣東西、用完一整疊，後面的都會往前遞補）。

        所以：重讀背包、用**道具編號**重找格號。
        - 找得到 → 計數歸零，下一拍照常喝。
        - 找不到 → 那是「道具真的沒了」，走既有的 `_exhausted()`
          （有勾回程就回城補給，沒勾就關掉那一項）—— 那條路是設計過的。
        """
        if self._miss < _MAX_MISS:
            return True
        # 先冷卻一下再重找 —— 不然會變成一條每秒好幾發的暗路。
        self._stop.wait(min(self._miss_wait, _MISS_COOLDOWN_MAX))
        self._miss_wait = min(self._miss_wait * 2, _MISS_COOLDOWN_MAX)
        if self._stop.is_set():
            return False
        # 格號現查（[MEM-028]）。找得到就繼續 —— 停掉補水會害死角色。
        self._refresh_bag(force=True)
        for item_id in (self._cfg.hp_item, self._cfg.sp_item):
            if not item_id:
                continue
            slot = self._slot_of(item_id)
            if slot is not None:
                self._miss = 0
                self._note(
                    f"⚠ 連續 {_MAX_MISS} 次喝不到（第 {index} 格）—— "
                    f"重找到 {item_name(item_id)} 在第 {slot} 格，繼續補水"
                )
                return True
        # 兩種道具都不在背包裡了 —— 那是「用完了」，走既有那條路
        # （勾了回程就回城補給）。**不是**「停用自動補水」。
        for kind, item_id in (("HP", self._cfg.hp_item), ("SP", self._cfg.sp_item)):
            if item_id:
                return self._exhausted(kind, item_id)
        self._fail("⚠ 沒有設定任何補充道具，自動補水停止")
        return False

    def _used(self, kind: str, index: int, percent: float, left: int) -> None:
        self._miss = 0
        self._miss_wait = _MISS_COOLDOWN
        if kind == "HP":
            self._stats.hp_used += 1
            self._stats.hp_left = left
        else:
            self._stats.sp_used += 1
            self._stats.sp_left = left
        self._note(f"{kind} {percent:.0f}% → 喝了第 {index} 格，剩 {left} 個")

    def _low_stock(self, item_id: int, left: int) -> bool | None:
        """水**快**沒了就先回去補（使用者 2026-08-29 指定：剩 5 瓶就回程）。

        為什麼不等到 0：喝到最後一瓶才想回城，那一路上就已經沒水可喝了 ——
        回程道具要現找、路上還會被打。留 5 瓶當回家的本錢。

        回 None = 還夠，照常跑。回 False = 已經回程（或回程失敗），迴圈要結束。
        沒勾「回程」的話這裡什麼都不做 —— 那時候的行為照舊（喝到 0 才關掉那一項）。
        """
        if left > LOW_STOCK or not self._cfg.wants_home():
            return None
        return self._go_home(f"{item_name(item_id)} 只剩 {left} 個")

    def _exhausted(self, kind: str, item_id: int) -> bool:
        """那個道具用完了（背包裡找不到了）。

        ⚠ 正常情況下**走不到這裡** —— 勾了回程的話剩 `LOW_STOCK` 瓶就先回去了。
        會走到這裡的是「沒勾回程」或「一次連喝跨過門檻」。

        勾了回程就先回程 —— **HP 或 SP 任一種用完就算**，不必等到兩種都用完。
        沒水了還留在原地，下一波怪就是送死。
        沒勾的話照舊：關掉這一項，另一項還有設定就繼續跑。
        """
        text = f"{item_name(item_id)} 用完了"
        if self._cfg.wants_home():
            return self._go_home(text)
        if kind == "HP":
            self._cfg.hp_item = None
        else:
            self._cfg.sp_item = None
        still = self._cfg.wants_hp() or self._cfg.wants_sp()
        text += f"，已關閉{kind}補充"
        if still:
            self._note(text)
            return True
        self._fail(f"{text}；沒有其他設定，自動補水停止")
        return False

    def _go_home(self, why: str, supplies: bool = True) -> bool:
        """用選好的道具回程，然後停掉自動補水。一律回 False（迴圈要結束）。

        `supplies=True`（預設）代表**是沒水才回去的** —— 呼叫端看到這個
        旗標就會接著跑補給、補完再走回練功點。外面叫它回去的（負重滿了）
        要傳 `supplies=False`：使用者指定那種情況「掛機不補給」。

        ⚠ **格號現查**（[MEM-028]），而且要**確認真的用掉了**才算回程成功 ——
        「送了封包就當作回去了」是安靜地做錯事：人還在野外，UI 卻顯示已回程。
        確認手法跟喝水同一套：那一格的數量有沒有少一個。
        """
        item_id = self._cfg.home_item
        self._refresh_bag(force=True)
        slot = self._slot_of(item_id)
        if slot is None:
            self._fail(f"{why}，但回程道具 {item_name(item_id)} 也沒有了，已停止")
            return False
        before = self._bag[slot][1]
        self._send(build_use_item(slot, self._aid))
        if not self._wait_used(slot, before):
            self._fail(
                f"{why}，送了回程道具 {item_name(item_id)} 但沒有用掉（第 {slot} 格），"
                "已停止 —— 請自己確認人在哪裡"
            )
            return False
        self._stats.went_home = True
        self._stats.needs_supplies = supplies
        tail = "，接著去補給" if supplies else "，自動補水停止"
        self._fail(f"{why} → 已用 {item_name(item_id)} 回程{tail}")
        return False

    # ---- 雜項 -------------------------------------------------------

    def _keep_in_sync(self, now: float) -> bool:
        """換地圖／換頻道後 socket 與背包位址都會失效，要重綁（比照 [PKT-038]）。"""
        if now - self._resync_at < _RESYNC_SEC:
            return True
        self._resync_at = now
        server = find_server(self._pid)
        if server is None:
            self._fail("⚠ 遊戲連線已中斷，自動補水已停止")
            return False
        if server == self._server:
            return True
        log.info("連線變了（%s → %s），重新綁定", self._server, server)
        if self._sock is not None:
            game_socket.close_socket(self._sock)
            self._sock = None
        sock = game_socket.open_game_socket(
            self._pid, server[0], server[1],
            timeout=game_socket.SOCKET_REBIND_SEC, should_stop=self._stop.is_set,
        )
        if not sock:
            self._fail("⚠ 換頻道後找不到新的遊戲 socket，自動補水已停止")
            return False
        self._sock, self._server = sock, server
        self._refresh_bag(force=True)   # 換頻道後背包容器可能換位置，重讀
        return True

    def _release_capture(self) -> None:
        if self._capture is not None:
            capture, self._capture = self._capture, None
            capture.stop()

    def _release_socket(self) -> None:
        if self._sock is not None:
            sock, self._sock = self._sock, None
            game_socket.close_socket(sock)

    def _release_reader(self) -> None:
        if self._reader is not None:
            reader, self._reader = self._reader, None
            reader.close()

    def _send(self, data: bytes) -> None:
        if self._sock is None:
            return
        if game_socket.send_on_socket(self._sock, data) < 0:
            log.warning("送封包失敗，socket 可能已失效，強制重新綁定")
            self._server = None
            self._resync_at = 0.0

    def _note(self, text: str) -> None:
        # 提示字一律進**執行日誌**，不放介面（使用者指定）。
        # 只在內容變動時記一筆 —— 這支每拍都會被呼叫，照記會把日誌洗成幾百行一樣的字。
        if text and text != self._stats.note:
            log.info("%s", text)
        self._stats.note = text
        self._push()

    def _wait_for_socket(self) -> tuple[int | None, tuple[str, int] | None]:
        """找遊戲的 socket，找不到就重試到逾時。回 (socket, 伺服器端點)。

        ⚠ **不能只試一次。** 剛登入、剛換地圖的那幾秒複製不到是正常的過渡
        （`auto_login` 也是這樣等的）。試一次就放棄等於「勾下去的時機不對就整個停用」，
        而使用者只看得到一行「找不到遊戲 socket」，看起來像壞掉。
        """
        deadline = time.monotonic() + _SOCKET_WAIT_SEC
        server = None
        while not self._stop.is_set():
            server = find_server(self._pid) or server
            if server is not None:
                sock = game_socket.find_game_socket(self._pid, server[0], server[1])
                if sock:
                    return sock, server
            if time.monotonic() >= deadline:
                break
            self._stop.wait(_SOCKET_POLL)
        return None, server

    def _fail(self, text: str) -> None:
        self._stats.failed = True
        self._stats.running = False
        self._stats.note = text
        # 訊息要講得出是**哪一隻角色**：多開的時候一行沒有身分的警告等於沒說。
        who = f"「{self._character}」" if self._character else ""
        log.warning("自動補水停用%s：%s", who, text)
        self._push()

    def _push(self) -> None:
        if self._on_update is not None:
            self._on_update(self._stats)

    def _release_bag(self) -> None:
        if self._watch is not None:
            self._watch.close()
            self._watch = None

    def _cleanup(self) -> None:
        # 收尾不能因為某一項出錯就漏掉後面的 —— 每一項都要放掉。
        for release in (self._release_capture, self._release_socket,
                        self._release_reader, self._release_bag):
            try:
                release()
            except Exception as exc:  # noqa: BLE001 - 收尾失敗不該蓋掉真正的錯誤
                log.debug("收尾時發生例外：%s", exc)
        self._stats.running = False
        self._push()
