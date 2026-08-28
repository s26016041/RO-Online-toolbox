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

from ro_toolbox.services.gamedata import (
    npc_links_on_map,
    warp_landings_on,
    warps_on_map,
)
from ro_toolbox.services.mapdata import GatError, MapTerrain, load_terrain
from ro_toolbox.services.walker import Walker
from ro_toolbox.services.warpzone import KEEP_OUT, keep_out, warp_cells

log = logging.getLogger(__name__)

#: A* 節點上限。跨圖時單張圖可能要從一角走到另一角（512×512 最大約 26 萬格）。
NODE_BUDGET = 260_000
#: 傳點資料只給一格，實際傳點是一片區域；踩不到就在這個半徑內換格再試。
WARP_RING = 3
#: 踩在傳點上等多久還沒換圖，就換附近另一格試（**只是重試節奏，不是成功依據**）。
WARP_SETTLE_SEC = 1.5
#: 同一個傳點試多久還過不去就放棄它（放棄的上限，不是成功的依據）。
WARP_GIVEUP_SEC = 15.0
#: 停在 NPC 前面等人手動做完，最多等多久。
#:
#: ⚠ 這是**放棄的上限，不是成功的依據**（CLAUDE.md：不准拿「等幾秒」當機制）。
#: 判定「過去了」只看一件事：記憶體裡的地圖名真的變成目的地那一張。
#: 逾時只是免得無限掛著，逾時要**大聲停止**，不准假裝成功往下走。
NPC_GIVEUP_SEC = 600.0
#: 重新規劃幾次還到不了就大聲放棄（防止兩張圖之間來回鬼打牆）。
MAX_REPLANS = 40
#: 目標座標不可走時，在這個半徑內找最近的可走格代替（NPC 站的格子常常不可走）。
GOAL_SNAP = 12
#: 走到離終點這麼近就算抵達。
ARRIVE_RADIUS = 2
#: 換圖後座標會停在上一張圖（[MEM-022]），等它更新的上限。超過就大聲停用。
STALE_POS_SEC = 10.0
#: 「在兩張圖之間來回刷」的判準：這麼久之內來回這麼多次就停手。
#:
#: ⚠ 這不是效率問題，是**會把自己刷到斷線**（使用者實測：走進一間店又馬上
#: 出來、來回刷換圖，最後整個連線被伺服器斷掉）。
#: 正常跨圖不會在 40 秒內同一對地圖來回 4 次 —— 走路本身就要花時間。
BOUNCE_WINDOW_SEC = 40.0
BOUNCE_LIMIT = 4
#: 座標落在不可走格時，往旁邊找可走格當起點的半徑（gat type 5 的語意未確認）。
START_SNAP = 3
#: 我們**要踩的**那道門，周圍這個半徑內不列入「繞開別的傳點」的禁區。
#:
#: ⚠ 不留這個洞的話，禁區會把終點自己包起來 —— A* 連門口都到不了，
#: 每一道門都變成「走不到」，症狀跟資料壞掉一模一樣。
DOOR_CLEAR = KEEP_OUT + 1
#: 路線規劃最多退讓幾次「落地之後走不到下一道門」（見 `_dead_end`）。
#:
#: ⚠ 每退一次要多跑一次 BFS，所以要有上限。用完**不是失敗**：照原本的路線走，
#: 每進一張圖之前都會再驗一次（見 `_replan`）。
ROUTE_TRIES = 8
#: 一次規劃裡最多做幾次**沒算過的**泛洪。一次約 0.1 秒（實測 400×400），
#: 整條路線幾十段都現算的話換一次圖就要卡好幾秒。
#:
#: ⚠ 這是**看多遠**的預算，不是「驗不完就當它是好的」的藉口 ——
#: 沒驗到的段落等真的走到那張圖再驗，那時它會變成第一段。
FILL_BUDGET = 4


