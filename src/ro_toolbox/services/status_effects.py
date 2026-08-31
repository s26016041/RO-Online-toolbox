"""身上現在有什麼狀態（EFST，畫面右上那排狀態圖示）。

    from ro_toolbox.services.status_effects import StatusEffects
    buffs = StatusEffects(scanner)
    buffs.locate()
    for row in buffs.read() or []:
        print(row.name, row.remaining_ms)

## 為什麼讀記憶體而不是收封包

封包（`0x0984` 進圖時整份重送、`0x043F`／`0x0196` 單一狀態上下）只看得到
「開始擷取之後」發生的事 —— 工具啟動時已經在身上的 buff 要等下一次換圖才知道。
記憶體裡是**當下的完整清單**，隨時問隨時準，而且不用管 Npcap 有沒有裝。

## 資料長什麼樣

客戶端把清單放在 ragexe 的一個 static `std::vector`（begin／end／capacity
三個相鄰指標），每筆 28 bytes。位址一律用程式碼特徵當場定位
（`signatures.STATUS_VEC_SIGS`，四條獨立骨架互相驗證），不記絕對位址。

| 偏移 | 意義 |
|---|---|
| `+0x00` | EFST 編號（`assets/efst.json.gz` 查中文名） |
| `+0x04` | 到期時刻，與 `GetTickCount()` 同時基 |
| `+0x08`／`+0x0C`／`+0x10` | val1~3（技能等級之類，隨狀態而異） |
| `+0x14` | 總時長（毫秒），**9999 = 無時限** |
| `+0x18` | 掛上當下的剩餘毫秒 |

## 失效時怎麼辦

- 特徵定位不到 → `read()` 回 `None` 並記 error（**大聲停用**）。
- vector 內容不合理（筆數對不上、指標亂掉）→ 一樣回 `None`，
  **不准挑看起來像的那幾筆**回去 —— 那就是安靜地做錯事。
- 單筆的時間算不出合理值（永久狀態的到期欄位本來就是垃圾）→
  `remaining_ms` 回 `None`，只顯示名稱（**安全退化**，不假裝有倒數）。
"""

from __future__ import annotations

import ctypes
import logging
import struct
from dataclasses import dataclass

from ro_toolbox.services import gamedata
from ro_toolbox.services.aob import locate_global
from ro_toolbox.services.memory_scan import MemoryScanner
from ro_toolbox.services.signatures import (
    STATUS_MAX_ENTRIES,
    STATUS_NO_TIME_LIMIT,
    STATUS_VEC_OFFSETS,
    STATUS_VEC_SIGS,
)

log = logging.getLogger(__name__)

#: 算得出來的剩餘時間要落在這個範圍才採用。
#: 上限：RO 沒有超過一天的狀態；超過就是那個欄位根本不是到期時刻。
#: 下限給 -5 秒的餘裕：讀取與比對之間本來就有時間差，剛好到期不該當成異常。
_MAX_REMAIN_MS = 24 * 60 * 60 * 1000
_MIN_REMAIN_MS = -5_000

_INT32 = 1 << 32


def _tick() -> int:
    """系統開機毫秒數。客戶端的到期欄位用的是同一個時基。"""
    return ctypes.windll.kernel32.GetTickCount()


def _elapsed(expire: int, now: int) -> int:
    """`expire - now`，處理 32 位元回繞（每 49.7 天一次）。

    直接相減的話，回繞當下會算出 ±49 天的差值 —— 那正是「安靜地做錯事」。
    """
    return ((expire - now + (1 << 31)) % _INT32) - (1 << 31)


@dataclass(frozen=True)
class ActiveStatus:
    """身上的一個狀態。"""

    efst: int
    #: 中文名稱；沒有中文名的內部狀態會回英文代號，再沒有就 `#編號`。
    name: str
    #: 剩餘毫秒。`None` = 無時限或算不出可信值（**不要顯示成 0**）。
    remaining_ms: int | None
    #: 總時長毫秒。`None` = 無時限。
    total_ms: int | None
    val1: int = 0
    val2: int = 0
    val3: int = 0

    @property
    def permanent(self) -> bool:
        return self.total_ms is None

    def describe(self) -> str:
        """介面上要顯示的字 —— **只有名字，不帶剩餘秒數**。

        ⚠ 使用者指定（2026-08-31）：「BUFF 狀態剩下幾秒文字不需要顯示出來」。
        那一行每秒都在跳（`5s` → `4s` → …），一排 buff 就是一整條在閃，
        看不出重點。剩餘時間**照樣讀、照樣存**在 `remaining_ms` 裡 ——
        補 buff 那條靠它決定要不要重放（見 `services/buffs.py`），
        只是不畫在畫面上。
        """
        return self.name


