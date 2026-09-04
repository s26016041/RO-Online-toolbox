"""角色座標：兩個來源，一個管「進圖時在哪」、一個管「走到哪了」。

## 為什麼換掉舊做法（GAMEDATA [MEM-047]）

舊版把座標錨在 `POSITION_X/Y_SIGS`（`cmp [x],ecx … mov [x],ecx`）。
2026-08-28 實機在 `izlude_in` 上讀到 **(112,181) 25 秒不變**，那是換圖前
`izlude` 的殘留 —— 而 `position_located` 照樣是 True，**很有自信地回錯值**。

靜態分析查出根因：那個全域在整個 33 MB 模組裡**只被兩條指令碰過**，
都在同一個函式（`ragexe+0xD7440`）裡，而那個函式是**小地圖畫標記**用的
（拿地圖寬高把格座標換算成螢幕座標、推顏色、做字串格式化）。
也就是說它是「我在小地圖上的那個點」，順便拿來當變化偵測器 ——
**不是角色位置的權威來源**。

對答案完全吻合：小地圖圖檔（`texture/…/map/<圖>.bmp`）只有 **742 張**，
我們有地形的是 **1082 張**。`izlude`／`izlu2dun` 有圖檔 → 全域正常；
`izlude_in` **沒有** → 從進圖到離開一次都沒被寫過。
**396 張（37%）沒有小地圖**，其中室內圖幾乎全中 —— `prt_in`、`payon_in01`
這些藥水店所在的圖都在裡面，所以這不是 `izlude_in` 特例。

⚠ 教訓：**特徵找得到 ≠ 找到的是對的東西。** 舊特徵每一項技術指標都漂亮
（1 處命中、四個立即值互驗、y 剛好 x+4），沒有人問過「這個全域的語意是什麼」。

## 客戶端根本沒有「目前在哪一格」這一個欄位

實機把整份記憶體翻過好幾遍才確定的事實：**位置分散在兩個地方**，
兩個都只在自己的時機正確。

- **進圖座標全域**（`MAP_ENTRY_*_SIGS`）
  - 對：伺服器 `0x0091` 說「你被移到這裡」的那一刻
  - 沒用：走了一步之後就過期，走再遠都不會變
- **移動元件**（`GID == AID` 的結構）
  - 對：在這張圖上動過之後，逐格精確
  - 沒用：剛換圖、還沒走過的時候整塊是空的

所以 `read()` 的順序是「**移動元件優先，沒有才用進圖座標**」——
而「沒有移動元件」與「還沒在這張圖上走過」是同一件事，所以這個退化是**準的**，
不是將就。

⚠ 但這個「準」有時效：**這張圖上一旦讀到過元件，進圖座標就永遠不能再用了**
（角色已經動過）。所以 `read()` 記著 `_moved_here` —— 之後元件再壞掉一律回 None，
不准退回去。少了這一條就是換個地方重演 [MEM-047]：回一個範圍內、站得住、
看起來完全合理的**錯**座標。

## 進圖座標全域（`ragexe` 靜態，用程式碼特徵定位）

寫入端有三處，兩處拿來當骨架（見 `signatures.MAP_ENTRY_X/Y_SIGS`）。
那個函式收的是「地圖名字串 ＋ x ＋ y ＋ 方向」，也就是 `0x0091`
（`ZC_NPCACK_MAPMOVE`）的內容 —— 同一段還寫了另一個全域 `0x15D2AC8`
（`std::string`，目前的地圖名）。

⚠ 它**不會**跟著走路更新（實機送一段移動、角色走了 9 格，這個全域紋風不動）。
[MEM-006] 當年把它（`HP-0x4290`）誤判成即時座標又推翻，就是因為這個性質；
現在它的角色很清楚：**只回答「進這張圖的時候我在哪」**。

## 移動元件（角色自己的實體）

角色在客戶端裡就是一個實體，版面跟怪物同一套（[MEM-014]）。**GID 就是 AID**，
而 AID 我們本來就讀得到（`StatusOffsets.aid`）—— 這是「存身分、當場查位置」：
掃記憶體找 `GID == AID` 的那塊，再用結構內容驗明正身，沒有任何寫死的位址。

| 偏移 | 型別 | 意義 |
|---|---|---|
| `+0x00`  | u32 | GID＝AID |
| `+0x38`  | u32 | 狀態（站著 1、走路 2）；**被回收時清成 0** |
| `+0x5C` / `+0x60` | i32 | **移動終點**；沒在走的時候就是目前所在格 |
| `+0x110` / `+0x114` | ptr | 路徑陣列的 begin／end（節點 0x10 bytes，開頭是 x,y）|
| `+0x120` / `+0x124` | f32 | 這一段移動的**起點格** |
| `+0x12C` | i32 | **目前走到路徑第幾個節點**，沒在走是 **-1** |
| `+0x130` | f32 | 累計里程（世界單位，每格 5；斜走 5√2）|

「現在在哪一格」只有兩行：

    idx < 0  → 讀 +0x5C/+0x60（站著，終點就是現在位置）
    idx >= 0 → 讀 路徑[idx]（走路中，這是客戶端認定的當下格）

實機驗證（自己複製 socket 送 `0x035F`，記憶體 30 Hz 取樣）：
送出 (65,92)→(65,104) 之後 `+0x5C/+0x60` **0.03 秒就變成終點**，
而 `+0x12C` 從 0 一路數到 11，解出來是 (65,93)、(65,94)…(65,115)，
每格約 0.15 秒，走完回 -1、終點欄位停在 (65,116)。

⚠ **`+0x5C/+0x60` 是伺服器驅動的**：整個過程客戶端沒有被點擊過
（封包是我們自己送的），它仍然更新了 —— 代表它是收到 `0x0087` 才寫的。
所以「移動被靜默拒絕」時它**不會**動，這正是我們要的性質。

## 怎麼分辨活的移動元件與殘留的

⚠⚠ **「只有一個候選」不等於「這個候選是對的」（[DAT-072]）。**
換圖那一刻新元件還沒填好（狀態欄是 0）、舊元件還沒被回收（三關全過），
所以「通過驗證的剛好一個，而且是上一張圖的殘留物」是**常態**，不是例外。
分辨用的關卡（`_near_reference`）因此要當**過濾**每次都跑，
挑人（`_ticking`／`_closest`）才是平手時的 tie-break。

換一次圖，客戶端就丟掉舊元件、另外配一個新的；**舊的不會被清乾淨**，
GID、座標都還在原地。實測一個行程裡同時有 **60~69 塊** `GID == AID` 的記憶體，
其中好幾塊的座標還落在當下地圖的可走格上。三道關卡一起用：

1. **`+0x38 != 0`**。實測四塊殘留元件（座標分別是 (110,182)、(249,42)、
   (198,205)、(65,84)，第一個就是換圖前 izlude 的位置）**全部是 0**，
   活的是 1（站著）或 2（走路中）。
2. **座標要站得住**（跟 `Traveler._settle()` 同一個判準：`START_SNAP` 格內
   有可走格）。兩層用不同判準的縫就是 [PKT-078] 卡住的地方。
3. **通過驗證的只能有一個**，不然大聲失敗（[MEM-041]：命中多個 ≠ 方法壞了，
   但驗完還分不出來就不准賭）。

## 試過但不能用的驗證欄位

- `-0x24`（怪物的存活旗標）在角色身上**會閃爍**：實機 150 秒裡多次
  1 → 0 → 1，每次只持續一拍。
- `+0x110`（路徑陣列指標）**只在「這張圖上走過路」之後才有值**。
  剛傳過來的角色那裡是 0 —— 拿它當存活旗標的話一換圖就整個定位失敗
  （實機症狀：「64 個 GID 命中都沒通過驗證」，走路功能全停）。
- `+0x134` 是個 tick，但**不是「最後更新時間」**：走路時它是**未來**的值
  （實測領先現在 965 ms）。所以它**不能拿來挑人**（誰的 tick 最新誰就是本人）。

  ⚠ 但它**可以拿來排除**：2026-09-01 重量一次（三隻角色、各 120 筆、30 秒、
  全程站著不動）—— 活的元件 tick 差 **-9 ~ 22 ms**，**0 筆**超過
  `actor.FRESH_MS`(2000)。也就是說舊筆記寫的「站久了又會過期」沒有重現，
  而殘留物的 tick 是真的停住的。所以 `_ticking()` 只在「好幾個候選同時
  驗過」時用來剔掉明顯停住的那些，剔不出來就什麼都不做（見 `_locate_component`）。

## 為什麼一定要有那道 tick 關卡（[DAT-061]）

「離上一次讀到的位置最近」這條 tie-break 對殘留物有**系統性偏心**：
被丟下的元件不會動，剛好就停在我們上一次讀到的那一格，而真的那個已經走開了。
實機 2026-09-01（補水走到 prt_fild05）：落地 (20,333) 之後座標**7 秒不變**，
`travel_bot` 判定「移動連續被伺服器忽略、角色一步都沒動」放棄那家店；
下一拍換挑另一個候選，座標立刻變成 (30,333) —— 人早就走了 10 格。
"""

