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
from typing import NamedTuple

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
from ro_toolbox.services.travel import cell_beside, nearest_map_with, nearest_walkable
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


class WalkResult(NamedTuple):
    """走過去的結果。

    ⚠⚠ **「沒走到」不等於「走不到」。** 舊版 `_walk()` 對每一種失敗都回同一個
    `None`，呼叫端只好把它們全部當成「這家店走不到」寫進 `shop_reach` ——
    實機 2026-09-03 斷線那一拍，三秒內把三家好店冷凍了一週：

        10:48:56 ⚠ 遊戲連線已中斷   → 記下來：izlude_in  的商店走不到
        10:48:59 找不到伺服器連線   → 記下來：lasagna    的商店走不到
        10:49:02 找不到伺服器連線   → 記下來：cmd_fild07 的商店走不到

    之後每一張圖的「道具商人」都被寫掉，挑店只剩同一張圖上的高級藥水商人 ——
    那家沒有紅色藥水（使用者：「自動補水 藥水商人都找錯」）。
    """

    #: 走到了 = 沿路看到的 NPC（可能是空 dict）。沒走到 = None。
    npc: dict | None = None
    #: ★ 真的**沒有路**可以走過去（`TravelStats.unreachable`）。
    #: 斷線、還沒登入、取消、逾時一律是 False。
    unreachable: bool = False
    #: 走太久放棄（`WALK_GIVEUP`）。**這不是「走不到」的證據** ——
    #: 逾時只能當放棄的上限（CLAUDE.md），所以不記憶，但換一家還是值得試。
    timed_out: bool = False
    #: ★ 路算得出來，但**角色一步都沒動**（對話框開著、背包太重、被定身）。
    #: 跟這家店無關 —— 不記憶，而且該**重試同一家**，換一家也是一樣的下場。
    stuck: bool = False

    @property
    def arrived(self) -> bool:
        return self.npc is not None


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
            #: 這一趟已經試過、走不到的**商人**（地圖 ＋ 他站的那一格）。
            #: ⚠ 記商人不記地圖：一張圖上可以有兩個藥水商人，第一個在走不到
            #: 的房間、第二個就在門口 —— 整張圖一起放棄的話，好的那個永遠
            #: 不會被試到。
            tried: set[tuple[str, tuple[int, int]]] = set()
            known: dict | None = None
            #: 連走都走不成（連線沒了／角色死了）—— 跟「走不到」要分開講。
            cant_start = False
            look, seller_cell = 0, (0, 0)
            for _ in range(_SHOP_TRIES):
                plan = self._find_shop(tried)
                if plan is None:
                    break
                target_map, cell, seller_cell, look, name = plan
                self.stats.shop_map = target_map
                self.stats.shop_name = name
                where = map_display_name(target_map) or target_map
                self._say(f"往 {where} 的{name}…")
                walked = self._walk(target_map, cell)
                if walked.arrived:
                    # 走到了 = 上次那筆「走不到」被推翻了（如果有的話）
                    shop_reach.note_good(target_map, seller_cell)
                    known = self._make_sure_he_is_visible(
                        target_map, cell, seller_cell, look, name, walked.npc or {}
                    )
                    break
                known = None
                if walked.stuck:
                    # ★★ **角色動不了跟這家店無關** —— 路算得出來，是送出去的
                    #    移動被伺服器靜默忽略（對話框開著、背包太重、被定身）。
                    #    所以：不記憶、也**不換一家**（換了也是一樣的下場，
                    #    而且旁邊那家常常是不賣紅藥的高級藥水商人），
                    #    就重試同一家。實機 2026-09-03：
                    #
                    #      14:05:11 ⚠ 在 izlude_in (60,108) 送出去的移動連續被
                    #               伺服器忽略，角色一步都沒動
                    #      14:05:11 記下來：izlude_in 的商店 (57,110) 走不到  ← 冤枉
                    #      14:05:13 補水：往 依斯魯得島內部 的高級藥水商人…
                    #      14:05:41 補水：⚠ 這家店沒有你設定的藥水，什麼都沒買
                    #
                    #    三秒後同一隻角色走去別的地方**走得動** —— 所以那句
                    #    「走不到」從頭到尾都是假的（[DAT-065]）。
                    self._say(f"⚠ 角色在 {where} 動不了（對話框開著／背包太重／"
                              f"狀態異常），等一下再試同一家")
                    if self._stop.is_set():
                        return
                    continue
                tried.add((target_map, seller_cell))
                if not walked.unreachable:
                    # ⛔⛔ **沒走到 ≠ 走不到。**
                    #
                    # 斷線、還沒登入、角色死了、使用者取消、逾時全都會走到這裡，
                    # 而這些跟「有沒有路過去」一點關係都沒有。寫進 `shop_reach`
                    # 就是把一家好店冷凍一週 —— 實機 2026-09-03 斷線那一拍，
                    # 三秒內記掉三家道具商人：
                    #
                    #     10:48:56 ⚠ 遊戲連線已中斷 → izlude_in  (57,110)
                    #     10:48:59 找不到伺服器連線 → lasagna    (165,125)
                    #     10:49:02 找不到伺服器連線 → cmd_fild07 (257,126)
                    #
                    # 之後每張圖的「道具商人」都被寫掉，只剩同一張圖上的
                    # 高級藥水商人可挑 —— 那家沒有紅色藥水也沒有回程道具，
                    # 於是每一趟都走到底才發現「這家店沒有你設定的藥水」
                    # （使用者：「自動補水 藥水商人都找錯」）。
                    if not walked.timed_out:
                        # 連走都走不成（連線沒了／角色死了）——**當場停**。
                        # 換一家也是同樣的下場：上面那份日誌裡後兩家各花 3 秒
                        # 失敗，純粹是噪音（memory：確定的失敗不准重試到逾時）。
                        # ⚠ 不覆蓋 `_walk()` 剛說的那句 —— 那句才寫著真正的
                        #   原因（「遊戲連線已中斷」「找不到伺服器連線」），
                        #   下面那句「都走不到」會把它蓋掉、變成假線索。
                        cant_start = True
                        break
                    self._say(f"⚠ 走去 {where} 的{name}太久了，換一家試試")
                    if self._stop.is_set():
                        return
                    continue
                # ⚠⚠ **這一趟的 `tried` 只活到這一趟結束** —— 下次補水又會挑到
                # 同一家最近的店、再失敗一次。實機 2026-09-01 的四次補水
                # **每一次都先去 prt_in、每一次都走不到**，每次白花 1.5~2 分鐘
                # （使用者：「狐狐狸一直找不到商店買水」）。所以要記到檔案裡，
                # 下次直接排最後（見 `services/shop_reach.py`：降級不是刪除）。
                shop_reach.note_bad(target_map, seller_cell)
                self._say(f"⚠ 走不到 {where} 的{name}，換一家試試")
                if self._stop.is_set():
                    return
            if known is not None:
                self._buy(look, seller_cell, known)
            elif not cant_start:
                self._say("⚠ 試過的藥水商人都走不到 —— 這趟沒補到水")
            # ⚠⚠ **買不買得到都要走回去。** 舊版在「每一家都走不到」時直接
            # return，於是角色就留在最後一次失敗的地方（城裡某個房間）：
            # `came_back` 是 False → 補給那條不接掛機、`_watch_farm_alive`
            # 又因為「人不在練功地圖上」不敢開 → **整晚站在城裡**，
            # 而且自動那一趟不跳框，早上起來才看得到。
            # 走回去至少能讓後面的流程繼續（走不到的店已經記進 `shop_reach`，
            # 下一趟會先挑別家）。
            self._go_back()
        except Exception as exc:  # noqa: BLE001 - 背景執行緒不能讓例外逸出
            log.exception("補水停了：%s", exc)
            self._say(f"補水停了：{exc}")
        finally:
            self.stats.running = False
            self._emit()

    def _find_shop(self, tried: set[tuple[str, tuple[int, int]]] | None = None):
        """回 (地圖, 走到哪一格, 商人格, 外觀, 名字)。找不到就回 None 並說明。

        `tried` 是**這一趟已經試過、走不到的商人**（地圖 ＋ 他站的那一格）——
        換一家的時候要跳過他們，不然 `nearest_map_with()` 每次都回同一張
        最近的圖。⚠ 記的是**商人的身分**不是地圖（CLAUDE.md：存身分）：
        一張圖上可以有兩個藥水商人，只有一個走不到。
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
        # ⚠⚠ **「出發前在哪」只准記一次。**
        #
        # 這一支在「走不到就換一家」的迴圈裡會被呼叫好幾次，而每一次的 `here`
        # 都是**現在**站的地方 —— 第一家走不到的話，人已經在城裡了，於是
        # 「家」就變成那張城裡的圖。實機 2026-09-01（使用者：「補水後角色會亂走」）：
        #
        #     18:30 從 prt_fild04 出發 → prt_fild05 走不到 → prt_in 走不到
        #     18:32 補水：買完了，**走回 普隆德拉內部**…      ← 回到城裡的房間
        #     19:31 補水：買完了，**走回 普隆德拉內部**…      ← 下一趟又從那裡出發
        #
        # 回到城裡之後自動打怪照樣接回去，於是角色就在城裡漫遊 —— 那就是
        # 使用者看到的「亂走」。而且下一趟的「家」又是城裡，會一直滾下去。
        # 完整經過見 GAMEDATA [DAT-062]。
        if not self.stats.home_map:
            self.stats.home_map = self._back_to or here
        tried = tried or set()
        # ⚠ 讀一次檔就好：下面要問**每一張**有藥水商人的圖「上次走得到嗎」。
        bad = shop_reach.snapshot()

        # 第一輪只挑沒被記成走不到的；全都被記過的話第二輪連他們也算 ——
        # **降級不是刪除**（`services/shop_reach.py`）：記憶本身不可以變成
        # 「一瓶水都買不到」的原因。
        for allow_bad in (False, True):
            target_map = here
            # ⚠ 腳下這張圖也要問記憶。舊版只對**別的**地圖查，於是第一次走失敗
            # 把人丟在 prt_in 之後，重試那一次 `here` 就是 prt_in，
            # 直接跳過黑名單又挑了同一家 —— 記憶在它專門要解的情境裡失效。
            want = self._key_items()
            seller = self._pick_seller(here, tried, bad, want, allow_bad=allow_bad)
            if seller is None:
                candidates = {
                    m for m in maps_with_potion_sellers()
                    if self._pick_seller(m, tried, bad, want, allow_bad=allow_bad)
                }
                found = nearest_map_with(here, candidates) if candidates else None
                if found is None:
                    continue                  # 換成「連走不到的也算」再找一次
                _route, target_map = found
                seller = self._pick_seller(target_map, tried, bad, want,
                                           allow_bad=allow_bad)
            if seller is not None:
                if allow_bad:
                    log.info("有藥水的圖全都被記成走不到 —— 這次忽略紀錄，照原本的挑")
                break
        else:
            self._say("⚠ 附近找不到藥水商人 —— 沒有路線可以走過去")
            return None

        x, y, name, look = seller
        try:
            terrain = load_terrain(target_map)
        except GatError as exc:
            self._say(f"⚠ {target_map} 的地形讀不到：{exc}")
            return None
        # ★★ **走到他旁邊，不是走到他身上。**
        #
        # NPC 佔著自己那一格，伺服器會**靜默忽略**目的地是那一格的移動要求
        # （不回錯誤、不回 0x0087，就是不動），而我們會一直重送同一個目標。
        # 實機量到（2026-09-03，狐狐狸 @ izlude_in，道具商人 (57,110)）：
        #
        #   (60,105) → (57,110)：路徑 10 格 ≤ 步幅 10，第一段就送 NPC 那一格
        #                        → 送出 21 次、**被拒 0 次**（一次 0x0087 都
        #                        沒回）、8 秒角色一步都沒動 → 「走不到」
        #   (64,100) → (57,110)：路徑 18 格 > 步幅，第一段送中間的自由格
        #                        → 走得動，停在 (56,110) 算抵達
        #   (64,100) → (57,109)：他旁邊那一格，3.9 秒走到
        #
        # 所以「走不到商人」不是地形問題，是我們把目的地設在他身上 ——
        # 而且**站得近反而更容易發生**（路徑短到一段就送完）。
        # `nearest_walkable()` 分不出來：GAT 只有地形，不知道誰站在上面。
        cell = cell_beside(terrain, (x, y)) or nearest_walkable(terrain, (x, y)) or (x, y)
        return target_map, cell, (x, y), look, name

    def _key_items(self) -> tuple[int, ...]:
        """挑店時**非有不可**的道具。

        補水的重點是藥水；沒設藥水才輪到回程道具。⚠ 這個判斷放在呼叫端 ——
        `shop_reach` 只回答「這家有沒有被記成不賣這幾樣」，不去猜哪一樣重要
        （CLAUDE.md：「該不該做 X」寫在呼叫端）。
        """
        if self._hp_item:
            return (self._hp_item,)
        return (self._home_item,) if self._home_item else ()

    @staticmethod
    def _pick_seller(map_name: str, tried: set, bad, items=(), *,
                     allow_bad: bool = False):
        """這張圖上還可以試的藥水商人。回 None ＝ 這張圖沒得試了。

        ⚠ **一張圖可以有好幾個藥水商人。** 上次走不到的只是**那一個**走不到，
        不是整張圖 —— 舊版只看 `sellers[0]`，於是「第一個在走不到的房間、
        第二個就在門口」的圖會被整個寫掉，門口那個永遠沒被試過。

        排除兩種：上次**走不到**的、以及上次開了店發現**沒賣**我們要的藥水的
        （`items`）。後者實機踩過 —— izlude_in 的「高級藥水商人」就在道具商人
        旁邊三格，貨架上沒有紅色藥水也沒有回程道具，但每一趟都要走到底、
        開了店才知道（使用者：「自動補水 藥水商人都找錯」）。

        `allow_bad=True` 是安全退化：全都被記過的時候還是要有東西可試。
        """
        stale = None
        for seller in potion_sellers_on(map_name):
            cell = (seller[0], seller[1])
            if (map_name, cell) in tried:
                continue                       # 這一趟已經走過、走不到
            if not bad.skip(map_name, cell, items):
                return seller
            stale = stale or seller
        return stale if allow_bad else None

    @staticmethod
    def _known_bad(map_name: str) -> bool:
        """這張圖上的藥水商人**全部**上次都走不到嗎（`services/shop_reach.py`）。

        ⚠ 「全部」很重要：只看第一個的話，一張有兩個商人的好圖會被整個寫掉
        （見 `_pick_seller`）。
        """
        sellers = potion_sellers_on(map_name)
        if not sellers:
            return False
        bad = shop_reach.snapshot()
        return all(bad.is_bad(map_name, (x, y)) for x, y, _name, _look in sellers)

    def _walk(self, target_map: str, cell: tuple[int, int] | None,
              what: str = "商人那裡") -> WalkResult:
        """走過去。回 `WalkResult`（沿路看到的 NPC ＋ **是不是真的走不到**）。

        ⚠ `unreachable` 只會在 `TravelBot` 自己算完路、說「到不了」時是 True。
        逾時放棄也**不算** —— 逾時是放棄的上限，不是「這條路不存在」的證據
        （CLAUDE.md：逾時只能當放棄的上限，不能當成功／失敗的依據）。
        """
        bot = TravelBot(self._pid, destination=target_map, destination_cell=cell,
                        on_update=lambda s: self._say(s.note))
        self._travel = bot
        bot.start()
        deadline = time.monotonic() + WALK_GIVEUP
        while bot.running and time.monotonic() < deadline and not self._stop.is_set():
            self._stop.wait(TICK)
        # ⚠ 「還在跑就被我們喊停」＝逾時。先問再 `stop()`，不然問不出來。
        timed_out = bool(bot.running) and not self._stop.is_set()
        arrived = bool(getattr(bot.stats, "arrived", False))
        unreachable = bool(getattr(bot.stats, "unreachable", False))
        stuck = bool(getattr(bot.stats, "stuck", False))
        known = bot.npc_seen
        bot.stop()
        self._travel = None
        if self._stop.is_set():
            self._say("已取消")
            return WalkResult()
        if not arrived:
            # ⚠ `what` 要講對：走回練功地圖那一段失敗印「沒走到商人那裡」，
            #   使用者會去查商人（實機 2026-09-05 就是這樣誤導的）。
            self._say(f"⚠ 沒走到{what}：{bot.stats.note}")
            return WalkResult(unreachable=unreachable, timed_out=timed_out,
                              stuck=stuck)
        return WalkResult(known)

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
            if not self._walk(target_map, away).arrived:
                return known
            back = self._walk(target_map, cell)
            if not back.arrived:
                return known
            known = remember_npcs(self._pid, target_map, back.npc or {})
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
        # ★ 貨架看過了才知道 —— 把「這家沒賣什麼」記下來，下一趟不要再撲空。
        #   ⚠ 記的是**開店那一包實際少了哪幾樣**，不是「這趟沒買到」：
        #   沒買到還可能是負重滿了、錢不夠，那些跟這家店賣什麼無關。
        if shopper.stats.missing and self.stats.shop_map:
            shop_reach.note_no_stock(self.stats.shop_map, cell, shopper.stats.missing)
        if self.stats.bought and self.stats.shop_map:
            shop_reach.note_good(self.stats.shop_map, cell, self.stats.bought)
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
        if self._walk(home, None, what=f"{where}").arrived:
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
