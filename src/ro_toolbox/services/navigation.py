"""讀出遊戲內建尋路（導航）現在指向哪張地圖。

按下遊戲的尋路按鈕時客戶端**一個封包都沒送**（實測 `封包/按下尋路.txt`：
只有 `0x0360`／`0x007F` 對時心跳），箭頭完全是客戶端自己算的。
所以要知道玩家想去哪，唯一的來源是記憶體 —— 這支就是那個唯讀的讀取端。

只讀不寫（CLAUDE.md：RO 掛 GameGuard，寫入會被反制）。

**位址一律用程式碼特徵定位**（`signatures.NAVI_GOAL_SIGS`），不寫死。

## ⚠ 走過一次冤枉路：讀錯全域了（[MEM-046]）

原本讀的是 `NAVI_DEST_SIGS` 定位到的那個 `std::string`。它有時候真的等於
目的地，所以測起來像對的 —— 但它其實比較像**「最近載入／請求的地圖檔」**：
人站在 izlu2dun 時裡面就是 `izlu2dun.rsw`，而且用完會被清空。
使用者回報「遊戲裡箭頭好好的，你卻說讀不到導航目標」就是這樣來的。

抓到它的方法是**數程式碼引用**：整個程式碼區段裡只有 1 處引用那個全域，
就是它自己的靜態建構子 —— 沒有任何程式用絕對位址寫它，那它憑什麼是目的地。
真正的目的地（`0x15ADD6C`）有 5 處引用。

教訓：**「讀出來像地圖名」不等於「這就是我要的那個地圖名」。** 認一個全域
之前先看誰在用它。

讀出來的內容**一定要驗**：

1. 像不像地圖名（ASCII、只有 `a-z0-9_@`、去掉 `.rsw` 副檔名）；
2. 我們**真的走得到那張圖**（拿不到地形就走不過去，讀到也沒用）。

驗不過一律回 None，讓呼叫端大聲停用，絕不拿一個看起來像地圖名的垃圾去走路。
"""

from __future__ import annotations

import logging
import re

from ro_toolbox.services.aob import locate_global
from ro_toolbox.services.mapdata import has_terrain
from ro_toolbox.services.memory_scan import MemoryScanner
from ro_toolbox.services.signatures import NAVI_DEST_MAX_BYTES, NAVI_GOAL_SIGS

log = logging.getLogger(__name__)

#: 合法的 RO 地圖檔名。實測涵蓋 `prt_fild08`、`1@mjo1`、`moc_para01` 這些寫法。
_MAP_NAME = re.compile(r"^[0-9a-z_@]{3,20}$")
#: 全域裡存的是帶副檔名的名字，這些都要剝掉。
_SUFFIXES = (".rsw", ".gat", ".gnd")


def clean_map_name(raw: str) -> str | None:
    """把全域讀到的字串洗成地圖名。不像地圖名就回 None。"""
    # C 字串：第一個 null 之後是上一個目標的殘留，不是這次的答案。
    # `MemoryScanner.read_string` 已經會截，這裡再截一次是為了不依賴它的行為。
    name = raw.split("\x00", 1)[0].strip().lower()
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name if _MAP_NAME.match(name) else None


class NavigationReader:
    """讀遊戲導航目標的地圖。一個行程一個。"""

    def __init__(self) -> None:
        self._scanner: MemoryScanner | None = None
        self._address: int | None = None
        #: 上一次讀到的是「空字串」（定位成功、但客戶端把目標清掉了）。
        #: 用來把「還沒設定目的地」跟「設了但我們讀不到」講清楚 ——
        #: 對使用者說「請先設定目的地」而他明明設了，是最惹人火的錯誤訊息。
        self.blank = False
        #: 上一次讀到的原始字串（**驗不過的時候要拿它去跟使用者講**）。
        #: 實機 2026-09-01 讀到過 `'座標'` —— 那是尋路視窗的分頁名，不是地圖。
        #: 使用者用「座標／NPC」那種找法時，這個全域裡就不是地圖名。
        self.raw = ""

    @property
    def located(self) -> bool:
        return self._address is not None

    @property
    def address(self) -> int | None:
        """定位到的全域位址（診斷用）。"""
        return self._address

    def attach(self, pid: int) -> bool:
        """附加並定位導航目標全域。定位失敗回 False（呼叫端要大聲停用）。"""
        self.close()
        scanner = MemoryScanner()
        try:
            scanner.open(pid)
        except OSError as exc:
            log.error("附加行程 %s 失敗：%s", pid, exc)
            return False
        address = locate_global(scanner, NAVI_GOAL_SIGS)
        if address is None:
            log.error("導航目標全域定位失敗（遊戲可能已改版），功能停用")
            scanner.close()
            return False
        self._scanner = scanner
        self._address = address
        log.info("導航目標全域 = %#x", address)
        return True

    def destination(self) -> str | None:
        """導航目標的地圖名（不含副檔名）。讀不到或驗不過回 None。

        驗不過的兩種情況都當「不知道」，不猜：
        - 內容不像地圖名 → 特徵可能指錯全域了（改版）；
        - 有這個名字但我們沒有它的地形 → 就算讀對了也走不過去。
        """
        if self._scanner is None or self._address is None:
            return None
        raw = self._scanner.read_string(self._address, NAVI_DEST_MAX_BYTES, "ascii")
        self.raw = raw or ""
        if not raw:
            # 空的 = 這一場還沒在遊戲裡設過尋路目標。
            # （舊版讀錯全域時這裡會**誤判**成「沒設定」，見上面的 [MEM-046]。）
            self.blank = True
            return None
        self.blank = False
        name = clean_map_name(raw)
        if name is None:
            log.warning("導航目標讀到 %r，不像地圖名 —— 不採用", raw)
            return None
        if not has_terrain(name):
            log.warning("導航目標 %s 沒有地形資料，走不過去 —— 不採用", name)
            return None
        return name

    def close(self) -> None:
        if self._scanner is not None:
            self._scanner.close()
        self._scanner = None
        self._address = None