from __future__ import annotations

import logging
import struct
import time

import numpy as np

from ro_toolbox.services import actor
from ro_toolbox.services.aob import locate_global
from ro_toolbox.services.mapdata import GatError, has_terrain, load_terrain
from ro_toolbox.services.memory_scan import MemoryScanner
from ro_toolbox.services.signatures import (
    MAP_ENTRY_X_SIGS,
    MAP_ENTRY_XY_GAP,
    MAP_ENTRY_Y_SIGS,
)
from ro_toolbox.services.travel import START_SNAP, nearest_walkable

log = logging.getLogger(__name__)

# ---- 移動元件的結構偏移 ---------------------------------------------------
#
# ⚠⚠ **定義在 `services/actor.py`，這裡只是取用。** 角色與怪共用同一套版面，
# 以前兩邊各寫一份，結果 `entities.py` 把 `+0x110` 當成「繪圖物件指標」、
# 把 `+0x120` 的 float 當成「現在站哪」—— 兩條這個檔早就寫著是錯的，
# 但沒有任何東西會發現（[MEM-058]）。要改偏移請改 `actor.py`。
OFF_STATE = actor.STATE            # u32 狀態：站著 1、走路 2、被回收 0
OFF_DEST_X = actor.DEST_X          # i32 移動終點 x（沒在走＝目前格）
OFF_DEST_Y = actor.DEST_Y          # i32 移動終點 y
OFF_PATH_BEGIN = actor.PATH_BEGIN  # 路徑陣列 begin（⚠ 沒走過路時是 0）
OFF_PATH_END = actor.PATH_END      # 路徑陣列 end（標準 vector 版面）
OFF_PATH_INDEX = actor.PATH_INDEX  # i32 目前走到第幾個節點，-1 = 沒在走
PATH_STRIDE = actor.PATH_STRIDE    # 一個路徑節點的大小，前 8 bytes 是 (i32 x, i32 y)

#: 讀一次要抓多少 bytes：從 GID 一路到路徑索引。
SPAN = OFF_PATH_INDEX + 4

MAX_STATE = actor.MAX_STATE
MAX_CELL = actor.MAX_CELL
MAX_PATH_NODES = actor.MAX_PATH_NODES
#: 一直找不到元件多久之後要大聲抱怨一次。
#:
#: ⚠ 這是**改版時唯一的警報**：結構偏移壞掉的話元件永遠驗不過，
#: `read()` 會一直回進圖座標 —— 範圍內、站得住、看起來完全合理，
#: 但角色早就走到別的地方了。那正是 [MEM-047] 的形狀。
#: 站在原地不動也會走到這條路，所以**只警告一次、不停用**（安全退化 ＋ 大聲）。
STALE_WARN_SEC = 30.0
#: 重驗候選的間隔。實測 67 個候選重驗一輪只要 **0.30 ms**，所以可以很密。
RELOCATE_COOLDOWN = 0.3
#: 還是找不到元件時，多久才准重新全掃一次。全掃實測 **0.6~0.8 秒**
#: （508 MB，其中 0.57 秒是 ReadProcessMemory 本身，沒得再快）。
#: 正常情況一張圖只會全掃一次（`invalidate()` 之後那一次）。
FULL_RESCAN_SEC = 3.0


#: 上一次讀到的位置多久之內還算得上參考點（見 `_closest`）。
#: 太舊的話角色早就走遠了，那時候寧可用伺服器給的進圖座標。
_REF_FRESH = 5.0

#: 角色每秒最多能移動幾格 —— 拿來把「離參考點不可能這麼遠」的候選擋掉
#: （見 `_near_reference`）。RO 走一格最快約 100 ms（加速術＋加速藥水），
#: 也就是 10 格/秒；這裡取**兩倍**當上限：這道關卡是用來擋掉差了幾百格的
#: 上一張圖殘留物，不是用來卡精度的，寧可放過也不要誤殺活的元件。
_DRIFT_CELLS_PER_SEC = 20.0
#: 起步的寬限（伺服器講的落點與客戶端第一拍本來就可能差幾格）。
_DRIFT_SLACK = 10