@dataclass(frozen=True, slots=True)
class Hop:
    """路線上的一段：在 `from_map` 的 (x, y) 這個傳點，會到 `to_map` 的 (to_x, to_y)。"""

    from_map: str
    x: int
    y: int
    to_map: str
    to_x: int
    to_y: int
    #: 這一段要**跟 NPC 講話**才過得去時，NPC 的名字（船員、傳送師…）。
    #: 空字串 = 走過去就會傳送。
    npc: str = ""
    #: 那隻 NPC 的**外觀編號**。認人要「外觀 ＋ 座標」兩個欄位同時對上，
    #: 不是猜一個 GID（[DAT-027]）。0 = 不知道，那就只能停下來等人。
    npc_id: int = 0

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
    allow_npc: bool = False,
) -> list[Hop] | None:
    """從 `start_map` 走到 `goal_map` 要經過哪些傳點。走不到回 None。

    BFS ＝ **最少換圖次數**。這跟遊戲內建箭頭用的成本函數（`navi_linkdistance_tw.lub`
    有每一段的實際步數）不完全一樣，所以偶爾會挑到「圖比較少但路比較長」的走法。
    先求走得到、走得對；要跟遊戲完全一致再換成加權版。

    `avoid` 是踩不過去的傳點（`Hop.key`）—— 規劃時就繞開，不會一直撞同一道牆。

    `allow_npc=True` 會把「要跟 NPC 講話」的連結也算進來（船夫、傳送師…），
    那些 `Hop.npc` 不是空的。**呼叫端必須自己處理**：走到那一格什麼都不會發生，
    要停下來等人手動做完（見 `Traveler._wait_for_npc`）。
    預設關著 —— 純走路走得到就別麻煩人。
    """
    if start_map == goal_map:
        return []
    avoid = avoid or set()
    came: dict[str, Hop] = {}
    seen = {start_map}
    queue: deque[str] = deque([start_map])
    while queue and len(seen) < max_maps:
        current = queue.popleft()
        links = [(x, y, d, dx, dy, "", 0) for x, y, d, dx, dy in warps_on_map(current)]
        if allow_npc:
            links += list(npc_links_on_map(current))
        for x, y, dest, dx, dy, who, who_id in links:
            if dest in seen or (current, x, y) in avoid:
                continue
            came[dest] = Hop(current, x, y, dest, dx, dy, who, who_id)
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
        walk = [(x, y, d, dx, dy, "", 0) for x, y, d, dx, dy in warps_on_map(current)]
        for x, y, dest, dx, dy, who, _id in walk + list(npc_links_on_map(current)):
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
        #: 最近幾次換圖：[(從哪張, 到哪張, 什麼時候)]。用來抓「來回刷」。
        self._hops: list[tuple[str, str, float]] = []
        self._warp_since = 0.0  # 開始踩這個傳點的時間
        self._warp_try = 0  # 換過幾格
        self._warp_cell: tuple[int, int] | None = None
        self._npc_wait: Hop | None = None   # 正在等人跟這個 NPC 講話
        self._npc_since = 0.0
        #: 「從我現在站的地方走得到哪些格」。室內圖一張地圖裡有好幾個互不相連的
        #: 房間，挑門要靠它（見 `_gate_options`）。換圖就作廢。
        self._reach: frozenset[tuple[int, int]] | None = None
        self._stale_since = 0.0  # 座標還停在上一張圖的起算時間（[MEM-022]）
        #: 「這張圖的這個落地點走得到哪些格」的快取（見 `_dead_end`）。
        #: 一次泛洪 0.1 秒，換一次目的地就作廢 —— 地形是靜態的，同一趟不必重算。
        self._entry_reach: dict[tuple[str, tuple[int, int]], frozenset] = {}
        self._fills = 0          # 這一次規劃還剩幾次泛洪額度（見 `FILL_BUDGET`）
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
    def terrain(self):
        """目前這張圖的地形（沒載到回 None）。給呼叫端挑走位目標用。"""
        return self._terrain

    @property
    def here_map(self) -> str:
        """目前這條路線是從哪張圖算出來的（＝角色現在在哪）。"""
        return self._route_map

    @property
    def npc_hop(self):
        """正在等人（或等程式）跟哪個 NPC 講話。沒有回 None。"""
        return self._npc_wait

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
        self._hops.clear()
        self._entry_reach.clear()
        self._clear_warp()
        self._walker.clear()
        self.note = f"準備前往 {map_name}"

    def resume(self) -> None:
        """暫停回來了：把**用時間算的東西**歸零，路線與黑名單原封不動留著。

        ⚠⚠ 非做不可。這支狀態機有三個「逾時＝放棄」的計時器，全都是拿現在的
        時間減去起算時間：踩傳點放棄（`WARP_GIVEUP_SEC`）、等座標更新
        （`STALE_POS_SEC`）、等 NPC（`NPC_GIVEUP_SEC`）。暫停五分鐘再回來，
        它們會**一次全部到期** —— 症狀是傳點被誤判成踩不過去而列入黑名單、
        地形被誤判成讀錯、NPC 被誤判成不見了。
        那段時間是**我們自己停掉的**，不能算在它們頭上。

        走路那一段直接清掉：暫停期間人可能被伺服器帶完最後一段、也可能自己
        走開了，舊路徑不再有效。清掉之後下一拍會從**現在真的站的位置**重算。
        """
        now = self._now()
        self._stale_since = 0.0
        if self._warp_cell is not None:
            self._warp_since = now
            self._warp_try = 0
        if self._npc_wait is not None:
            self._npc_since = now
        self._walker.clear()

    def clear(self) -> None:
        self._goal_map = ""
        self._goal_cell = None
        self._route = []
        self._route_map = ""
        self._npc_wait = None
        self._clear_warp()
        self._walker.clear()

    # ---- 主迴圈每拍呼叫 ---------------------------------------------

    def _bouncing(self, to_map: str) -> bool:
        """是不是在兩張圖之間來回刷？

        ⚠⚠ 這不是「效率不好」，是**會把自己刷到斷線**。使用者實測：
        自動尋路走進一間店（`s_atelier`）之後又馬上出來、再進去…
        來回刷換圖，最後**整個連線被伺服器斷掉**，接著才是回連、卡登那一串。

        會這樣是因為換圖之後我們立刻重新規劃，而**腳下那道門就是新路線的
        第一段** —— 踩回去、被傳回來、再踩回去。

        正確的路線修法還沒有足夠證據（要看那幾拍的日誌），但
        「**把自己刷到斷線**」這件事本身就該擋 ——
        CLAUDE.md：失效模式只准「大聲停用」或「安全退化」，
        而一直做會造成傷害的動作兩種都不是。
        """
        now = self._now()
        self._hops.append((self._route_map, to_map, now))
        self._hops[:] = [h for h in self._hops if now - h[2] <= BOUNCE_WINDOW_SEC]
        pair = {self._route_map, to_map}
        back_and_forth = [h for h in self._hops if {h[0], h[1]} == pair]
        return len(back_and_forth) >= BOUNCE_LIMIT

    def update(self, map_name: str, pos: tuple[int, int]) -> str:
        if not self._goal_map:
            return "idle"
        if not map_name:
            return "walking"  # 換圖中間讀不到地圖名是正常過渡，不亂動

        # ⚠ 順序有意義：**地形與座標先確定，才輪到規劃路線**。
        # 挑傳點要看「從我站的地方走不走得到那道門」（室內圖一張地圖裡有好幾個
        # 互不相連的房間，見 `_gate_options`），沒有地形與可信的座標就挑不了。
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

        # 地圖名變了（或第一次跑）：這是唯一被承認的「過去了」訊號。
        if map_name != self._route_map:
            if self._route_map:
                log.info("換圖 %s → %s，重新規劃", self._route_map, map_name)
                if self._bouncing(map_name):
                    self.note = (
                        f"⚠ 在 {self._route_map} 與 {map_name} 之間來回了"
                        f" {len(self._hops)} 次 —— 多半是踩到腳下那道門又被傳回來。"
                        "已停止（再刷下去會被伺服器斷線）。"
                    )
                    self.clear()
                    return "blocked"
            if not self._replan(map_name):
                return "blocked"
            if not self._route and self._goal_cell is None:
                self.note = self._arrival_note(map_name, pos)
                self.clear()
                return "arrived"

        # 正在等人跟 NPC 講話：什麼都不做，連走路封包都不送。
        # 換圖了的話上面那段早就重新規劃、把這個狀態清掉了。
        if self._npc_wait is not None:
            return self._wait_for_npc()

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

    def _warp_avoid(
        self, map_name: str, keep: tuple[int, int] | None, *, wide: bool
    ) -> frozenset[tuple[int, int]]:
        """這張圖上「走過去的路上不准踩」的格子 —— 我們要踩的那道門除外。

        `wide=True` 是連傳點周圍都繞開（`warpzone.KEEP_OUT`），
        `wide=False` 只繞開傳點本體。分兩階是因為**擋過頭比不擋更糟**：
        禁區把窄路封死時整段會變成「走不到」，看起來就像地圖資料壞掉。
        """
        cells = warp_cells(map_name)
        if not cells:
            return frozenset()
        zone = keep_out(cells) if wide else frozenset(cells)
        if keep is None:
            return zone
        return zone - keep_out({keep}, radius=DOOR_CLEAR)

    def _path_to(
        self,
        terrain: MapTerrain,
        map_name: str,
        pos: tuple[int, int],
        goal: tuple[int, int],
    ) -> tuple[list[tuple[int, int]] | None, frozenset[tuple[int, int]]]:
        """算路徑，而且**不准穿過別的傳點**。回 (路徑, 這一段要避開的格子)。

        ⚠⚠ 使用者實測的斷線鏈就是從這裡開始的（細節見 `services/warpzone.py`
        檔頭）：A* 不知道傳點的存在，而**從門裡出來時人就站在門邊** ——
        只要目標在門的另一側，路徑第一格就是那道門。被傳回去、走出來、
        再被傳回去，來回刷到伺服器把連線切斷（WSA 10054）。

        擋不動就一階一階退（整片禁區 → 只擋傳點本體 → 完全不擋）：
        寧可繞遠也不能因為擋過頭就走不了路。**每退一階都留下紀錄** ——
        安靜地退回舊行為，等於這個修正沒發生過。
        """
        plans: list[frozenset[tuple[int, int]]] = []
        for wide in (True, False):
            avoid = self._warp_avoid(map_name, goal, wide=wide)
            if avoid and avoid not in plans:
                plans.append(avoid)
        plans.append(frozenset())
        for index, avoid in enumerate(plans):
            path = terrain.find_path(
                pos, goal, node_budget=NODE_BUDGET, blocked=avoid or None
            )
            if path is None:
                continue
            if not avoid and index:
                log.warning(
                    "⚠ %s 上從 %s 到 %s 只有**穿過別的傳點**才走得到 —— "
                    "這一段可能被傳到計畫外的地圖（真的被傳走會自動重新規劃）",
                    map_name, pos, goal,
                )
            return path, avoid
        return None, frozenset()

    def _plan(self, map_name: str, avoid: set[tuple[str, int, int]]):
        """算一條路線：**換最少張圖的那條，船／飛空艇／傳送師都算進來**。

        使用者指定：「自動尋路盡量選擇可以 NPC 傳送、走最少地圖的路線」。
        所以一律 `allow_npc=True` —— BFS 本來就在最小化換圖次數，把 NPC 連結
        放進去之後，只要搭一次船比繞十幾張野外圖近，它就會挑船。
        實測 `prontera → cmd_fild03`：純走路 26 段，加上飛空艇 18 段。

        ⛔ **舊行為（先試純走路，走得到就不麻煩人）已經拿掉。** 那是為了
        「不要為了少換一張圖就叫你去搭船」，但使用者要的正是相反的偏好。
        代價講清楚：NPC 那一段要對話（`travel_bot` 會自己講，看不懂選單就停
        下來等人），而傳送師要花錢。純走路的路線仍然在候選裡 —— 只是不再
        無條件優先。

        ⚠ 862 條 NPC 連結**每一條都有外觀編號**（實測），所以不必再分
        「講得動的」與「講不動的」兩層 —— 認人的資料一定有。
        """
        return plan_route(map_name, self._goal_map, avoid, allow_npc=True)

    def _dead_end(self, route: list[Hop]) -> Hop | None:
        """路線上哪一段**落地之後走不到下一道門**？沒有就回 None。

        ⚠⚠ 這是使用者實測「自動尋路走進一間店又走出來」的**根因**。
        `plan_route` 是**地圖層級**的 BFS：看到 `s_atelier` 上有一道門通往
        `rachel`，就以為「進得去 s_atelier 就走得到 rachel」。
        實際上 `s_atelier`（思念的工房）是**一張地圖裡四個互不相連的房間**，
        每間各自通往一座城，房間之間要再踩一次同圖內的傳點才過得去。實測：

            從 prontera 落在 (13,119)，這一塊只有 282 格 / 全圖 2179 格。
            走得到的門只有 (10,119)→prontera 與 (31,128)→s_atelier；
            (131,75)→rachel、(106,121)→yuno、(18,79)→lighthalzen 全部走不到。

        所以進去之後每一道門都算不出路，只能原路走回 prontera —— 那就是
        「進房子又回頭出來」。而落地點就在門邊，回頭那一步又踩回同一道門，
        來回刷到 **WSA 10054**（伺服器把連線切掉）。

        要在**還沒走過去之前**就發現，判斷用的是手上已經有的資料
        （`assets/terrain` ＋ `assets/warps.json.gz`），不是猜的。

        ⚠ 通往同一張圖的門要**全部**看過（常常有好幾道，見 `_gate_options`），
        只看 BFS 任意挑中的那一道會把好路誤判成死路。
        ⚠ 判斷不了（地形讀不到、泛洪預算用完）就**不擋**：「不確定」不等於
        「走不到」。沒驗到的段落等真的走到那張圖再驗 —— 那時它就是第一段。
        """
        for index, hop in enumerate(route):
            if self._fills <= 0:
                break  # 這一拍看到這裡為止，剩下的等走到那張圖再說
            if hop.npc:
                continue  # 要跟人講話才過得去：過去之後落在哪裡我們不知道
            nxt = route[index + 1] if index + 1 < len(route) else None
            if nxt is None:
                if self._goal_cell is None:
                    return None  # 進得了那張圖就算抵達，不必判斷房間
                wanted = [self._goal_cell]
            else:
                wanted = [nxt.cell]
                if not nxt.npc:
                    wanted += [
                        (x, y)
                        for x, y, dest, _dx, _dy in warps_on_map(hop.to_map)
                        if dest == nxt.to_map
                    ]
            if self._lands_within_reach(hop, wanted) is False:
                return hop
        return None

    def _lands_within_reach(
        self, hop: Hop, wanted: list[tuple[int, int]]
    ) -> bool | None:
        """踩完 `hop` 落地之後，`wanted` 裡有任何一格走得到嗎？不確定回 None。

        泛洪結果快取起來（同一趟裡同一個落地點只算一次），並且吃 `_fills` 預算。
        """
        try:
            terrain = self._load(hop.to_map)
        except GatError:
            return None
        land = nearest_walkable(terrain, (hop.to_x, hop.to_y), radius=START_SNAP)
        if land is None:
            return None
        key = (hop.to_map, land)
        reach = self._entry_reach.get(key)
        if reach is None:
            self._fills -= 1
            reach = terrain.reachable_from(land)
            self._entry_reach[key] = reach
        if not reach:
            return None
        for cell in wanted:
            spot = nearest_walkable(terrain, cell)
            if spot is not None and spot in reach:
                return True
        return False

    def _replan(self, map_name: str) -> bool:
        self._replans += 1
        if self._replans > MAX_REPLANS:
            self.note = f"⚠ 重新規劃 {MAX_REPLANS} 次仍到不了 {self._goal_map}，已放棄"
            return False
        # 「正在計算」要寫進日誌：BFS 跑得再快，使用者按下按鈕到看到第一步
        # 中間還是有一段空白。空白期沒有任何字，看起來就像沒反應。
        log.info("正在計算 %s → %s 的路線…（第 %d 次規劃）",
                 map_name, self._goal_map, self._replans)
        self.note = f"正在計算前往 {self._goal_map} 的路線…"
        # ⚠ BFS 是**地圖層級**的，它以為「進得去那張圖就走得到圖上任何一道門」。
        # 室內圖不是這樣（`s_atelier` 是一張圖四個互不相連的房間），所以算完
        # 要再問一次 `_dead_end`：落地之後真的走得到下一道門嗎？走不到就把
        # 那道門排除再算一次 —— **在還沒走過去之前**，不是走進去才發現。
        keep = set(self._avoid)   # 這一輪之前就有的黑名單（真的踩過去失敗的那些）
        self._fills = FILL_BUDGET
        route = None
        for _ in range(ROUTE_TRIES):
            route = self._plan(map_name, self._avoid)
            if route is None:
                break            # 繞不開了 —— 下面退回原本那條
            dead = self._dead_end(route)
            if dead is None:
                break
            log.info(
                "%s 的落地點 (%d,%d) 走不到接下來要走的門"
                "（一張地圖裡好幾個互不相連的房間）—— 不走 %s (%d,%d) 這道門，重算",
                dead.to_map, dead.to_x, dead.to_y, dead.from_map, dead.x, dead.y,
            )
            self._avoid.add(dead.key)
        else:
            route = None         # 試完 `ROUTE_TRIES` 條都是死路，一樣退回原本那條
        if route is None:
            # ⚠ 繞不開「落地在走不出去的房間」。**不要因此停掉整趟** ——
            # 那代表 `plan_route` 這種地圖層級的 BFS 表達不出「同一張圖裡要再踩
            # 一次內部傳點換房間」（GAMEDATA [DAT-032]），**不代表路真的不通**。
            # 退回原本那條照走：途中每進一張圖都會再驗一次，真的踩不過去還有
            # `_give_up_leg` 的黑名單與 `_bouncing` 收尾。
            excluded = self._avoid - keep
            self._avoid = keep
            route = self._plan(map_name, keep)
            if route is None:
                self.note = _no_route_note(map_name, self._goal_map, excluded=bool(keep))
                return False
            if excluded:
                log.warning(
                    "⚠ 到 %s 的路線繞不開「落地在走不出去的房間」（排除了 %d 道門"
                    "還是繞不掉）—— 先照原路走，進每一張圖之前會再驗一次",
                    self._goal_map, len(excluded),
                )
        self._route = route
        self._route_map = map_name
        # ⚠ 不清 `_terrain`：這支現在是在地形載好、座標確認過之後才被呼叫的，
        # 清掉的話 `_start_leg` 會拿到 None，變成「找不到目標格」。
        self._npc_wait = None    # 換圖了 = 那一段過去了（或人自己走去別的地方）
        self._clear_warp()
        self._walker.clear()
        if route:
            log.info("路線算好了：%d 段 —— %s", len(route),
                     " → ".join([map_name] + [hop.to_map for hop in route]))
            self.note = self._progress_note()
        else:
            log.info("已經在 %s 上了，只剩最後一段", map_name)
        return True

    def _arrival_note(self, map_name: str, pos: tuple[int, int]) -> str:
        """到了。順便**大聲說出**「這張圖分成好幾個互不相連的房間」這件事。

        遊戲的尋路目標只給**地圖名**（`navigation.py` 讀到的就是一個字串），
        但主城的室內圖是一張圖裡好幾間店：`prt_in` 實測 26 個互不相連的區塊、
        22 道各自獨立通往 prontera 的門。我們只保證進得了這張圖，
        進到的是不是你要的那一間**沒有資料可以判斷**（要有目標座標才行）。

        那就講清楚，不要安靜地宣告成功 —— CLAUDE.md：安靜地做錯事一律當 bug。
        """
        base = f"已抵達 {map_name}"
        terrain = self._terrain
        if terrain is None or self._goal_cell is not None:
            return base
        reach = self._reachable(terrain, pos)
        if not reach:
            return base
        # ⚠ 判準是「**還有幾個入口**落在我走不到的地方」，不是「這張圖有沒有
        # 不相連的區塊」。野外圖的邊角本來就有一堆走不進去的小口袋
        # （實測 prt_fild08 從第一格泛洪只有 799 / 90798 格），拿那個當判準
        # 等於每張圖都跳警告 —— 警告一旦每次都出現，就等於沒有警告。
        others = 0
        for cell in warp_landings_on(map_name):
            spot = nearest_walkable(terrain, cell, radius=3)
            if spot is not None and spot not in reach:
                others += 1
        # 只在**過半**的入口都通到我走不到的地方時才講。野外圖的角落常有一兩個
        # 走不進去的小口袋（實測 prt_fild08 是 1/6），那種每次都跳警告
        # 等於警告失效；過半才代表「你要的地方比較可能不在我站的這一塊」。
        if others * 2 <= len(warp_landings_on(map_name)):
            return base
        return (
            f"{base}，但⚠ 這張圖 {len(warp_landings_on(map_name))} 個入口裡有"
            f" {others} 個通到我**走不過去**的區域"
            f"（室內圖是一張地圖裡好幾間店）。遊戲的尋路目標只給地圖名、沒給座標，"
            f"我只能保證把你帶進這張圖 —— 是不是你要的那一間請自己確認"
        )

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
        self._reach = None       # 換圖了，上一張圖算出來的可走區塊不算數
        self._stale_since = 0.0  # 新地圖重新起算「等座標更新」
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

    def _gate_options(self, map_name: str) -> list[Hop]:
        """這張圖上通往**下一張圖**的所有傳點，目前選的那個排第一。

        ⚠ 為什麼不能只用 `route[0]`：`plan_route` 是 BFS，只挑「最少換圖」，
        同一個目的地有好幾道門時它挑到哪一道**完全是任意的**。
        主城的室內圖是一張地圖裡好幾間店：`prt_in` 實測 26 個互不相連的區塊、
        22 道各自獨立通往 prontera 的門。人站在藥水店裡時，只有那一間的門走得到；
        挑到別間的門，A* 回「走不到」，整段就被判失敗、傳點被列黑名單，
        然後下一拍再挑到另一道也走不到的門 —— 磨到 `MAX_REPLANS` 才大聲放棄。
        症狀正是使用者回報的「主城商店裡面沒辦法尋路」。

        黑名單（`_avoid`）裡的門不放進來：那些是**真的踩過不去**的，不是挑錯。
        """
        hop = self._route[0]
        if hop.npc:
            return [hop]  # 要跟 NPC 講話的那種：人站在哪就是哪，不能換一道門
        out = [hop]
        seen = {hop.cell}
        for x, y, dest, dx, dy in warps_on_map(map_name):
            if dest != hop.to_map or (x, y) in seen or (map_name, x, y) in self._avoid:
                continue
            seen.add((x, y))
            out.append(Hop(map_name, x, y, dest, dx, dy))
        return out

    def _reachable(self, terrain: MapTerrain, pos: tuple[int, int]) -> frozenset:
        """「從我站的這一格走得到哪些格」。同一塊區域只算一次。

        只在**第一道門走不到**的時候才算 —— 一般地圖第一道門就走得到，
        不必為了沒發生的問題每次泛洪整張圖。
        """
        cached = self._reach
        if cached is not None and pos in cached:
            return cached
        log.info("正在算 %s 上「從 (%d,%d) 走得到哪些格」…", self._terrain_map, *pos)
        reach = terrain.reachable_from(pos)
        self._reach = reach
        log.info("%s 上我這一塊有 %d 格，整張圖 %d 格可走",
                 self._terrain_map, len(reach), terrain.walkable_cells())
        return reach

    def _reachable_gates(
        self, terrain: MapTerrain, pos: tuple[int, int], hops: list[Hop]
    ) -> list[Hop]:
        """把走不到的門篩掉，剩下的由近到遠排。算不出可走區域就原封不動退回。"""
        reach = self._reachable(terrain, pos)
        if not reach:
            return list(hops)
        near = []
        for hop in hops:
            cell = nearest_walkable(terrain, hop.cell)
            if cell is not None and cell in reach:
                near.append(hop)
        near.sort(key=lambda h: max(abs(h.x - pos[0]), abs(h.y - pos[1])))
        return near

    def _start_leg(self, map_name: str, pos: tuple[int, int]) -> str:
        terrain = self._terrain
        if terrain is None:
            self.note = f"⚠ {map_name} 上找不到可以走到的目標格"
            return self._give_up_leg(map_name)

        if not self._route:
            # 最後一段：走到指定座標（沒指定座標的話上面早就回 arrived 了）
            goal = self._leg_goal()
            if goal is None:
                self.note = f"⚠ {map_name} 上找不到可以走到的目標格"
                return self._give_up_leg(map_name)
            if max(abs(goal[0] - pos[0]), abs(goal[1] - pos[1])) <= ARRIVE_RADIUS:
                return self._on_leg_done(pos)
            log.info("正在計算 %s 上從 %s 到目的地 %s 的路徑…", map_name, pos, goal)
            path, avoid = self._path_to(terrain, map_name, pos, goal)
            if not path:
                self.note = f"⚠ {map_name} 上走不到 {goal}"
                return self._give_up_leg(map_name)
            self._walker.set_path(path, avoid=avoid)
            self._walker.update(pos)
            self.note = self._progress_note()
            return "walking"

        # 這一段要走到「通往下一張圖的門」。同一個目的地常常有好幾道門，
        # 走得到的才算數（見 `_gate_options`）。
        options = self._gate_options(map_name)
        doors = len(options)     # 記下原本有幾道；options 之後會被篩掉一部分
        widened = False
        index = 0
        while index < len(options):
            hop = options[index]
            index += 1
            goal = nearest_walkable(terrain, hop.cell)
            if goal is None:
                continue  # 這道門周圍一格都站不上去，換下一道
            if max(abs(goal[0] - pos[0]), abs(goal[1] - pos[1])) <= ARRIVE_RADIUS:
                self._route[0] = hop
                return self._on_leg_done(pos)
            log.info("正在計算 %s 上從 %s 到 %s 傳點 %s 的路徑…",
                     map_name, pos, hop.to_map, goal)
            # ⚠ 路上**不准穿過別的傳點**：出門就站在門邊，不擋的話第一步就
            # 踩回去，來回刷到被伺服器斷線（見 `_path_to`）。
            path, avoid = self._path_to(terrain, map_name, pos, goal)
            if path:
                if hop != self._route[0]:
                    log.info("%s 上通往 %s 的門有 %d 道，從這裡走得到的是 %s",
                             map_name, hop.to_map, doors, goal)
                self._route[0] = hop
                self._walker.set_path(path, avoid=avoid)
                self._walker.update(pos)
                self.note = self._progress_note()
                return "walking"
            if not widened and len(options) > 1:
                # 第一道門走不到 → 先把「我這一塊通到哪」一次算出來，
                # 剩下的門用查表篩，不要一道一道去跑 A*。
                widened = True
                options = options[:index] + self._reachable_gates(
                    terrain, pos, options[index:]
                )
        self.note = f"⚠ {map_name} 上走不到任何一道通往 {self._route[0].to_map} 的傳點"
        return self._give_up_leg(map_name)

    def _on_leg_done(self, pos: tuple[int, int]) -> str:
        """這張圖上該走的走完了。還有下一段就開始踩傳點，不然就是到了。"""
        if not self._route:
            self.note = f"已抵達 {self._goal_map}"
            self.clear()
            return "arrived"
        hop = self._route[0]
        if hop.npc:
            # 這一段要跟 NPC 講話才過得去，我們不會對話 —— 走到他面前停下來等人。
            self._npc_wait = hop
            self._npc_since = self._now()
            self._walker.clear()          # 站住，別再往前走
            return self._wait_for_npc()
        self._warp_cell = hop.cell
        self._warp_since = self._now()
        self._warp_try = 0
        self.note = f"踩傳點前往 {hop.to_map}"
        return self._push_warp(pos)

    def _wait_for_npc(self) -> str:
        """停在 NPC 前面等人手動做完（搭船、傳送師、告示牌）。

        ⚠ **「過去了」的唯一依據是地圖名真的變成目的地那一張**
        （`update()` 開頭就會抓到並自動重新規劃）—— 不是等幾秒、也不是
        「應該講完了吧」。CLAUDE.md：逾時只能當放棄的上限，不能當成功的依據。

        期間完全不送走路封包：人在跟 NPC 對話時被拉著走，選單會被打斷。
        """
        hop = self._npc_wait
        if hop is None:
            return "walking"
        left = NPC_GIVEUP_SEC - (self._now() - self._npc_since)
        if left <= 0:
            self._npc_wait = None
            self.note = (
                f"⚠ 等了 {NPC_GIVEUP_SEC / 60:.0f} 分鐘還沒到 {hop.to_map}，已停止"
            )
            return "blocked"
        self.note = (
            f"⏸ 停在 {hop.from_map} ({hop.x},{hop.y})：請自己跟「{hop.npc}」"
            f"講話到 {hop.to_map}\n"
            f"到了我就自動繼續（還等 {left / 60:.0f} 分鐘）"
        )
        return "waiting"

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
        """這一段走不成：把當前傳點列黑名單並重新規劃。規劃不出來就 blocked。

        ⚠ **放棄的理由要留著**。`_no_route_note` 只會說「找不到通往 X 的路」，
        但真正的原因常常更具體（例如「這間房間的門一道都走不到」）——
        只印通用句的話，使用者看到的是一句跟遊戲畫面矛盾的話（傳點明明就在那）。
        """
        reason = self.note if self.note.startswith("⚠") else ""
        if self._route:
            self._avoid.add(self._route[0].key)
        self._clear_warp()
        self._walker.clear()
        self._route_map = ""  # 逼下一拍重新規劃
        # ⚠ 這裡也要 `allow_npc=True`，判準才跟 `_plan()` 一致。
        # 不一致的話會出現「其實有船可以搭，卻回報走不到」的假失敗。
        if plan_route(map_name, self._goal_map, self._avoid, allow_npc=True) is None:
            fallback = _no_route_note(map_name, self._goal_map, excluded=True)
            self.note = f"{reason}\n{fallback}" if reason else fallback
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
