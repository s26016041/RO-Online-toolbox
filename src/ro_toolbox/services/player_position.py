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

| 來源 | 什麼時候是對的 | 什麼時候沒用 |
|---|---|---|
| **進圖座標全域**（`MAP_ENTRY_*_SIGS`）| 伺服器 `0x0091` 說「你被移到這裡」的那一刻 | 走了一步之後就過期，走再遠都不會變 |
| **移動元件**（`GID == AID` 的結構）| 在這張圖上動過之後，逐格精確 | 剛換圖、還沒走過的時候整塊是空的 |

所以 `read()` 的順序是「**移動元件優先，沒有才用進圖座標**」——
而「沒有移動元件」與「還沒在這張圖上走過」是同一件事，所以這個退化是**準的**，
不是將就。

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
  （實測領先現在 965 ms），站久了又會過期。當新鮮度用會兩頭錯。
"""

from __future__ import annotations

import logging
import struct
import time

import numpy as np

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
# 屬 CLAUDE.md 允許寫死的「結構偏移」類別（同一個結構內部的欄位距離，
# 大更新才會壞）。出處：2026-08-28 實機，先用 `GID == AID` 找到結構，
# 再一邊送移動封包一邊 30 Hz 取樣整塊結構，看哪些 dword 在走路途中變化。
OFF_STATE = 0x38        # u32 狀態：站著 1、走路 2、被回收 0
OFF_DEST_X = 0x5C       # i32 移動終點 x（沒在走＝目前格）
OFF_DEST_Y = 0x60       # i32 移動終點 y
OFF_PATH_BEGIN = 0x110  # 路徑陣列 begin（⚠ 沒走過路時是 0，不能當存活旗標）
OFF_PATH_END = 0x114    # 路徑陣列 end（begin/end/cap 的標準 vector 版面）
OFF_PATH_INDEX = 0x12C  # i32 目前走到第幾個節點，-1 = 沒在走
PATH_STRIDE = 0x10      # 一個路徑節點的大小，前 8 bytes 是 (i32 x, i32 y)

#: 讀一次要抓多少 bytes：從 GID 一路到路徑索引。
SPAN = OFF_PATH_INDEX + 4

#: 狀態欄位的合理範圍。實測只看過 1（站著）與 2（走路）；
#: 放寬到 8 是留給還沒看過的狀態（坐下、死亡…），但**不能無上限** ——
#: 堆積垃圾的這個位置常常是指標或很大的數字。
MAX_STATE = 8
#: RO 沒有超過 512x512 的地圖；0 是地圖邊界（任何圖上都不可走）。
#: ⚠ (0,0) 一定要擋掉 —— [MEM-039] 就是被 (0,0) 通過驗證害的。
MAX_CELL = 512
#: 路徑不可能有這麼多節點（單次移動上限 17 格）。用來擋「指標像陣列但其實是垃圾」。
MAX_PATH_NODES = 256
#: 元件掉了之後多久才准重找一次。**每一拍都找會讓 bot 定格。**
RELOCATE_COOLDOWN = 2.0
#: 多久才准全掃一次。全掃一趟實測 0.7~0.8 秒（511 MB）——
#: 剛換圖還沒走路的時候元件本來就不存在，每 2 秒全掃一次等於一直卡頓。
#: 平常只掃**上次有命中的區段**（實測幾毫秒），全掃留給久久一次的兜底。
FULL_RESCAN_SEC = 15.0


def _cell_ok(x: int, y: int) -> bool:
    return 0 < x < MAX_CELL and 0 < y < MAX_CELL


class PlayerPosition:
    """角色現在站在哪一格。問不出可信答案就回 None —— **絕不回殘留值**。

    `locate()` 做兩件事：用程式碼特徵找進圖座標全域（很快），
    再全掃一次記憶體找移動元件（0.7~0.8 秒；剛換圖時可能還不存在，不算失敗）。
    之後重找只掃「上次有命中的區段」，全掃 `FULL_RESCAN_SEC` 才做一次。
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
        #: 上次掃到 GID 的記憶體區段。之後只掃這些（幾毫秒），
        #: 全掃留給 `FULL_RESCAN_SEC` 一次的兜底 —— 元件通常配在同一塊堆積裡。
        self._hot: list[tuple[int, int]] = []
        self._last_full = 0.0
        #: 地形快取：驗證「這一格站得住嗎」用，換圖才重載
        self._terrain_map = ""
        self._terrain = None

    # ---- 定位 -------------------------------------------------------

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

    def _locate_component(self) -> bool:
        """掃記憶體找 `GID == aid` 的移動元件。**通過驗證的必須剛好一個**。

        找不到**不是錯誤**：剛換圖、還沒在這張圖上走過的時候本來就沒有。
        那時候由進圖座標全域回答，等角色走第一步之後再自己接上。
        """
        self._addr = None
        self._last_locate = self._now()
        if not (0 < self._aid < 0x7FFF_FFFF):
            log.error("角色 AID 讀不到（%r），移動元件無法定位", self._aid)
            return False
        hits, hot = self._scan(self._aid, self._hot)
        good = [addr for addr in hits if self._component_at(addr) is not None]
        if not good and self._now() - self._last_full >= FULL_RESCAN_SEC:
            # 熱區段裡沒有 —— 久久一次翻遍整份記憶體（新配置的可能落在別處）。
            self._last_full = self._now()
            hits, hot = self._scan(self._aid, None)
            self._hot = hot
            good = [addr for addr in hits if self._component_at(addr) is not None]
        elif hot:
            self._hot = hot
        if len(good) > 1:
            # 驗完還是不只一個＝真的分不出來。不准賭 —— 賭錯就是照著別人的位置走。
            log.error(
                "有 %d 個移動元件都像是角色本人（%s），分不出來，只好改用進圖座標",
                len(good), [hex(a) for a in good],
            )
            return False
        if not good:
            log.info(
                "還沒找到角色的移動元件（AID %d，%d 個 GID 命中）"
                "—— 剛換圖還沒走過路的話這是正常的，先用進圖座標",
                self._aid, len(hits),
            )
            return False
        self._addr = good[0]
        self._complained = False
        log.info("角色移動元件定位於 %#x（AID %d，%d 個 GID 命中）",
                 self._addr, self._aid, len(hits))
        return True

    def invalidate(self) -> None:
        """丟掉記著的元件位址，下一次 `read()` 會重新找。

        換地圖時一定要呼叫：客戶端會把舊元件回收，而**回收不等於清乾淨** ——
        GID 可能還在原地，只是不再更新（[MEM-047] 那個「很有自信的錯值」
        就是這樣來的）。與其賭它有沒有被重用，不如當場重找。
        """
        self._addr = None
        self._last_locate = 0.0

    def forget(self) -> None:
        """完全重置（換行程／收攤時用）。"""
        self.invalidate()
        self._aid = 0
        self._entry = None
        self._hot = []
        self._last_full = 0.0
        self._terrain_map = ""
        self._terrain = None
        self._complained = False

    # ---- 讀取 -------------------------------------------------------

    def read(self, map_name: str = "") -> tuple[int, int] | None:
        """回目前所在格。兩個來源都問不出可信答案就回 None。

        `map_name` 是**現在這張圖**（呼叫端從角色結構讀）。有它才驗得了
        「這一格站得住嗎」—— 那是分辨「上一張圖的殘留值」最有效的一關。
        """
        pos = self._component_pos()
        if pos is None and self._addr is None and self._can_relocate():
            self._locate_component()
            pos = self._component_pos()
        if pos is not None and self._on_map(pos, map_name):
            return pos
        entry = self._entry_pos()
        if entry is not None and self._on_map(entry, map_name):
            return entry
        return None

    def _component_pos(self) -> tuple[int, int] | None:
        """從記著的元件位址讀一次；驗不過就把位址丟掉（下一拍重找）。"""
        if self._addr is None:
            return None
        pos = self._component_at(self._addr)
        if pos is not None:
            return pos
        if not self._complained:
            log.warning("角色移動元件 %#x 已失效（換圖或被回收），重新定位中",
                        self._addr)
            self._complained = True
        self._addr = None
        return None

    def _can_relocate(self) -> bool:
        return bool(self._aid) and self._now() - self._last_locate >= RELOCATE_COOLDOWN

    def _component_at(self, addr: int) -> tuple[int, int] | None:
        """從一個候選位址讀座標；任何一項驗不過就回 None。

        這同時是 `_locate_component()` 的驗證函式 —— **兩條路用完全一樣的判準**，
        才不會出現「掃描時認得、每拍讀的時候不認得」那種縫（[PKT-078]）。
        """
        raw = self._scanner.read_region(addr, SPAN)
        if raw is None or len(raw) < SPAN:
            return None
        buf = bytes(raw)
        gid, = struct.unpack_from("<I", buf, 0)
        if gid != self._aid:
            return None
        state, = struct.unpack_from("<I", buf, OFF_STATE)
        # 被回收的元件這裡是 0；堆積垃圾這裡通常是指標或很大的數字。
        if not (0 < state <= MAX_STATE):
            return None
        dest_x, dest_y = struct.unpack_from("<ii", buf, OFF_DEST_X)
        if not _cell_ok(dest_x, dest_y):
            return None
        index, = struct.unpack_from("<i", buf, OFF_PATH_INDEX)
        if index < 0:
            return dest_x, dest_y          # 站著不動：終點就是現在的位置
        # 走路中才需要路徑陣列。⚠ 剛傳過來還沒走的角色這裡是 0，
        #   所以**不能**拿它當存活旗標（第一版就是這樣一換圖就全滅）。
        begin, end = struct.unpack_from("<II", buf, OFF_PATH_BEGIN)
        if begin == 0 or end < begin:
            return None
        span = end - begin
        if span % PATH_STRIDE or span // PATH_STRIDE > MAX_PATH_NODES:
            return None
        if index >= span // PATH_STRIDE:
            return None                    # 索引超出陣列＝解錯了，不要硬讀
        node = self._scanner.read_region(begin + index * PATH_STRIDE, 8)
        if node is None or len(node) < 8:
            return None
        x, y = struct.unpack("<ii", bytes(node))
        return (x, y) if _cell_ok(x, y) else None

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

    def _scan(self, aid: int, regions):
        """找出所有 `u32 == aid` 的位址，順便回報哪些區段有命中。

        `regions=None` 代表全掃（實測整份 511 MB 要 0.7~0.8 秒）；
        給一份清單就只掃那些（熱區段，幾毫秒）。
        """
        if regions is None:
            try:
                regions = self._scanner.regions(writable_only=True)
            except RuntimeError:
                return [], []
        hits: list[int] = []
        hot: list[tuple[int, int]] = []
        for base, size in regions:
            raw = self._scanner.read_region(base, size)
            if raw is None or len(raw) < 4:
                continue
            words = np.frombuffer(raw, dtype="<u4", count=len(raw) // 4)
            found = np.nonzero(words == aid)[0]
            if not len(found):
                continue
            hot.append((base, size))
            for i in found:
                hits.append(base + int(i) * 4)
        return hits, hot