def _cell_ok(x: int, y: int) -> bool:
    return 0 < x < MAX_CELL and 0 < y < MAX_CELL


class PlayerPosition:
    """角色現在站在哪一格。問不出可信答案就回 None —— **絕不回殘留值**。

    `locate()` 做兩件事：用程式碼特徵找進圖座標全域（很快），
    再全掃一次記憶體找移動元件（0.6~0.8 秒；剛換圖時可能還驗不出來，不算失敗）。
    **全掃只做一次**：元件在角色走第一步之前就帶著 `GID == AID` 了，
    所以之後只重驗那份候選清單（0.3 ms）。
    `read()` 只讀 0x130 bytes，每次都重驗，所以換圖或元件被回收時會**當場發現**。
    """

    def __init__(self, scanner: MemoryScanner, now=time.monotonic) -> None:
        self._scanner = scanner
        self._now = now
        self._aid = 0
        #: 移動元件的位址（每次開遊戲都不一樣，只活在記憶體裡）
        self._addr: int | None = None
        #: 進圖座標全域的位址（程式碼特徵定位出來的）
        self._entry: int | None = None
        self._last_locate = 0.0
        #: 上一次是不是已經抱怨過元件失效了（別每一拍噴一行）
        self._complained = False
        #: 「還沒找到元件」這句話講過了沒。這條路每 0.3 秒走一次，不擋會洗版。
        self._said_missing = False
        #: 上一次讀成功的位置與時刻。**位置在時間上是連續的** ——
        #: 兩拍之間角色跑不了多遠，所以它是分辨「哪個候選才是本人」最好的參考。
        self._last_pos: tuple[int, int] | None = None
        self._last_pos_at = 0.0
        #: 「有好幾個候選」講過了沒（不擋就是每拍一行 ERROR）。
        self._said_many = False
        #: 「有候選離參考點太遠被擋掉」講過了沒（同上，每 0.3 秒會走一次）。
        self._said_far = False
        #: **我們親眼看到**伺服器把角色移走的時刻（`invalidate()` 寫的）。
        #: `None` = 這輩子還沒看過 —— 那時進圖座標只是「上次進圖時在哪」，
        #: 角色早就走遠了，**不能**拿它當「離落點不可能太遠」的錨（見 `_near_reference`）。
        self._warped_at: float | None = None
        #: 最後一次 `read()` 回的是不是**即時**座標（移動元件）。
        #: False = 回的是進圖座標，那個值**角色走了也不會變**——
        #: 呼叫端的「卡住偵測」不可以拿它當「有沒有在動」的依據（見 farm_bot）。
        self._live = False
        #: 上次全掃找到的**所有** `GID == aid` 位址。之後只重驗這一份（0.3 ms）。
        #: ⚠ 這是整個設計的關鍵，見 `_locate_component()` 的說明。
        self._candidates: list[int] = []
        self._last_full = 0.0
        #: 從什麼時候開始「這張圖上一直沒有元件」（None = 現在有）。
        self._missing_since: float | None = None
        self._warned_missing = False
        #: 在**這張圖**上讀到過移動元件了嗎？
        #: 讀到過就代表角色已經在這張圖上動過 —— 那一刻起「進圖座標」就過期了，
        #: 元件再壞掉也**不准**退回去用它（見 `read()`）。
        self._moved_here = False
        #: 上一次 `read()` 是為哪一張圖問的（自己偵測換圖用，見 `read()`）。
        self._read_map = ""
        #: 上一次看到的進圖座標。**它一變就是伺服器把我們移動了**。
        self._entry_seen: tuple[int, int] | None = None
        #: 地形快取：驗證「這一格站得住嗎」用，換圖才重載
        self._terrain_map = ""
        self._terrain = None

    # ---- 定位 -------------------------------------------------------

    @property
    def live(self) -> bool:
        """最後一次讀到的是**即時**座標嗎？

        `False` 代表回的是進圖座標 —— 範圍內、站得住、看起來完全合理，
        但**角色走了它也不會變**。誰拿它當「有沒有在動」的依據，
        誰就會在角色明明在跑的時候判定「卡住了」（[MEM-054]）。
        """
        return self._live

    @property
    def located(self) -> bool:
        """有沒有任何一個來源可用。兩個都沒有才算走路類功能不能用。"""
        return self._entry is not None or self._addr is not None

    @property
    def address(self) -> int | None:
        """移動元件的位址。**只供顯示與除錯**，不要存檔。"""
        return self._addr

    @property
    def entry_address(self) -> int | None:
        return self._entry

    def locate(self, aid: int) -> bool:
        self._aid = aid
        self._entry = self._locate_entry()
        self._locate_component()
        if not self.located:
            log.error("角色座標兩個來源都定位不到 —— 走路類功能將停用")
        return self.located

    def _locate_entry(self) -> int | None:
        """進圖座標全域：兩條互相獨立的骨架 ＋ `y == x+4`，任一項不符就回 None。"""
        x = locate_global(self._scanner, MAP_ENTRY_X_SIGS)
        y = locate_global(self._scanner, MAP_ENTRY_Y_SIGS)
        if x is None or y is None:
            log.error("進圖座標全域定位失敗（遊戲可能已改版）")
            return None
        if y - x != MAP_ENTRY_XY_GAP:
            log.error(
                "進圖座標全域不一致：x=%#x y=%#x（相差 %d，應為 %d）—— 判定為解錯",
                x, y, y - x, MAP_ENTRY_XY_GAP,
            )
            return None
        log.info("進圖座標全域定位於 %#x", x)
        return x

    def _locate_component(self, map_name: str = "") -> bool:
        """在候選裡找出角色的移動元件。**通過驗證的必須剛好一個**。

        ## 為什麼只要全掃一次（實機量出來的關鍵事實）

        **元件物件在角色走第一步之前就已經帶著 `GID == AID` 了** ——
        只是狀態與終點欄位還沒填。實機兩次驗證：剛傳過去、還沒動的時候
        先把所有 `GID == AID` 的位址拍下來，再送一個移動，
        **真正動起來的那一塊本來就在那份快照裡**。

        所以：換圖之後全掃**一次**把候選拍下來（0.6~0.8 秒），之後每次只重驗
        那 60~70 個候選（**0.30 ms**）—— 角色一走第一步就會被接上。

        ⚠⚠ 第一版不是這樣做的，是「只掃上次有命中的區段、全掃 15 秒一次」。
        結果實機一換圖就中招：新元件配在冷區段裡，於是**整整 15 秒都讀進圖座標**，
        而角色其實正在走 —— `travel_bot` 看到「位置一直沒變」就判定
        「一步都沒動、可能是背包太重」把趕路停掉（[DAT-042] 那條判斷本身沒錯，
        錯的是餵給它的座標）。**便宜的快取不能拿正確性去換。**

        找不到元件**不是錯誤**：剛換圖、還沒在這張圖上走過的時候本來就找不到
        （狀態欄位是 0）。那時候由進圖座標全域回答。
        """
        self._addr = None
        self._last_locate = self._now()
        if not (0 < self._aid < 0x7FFF_FFFF):
            log.error("角色 AID 讀不到（%r），移動元件無法定位", self._aid)
            return False
        # 候選清單空了（換圖／第一次），或候選裡怎麼樣都驗不出來 —— 重新全掃。
        if not self._candidates or self._now() - self._last_full >= FULL_RESCAN_SEC:
            self._last_full = self._now()
            self._candidates = self._scan(self._aid)
        looked = {a: self._look_at(a) for a in self._candidates}
        seen = {a: cell for a, (_mine, cell) in looked.items() if cell is not None}
        good = [a for a, (mine, cell) in looked.items() if mine and cell is not None]
        # ★ 「本人，但這張圖上還沒走過」—— 終點欄位還沒被寫過，所以它說不出
        #   自己在哪，但它**就是**我們的元件（[MEM-060]）。留著當備胎：
        #   有說得出位置的就用那些，一個都沒有時才輪到它。
        unplaced = [a for a, (mine, cell) in looked.items() if mine and cell is None]
        # ★ 先用「離參考點不可能這麼遠」把上一張圖的殘留物擋掉 ——
        #   **這一關要在挑人之前做**，因為剛換圖的時候通過驗證的常常只有一個，
        #   而那一個就是殘留物（見 `_near_reference`）。
        good = self._near_reference(good, seen, map_name)
        if len(good) > 1:
            # ⚠⚠ **先把「動作 tick 已經停住」的那些剔掉。**
            #
            # 「離上一次的位置最近」（下面那條）有一個很難發現的偏心：
            # **被丟下的元件不會動，剛好就停在我們上一次讀到的那一格** ——
            # 而真的那個已經走開了。於是換圖之後只要同時有兩個候選，
            # 那條規則會**系統性地**挑到殘留的那個，而且它「範圍內、站得住」，
            # 一路驗得過，沒有任何人會發現。
            #
            # 實機 2026-09-01（補水走到 prt_fild05）：落地 (20,333) 之後
            # 座標**整整 7 秒不變**，`travel_bot` 判定「移動連續被伺服器忽略、
            # 角色一步都沒動」而放棄那家店；下一拍換挑另一個候選，座標立刻
            # 變成 (30,333) —— 人早就走了 10 格（GAMEDATA [DAT-061]）。
            #
            # tick 是**排除**用的，不是選擇用的：全部看起來都停住時什麼都不做
            # （安全退化），只有真的分得出來才縮小範圍。判準與怪物那邊同一份
            # （`actor.FRESH_MS`，[MEM-059] 對 1007 筆活體取樣 0 誤殺）。
            alive = [a for a in good if self._ticking(a)]
            if alive and len(alive) < len(good):
                log.info("移動元件候選 %d 個，其中 %d 個的動作 tick 已經停住"
                         "（被丟下的殘留），只留還在跳的那些",
                         len(good), len(good) - len(alive))
                good = alive
        if len(good) > 1:
            # ⚠⚠ **不要因為「有兩個」就整個放棄。** 實機 2026-08-30：兩個候選
            # 同時驗過（`['0x2ed44750', '0x3f27c5d8']`），舊版直接回退到進圖座標
            # —— 那個值角色一移動就是錯的，於是走位、打怪、脫離傳點全部瞎掉，
            # 使用者回報「死了，完全不能用」。而且那句 ERROR 沒節流，
            # **一秒印六行**，把真正該看的訊息全沖掉。
            #
            # tick 都分不出來時的判準是**位置在時間上是連續的**：兩拍之間
            # （0.2~0.3 秒）角色跑不了多遠，所以「離上一次讀到的位置最近」的
            # 那個才是本人；被回收的舊元件會停在別的地方（多半是上一張圖）。
            # 剛換圖還沒有上一次的話，用伺服器給的**進圖座標**當參考 ——
            # 那是伺服器剛剛才講過的落點，權威度最高。
            picked = self._closest(good, seen)
            if picked is not None:
                if not self._said_many:
                    self._said_many = True
                    log.info(
                        "有 %d 個移動元件都像是角色本人（%s）—— "
                        "用「離上一次的位置最近」挑了 %s",
                        len(good), [hex(a) for a in good], hex(picked),
                    )
                good = [picked]
            else:
                # 連參考點都沒有（剛接上、進圖座標也讀不到）—— 那才真的分不出來。
                # ⚠ 節流：這條每 0.3 秒就會走一次。
                if not self._said_many:
                    self._said_many = True
                    log.error(
                        "有 %d 個移動元件都像是角色本人（%s），而且沒有參考點"
                        "可以分辨，只好改用進圖座標",
                        len(good), [hex(a) for a in good],
                    )
                return False
        if not good and unplaced:
            # 剛換圖、還沒走第一步：只有「還沒走過」的形狀認得出本人。
            if len(unplaced) > 1:
                if not self._said_many:
                    self._said_many = True
                    log.error(
                        "有 %d 個「還沒走過」的元件都像是角色本人（%s）—— 分不出來，"
                        "先用進圖座標", len(unplaced), [hex(a) for a in unplaced],
                    )
                return False
            self._addr = unplaced[0]
            self._complained = False
            self._said_missing = False
            self._said_far = False
            log.info(
                "角色移動元件定位於 %#x（AID %d，%d 個候選）—— "
                "這張圖上還沒走過，位置先用進圖座標，走第一步就會變即時的",
                self._addr, self._aid, len(self._candidates),
            )
            return True
        if not good:
            # ⚠ 這條路每 0.3 秒就會走一次（`RELOCATE_COOLDOWN`）——
            #   每次都印的話是**一秒三行**的洗版，三個分身同時開著更慘，
            #   而且會把真正該看的訊息沖掉（使用者實測回報）。
            #   狀態沒變就閉嘴：找到元件時 `_said_missing` 會被清掉，
            #   下次再掉出來才會再說一次。
            if not self._said_missing:
                self._said_missing = True
                log.info(
                    "還沒找到角色的移動元件（AID %d，%d 個候選）"
                    "—— 剛換圖還沒走過路的話這是正常的，走一步就會接上，先用進圖座標",
                    self._aid, len(self._candidates),
                )
            return False
        self._addr = good[0]
        self._complained = False
        self._said_missing = False
        self._said_far = False
        log.info("角色移動元件定位於 %#x（AID %d，%d 個候選）",
                 self._addr, self._aid, len(self._candidates))
        return True

    def invalidate(self) -> None:
        """丟掉記著的元件位址**與候選清單**，下一次 `read()` 會重新全掃。

        換地圖時一定要呼叫：客戶端會把舊元件回收，而**回收不等於清乾淨** ——
        GID 可能還在原地，只是不再更新（[MEM-047] 那個「很有自信的錯值」
        就是這樣來的）。與其賭它有沒有被重用，不如當場重找。
        """
        self._addr = None
        self._last_locate = 0.0
        # ⚠ 候選也要丟：換圖之後客戶端會另外配一個新元件，
        #   舊的候選清單裡沒有它（第一版就是漏了這一步，害整整 15 秒讀到舊值）。
        self._candidates = []
        self._last_full = 0.0
        # 新的一張圖：進圖座標又變成可信的了，直到角色在這裡走第一步。
        self._moved_here = False
        self._missing_since = None
        self._warned_missing = False
        # ⚠ 換圖了，上一張圖的位置**不能**再拿來當參考點（見 `_closest`）——
        # 那正是「被回收的舊元件」會停在的地方，拿它比對等於挑到舊的那個。
        self._last_pos = None
        self._last_pos_at = 0.0
        self._said_many = False
        self._said_far = False
        # ★ 我們**親眼看到**伺服器把角色移走了 —— 從這一刻起，進圖座標就是
        #   「角色現在在哪」的權威答案，而且角色只能用走的離開它。
        #   `_near_reference()` 靠這個把上一張圖的殘留元件擋在外面。
        self._warped_at = self._now()

    def forget(self) -> None:
        """完全重置（換行程／收攤時用）。"""
        self.invalidate()
        self._aid = 0
        self._entry = None
        self._entry_seen = None
        self._read_map = ""
        self._terrain_map = ""
        self._terrain = None
        self._complained = False
        # 換行程了，上一個行程的「剛被移動」不算數。
        self._warped_at = None

    # ---- 讀取 -------------------------------------------------------

    def read(self, map_name: str = "") -> tuple[int, int] | None:
        """回目前所在格。兩個來源都問不出可信答案就回 None。

        `map_name` 是**現在這張圖**（呼叫端從角色結構讀）。有它才驗得了
        「這一格站得住嗎」—— 那是分辨「上一張圖的殘留值」最有效的一關。

        ⚠⚠ **開頭那兩道偵測不是多餘的。**

        使用者 2026-09-03：「常常換圖抓不到座標，右上角地圖明明都及時會換」。
        舊版把「換圖了」這件事**完全外包給呼叫端**（`character._note_map()` 比對
        角色結構裡的地圖名，變了才呼叫 `invalidate()`）。可是換圖那一刻有三件事
        各自發生、順序不保證：

        1. 客戶端**回收舊的移動元件** → `_component_pos()` 開始回 None
        2. 伺服器的 `0x0091` 寫**進圖座標全域**（右上角小地圖也是這一刻換的）
        3. **角色結構裡的地圖名**變成新的那張

        只要 1 早於 3，中間那段時間 `_moved_here` 還記著上一張圖 ——
        於是 `read()` 走到「這張圖上動過了，進圖座標不可信」那條，**回 None**。
        呼叫端看到的就是「讀不到角色座標」，然後開始送移動去逼位置出來
        （實機 22:06:26 起連送 657 個目標）。

        所以這裡自己看得到的兩個訊號都要用：地圖名變了、**或**進圖座標變了
        （後者跟小地圖同一刻，不需要等任何人通知，也不需要接到封包）。
        """
        self._watch_for_a_move(map_name)
        pos = self._component_pos()
        if pos is None and self._addr is None and self._can_relocate():
            self._locate_component(map_name)
            pos = self._component_pos()
        if pos is not None and self._on_map(pos, map_name):
            self._moved_here = True
            self._missing_since = None
            self._warned_missing = False
            self._said_many = False      # 分得出來了，下次再平手要重講一次
            self._said_far = False
            self._live = True
            # 記下來當「位置是連續的」那個參考點（見 `_closest`）。
            self._last_pos = pos
            self._last_pos_at = self._now()
            return pos
        self._live = False
        if self._moved_here:
            # ⚠⚠ 這張圖上已經讀到過元件 = 角色已經動過 = **進圖座標早就過期了**。
            #    這時候退回去用它，就是換一個地方重演 [MEM-047]：
            #    回一個範圍內、站得住、看起來完全合理的**錯**座標。
            #    寧可回 None 讓呼叫端停下來。
            return None
        entry = self._entry_pos()
        if entry is not None and self._on_map(entry, map_name):
            if self._addr is None:
                self._note_missing()
            else:
                # 元件綁著，只是角色還沒在這張圖上走過 —— 這是**正常狀態**，
                # 不是「找不到」，更不是改版（[MEM-060]）。不准喊狼來了。
                self._missing_since = None
                self._warned_missing = False
            return entry
        return None

    def _watch_for_a_move(self, map_name: str) -> None:
        """自己發現「伺服器把我們移動了」，不等呼叫端通知。

        兩個訊號，任一個成立就當場 `invalidate()`：

        - **地圖名變了**：呼叫端每一拍都從角色結構讀進來，比對一下不花錢。
          呼叫端（`character._note_map()`）通常已經先做過了 —— 那時候
          `_moved_here` 是 False、`_addr` 是 None，這裡就不會重做一次。
        - ★ **進圖座標全域變了**：那個全域只有 `0x0091`（伺服器說「你被移到
          這裡」）會寫，也就是右上角小地圖換掉的**同一刻**。它不依賴角色結構
          的地圖名什麼時候更新，也不需要封包擷取接得到 —— 連線正在重綁、
          封包漏接的時候照樣準（實機 22:06 那次就是封包沒接到）。

        ⚠ 同一張圖上被傳走（同圖傳點）也會寫，那**也**是「被移動了」，
        一樣該重找元件 —— 不是誤判。
        """
        entry = self._entry_pos()
        # ⚠ 第一次讀到不算「變了」—— 還沒有基準線可以比。把它記下來就好，
        #   不然每一個剛建好的 PlayerPosition 都會先白白重掃一次。
        moved = (entry is not None and self._entry_seen is not None
                 and entry != self._entry_seen)
        if entry is not None:
            self._entry_seen = entry
        # ⚠ 跟上面的 `_entry_seen` 同一個道理：**第一次讀到地圖名不算「換圖」**。
        #   少了這一條，每個剛建好的 `PlayerPosition` 第一拍就會 `invalidate()`
        #   一次（實機 11:09:57「偵測到角色被移動（換圖）」就是這樣來的），
        #   於是 `_warped_at` 被設成「剛剛」—— 但角色其實在這張圖上待很久了、
        #   早就離進圖座標很遠。那會讓 `_near_reference()` 拿一個**假的**落點
        #   當錨，把**活的**元件當成殘留物剔掉。
        changed_map = bool(map_name) and bool(self._read_map) and map_name != self._read_map
        if map_name:
            self._read_map = map_name
        if not (moved or changed_map):
            return
        # 已經是乾淨狀態（呼叫端剛 invalidate 過）就不要再重掃一次。
        if self._addr is None and not self._moved_here:
            return
        log.info("座標來源：偵測到角色被移動（%s）—— 重新定位移動元件",
                 "換圖" if changed_map else f"進圖座標變成 {entry}")
        self.invalidate()

    def moving(self) -> bool | None:
        """客戶端認為角色**現在正在走**嗎？讀不到回 None。

        ⚠ 呼叫端不可以把 None 當成「站著」。這個值的用途是「不要在角色
        明明還在走的時候重送移動封包」（見 `services/walker.py`）——
        讀不到就退回原本的計時器判斷，那是安全的那一邊。

        為什麼可信：`+0x38`／路徑索引是**伺服器確認 `0x0087` 之後**才寫的
        （見本檔開頭的實機驗證），所以「客戶端說在走」代表這一段真的被接受了；
        被怪打斷時客戶端會停下來，這裡就會變成 False，重送照樣會發生。
        """
        if self._addr is None:
            return None
        raw = self._scanner.read_region(self._addr, SPAN)
        if raw is None or len(raw) < SPAN:
            return None
        buf = bytes(raw)
        gid, = struct.unpack_from("<I", buf, 0)
        if gid != self._aid:
            return None
        state, = struct.unpack_from("<I", buf, OFF_STATE)
        if not (0 < state <= MAX_STATE):
            return None
        index, = struct.unpack_from("<i", buf, OFF_PATH_INDEX)
        begin, = struct.unpack_from("<I", buf, OFF_PATH_BEGIN)
        return index >= 0 and begin != 0

    def _note_missing(self) -> None:
        """一直沒有元件 → 講一次話。改版把結構偏移弄壞時這是唯一的警報。"""
        now = self._now()
        if self._missing_since is None:
            self._missing_since = now
            return
        if self._warned_missing or now - self._missing_since < STALE_WARN_SEC:
            return
        self._warned_missing = True
        log.warning(
            "已經 %.0f 秒找不到角色的移動元件，現在回報的是**進圖座標** ——"
            "角色如果有在移動，這個值就是錯的。"
            "站著不動的話這是正常的；一直出現代表遊戲改版讓結構偏移失效了"
            "（見 GAMEDATA [MEM-048]）",
            now - self._missing_since,
        )

    def _component_pos(self) -> tuple[int, int] | None:
        """從記著的元件位址讀一次；**不是本人**才把位址丟掉（下一拍重找）。

        ⚠ 「本人但這張圖上還沒走過」不算驗不過（[MEM-060]）——
        那時候位置由進圖座標回答，但**綁定要留著**：角色一走第一步，
        同一個位址的終點欄位就有值了，不必再全掃一次（0.6~0.8 秒）。
        """
        if self._addr is None:
            return None
        mine, pos = self._look_at(self._addr)
        if mine:
            return pos          # None = 本人，只是還不知道自己在哪
        if not self._complained:
            log.warning("角色移動元件 %#x 已失效（換圖或被回收），重新定位中",
                        self._addr)
            self._complained = True
        self._addr = None
        return None

    def _can_relocate(self) -> bool:
        return bool(self._aid) and self._now() - self._last_locate >= RELOCATE_COOLDOWN

    def _closest(self, good: list[int], seen: dict) -> int | None:
        """好幾個候選都像本人時，挑**離參考點最近**的那個。沒有參考點回 None。

        參考點的優先序：

        1. **上一次讀成功的位置** —— 位置在時間上是連續的，兩拍之間
           （`RELOCATE_COOLDOWN` 0.3 秒）角色跑不了多遠。
        2. **進圖座標** —— 剛換圖還沒有第 1 項時用它；那是伺服器剛講過的落點。

        ⚠ 這不是猜：被回收的舊元件會停在它最後的位置（多半是上一張圖），
        跟現在的參考點差很遠。真的兩個都很近的話，挑哪個都不會錯得離譜。
        """
        reference = None
        if self._last_pos is not None and self._now() - self._last_pos_at < _REF_FRESH:
            reference = self._last_pos
        if reference is None:
            reference = self._entry_pos()
        if reference is None:
            return None
        best = None
        best_gap = None
        for addr in good:
            cell = seen.get(addr)
            if cell is None:
                continue
            gap = max(abs(cell[0] - reference[0]), abs(cell[1] - reference[1]))
            if best_gap is None or gap < best_gap:
                best, best_gap = addr, gap
        return best

    def _near_reference(
        self, good: list[int], seen: dict, map_name: str
    ) -> list[int]:
        """把「離參考點遠到不可能」的候選丟掉。沒有參考點就原封不動回傳。

        ## 為什麼一定要有這一關（實機 2026-09-04，[DAT-072]）

        `_closest()` 只在**好幾個候選同時驗過**的時候才會被叫到。可是換圖那一刻
        的真實情況是**只有一個候選驗得過，而那一個是上一張圖的殘留物**：

        - 新元件已經在堆積上（`GID == AID` 掃得到），但狀態欄位還是 0 → 驗不過。
        - 舊元件要再過 **1~2 秒**客戶端才回收；在那之前它 `state==1`、
          `dest` 還是上一張圖的格子 → **三關全過**。

        實機日誌（白狐，11:10:07 被移到 `mjolnir_12` (199,375)）：

            11:10:08  地圖從 aldebaran 換到 mjolnir_12，重新定位角色移動元件
            11:10:08  角色移動元件定位於 0x1fe02aa0     ← aldebaran 那顆，(133,103)
            11:10:09  角色移動元件 0x1fe02aa0 已失效     ← 客戶端終於回收它
            11:10:13  讀不到角色座標，送一步移動把位置逼出來

        `mjolnir_12` 夠大，(133,103) 在上面**站得住** —— 於是 `_on_map()` 放行、
        `read()` 回了上一張圖的座標，還把 `_moved_here` 設成 True。
        `_moved_here` 一旦為 True，進圖座標就**永遠**不能再用了（那條規則本身是
        對的），所以兩秒後殘留物被回收，`read()` 就一路回 None ——
        使用者看到的「每次換圖都找不到座標」就是這樣來的，
        而且前兩秒還安靜地回了**錯**座標。

        ## 判準：位置在時間上是連續的

        跟 `_closest()` 同一個道理，只是改當**過濾**用：角色從參考點走到現在
        最多能走 `_DRIFT_CELLS_PER_SEC × 經過秒數 + _DRIFT_SLACK` 格。
        差了幾百格的那顆不是本人，不管它驗得多漂亮。

        參考點的優先序與失效條件：

        1. **上一次讀成功的位置**（`_REF_FRESH` 秒內）。
        2. **進圖座標**，而且**必須是我們親眼看到伺服器移動角色之後的**
           （`_warped_at`）。⚠ 沒看到過就不能用：那時它只是「上次進圖時在哪」，
           角色早就走遠了，拿它當錨會把**活的**元件誤殺。
           進圖座標本身不在這張圖上（換圖訊號到了但全域還沒寫）也不能用。
        """
        if not good:
            return good
        ref = self._reference(map_name)
        if ref is None:
            return good
        (rx, ry), elapsed = ref
        limit = _DRIFT_CELLS_PER_SEC * max(elapsed, 0.0) + _DRIFT_SLACK
        near, far = [], []
        for addr in good:
            cell = seen.get(addr)
            if cell is None:
                continue
            gap = max(abs(cell[0] - rx), abs(cell[1] - ry))
            (near if gap <= limit else far).append((addr, cell, gap))
        if not far:
            return good
        if not near:
            # 全部都太遠 = 這張圖上還沒有活的元件（剛換圖的正常狀態）。
            # 回空清單，讓 `read()` 退回**進圖座標** —— 那是伺服器剛講過的落點，
            # 比「上一張圖的殘留值」正確得多。
            if not self._said_far:
                self._said_far = True
                log.info(
                    "%d 個候選全都離參考點 (%d,%d) 超過 %.0f 格"
                    "（最近的差 %d 格）—— 判定為上一張圖的殘留元件，改用進圖座標",
                    len(far), rx, ry, limit, min(g for _, _, g in far),
                )
            return []
        if not self._said_far:
            self._said_far = True
            log.info(
                "剔掉 %d 個離參考點 (%d,%d) 超過 %.0f 格的候選（殘留元件），剩 %d 個",
                len(far), rx, ry, limit, len(near),
            )
        return [addr for addr, _, _ in near]

    def _reference(self, map_name: str) -> tuple[tuple[int, int], float] | None:
        """挑一個參考點，回 `((x, y), 經過幾秒)`；沒有可信的參考點回 None。"""
        now = self._now()
        if self._last_pos is not None and now - self._last_pos_at < _REF_FRESH:
            return self._last_pos, now - self._last_pos_at
        if self._warped_at is None:
            return None
        entry = self._entry_pos()
        if entry is None or not self._on_map(entry, map_name):
            return None
        return entry, now - self._warped_at

    def _ticking(self, addr: int) -> bool:
        """這個候選的**動作 tick** 還在跳嗎（＝它還是客戶端正在用的那個）。

        只在「好幾個候選同時驗過」時才會被叫到，所以多讀 4 bytes 不心疼。
        讀不到一律回 True —— 這支是用來**排除**明顯的殘留物，
        不是用來挑人；問不出來就別動它（安全退化）。

        走路中 tick 是**未來**的值（實測領先約 965 ms），所以差值要當有號數看：
        負的代表領先，一樣算新鮮。判準與怪物那邊共用 `actor.FRESH_MS`。
        """
        raw = self._scanner.read_region(addr + actor.TICK, 4)
        if raw is None or len(raw) < 4:
            return True
        tick, = struct.unpack("<I", bytes(raw))
        age = (actor.now_tick() - tick) & 0xFFFF_FFFF
        if age > 1 << 31:
            age -= 1 << 32          # tick 領先現在（走路中）
        return age < actor.FRESH_MS

    def _component_at(self, addr: int) -> tuple[int, int] | None:
        """這個候選現在說自己在哪一格。**不是本人或它自己也不知道**就回 None。"""
        return self._look_at(addr)[1]

    def _look_at(self, addr: int) -> tuple[bool, tuple[int, int] | None]:
        """回 `(這是不是本人, 它現在說自己在哪一格)`。

        ## ⚠⚠ 為什麼要拆成兩個問題（實機 2026-09-04，[MEM-060]）

        舊版只有一個問題：「讀得到座標嗎」。讀不到就當成「不是本人」——
        於是**剛換圖、還沒在這張圖上走過的角色整整找不到**：

            16:06:08  伺服器說我被移到 aldebaran (197, 68)
            16:06:09  還沒找到角色的移動元件（49 個候選）
            16:06:39  已經 30 秒找不到角色的移動元件…一直出現代表遊戲改版

        使用者：「我不想再出現這個，最好是每次換地圖都會找不到座標」、
        「用 AOB 照理來說每次都查應該不會出現這問題」—— **他是對的**。
        當場唯讀量了三隻角色（`GID == AID` 全掃）：

            狐狐狸（走過路）  候選 139｜dest 合法 1 個  dest=(23,24)   age=7ms
            狐狐狸2（走路中）候選 190｜dest 合法 1 個  dest=(245,52)  age=16ms
            白狐（剛換圖）    候選  90｜dest 合法 0 個
                             0x2ef79b58 state=1 idx=-1 begin=0 **dest=(0,2)** age=13ms

        **元件一直都在，而且 tick 13 ms 還在跳** —— 是 `+0x5C/+0x60` 這個
        「**移動終點**」欄位在角色還沒走之前根本沒被寫過。拿它當「是不是本人」
        的驗證條件，就等於「沒走過的角色一律不算本人」。
        （另外量過：把元件前後 0x200 bytes 全掃一遍，**(197,68) 一個欄位都沒有** ——
        客戶端在角色走第一步之前真的不知道自己在哪，那時只有進圖座標算數。）

        所以「這是不是我的元件」與「它知不知道自己在哪」是**兩個不同的問題**，
        混在一起就是這個 bug。⚠ 但兩條路（掃描時、每拍讀取時）仍然共用這一支，
        判準只有一份 —— [PKT-078] 那個「掃描時認得、讀的時候不認得」的縫還在防。
        """
        raw = self._scanner.read_region(addr, SPAN)
        if raw is None or len(raw) < SPAN:
            return False, None
        buf = bytes(raw)
        gid, = struct.unpack_from("<I", buf, 0)
        if gid != self._aid:
            return False, None
        state, = struct.unpack_from("<I", buf, OFF_STATE)
        # 被回收的元件這裡是 0；堆積垃圾這裡通常是指標或很大的數字。
        if not (0 < state <= MAX_STATE):
            return False, None
        dest_x, dest_y = struct.unpack_from("<ii", buf, OFF_DEST_X)
        if not _cell_ok(dest_x, dest_y):
            # ★ **「本人，只是這張圖上還沒走過」**：終點欄位沒被寫過，
            #   但路徑索引與陣列都乾淨地說「沒在走」，而且動作 tick 還在跳。
            #   實機這個形狀在 90 個候選裡**唯一命中**（見上面的量測）。
            #   認得它就不必每 0.3 秒重新全掃 —— 角色一走第一步，
            #   終點欄位就有值，同一個位址直接變成即時座標。
            index, = struct.unpack_from("<i", buf, OFF_PATH_INDEX)
            begin, end = struct.unpack_from("<II", buf, OFF_PATH_BEGIN)
            unmoved = index < 0 and begin == 0 and end == 0
            return (unmoved and self._ticking(addr)), None
        index, = struct.unpack_from("<i", buf, OFF_PATH_INDEX)
        if index < 0:
            return True, (dest_x, dest_y)  # 站著不動：終點就是現在的位置
        # 走路中才需要路徑陣列。⚠ 剛傳過來還沒走的角色這裡是 0，
        #   所以**不能**拿它當存活旗標（第一版就是這樣一換圖就全滅）。
        begin, end = struct.unpack_from("<II", buf, OFF_PATH_BEGIN)
        if end < begin:
            return False, None
        if begin == 0:
            # ⚠⚠ **剛被傳過來、一步都還沒走**：路徑陣列還沒配置（`begin` 是 0），
            #   但 `index` 已經是 0（不是 -1），於是舊版把整個元件判成「不是本人」。
            #   後果：傳送完一直「找不到移動元件」，自動打怪／尋路只好拿進圖座標
            #   每 0.3 秒推一步去逼它出現 —— 使用者實測回報：
            #   「每次用了傳送都會一直找不到位置在那邊一直嘗試，可是肯定已經有
            #   位置了，因為地圖下面那個座標已經是正確的」。
            #
            #   客戶端確實知道自己在哪：沒有路徑就是**沒在走**，那 `dest` 就是
            #   目前這一格（跟 `index < 0` 那條同一個道理）。
            #   ⚠ 這不會放寬「認錯人」的風險：`gid == aid`、`state`、`dest` 範圍
            #   三關都還在，真的有好幾個過關時 `_closest()` 會拿**伺服器剛給的
            #   進圖座標**當參考挑出本人。
            return True, (dest_x, dest_y)
        span = end - begin
        if span % PATH_STRIDE or span // PATH_STRIDE > MAX_PATH_NODES:
            return False, None
        if index >= span // PATH_STRIDE:
            # 索引超出陣列＝解錯了，不要硬讀。
            # ⚠ **不要**在這裡退回 dest：那是「沒量過就放寬」。真的走完的元件
            #   長什麼樣還沒實機看過，而  配一節點的路徑明顯是垃圾。
            return False, None
        node = self._scanner.read_region(begin + index * PATH_STRIDE, 8)
        if node is None or len(node) < 8:
            return False, None
        x, y = struct.unpack("<ii", bytes(node))
        return (True, (x, y)) if _cell_ok(x, y) else (False, None)

    def _entry_pos(self) -> tuple[int, int] | None:
        """伺服器在 `0x0091` 說的「你被移到這裡」。走過一步之後就過期。"""
        if self._entry is None:
            return None
        raw = self._scanner.read_region(self._entry, 8)
        if raw is None or len(raw) < 8:
            return None
        x, y = struct.unpack("<ii", bytes(raw))
        return (x, y) if _cell_ok(x, y) else None

    # ---- 驗證 -------------------------------------------------------

    def _on_map(self, pos: tuple[int, int], map_name: str) -> bool:
        """這一格是**這張圖**上站得住的地方嗎？

        判準跟 `Traveler._settle()` 一模一樣（範圍內 ＋ `START_SNAP` 格內
        站得住），不是另寫一套 —— 兩層用不同判準的縫就是 [PKT-078] 卡住的地方。
        沒有這張圖的地形就只驗範圍（安全退化，不是拒絕）。
        """
        terrain = self._terrain_for(map_name)
        if terrain is None:
            return True                    # 沒地形就沒得驗，範圍已經驗過了
        x, y = pos
        if not (0 <= x < terrain.width and 0 <= y < terrain.height):
            return False
        return nearest_walkable(terrain, pos, radius=START_SNAP) is not None

    def _terrain_for(self, map_name: str):
        """地形快取。⚠ 不准用「檔案在不在」判斷，要問資料層（CLAUDE.md）。"""
        if not map_name:
            return None
        if map_name != self._terrain_map:
            self._terrain_map = map_name
            self._terrain = None
            if has_terrain(map_name):
                try:
                    self._terrain = load_terrain(map_name)
                except GatError as exc:
                    log.warning("載入 %s 的地形失敗：%s", map_name, exc)
        return self._terrain

    # ---- 掃描 -------------------------------------------------------

    def _scan(self, aid: int) -> list[int]:
        """全掃：列出所有 `u32 == aid` 的位址。

        實測 508 MB／1962 個區段要 **0.6~0.8 秒**，其中 **0.57 秒是
        ReadProcessMemory 本身** —— 比對只佔 0.04 秒，換 regex 反而更慢（0.80 秒）。
        也就是說這條路沒有「再優化一點」的空間，只能**少做幾次**（見 `_locate_component`）。
        """
        hits: list[int] = []
        try:
            regions = self._scanner.regions(writable_only=True)
        except RuntimeError:
            return hits
        for base, size in regions:
            raw = self._scanner.read_region(base, size)
            if raw is None or len(raw) < 4:
                continue
            words = np.frombuffer(raw, dtype="<u4", count=len(raw) // 4)
            for i in np.nonzero(words == aid)[0]:
                hits.append(base + int(i) * 4)
        return hits
