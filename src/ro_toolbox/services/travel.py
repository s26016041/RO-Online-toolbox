"""跨地圖尋路：算出要經過哪些傳點，再一段一段走完。

**為什麼要自己算**：使用者按下遊戲內建的尋路按鈕時，客戶端**一個封包都沒送**
（實測 `封包/按下尋路.txt`：只有 `0x0360`／`0x007F` 對時心跳）。畫面上的箭頭是
客戶端拿 `navi_link_tw.lub` 自己算的，伺服器根本不知道你要去哪。所以沒有「箭頭」
可以讀，只能用**同一份資料**算出同一條路 —— `assets/warps.json.gz` 就是從
`navi_link_tw.lub` 抽出來的（見 `tools/build_warp_table.py`）。

三個設計決定，都是為了「絕不安靜地做錯事」：

1. **每次換圖都從「現在真的在哪張圖」重新規劃**。RO 的傳點是一片區域，
   A* 算出來的路可能剛好穿過**別的**傳點，把人送到計畫外的地圖。
   與其做完美迴避，不如讓錯誤自癒：下一拍發現地圖不是預期的那張，就從那裡重算。
   繞遠路看得出來，走錯了也絕不會安靜地繼續。
2. **「換圖成功」只認一個訊號：記憶體裡的地圖名變了**。不睡幾秒當作「應該傳好了」。
   踩上傳點後每一拍重讀地圖名；沒變就換傳點附近的另一格再踩。
   逾時只當**放棄的上限**，放棄就把那個傳點列入黑名單並重新規劃，不會傻等。
3. **規劃不出來／走不到一律大聲停下**，不會繼續亂走。

走路本身交給 `Walker`（單次移動 ≤17 格、要等 `0x0087` 確認，見 GAMEDATA [PKT-030]）。
本模組不碰 socket、不碰記憶體，純邏輯 —— 位置與地圖名由呼叫端每拍餵進來。
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from ro_toolbox.services.gamedata import npc_links_on_map, warps_on_map
from ro_toolbox.services.mapdata import GatError, MapTerrain, load_terrain
from ro_toolbox.services.walker import Walker

log = logging.getLogger(__name__)

#: A* 節點上限。跨圖時單張圖可能要從一角走到另一角（512×512 最大約 26 萬格）。
NODE_BUDGET = 260_000
#: 傳點資料只給一格，實際傳點是一片區域；踩不到就在這個半徑內換格再試。
WARP_RING = 3
#: 踩在傳點上等多久還沒換圖，就換附近另一格試（**只是重試節奏，不是成功依據**）。
WARP_SETTLE_SEC = 1.5
#: 同一個傳點試多久還過不去就放棄它（放棄的上限，不是成功的依據）。
WARP_GIVEUP_SEC = 15.0
#: 重新規劃幾次還到不了就大聲放棄（防止兩張圖之間來回鬼打牆）。
MAX_REPLANS = 40
#: 目標座標不可走時，在這個半徑內找最近的可走格代替（NPC 站的格子常常不可走）。
GOAL_SNAP = 12
#: 走到離終點這麼近就算抵達。
ARRIVE_RADIUS = 2
#: 換圖後座標會停在上一張圖（[MEM-022]），等它更新的上限。超過就大聲停用。
STALE_POS_SEC = 10.0
#: 座標落在不可走格時，往旁邊找可走格當起點的半徑（gat type 5 的語意未確認）。
START_SNAP = 3


@dataclass(frozen=True, slots=True)
class Hop:
    """路線上的一段：在 `from_map` 的 (x, y) 這個傳點，會到 `to_map` 的 (to_x, to_y)。"""

    from_map: str
    x: int
    y: int
    to_map: str
    to_x: int
    to_y: int

    @property
    def cell(self) -> tuple[int, int]:
        return self.x, self.y

    @property
    def key(self) -> tuple[str, int, int]:
        """黑名單用的身分（存身分不存位置：地圖＋傳點座標，不是路線第幾段）。"""
        return self.from_map, self.x, self.y


def plan_route(
    start_map: str,
    goal_map: str,
    avoid: set[tuple[str, int, int]] | None = None,
    max_maps: int = 4000,
) -> list[Hop] | None:
    """從 `start_map` 走到 `goal_map` 要經過哪些傳點。走不到回 None。

    BFS ＝ **最少換圖次數**。這跟遊戲內建箭頭用的成本函數（`navi_linkdistance_tw.lub`
    有每一段的實際步數）不完全一樣，所以偶爾會挑到「圖比較少但路比較長」的走法。
    先求走得到、走得對；要跟遊戲完全一致再換成加權版。

    `avoid` 是踩不過去的傳點（`Hop.key`）—— 規劃時就繞開，不會一直撞同一道牆。
    """
    if start_map == goal_map:
        return []
    avoid = avoid or set()
    came: dict[str, Hop] = {}
    seen = {start_map}
    queue: deque[str] = deque([start_map])
    while queue and len(seen) < max_maps:
        current = queue.popleft()
        for x, y, dest, dx, dy in warps_on_map(current):
            if dest in seen or (current, x, y) in avoid:
                continue
            came[dest] = Hop(current, x, y, dest, dx, dy)
            if dest == goal_map:
                return _trace(came, start_map, goal_map)
            seen.add(dest)
            queue.append(dest)
    return None


def why_no_route(start_map: str, goal_map: str) -> tuple[str, int, int, str, str] | None:
    """走不到的時候，找出**第一個卡住的 NPC 連結**：(地圖, x, y, 目的地, NPC 名)。

    為什麼要它：navi_link 裡有 862 條連結是**要跟 NPC 講話**才過得去的
    （船夫、傳送師、告示牌），我們不會對話所以不放進 `plan_route`。
    但那些地方遊戲裡的箭頭走得通 —— 使用者看到的是「遊戲正常，你卻說找不到」。

    把 NPC 連結也放進去再 BFS 一次：走得通的話，回傳路上第一個 NPC 連結，
    讓呼叫端**講清楚要去找誰**，而不是丟一句「找不到路線」。
    回 None 代表就算加上 NPC 連結也到不了 —— 那是真的沒路。
    """
    if start_map == goal_map:
        return None
    came: dict[str, tuple[Hop, tuple | None]] = {}
    seen = {start_map}
    queue: deque[str] = deque([start_map])
    while queue and len(seen) < 4000:
        current = queue.popleft()
        walk = [(x, y, d, dx, dy, "") for x, y, d, dx, dy in warps_on_map(current)]
        for x, y, dest, dx, dy, who in walk + list(npc_links_on_map(current)):
            if dest in seen:
                continue
            gate = (current, x, y, dest, who) if who else None
            came[dest] = (Hop(current, x, y, dest, dx, dy), gate)
            if dest == goal_map:
                # 沿著路徑回推，回傳**最靠近起點**的那個 NPC 關卡
                node, gates = goal_map, []
                while node != start_map:
                    hop, g = came[node]
                    if g is not None:
                        gates.append(g)
                    node = hop.from_map
                return gates[-1] if gates else None
            seen.add(dest)
            queue.append(dest)
    return None


def _no_route_note(start_map: str, goal_map: str, excluded: bool = False) -> str:
    """走不到時給人看的一句話。**能講出原因就別只說「找不到」。**

    使用者實測回報：人在 izlu2dun（拜倫島），遊戲裡的箭頭好好的，我們卻說
    找不到路 —— 因為回 izlude 那條要**搭船**（跟 NPC 講話），不在可走的傳點裡。
    """
    tail = "（已排除走不通的傳點）" if excluded else ""
    gate = why_no_route(start_map, goal_map)
    if gate is None:
        return f"⚠ 從 {start_map} 找不到通往 {goal_map} 的路{tail}"
    where, x, y, dest, who = gate
    return (
        f"⚠ 到 {goal_map} 的路要**跟 NPC 對話**才過得去，自動尋路不會講話：\n"
        f"請自己在 {where} ({x},{y}) 找「{who}」到 {dest}，到了再按一次自動尋路"
    )


def _trace(came: dict[str, Hop], start_map: str, goal_map: str) -> list[Hop]:
    route: list[Hop] = []
    node = goal_map
    while node != start_map:
        hop = came[node]
        route.append(hop)
        node = hop.from_map
    route.reverse()
    return route


def nearest_walkable(
    terrain: MapTerrain, cell: tuple[int, int], radius: int = GOAL_SNAP
) -> tuple[int, int] | None:
    """離 `cell` 最近的可走格（含它自己）。半徑內都不可走就回 None。

    需要它是因為兩種格子常常站不上去：傳點資料給的那一格、以及 NPC 佔住的那一格。
    找不到就回 None 讓呼叫端安全退化，不要硬走一個站不上去的目標。
    """
    if terrain.is_walkable(*cell):
        return cell
    for r in range(1, radius + 1):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue  # 只看這一圈，由內往外
                x, y = cell[0] + dx, cell[1] + dy
                if terrain.is_walkable(x, y):
                    return x, y
    return None


class Traveler:
    """把一條跨地圖路線走完。呼叫端每拍餵 (地圖名, 座標)，看回傳狀態決定下一步。

    狀態：
        idle      沒有目的地
        walking   正在走（含正在踩傳點）
        arrived   到了
        blocked   到不了（規劃不出路線／傳點過不去／地形讀不到）→ 呼叫端該大聲停用

    `note` 隨時可讀，是給人看的一句話（現在在做什麼、卡在哪）。
    """

    def __init__(
        self,
        walker: Walker,
        now: Callable[[], float],
        terrain_loader: Callable[[str], MapTerrain] = load_terrain,
    ) -> None:
        self._walker = walker
        self._now = now
        self._load = terrain_loader
        self._terrain: MapTerrain | None = None
        self._terrain_map = ""
        self._goal_map = ""
        self._goal_cell: tuple[int, int] | None = None
        self._route: list[Hop] = []
        self._route_map = ""  # 這條路線是從哪張圖算出來的
        self._avoid: set[tuple[str, int, int]] = set()
        self._replans = 0
        self._warp_since = 0.0  # 開始踩這個傳點的時間
        self._warp_try = 0  # 換過幾格
        self._warp_cell: tuple[int, int] | None = None
        self._stale_since = 0.0  # 座標還停在上一張圖的起算時間（[MEM-022]）
        self.note = ""

    def _settle(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        """把座標校正成「這張地圖上真的站得住的一格」。還沒更新就回 None。

        [MEM-022]：換圖之後 `map_name` 已經是新圖、座標卻還是上一張圖的最後位置。
        那個值 <512 所以會通過所有範圍檢查，拿去算 A* 只會得到「找不到路」，
        症狀是「明明有傳點卻說走不到」。所以這裡分兩種情況：

        - 完全不在這張圖的範圍內 → 還沒更新，回 None 讓呼叫端等（角色一走就會更新）。
        - 在範圍內但踩在不可走格 → 往旁邊挪一格當起點。gat 的 type 5 語意還沒確認
          （[DAT-008]），保守起見不算可走，但不能因此就說人站在牆裡走不了。
        """
        terrain = self._terrain
        if terrain is None:
            return None
        x, y = pos
        if not (0 <= x < terrain.width and 0 <= y < terrain.height):
            return None
        if terrain.is_walkable(x, y):
            return pos
        return nearest_walkable(terrain, pos, radius=START_SNAP)

    # ---- 控制 -------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(self._goal_map)

    @property
    def goal_map(self) -> str:
        return self._goal_map

    @property
    def route(self) -> list[Hop]:
        """目前規劃的剩餘路線（診斷／顯示用）。"""
        return list(self._route)

    def set_goal(self, map_name: str, cell: tuple[int, int] | None = None) -> None:
        """設定目的地。`cell` 不給就走到那張圖為止（踏進去就算到）。"""
        self._goal_map = map_name
        self._goal_cell = cell
        self._route = []
        self._route_map = ""
        self._avoid.clear()
        self._replans = 0
        self._stale_since = 0.0
        self._clear_warp()
        self._walker.clear()
        self.note = f"準備前往 {map_name}"

    def clear(self) -> None:
        self._goal_map = ""
        self._goal_cell = None
        self._route = []
        self._route_map = ""
        self._clear_warp()
        self._walker.clear()

    # ---- 主迴圈每拍呼叫 ---------------------------------------------

    def update(self, map_name: str, pos: tuple[int, int]) -> str:
        if not self._goal_map:
            return "idle"
        if not map_name:
            return "walking"  # 換圖中間讀不到地圖名是正常過渡，不亂動

        # 地圖名變了（或第一次跑）：這是唯一被承認的「過去了」訊號。
        if map_name != self._route_map:
            if self._route_map:
                log.info("換圖 %s → %s，重新規劃", self._route_map, map_name)
            if not self._replan(map_name):
                return "blocked"
            if not self._route and self._goal_cell is None:
                self.note = f"已抵達 {map_name}"
                self.clear()
                return "arrived"

        if not self._load_terrain(map_name):
            return "blocked"

        # 座標可能還停在上一張圖（[MEM-022]）—— 那個值會通過所有範圍檢查，
        # 拿去算 A* 只會得到「無路可走」，看起來像地圖資料壞掉。等它更新才動。
        here = self._settle(pos)
        if here is None:
            now = self._now()
            if not self._stale_since:
                self._stale_since = now
                self.note = f"等座標更新到 {map_name}…"
            elif now - self._stale_since > STALE_POS_SEC:
                self.note = (
                    f"⚠ 進 {map_name} 後 {STALE_POS_SEC:.0f} 秒，"
                    f"座標 {pos} 仍不在這張圖上，已停止"
                )
                return "blocked"
            return "walking"
        if self._stale_since:
            # 座標追上來了就要把「等座標更新」收掉。不收的話它會一路掛著 ——
            # 實測穿越普隆德拉那 39 秒，人在走、狀態卻寫著「等座標更新」。
            self._stale_since = 0.0
            self.note = self._progress_note()
        pos = here

        # 正在踩傳點：成功與否只看地圖名（上面已判斷），這裡只負責換格再踩。
        if self._warp_cell is not None:
            return self._push_warp(pos)

        state = self._walker.update(pos)
        if state == "walking":
            return "walking"
        if state == "arrived":
            return self._on_leg_done(pos)
        if state == "blocked":
            return self._on_leg_blocked(map_name)
        return self._start_leg(map_name, pos)

    # ---- 內部 -------------------------------------------------------

    def _replan(self, map_name: str) -> bool:
        self._replans += 1
        if self._replans > MAX_REPLANS:
            self.note = f"⚠ 重新規劃 {MAX_REPLANS} 次仍到不了 {self._goal_map}，已放棄"
            return False
        route = plan_route(map_name, self._goal_map, self._avoid)
        if route is None:
            self.note = _no_route_note(map_name, self._goal_map)
            return False
        self._route = route
        self._route_map = map_name
        self._terrain = None
        self._terrain_map = ""
        self._stale_since = 0.0  # 新地圖重新起算「等座標更新」
        self._clear_warp()
        self._walker.clear()
        if route:
            self.note = self._progress_note()
        return True

    def _progress_note(self) -> str:
        """現在在做什麼的一句話。狀態文字要跟得上實際行為，否則就是另一種安靜的錯。"""
        if not self._route:
            return f"走向 {self._goal_map} 的目的地點"
        return f"前往 {self._goal_map}：還要換 {len(self._route)} 張圖"

    def _load_terrain(self, map_name: str) -> bool:
        if self._terrain is not None and self._terrain_map == map_name:
            return True
        try:
            self._terrain = self._load(map_name)
        except GatError as exc:
            self._terrain = None
            self.note = f"⚠ 讀不到 {map_name} 的地形，無法尋路：{exc}"
            return False
        self._terrain_map = map_name
        return True

    def _leg_goal(self) -> tuple[int, int] | None:
        """這張圖上要走到哪一格：下一個傳點，或最後一段的目的座標。"""
        terrain = self._terrain
        if terrain is None:
            return None
        raw = self._route[0].cell if self._route else self._goal_cell
        if raw is None:
            return None
        return nearest_walkable(terrain, raw)

    def _start_leg(self, map_name: str, pos: tuple[int, int]) -> str:
        terrain = self._terrain
        goal = self._leg_goal()
        if terrain is None or goal is None:
            self.note = f"⚠ {map_name} 上找不到可以走到的目標格"
            return self._give_up_leg(map_name)
        if max(abs(goal[0] - pos[0]), abs(goal[1] - pos[1])) <= ARRIVE_RADIUS:
            return self._on_leg_done(pos)
        path = terrain.find_path(pos, goal, node_budget=NODE_BUDGET)
        if not path:
            self.note = f"⚠ {map_name} 上走不到 {goal}"
            return self._give_up_leg(map_name)
        self._walker.set_path(path)
        self._walker.update(pos)
        return "walking"

    def _on_leg_done(self, pos: tuple[int, int]) -> str:
        """這張圖上該走的走完了。還有下一段就開始踩傳點，不然就是到了。"""
        if not self._route:
            self.note = f"已抵達 {self._goal_map}"
            self.clear()
            return "arrived"
        self._warp_cell = self._route[0].cell
        self._warp_since = self._now()
        self._warp_try = 0
        self.note = f"踩傳點前往 {self._route[0].to_map}"
        return self._push_warp(pos)

    def _push_warp(self, pos: tuple[int, int]) -> str:
        """站上傳點格，等地圖名變 —— 換圖成功會在 update() 開頭被抓到。

        踩不動時**換傳點附近的另一格**再試：傳點資料只給一格，實際是一片區域，
        給的那一格未必真的會傳。逾時只是放棄的上限，放棄就把它列黑名單重新規劃。
        """
        warp = self._warp_cell
        terrain = self._terrain
        if warp is None or terrain is None or not self._route:
            return "walking"
        now = self._now()
        if now - self._warp_since > WARP_GIVEUP_SEC:
            hop = self._route[0]
            log.warning("傳點 %s%s 踩不過去，列入黑名單", hop.from_map, hop.cell)
            self.note = f"⚠ {hop.from_map} 的傳點過不去，改走別條"
            return self._give_up_leg(hop.from_map)

        state = self._walker.update(pos)
        if state == "walking":
            return "walking"

        # 走到了（或沒路徑可走）卻還沒換圖 → 隔一段時間換這一圈的下一格再踩。
        if now - self._warp_since < WARP_SETTLE_SEC * (self._warp_try + 1):
            return "walking"
        self._warp_try += 1
        cell = self._ring_cell(warp, self._warp_try)
        if cell is None or cell == pos:
            return "walking"
        path = terrain.find_path(pos, cell, node_budget=NODE_BUDGET)
        if path:
            self._walker.set_path(path)
            self._walker.update(pos)
        return "walking"

    def _ring_cell(self, warp: tuple[int, int], index: int) -> tuple[int, int] | None:
        """傳點附近第 `index` 個候選格（由內往外一圈一圈試）。試完回 None。"""
        terrain = self._terrain
        if terrain is None:
            return None
        candidates: list[tuple[int, int]] = []
        for r in range(WARP_RING + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    cell = (warp[0] + dx, warp[1] + dy)
                    if terrain.is_walkable(*cell):
                        candidates.append(cell)
        return candidates[index] if index < len(candidates) else None

    def _give_up_leg(self, map_name: str) -> str:
        """這一段走不成：把當前傳點列黑名單並重新規劃。規劃不出來就 blocked。"""
        if self._route:
            self._avoid.add(self._route[0].key)
        self._clear_warp()
        self._walker.clear()
        self._route_map = ""  # 逼下一拍重新規劃
        if plan_route(map_name, self._goal_map, self._avoid) is None:
            self.note = _no_route_note(map_name, self._goal_map, excluded=True)
            return "blocked"
        return "walking"

    def _on_leg_blocked(self, map_name: str) -> str:
        self.note = f"⚠ {map_name} 上這條路走不成，重新規劃"
        self._route_map = ""  # 下一拍重新規劃
        self._walker.clear()
        return "walking"

    def _clear_warp(self) -> None:
        self._warp_cell = None
        self._warp_try = 0
        self._warp_since = 0.0
