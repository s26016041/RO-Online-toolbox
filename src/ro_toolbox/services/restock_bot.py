"""按一下「補水」：自己走去最近的藥水商人，補完再說一聲。

把三段本來各自能跑的東西接起來（做法照 `tools/restock_test.py`，那支已經實機
跑通過整條流程）：

1. **找店**：先看腳下這張圖有沒有藥水商人；沒有就用 `nearest_map_with()`
   找最近的一張。都沒有就大聲說，不要亂走。
2. **走過去**：`TravelBot`，目的地是**商人腳邊那一格**（`nearest_walkable`）。
   ⚠ 走路那一段還負責**把沿路看到的 NPC 記下來**：實體只在「進入視野」時
   送一次封包（[PKT-061]），到了才開擷取是接不到的 —— 認不出商人就開不了店。
   ⚠⚠ 走 5 格是**看不到任何實體封包**的（人本來就站在旁邊）。所以這裡還要
   **記住這次執行看過的 NPC**（`_NPC_SEEN`），走不出新東西時就拿記著的用；
   連記著的都沒有就**走遠再走回來**（`_shake_view()`）逼他重新進一次視野。
   使用者實機 2026-09-01：第一趟認得出商人、第二三趟都「走到了卻認不出商人」，
   因為那時人就站在他旁邊，走 5 格不會有任何人進視野。
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
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ro_toolbox.core.ro_protocol import unpack_position
from ro_toolbox.services import bag, shop_reach
from ro_toolbox.services.game_link import GameLink
from ro_toolbox.services.gamedata import (
    item_name,
    map_display_name,
    maps_with_potion_sellers,
    potion_sellers_on,
)
from ro_toolbox.services.mapdata import GatError, load_terrain
from ro_toolbox.services.restock import (
    HOME_TARGET,
    NPC_SNAP,
    Restocker,
    RestockOrder,
)
from ro_toolbox.services.travel import nearest_map_with, nearest_walkable
from ro_toolbox.services.travel_bot import OUT_OF_VIEW as _OUT_OF_VIEW
from ro_toolbox.services.travel_bot import TravelBot

log = logging.getLogger(__name__)

#: 走去商人最多花多久。**放棄的上限，不是成功的依據**（CLAUDE.md）。
WALK_GIVEUP = 240.0
#: 走遠再走回來最多做幾輪（逼 NPC 重新進一次視野）。
_SHAKE_ROUNDS = 2
#: 這一次執行期間，各行程在各張圖上看過的 NPC：`{(pid, 地圖): {gid: (外觀, x, y)}}`。
#:
#: ⚠ 為什麼要記：實體只在**進入視野**時送一次封包（[PKT-061]）。第二趟補水
#: 出發時人已經站在商人旁邊，那一包早就過去了 —— 走那 5 格不會有任何人重新
#: 進視野，於是「走到了卻認不出商人」。記的是**身分（GID）不是位置**
#: （CLAUDE.md），而且只活在這一次執行期間：換一次遊戲就是新的 PID，
#: 舊的自然不會被拿來用。
_NPC_SEEN: dict[tuple[int, str], dict[int, tuple[int, int, int]]] = {}
_NPC_LOCK = threading.Lock()


def remember_npcs(pid: int, map_name: str, seen: dict) -> dict:
    """把這一趟看到的 NPC 併進記憶，回傳這張圖上目前認得的全部 NPC。"""
    if not map_name:
        return dict(seen or {})
    with _NPC_LOCK:
        known = _NPC_SEEN.setdefault((pid, map_name), {})
        known.update(seen or {})
        return dict(known)


def forget_npcs(pid: int) -> None:
    """遊戲關掉／換人了就把記憶丟掉 —— GID 是伺服器給的，不是永久的。"""
    with _NPC_LOCK:
        for key in [k for k in _NPC_SEEN if k[0] == pid]:
            _NPC_SEEN.pop(key, None)


#: 站到商人旁邊之後，整段買賣最多花多久。
SHOP_GIVEUP = 90.0
#: 主迴圈一拍。
#: 走不到就換一家，最多試這麼多家。
#:
#: ⚠ 要有上限：每一家都要走過去才知道走不走得到，一趟幾十秒 ——
#: 無限試下去等於整晚都在城裡繞。
_SHOP_TRIES = 3
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
            # ⚠⚠ **走不到那家店要換一家，不是整趟放棄。**
            #
            # 實機 2026-08-29（狐狐狸，蝴蝶翅膀回到 prontera）：挑了 prt_in
            # 的道具商人，但踩進去的門把人放在 (180,97)，而商人在 (126,76)
            # —— **prt_in 是一張地圖裡好幾個互不相連的房間**（[DAT-029]），
            # 那兩格永遠走不到彼此。結果是重新規劃 40 次同一條算不出來的路，
            # 然後「沒走到商人那裡」，藥水一瓶都沒補，人就留在城裡。
            #
            # 換一張有藥水商人的圖就好了（izlude_in 那家一直都走得到）。
            skip: set[str] = set()
            for _ in range(_SHOP_TRIES):
                plan = self._find_shop(skip)
                if plan is None:
                    return
                target_map, cell, seller_cell, look, name = plan
                self.stats.shop_map = target_map
                self.stats.shop_name = name
                where = map_display_name(target_map) or target_map
                self._say(f"往 {where} 的{name}…")
                known = self._walk(target_map, cell)
                if known is not None:
                    # 走到了 = 上次那筆「走不到」被推翻了（如果有的話）
                    shop_reach.note_good(target_map, seller_cell)
                    known = self._make_sure_he_is_visible(
                        target_map, cell, seller_cell, look, name, known
                    )
                    break
                skip.add(target_map)
                # ⚠⚠ **這一趟的 `skip` 只活到這一趟結束** —— 下次補水又會挑到
                # 同一家最近的店、再失敗一次。實機 2026-09-01 的四次補水
                # **每一次都先去 prt_in、每一次都走不到**，每次白花 1.5~2 分鐘
                # （使用者：「狐狐狸一直找不到商店買水」）。所以要記到檔案裡，
                # 下次直接排最後（見 `services/shop_reach.py`：降級不是刪除）。
                shop_reach.note_bad(target_map, seller_cell)
                self._say(f"⚠ 走不到 {where} 的{name}，換一家試試")
                if self._stop.is_set():
                    return
            else:
                return
            self._buy(look, seller_cell, known)
            self._go_back()
        except Exception as exc:  # noqa: BLE001 - 背景執行緒不能讓例外逸出
            log.exception("補水停了：%s", exc)
            self._say(f"補水停了：{exc}")
        finally:
            self.stats.running = False
            self._emit()

    def _find_shop(self, skip: set[str] | None = None):
        """回 (地圖, 走到哪一格, 商人格, 外觀, 名字)。找不到就回 None 並說明。

        `skip` 是**這一趟已經試過、走不到的地圖** —— 換一家的時候要跳過它們，
        不然 `nearest_map_with()` 每次都回同一張最近的圖。
        """
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
        skip = skip or set()
        sellers = [] if here in skip else potion_sellers_on(here)
        target_map = here
        if not sellers:
            candidates = set(maps_with_potion_sellers()) - skip
            # ⚠ 上次走不到的排最後，**不是排除**：`usable` 空了就退回整份清單
            # （安全退化 —— 記憶本身不可以變成「一瓶水都買不到」的原因）。
            usable = {m for m in candidates if not self._known_bad(m)}
            if candidates and not usable:
                log.info("有藥水的圖全都被記成走不到 —— 這次忽略紀錄，照原本的挑")
            found = nearest_map_with(here, usable or candidates) if candidates else None
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

    @staticmethod
    def _known_bad(map_name: str) -> bool:
        """這張圖的藥水商人上次走不到嗎（見 `services/shop_reach.py`）。"""
        sellers = potion_sellers_on(map_name)
        if not sellers:
            return False
        x, y, _name, _look = sellers[0]
        return shop_reach.is_bad(map_name, (x, y))

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

    def _make_sure_he_is_visible(
        self, target_map: str, cell, seller_cell, look: int, name: str, known: dict
    ) -> dict:
        """走到了，但認得出這個商人嗎？認不出就**走遠再走回來**。

        ⚠ 實體只在「進入視野」時送一次封包（[PKT-061]）。第二趟補水出發時人
        已經站在商人旁邊，走那 5 格**不會有任何人重新進視野** —— 使用者實機
        2026-09-01 連續兩趟都停在「走到了卻認不出商人（外觀 83 @ (290,221)）」。

        兩層保險，都很便宜：

        1. **這次執行記過的**（`_NPC_SEEN`）：第一趟走過來時看到的 GID
           還在，直接拿來用。
        2. 記憶裡也沒有 → 走到視野外再走回來，逼伺服器重送一次那一包
           （做法跟 `travel_bot._shake_view()` 一樣，那條路已經實機驗過）。

        ⚠ 一定要走 `Walker`（`TravelBot`），不能自己送一個很遠的走路封包：
        單次移動超過 17 格伺服器**靜默忽略**（[PKT-030]）。
        """
        known = remember_npcs(self._pid, target_map, known)
        if self._can_see(known, seller_cell, look):
            return known
        try:
            terrain = load_terrain(target_map)
        except GatError:
            return known                      # 讀不到地形就別亂走，交給下一段大聲說
        for round_no in range(1, _SHAKE_ROUNDS + 1):
            if self._stop.is_set():
                return known
            away = terrain.random_walkable(
                random, near=seller_cell, radius=_OUT_OF_VIEW + 8,
                min_radius=_OUT_OF_VIEW,
            )
            if away is None:
                return known
            self._say(f"認不出{name}，先走遠一點讓他重新進視野（第 {round_no} 次）")
            if self._walk(target_map, away) is None:
                return known
            back = self._walk(target_map, cell)
            if back is None:
                return known
            known = remember_npcs(self._pid, target_map, back)
            if self._can_see(known, seller_cell, look):
                return known
        return known

    @staticmethod
    def _can_see(known: dict, seller_cell, look: int) -> bool:
        """記著的 NPC 裡有沒有「外觀對、位置也對」的那一個（[DAT-027]）。"""
        return any(
            info[0] == look
            and max(abs(info[1] - seller_cell[0]), abs(info[2] - seller_cell[1]))
            <= NPC_SNAP
            for info in known.values()
        )

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

    def _home_count(self) -> int:
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