class StatusEffects:
    """讀「身上有什麼狀態」。位址每次 `locate()` 重找，不跨行程、不存檔。"""

    def __init__(self, scanner: MemoryScanner, now=_tick) -> None:
        self._scanner = scanner
        self._now = now
        self._addr: int | None = None
        #: 同一種失敗只抱怨一次，不然每一拍一行會把日誌洗掉。
        self._complained = ""

    # ---- 定位 -------------------------------------------------------

    @property
    def located(self) -> bool:
        return self._addr is not None

    @property
    def address(self) -> int | None:
        """vector 的位址。**只供顯示與除錯**，不要存檔（每次改版都會變）。"""
        return self._addr

    def locate(self) -> bool:
        # 掃程式碼區段要讀 12 MB —— 行程剛好死掉／還沒開起來時會丟例外。
        # 那不該把呼叫端（角色定位）整個拉下水：這裡自己吞掉並停用就好。
        try:
            self._addr = locate_global(self._scanner, STATUS_VEC_SIGS)
        except Exception as exc:  # noqa: BLE001
            log.error("狀態清單定位時發生例外：%s", exc)
            self._addr = None
        if self._addr is None:
            log.error("狀態清單定位失敗 —— 這個功能停用（遊戲可能已改版）")
        return self._addr is not None

    def forget(self) -> None:
        self._addr = None
        self._complained = ""

    # ---- 讀取 -------------------------------------------------------

    def read(self) -> list[ActiveStatus] | None:
        """回傳身上的狀態；`None` = 讀不到／內容不可信（呼叫端要停用顯示）。

        空清單（`[]`）跟 `None` 是**兩件不同的事**：前者是「確定身上沒東西」，
        後者是「問不出來」。分不清的話會把「沒 buff」跟「壞掉」混成一種顯示。
        """
        if self._addr is None:
            return None
        head = self._scanner.read_region(self._addr, 12)
        if head is None or len(head) < 12:
            return self._fail("head", "讀不到狀態清單的表頭")
        begin, end, capacity = struct.unpack("<III", bytes(head[:12]))

        off = STATUS_VEC_OFFSETS
        if begin == 0 and end == 0:
            # vector 還沒配置過 —— 例如還在選角畫面。這是「確定沒有」，不是錯誤。
            return []
        if not begin or end < begin or capacity < end:
            return self._fail("ptr", f"狀態清單指標不合理 {begin:#x}/{end:#x}/{capacity:#x}")
        span = end - begin
        if span % off.stride:
            return self._fail("stride", f"狀態清單長度 {span} 不是 {off.stride} 的倍數")
        count = span // off.stride
        if count > STATUS_MAX_ENTRIES:
            return self._fail("count", f"狀態清單筆數 {count} 太多，不採用")
        if count == 0:
            self._complained = ""
            return []

        raw = self._scanner.read_region(begin, span)
        if raw is None or len(raw) < span:
            return self._fail("body", "讀不到狀態清單內容")
        raw = bytes(raw)

        now = self._now()
        rows: list[ActiveStatus] = []
        def field(base: int, offset: int) -> int:
            return struct.unpack_from("<I", raw, base + offset)[0]

        for index in range(count):
            base = index * off.stride
            efst = field(base, off.efst)
            total = field(base, off.total_ms)
            if efst >= 1 << 16:
                return self._fail("efst", f"第 {index} 筆的 EFST 編號 {efst} 不合理")
            rows.append(ActiveStatus(
                efst=efst,
                name=gamedata.efst_name(efst),
                remaining_ms=self._remaining(field(base, off.expire_tick), total, now),
                # 無時限、或明顯不是時間的殘值（永久狀態的欄位是垃圾）都回 None。
                total_ms=(
                    None
                    if total == STATUS_NO_TIME_LIMIT or total > _MAX_REMAIN_MS
                    else total
                ),
                val1=field(base, off.val1),
                val2=field(base, off.val2),
                val3=field(base, off.val3),
            ))
        self._complained = ""
        return rows

    def has(self, efst: int) -> bool | None:
        """身上有沒有某個狀態。`None` = 問不出來（**不要當成沒有**）。"""
        rows = self.read()
        if rows is None:
            return None
        return any(row.efst == efst for row in rows)

    # ---- 內部 -------------------------------------------------------

    @staticmethod
    def _remaining(expire: int, total: int, now: int) -> int | None:
        """剩餘毫秒；算不出可信值就回 None（只顯示名稱，不假裝有倒數）。

        無時限的狀態（total = 9999，馴鷹術／手推車這種）到期欄位是沒有意義的
        殘值，實機讀到的是「六天前」—— 拿去顯示就會變成很有自信的錯誤。
        """
        if total == STATUS_NO_TIME_LIMIT:
            return None
        left = _elapsed(expire & 0xFFFFFFFF, now)
        if left > _MAX_REMAIN_MS or left < _MIN_REMAIN_MS:
            return None
        return max(0, left)

    def _fail(self, key: str, message: str) -> None:
        if self._complained != key:
            self._complained = key
            log.warning("%s", message)
        return None
