"""實體（actor）結構的**唯一一份**版面定義：角色與怪物共用同一套。

## 為什麼一定要只有一份

以前 `player_position.py`（角色自己）與 `entities.py`（怪）各寫一份，
於是同一個欄位被賦予**兩種互相矛盾的意義**，而且沒有任何東西會發現：

| 偏移 | player_position 說 | entities 說 | 實機量到的（2026-09-01） |
|---|---|---|---|
| `+0x110` | 路徑陣列 begin，**沒走過路時是 0** | 「繪圖物件指標，0＝死了」 | 路徑陣列 begin |
| `+0x120/4` | 這一段移動的插值座標 | 「就是牠現在站的格」 | 插值座標，**沒走過就是 (0,0)** |
| `-0x24` | 角色身上會閃爍，不能當存活旗標 | 「存活旗標，1＝活著」 | 會閃爍 |

三條全是 `entities.py` 錯，代價是**記憶體幾乎看不到怪**（見 GAMEDATA [MEM-058]）。
CLAUDE.md 那條「同一個位址不准在第二個地方再寫一次」對**結構偏移**同樣成立：
抄第二份就等於保證有一份會過期。

## 偏移一律以 **GID 欄位**為基準

GID 是這個結構裡唯一「我們有辦法當場搜出來」的錨（角色是 `GID == AID`，
怪是「GID 旁邊就是 class ID」），所以所有偏移都相對它寫。

出處：2026-08-28 用送移動封包 ＋ 30 Hz 取樣整塊結構量出來（角色），
2026-09-01 用封包當答案卡在怪身上核對過（[MEM-058]）。
屬 CLAUDE.md 允許寫死的「結構偏移」類別 —— 大更新才會壞。

| 偏移 | 型別 | 意義 |
|---|---|---|
| `-0x110` | ptr  | vtable（一定落在 Ragexe.exe 的模組映像內） |
| `-0x04`  | i32  | class ID（怪種／職業） |
| `+0x00`  | u32  | GID（角色的就是 AID） |
| `+0x38`  | u32  | 狀態：站著 1、走路 2…（**怪身上會跑到 0~6，不能當存活旗標**）|
| `+0x5C` / `+0x60` | i32 | **移動終點**；沒在走的時候就是目前所在格 |
| `+0x110` / `+0x114` | ptr | 路徑陣列 begin／end（節點 0x10 bytes，開頭是 x,y）|
| `+0x120` / `+0x124` | f32 | 這一段移動的插值座標（**沒走過路就是 0**）|
| `+0x12C` | i32  | 目前走到路徑第幾個節點，沒在走是 -1 |
| `+0x134` | u32  | **動作 tick**（毫秒，跟 `GetTickCount()` 同一個時基）|

## 「牠現在站哪」只有兩行

    index < 0（或沒有路徑陣列）→ 讀 +0x5C/+0x60
    index >= 0                  → 讀 路徑[index]

實機對答案（2026-09-01，狐狐狸 @ mjolnir_07，60 秒／177 拍，
用伺服器封包當答案卡）：**中位差 0 格、平均 0.09 格、最大 4 格**。
同一份樣本裡拿 `+0x120/+0x124` 的 float 去比是**中位 181 格**
（因為多數怪從沒走過，那裡是 (0,0)）。

## 「牠還在世界上嗎」用 tick，不要用旗標

`+0x134` 對還在世界上的實體**一直在更新**，被丟掉的實體就停住不動了。
實機門檻掃描（同一輪 176 拍）：

    tickΔ 門檻   記憶體看到  兩邊都有  只有封包  只有記憶體
       0.5 秒      0.63       0.55      0.07      0.09
       1~10 秒     0.66       0.55      0.07      0.12
       30 秒       0.69       0.55      0.07      0.15
       不設限      1.26       0.55      0.07      0.72   ← 多出來的全是殘留

1 秒到 10 秒之間完全平坦 = 活的與殘留之間有很乾淨的斷層，
取 **2 秒**（`FRESH_MS`）落在斷層正中間，兩邊都不敏感。
"""

from __future__ import annotations

import struct
import sys
import time

# ---- 相對 GID 欄位的偏移（唯一定義）--------------------------------------
VTABLE = -0x110
CLASS = -0x04
GID = 0x00
STATE = 0x38
DEST_X = 0x5C
DEST_Y = 0x60
PATH_BEGIN = 0x110
PATH_END = 0x114
MOVE_LERP_X = 0x120  # ⚠ 插值座標，不是「現在站哪」。要位置請用 cell_from()
MOVE_LERP_Y = 0x124
PATH_INDEX = 0x12C
TICK = 0x134

PATH_STRIDE = 0x10
#: 路徑不可能有這麼多節點（單次移動上限 17 格）。擋「指標像陣列但其實是垃圾」。
MAX_PATH_NODES = 256
#: 狀態欄位的合理範圍。角色只看過 1／2；怪身上會跑到 0~6（動作狀態），
#: 所以**只拿來擋垃圾，不拿來判斷死活**。
MAX_STATE = 8
#: RO 沒有超過 512x512 的地圖；0 是地圖邊界（任何圖上都不可走）。
#: ⚠ (0,0) 一定要擋掉 —— [MEM-039] 就是被 (0,0) 通過驗證害的。
MAX_CELL = 512

