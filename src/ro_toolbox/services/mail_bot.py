"""自動寄信：背景一直看背包，**哪一樣湊到數量就寄哪一樣**。

使用者指定（2026-08-30）：

- 「寄信設定」可以選多個背包物品，每個各自填數量。
- 「**只要那樣物品數量達到我選擇的就會寄信，不需要全部湊齊才寄**」。
- 可以選寄給誰、有一個啟用開關，**這些一切都要記錄**（見 `mail_store`）。
- 「這功能背景一直跑」。

⚠ 自己一條連線、自己一個執行緒，跟自動打怪互不相干（跟 `PotionBot`、
`BuffBot` 同一個形狀）。寄信不會移動角色，所以不必跟走路那幾支搶。

⚠⚠ **格號現查**（[MEM-028]）：設定存的是道具編號，要寄的那一刻才去背包
查它現在在第幾格。存格號的那一刻沒有錯，錯的是三分鐘後 ——
而且它會**安靜地寄錯東西**。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from ro_toolbox.services import bag, mail_store
from ro_toolbox.services.game_link import GameLink
from ro_toolbox.services.gamedata import item_name
from ro_toolbox.services.mail import MailRun

log = logging.getLogger(__name__)

#: 多久看一次背包。寄信不急，看太密只是浪費 —— 背包全掃要 1.5 秒級。
TICK = 3.0
#: 兩封信之間至少隔多久。伺服器對連續寄信有節流，太密會被拒絕。
GAP = 2.0
#: 同一條規則寄失敗之後等多久再試（1→2→4…上限）。
BACKOFF_START = 5.0
BACKOFF_MAX = 300.0


@dataclass
class MailStats:
    """給介面看的。"""

    running: bool = False
    sent: int = 0
    failed: bool = False
    note: str = ""
    #: {道具編號: 現在有幾個}，最後一次看到的。
    counts: dict[int, int] = field(default_factory=dict)


class MailBot:
    """一個遊戲視窗一個。設定變了就 `set_config()`，不必關掉重開。"""

    def __init__(self, pid: int, config=None, on_update=None) -> None:
        self._pid = pid
        self._config = config or mail_store.MailSaved()
        self._on_update = on_update
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._link = GameLink(pid, on_packet=self._on_packet, need_position=False)
        self._run: MailRun | None = None
        self._name = ""
        self._aid = 0
        #: 道具編號 → 下次可以再試的時刻（寄失敗之後退避）。
        self._retry: dict[int, tuple[float, float]] = {}
        self._last_sent = 0.0
        self._stats = MailStats()

    # ---- 對外 -------------------------------------------------------

    @property
    def stats(self) -> MailStats:
        return self._stats

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_config(self, config) -> None:
        """換掉設定。改完馬上生效，退避也歸零（等於「再試一次」）。"""
        self._config = config
        self._retry.clear()

    def start(self) -> bool:
        if self.running:
            return True
        self._stop.clear()
        self._stats = MailStats(running=True, note="連線中…")
        self._emit()
        self._thread = threading.Thread(
            target=self._run_thread, name=f"mail-{self._pid}", daemon=True
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

    def _on_packet(self, packet) -> None:  # noqa: ANN001 - RoPacket
        """把回應餵給正在跑的那一封信。**只收進來的**。"""
        if packet.outbound:
            return
        run = self._run
        if run is not None:
            run.feed(packet.opcode, packet.payload)

    def _run_thread(self) -> None:
        try:
            if self._setup():
                self._loop()
        except Exception as exc:  # noqa: BLE001 - 背景執行緒不能讓例外逸出
            log.exception("自動寄信停了：%s", exc)
            self._fail(f"自動寄信停了：{exc}")
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
        if status is None or not status.name:
            self._fail("讀不到角色（還沒進到遊戲裡？）")
            return False
        # 寄件人名字要跟著送出去那一包（見 `mail.build_send`）。
        self._name = status.name
        self._aid = status.aid
        self._note("看著背包…")
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._link.dead:
                self._fail("⚠ 遊戲連線已中斷，自動寄信已停止")
                return
            self._link.resync()
            self._tick()
            self._stop.wait(TICK)

    def _tick(self) -> None:
        config = self._config
        if not config.usable:
            return
        now = time.monotonic()
        if now - self._last_sent < GAP:
            return
        rows = self._bag()
        if rows is None:
            return
        counts: dict[int, int] = {}
        for _slot, (item_id, amount) in rows.items():
            counts[item_id] = counts.get(item_id, 0) + amount
        self._stats.counts = counts

        rule = mail_store.due(config, counts)
        if rule is None:
            return
        until, _ = self._retry.get(rule.item_id, (0.0, 0.0))
        if now < until:
            return          # 這一樣剛寄失敗，等退避時間到
        self._send_one(rule, rows, now)

    def _send_one(self, rule, rows: dict, now: float) -> None:
        """寄一封。**格號現查**（[MEM-028]）—— 不能用上一拍記下來的。"""
        slot = None
        for index, (item_id, amount) in sorted(rows.items()):
            if item_id == rule.item_id and amount >= rule.amount:
                slot = index
                break
        if slot is None:
            # 同一個道具分散在好幾格（不可堆疊的裝備）—— 這一版不處理，
            # **安靜跳過**比亂寄一格好。
            log.debug("道具 %s 沒有單一格湊得到 %d 個，跳過",
                      item_name(rule.item_id), rule.amount)
            return

        name = item_name(rule.item_id) or f"#{rule.item_id}"
        self._note(f"寄 {rule.amount} 個 {name} 給「{self._config.receiver}」…")
        run = MailRun(self._link.send, self._name, self._config.receiver)
        self._run = run
        try:
            ok = run.run(
                slot, rule.amount,
                should_stop=self._stop.is_set,
                wait=self._stop.wait,
            )
        finally:
            self._run = None
        self._last_sent = time.monotonic()
        if ok:
            self._retry.pop(rule.item_id, None)
            self._stats.sent += 1
            self._note(f"已寄出 {rule.amount} 個 {name} 給「{self._config.receiver}」")
            return
        # ⚠ 失敗**不要停掉整個功能**（補水那條的教訓）—— 退避重試就好。
        _, wait = self._retry.get(rule.item_id, (0.0, 0.0))
        wait = min(BACKOFF_MAX, wait * 2 if wait else BACKOFF_START)
        self._retry[rule.item_id] = (time.monotonic() + wait, wait)
        self._note(f"{run.note or '寄信沒成功'}，{wait:.0f} 秒後再試")

    def _bag(self):
        try:
            return bag.as_dict(self._pid)
        except Exception as exc:  # noqa: BLE001 - 讀不到就這一拍不做事
            log.debug("讀不到背包，這一拍不寄信：%s", exc)
            return None

    # ---- 雜項 -------------------------------------------------------

    def _note(self, text: str) -> None:
        if text and text != self._stats.note:
            log.info("%s", text)
        self._stats.note = text
        self._emit()

    def _fail(self, message: str) -> None:
        self._stats.failed = True
        self._stats.running = False
        self._stats.note = message
        log.warning("自動寄信停用「%s」：%s", self._name or self._pid, message)
        self._emit()

    def _emit(self) -> None:
        if self._on_update is not None:
            self._on_update(self._stats)


__all__ = ["MailBot", "MailStats"]
