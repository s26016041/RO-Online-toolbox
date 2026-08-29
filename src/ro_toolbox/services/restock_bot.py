"""按一下「補水」：自己走去最近的藥水商人，補完再說一聲。

把三段本來各自能跑的東西接起來（做法照 `tools/restock_test.py`，那支已經實機
跑通過整條流程）：

1. **找店**：先看腳下這張圖有沒有藥水商人；沒有就用 `nearest_map_with()`
   找最近的一張。都沒有就大聲說，不要亂走。
2. **走過去**：`TravelBot`，目的地是**商人腳邊那一格**（`nearest_walkable`）。
   ⚠ 走路那一段還負責**把沿路看到的 NPC 記下來**：實體只在「進入視野」時
   送一次封包（[PKT-061]），到了才開擷取是接不到的 —— 認不出商人就開不了店。
3. **買**：`Restocker`（認人 → 開店 → 量單位重 → 買到負重 65%）。
   回程道具另外補到 20 個（`RestockOrder.home_needed()`）。
   ⚠ 買完**一定要關掉商店視窗**（`0x09D4`）—— 對話開著時角色**不能移動**，
   不關就走不回去（使用者實測回報）。
4. **走回去**：回到出發時的那張圖。補完站在商人旁邊等於掛機停擺。

⚠ 這是**手動觸發**的一次性動作，不是常駐 bot：走完、買完、講一句話就收工。
中途每一段都有放棄上限（不是成功依據），逾時就停下來說清楚卡在哪。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ro_toolbox.core.ro_protocol import unpack_position
from ro_toolbox.services import bag
from ro_toolbox.services.game_link import GameLink
from ro_toolbox.services.gamedata import (
    item_name,
    map_display_name,
    maps_with_potion_sellers,
    potion_sellers_on,
)
from ro_toolbox.services.mapdata import GatError, load_terrain
from ro_toolbox.services.restock import HOME_TARGET, Restocker, RestockOrder
from ro_toolbox.services.travel import nearest_map_with, nearest_walkable
from ro_toolbox.services.travel_bot import TravelBot

log = logging.getLogger(__name__)

#: 走去商人最多花多久。**放棄的上限，不是成功的依據**（CLAUDE.md）。
WALK_GIVEUP = 240.0
#: 站到商人旁邊之後，整段買賣最多花多久。
SHOP_GIVEUP = 90.0
#: 主迴圈一拍。
TICK = 0.2


@dataclass
class RestockRun:
    """給介面看的一次補水過程。"""

    running: bool = False
    #: 走完＋買完了嗎（`done` 才算成功，`note` 一律有話說）。
    done: bool = False
    #: 錢不夠。⚠ 介面要跳通知（使用者指定）。
    broke: bool = False
    bought: dict[int, int] = field(default_factory=dict)
    note: str = ""
    #: 去哪家店（地圖代碼／中文名）。
    shop_map: str = ""
    shop_name: str = ""
    #: 出發前在哪張圖（補完要走回去）。
    home_map: str = ""
    #: 走回去了沒。
    came_back: bool = False

    def summary(self) -> str:
        """一句話講完這次補了什麼。給通知用。"""
        if not self.bought:
            return self.note or "沒買到東西"
        parts = [f"{item_name(i)} {n} 個" for i, n in sorted(self.bought.items())]
        where = map_display_name(self.shop_map) or self.shop_map
        return f"在{where}補了 " + "、".join(parts)


class RestockBot:
    """一次性的「去補水」。`start()` 之後每一段都自己走完，結束就 `running=False`。"""

    def __init__(
        self,
        pid: int,
        hp_item: int | None,
        home_item: int | None = None,
        on_update: Callable[[RestockRun], None] | None = None,
        back_to: str = "",
    ) -> None:
        self._pid = pid
        self._hp_item = hp_item
        self._home_item = home_item
        #: 補完要走回哪張圖。空字串 = 走回出發時站的那張。
        #:
        #: ⚠ 「沒水了自動回城補給」那條路一定要指定：那時候角色**已經被
        #: 回程道具傳到城裡了**，出發點就是城裡，走回出發點等於原地不動 ——
        #: 人還是沒回到練功點。
        self._back_to = back_to
        self._on_update = on_update
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._travel: TravelBot | None = None
        self.stats = RestockRun()

    # ---- 對外 -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return True
        if not self._hp_item and not self._home_item:
            self._say("沒有選要補什麼")
            return False
        self._stop.clear()
        self.stats = RestockRun(running=True, note="找最近的藥水商人…")
        self._emit()
        self._thread = threading.Thread(
            target=self._run, name=f"restock-{self._pid}", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        travel = self._travel
        if travel is not None:
            travel.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        self.stats.running = False
        self._emit()

    # ---- 流程 -------------------------------------------------------

    def _run(self) -> None:
        try:
            plan = self._find_shop()
            if plan is None:
                return
            target_map, cell, seller_cell, look, name = plan
            self.stats.shop_map = target_map
            self.stats.shop_name = name
            where = map_display_name(target_map) or target_map
            self._say(f"往 {where} 的{name}…")
            known = self._walk(target_map, cell)
            if known is None:
                return
            self._buy(look, seller_cell, known)
            self._go_back()
        except Exception as exc:  # noqa: BLE001 - 背景執行緒不能讓例外逸出
            log.exception("補水停了：%s", exc)
            self._say(f"補水停了：{exc}")
        finally:
            self.stats.running = False
            self._emit()

    def _find_shop(self):
        """回 (地圖, 走到哪一格, 商人格, 外觀, 名字)。找不到就回 None 並說明。"""
        from ro_toolbox.services.character import CharacterReader

        reader = CharacterReader()
        try:
            if not reader.attach(self._pid):
                self._say("讀不到角色（還沒進到遊戲裡？）")
                return None
            status = reader.read()
        finally:
            reader.close()
        if status is None or not status.map_name:
            self._say("讀不到角色現在在哪張圖")
            return None

        here = status.map_name
        self.stats.home_map = self._back_to or here
        sellers = potion_sellers_on(here)
        target_map = here
        if not sellers:
            found = nearest_map_with(here, set(maps_with_potion_sellers()))
            if found is None:
                self._say("⚠ 附近找不到藥水商人 —— 沒有路線可以走過去")
                return None
            _route, target_map = found
            sellers = potion_sellers_on(target_map)
        if not sellers:
            self._say("⚠ 附近找不到藥水商人")
            return None

        x, y, name, look = sellers[0]
        try:
            terrain = load_terrain(target_map)
        except GatError as exc:
            self._say(f"⚠ {target_map} 的地形讀不到：{exc}")
            return None
        cell = nearest_walkable(terrain, (x, y)) or (x, y)
        return target_map, cell, (x, y), look, name

    def _walk(self, target_map: str, cell: tuple[int, int] | None) -> dict | None:
        """走過去。回沿路看到的 NPC（認商人要用），走不到就回 None。"""
        bot = TravelBot(self._pid, destination=target_map, destination_cell=cell,
                        on_update=lambda s: self._say(s.note))
        self._travel = bot
        bot.start()
        deadline = time.monotonic() + WALK_GIVEUP
        while bot.running and time.monotonic() < deadline and not self._stop.is_set():
            self._stop.wait(TICK)
        arrived = bool(getattr(bot.stats, "arrived", False))
        known = bot.npc_seen
        bot.stop()
        self._travel = None
        if self._stop.is_set():
            self._say("已取消")
            return None
        if not arrived:
            self._say(f"⚠ 沒走到商人那裡：{bot.stats.note}")
            return None
        return known

    def _buy(self, look: int, cell: tuple[int, int], known: dict) -> None:
        order = RestockOrder(
            hp_item=self._hp_item,
            home_item=self._home_item,
            home_have=self._home_count(),
        )
        if not order.wanted():
            self._say(f"藥水與回程道具都夠了（回程道具已有 {order.home_have} 個），不用補")
            self.stats.done = True
            return

        link = GameLink(self._pid, on_packet=lambda pkt: self._feed(shopper, pkt))
        shopper = Restocker(link.send, time.monotonic, order)
        problem = link.open()
        if problem:
            self._say(f"接不上遊戲：{problem}")
            return
        try:
            shopper.start(look, cell)
            for gid, info in known.items():
                shopper.note_entity(gid, info[0], info[1], info[2])
            deadline = time.monotonic() + SHOP_GIVEUP
            last = ""
            while shopper.active and time.monotonic() < deadline and not self._stop.is_set():
                state = shopper.update()
                if shopper.stats.note != last:
                    last = shopper.stats.note
                    self._say(last)
                if state in ("done", "blocked"):
                    break
                self._stop.wait(TICK)
        finally:
            link.close()
        self.stats.bought = dict(shopper.stats.bought)
        self.stats.broke = shopper.stats.broke
        self.stats.done = bool(shopper.stats.bought) or "不用補" in shopper.stats.note
        self._say(self.stats.summary() if self.stats.bought else shopper.stats.note)

    def _go_back(self) -> None:
        """走回出發時那張圖。

        補完站在商人旁邊等於掛機停擺 —— 使用者要的是「補完可以回原本練功點」。
        ⚠ 這一段**失敗不算整趟失敗**：東西已經買到了，走不回去只是還要人自己走。
        """
        home = self.stats.home_map
        if not home or self._stop.is_set():
            return
        if home == self.stats.shop_map:
            self.stats.came_back = True
            return          # 本來就在同一張圖，不用走
        where = map_display_name(home) or home
        self._say(f"買完了，走回 {where}…")
        if self._walk(home, None) is not None:
            self.stats.came_back = True
            self._say(f"{self.stats.summary()}；已經走回 {where}")
        """背包裡有幾個回程道具。**現查**，不存格號（[MEM-028]）。"""
        if not self._home_item:
            return 0
        try:
            rows = bag.as_dict(self._pid)
        except Exception as exc:  # noqa: BLE001
            log.warning("讀不到背包，回程道具當成 0 個：%s", exc)
            return 0
        return sum(n for _slot, (item_id, n) in rows.items() if item_id == self._home_item)

    # ---- 雜項 -------------------------------------------------------

    @staticmethod
    def _feed(shopper: Restocker, packet) -> None:
        shopper.feed(packet.opcode, packet.payload)
        for parsed in _npc_in(packet):
            shopper.note_entity(*parsed)

    def _say(self, text: str) -> None:
        if text and text != self.stats.note:
            self.stats.note = text
            log.info("補水：%s", text)
            self._emit()

    def _emit(self) -> None:
        if self._on_update is not None:
            self._on_update(self.stats)


def _npc_in(packet):
    """從封包挖出 (GID, 外觀, x, y)。版面沿用 `travel_bot` 那一份，不另外寫一套。"""
    from ro_toolbox.services import travel_bot as tb

    payload = packet.payload
    if packet.opcode not in tb._OP_ENTITY or len(payload) < tb._ENT_POS + 3:
        return []
    if payload[tb._ENT_OBJTYPE] != tb._OBJTYPE_NPC:
        return []
    gid = int.from_bytes(payload[tb._ENT_GID:tb._ENT_GID + 4], "little")
    look = int.from_bytes(payload[tb._ENT_CLASS:tb._ENT_CLASS + 2], "little")
    x, y, _dir = unpack_position(payload[tb._ENT_POS:tb._ENT_POS + 3])
    return [(gid, look, x, y)]


__all__ = ["HOME_TARGET", "RestockBot", "RestockRun"]