#: 從 vtable 讀到動作 tick 要涵蓋多少 bytes（讀一整塊比分次讀便宜）。
HEAD = -VTABLE          # 0x110：GID 之前要往回讀多少
SPAN = TICK + 4         # 0x138：GID 之後要往後讀多少
BLOCK = HEAD + SPAN     # 一次讀 0x248 bytes 就有全部欄位

#: 動作 tick 落後現在多久之內算「還在世界上」。出處見模組開頭的門檻掃描。
FRESH_MS = 2000


def cell_ok(x: int, y: int) -> bool:
    return 0 < x < MAX_CELL and 0 < y < MAX_CELL


def now_tick() -> int:
    """跟遊戲的 `+0x134` 同一個時基的毫秒 tick。

    Windows 用 `GetTickCount()`（實機量到活的實體 tickΔ 在 ±0.5 秒內）。
    其他平台沒有這個時基，退回單調時鐘 —— 那裡本來就沒有遊戲可讀，
    只有測試會走到。
    """
    if sys.platform == "win32":
        import ctypes

        return int(ctypes.windll.kernel32.GetTickCount())
    return int(time.monotonic() * 1000) & 0xFFFF_FFFF


def tick_age(actor_tick: int, now: int | None = None) -> int:
    """動作 tick 落後現在幾毫秒。負數 = 它是未來的值（走路中會這樣）。"""
    now = now_tick() if now is None else now
    age = (now - actor_tick) & 0xFFFF_FFFF
    return age - 0x1_0000_0000 if age > 0x7FFF_FFFF else age


class ActorView:
    """一塊 `BLOCK` bytes 的實體結構快照。欄位都用上面那份偏移解。

    `base` 是這塊 buffer 對應到的**記憶體位址**（也就是 GID 欄位減 `HEAD`），
    只拿來把 buffer 內的偏移換算回真實位址，不存不記。
    """

    __slots__ = ("_buf", "base")

    def __init__(self, buf: bytes, base: int = 0) -> None:
        self._buf = buf
        self.base = base

    @classmethod
    def read(cls, scanner, gid_addr: int) -> "ActorView | None":  # noqa: ANN001
        """從 GID 欄位的位址讀一整塊。讀不完整回 None。"""
        raw = scanner.read_region(gid_addr + VTABLE, BLOCK)
        if raw is None or len(raw) < BLOCK:
            return None
        return cls(bytes(raw), gid_addr + VTABLE)

    def _u32(self, off: int) -> int:
        return struct.unpack_from("<I", self._buf, HEAD + off)[0]

    def _i32(self, off: int) -> int:
        return struct.unpack_from("<i", self._buf, HEAD + off)[0]

    @property
    def vtable(self) -> int:
        return self._u32(VTABLE)

    @property
    def gid(self) -> int:
        return self._u32(GID)

    @property
    def class_id(self) -> int:
        return self._i32(CLASS)

    @property
    def state(self) -> int:
        return self._u32(STATE)

    @property
    def tick(self) -> int:
        return self._u32(TICK)

    @property
    def path_index(self) -> int:
        return self._i32(PATH_INDEX)

    @property
    def path_begin(self) -> int:
        return self._u32(PATH_BEGIN)

    @property
    def path_end(self) -> int:
        return self._u32(PATH_END)

    @property
    def dest(self) -> tuple[int, int]:
        return self._i32(DEST_X), self._i32(DEST_Y)

    def fresh(self, now: int | None = None, limit_ms: int = FRESH_MS) -> bool:
        """這個實體還在世界上嗎（動作 tick 還在更新嗎）。"""
        return tick_age(self.tick, now) < limit_ms

    def cell(self, scanner) -> tuple[int, int] | None:  # noqa: ANN001
        """牠**現在站的那一格**。驗不過回 None —— 絕不退回插值座標。

        走路中要多讀 8 bytes（路徑節點）；站著就是 `dest`，不必再讀。
        """
        dest_x, dest_y = self.dest
        if not cell_ok(dest_x, dest_y):
            return None
        index = self.path_index
        begin, end = self.path_begin, self.path_end
        # 沒在走（index < 0），或路徑陣列還沒配置（剛出現／剛傳過來）：
        # 終點就是現在這一格。`begin == 0` 那條是 [MEM-057] 踩過的坑。
        if index < 0 or begin == 0:
            return dest_x, dest_y
        span = end - begin
        if span <= 0 or span % PATH_STRIDE or span // PATH_STRIDE > MAX_PATH_NODES:
            return None
        if index >= span // PATH_STRIDE:
            return None                      # 索引超出陣列＝解錯了，不要硬讀
        node = scanner.read_region(begin + index * PATH_STRIDE, 8)
        if node is None or len(node) < 8:
            return None
        x, y = struct.unpack("<ii", bytes(node))
        return (x, y) if cell_ok(x, y) else None

    def walking(self) -> bool:
        """客戶端認為牠正在走路嗎（有路徑陣列而且索引還沒走完）。"""
        return self.path_begin != 0 and self.path_index >= 0


__all__ = [
    "BLOCK",
    "CLASS",
    "DEST_X",
    "DEST_Y",
    "FRESH_MS",
    "GID",
    "HEAD",
    "MAX_CELL",
    "MAX_PATH_NODES",
    "MAX_STATE",
    "MOVE_LERP_X",
    "MOVE_LERP_Y",
    "PATH_BEGIN",
    "PATH_END",
    "PATH_INDEX",
    "PATH_STRIDE",
    "SPAN",
    "STATE",
    "TICK",
    "VTABLE",
    "ActorView",
    "cell_ok",
    "now_tick",
    "tick_age",
]
