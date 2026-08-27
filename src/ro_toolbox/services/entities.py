"""從遊戲自己的記憶體讀「附近有哪些怪」—— 比封包可靠得多。

**這是輔助來源，不是主來源。** 誠實的 A/B（70 秒、189 次取樣，30 格以內）：
**記憶體平均 0.16 隻、封包平均 0.40 隻**；兩邊都看到 29 次、只有記憶體看到 1 次、
只有封包看到 46 次。也就是說它偶爾能補到封包漏收的那一隻，但整體涵蓋率**輸給封包**，
原因是還沒找到實體清單、只能靠特徵掃描，而掃描要輪過整份記憶體才會涵蓋到。
（一開始量到「記憶體是封包的 7 倍」是錯的：那次沒開地圖與可走格過濾、
視野放到 40 格，多出來的都是假陽性。）

怎麼找到的（[MEM-014]）：以前只有 GID 一個錨點，光靠「值像座標」會被尋路陣列、
路徑緩衝、UTF-16 路徑字串大量誤中（[MEM-010]）。[PKT-029] 解出 class ID 之後，
用「同一塊記憶體同時有 GID 和 class ID」兩個獨立條件，候選從 96 個直接縮到 1 個。

**全程唯讀**（ReadProcessMemory），不寫、不注入，GameGuard 看不到。
"""

from __future__ import annotations

import logging
import struct
import threading
import time

import numpy as np

from ro_toolbox.services.gamedata import mob_names, mobs_on_map
from ro_toolbox.services.mapdata import MapTerrain
from ro_toolbox.services.memory_scan import MemoryScanner

log = logging.getLogger(__name__)

# 結構偏移（相對 GID 欄位）。屬 CLAUDE.md 允許寫死的「結構偏移」類別，
# 出處與驗證方式見 GAMEDATA [MEM-014]：GID exact-match → class ID 對得上 →
# 座標對得上 → 追蹤 12 秒與封包一致 45 次、不一致 0 次。
OFF_CLASS = -0x04   # int32 怪種編號
OFF_ALIVE = -0x24   # int32 存活旗標：活著是 1，死掉的瞬間變 0
OFF_RENDER = 0x110  # 繪圖物件指標：死掉時被清成 0（畫面上就是這樣消失的）
OFF_X = 0x120       # float32 格座標 x
OFF_Y = 0x124       # float32 格座標 y
_SPAN = OFF_Y + 4   # 一筆要讀到的最遠位元組
_HEAD = -OFF_ALIVE  # 要往前讀多少（存活旗標在 GID 前面）

_MAX_GID = 0x7FFF_FFFF
_FULL_RESCAN_SEC = 5.0  # 冷區段輪掃一輪結束後，至少隔這麼久才重新輪一次
_SWEEP_CHUNK = 120  # 每次多掃幾個冷區段（把 1.5 秒的全掃攤平成每拍幾十毫秒）
_MIN_REGION = 0x1000
#: 讀「一隻已知的怪」要抓多少位元組：從存活旗標（GID-0x24）一路到 y（GID+0x124）。
_ONE_START = OFF_ALIVE               # 相對 GID 的起點（負的）
_ONE_SIZE = -OFF_ALIVE + OFF_Y + 4   # 0x24 + 0x128 = 0x14C
#: 緩衝內的欄位位置（相對 `_ONE_START`）
_B_ALIVE = 0
_B_CLASS = -OFF_ALIVE + OFF_CLASS
_B_GID = -OFF_ALIVE
_B_RENDER = -OFF_ALIVE + OFF_RENDER
_B_X = -OFF_ALIVE + OFF_X
_B_Y = -OFF_ALIVE + OFF_Y
#: 背景發現執行緒兩輪之間睡多久。掃描本身就會花時間，這只是別把 CPU 佔滿。
_DISCOVER_IDLE = 0.05


class MemoryEntity:
    """記憶體裡的一隻怪。"""

    __slots__ = ("gid", "class_id", "x", "y", "addr")

    def __init__(self, gid: int, class_id: int, x: float, y: float, addr: int) -> None:
        self.gid = gid
        self.class_id = class_id
        self.x = int(round(x))
        self.y = int(round(y))
        self.addr = addr

    @property
    def pos(self) -> tuple[int, int]:
        return self.x, self.y

    def __repr__(self) -> str:
        return f"MemoryEntity({self.class_id}#{self.gid} @{self.pos})"


class EntityScanner:
    """掃遊戲記憶體找附近的怪。

    每一筆都要同時通過驗證才算數（少一道就退掉，寧可漏看也不要打空氣）：
      1. **存活旗標 `-0x24` == 1，且繪圖指標 `+0x110` 不是 0**
         —— 死掉的怪結構會留在記憶體裡沒被回收，但這兩個欄位會被清掉。
         實測 70 秒：已確認死亡的實體被掃到 65 次，其中 **60 次兩個旗標都已清空**，
         剩下 5 次都發生在死亡當下那一瞬間（客戶端還沒處理完）。
      2. class ID 在**這張地圖的出沒表**裡（[DAT-016]）
      3. GID 在合理範圍
      4. 座標是有限浮點、落在地圖範圍內、且**站在 .gat 的可走格上**
      5. 離角色不超過 view（超出視野的不可能是現在看得到的怪）

    每次掃「曾經掃到過怪」的熱區段（實測 4 ms），再加掃一小段冷區段慢慢輪完
    整份記憶體 —— 一次掃全部要 1.5 秒，直接做會讓 bot 每隔幾秒定格一下。
    """

    def __init__(
        self,
        terrain: MapTerrain,
        map_name: str,
        view: int = 30,
        now=time.monotonic,
        extra_classes=(),
    ) -> None:
        self._terrain = terrain
        self._view = view
        self._now = now
        self._scanner = MemoryScanner()
        self._hot: list[tuple[int, int]] = []
        self._sweep: list[tuple[int, int]] = []
        self._sweep_index = 0
        self._last_full = 0.0
        self._lut, self.map_filtered = self._build_lut(map_name, extra_classes)
        #: 診斷用：最近一次掃描花多少秒、掃了幾個區段
        self.last_cost = 0.0
        self.last_regions = 0
        #: gid → 那隻怪的結構位址。**找到一次就記住，之後只讀那個位址。**
        #: 掃描是為了「發現新的怪」，讀位置不該每次都重掃整份記憶體。
        self._known: dict[int, int] = {}
        self._known_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: 診斷用：背景發現跑了幾輪、目前記著幾隻
        self.discovered = 0

    @staticmethod
    def _build_lut(map_name: str, extra_classes=()) -> tuple[np.ndarray, bool]:
        """做 class ID 的查表陣列。優先只收這張圖會出的怪 —— 這是最強的過濾。

        `extra_classes` 是額外要放行的外觀編號（例如某隻 NPC 的 100）。
        NPC 不在怪物表裡，不明確放行就會被這道過濾擋掉。
        """
        lut = np.zeros(65536, dtype=bool)
        on_map = mobs_on_map(map_name)
        source = on_map or mob_names()
        for class_id in source:
            if 0 < class_id < 65536:
                lut[class_id] = True
        for class_id in extra_classes:
            if 0 < class_id < 65536:
                lut[class_id] = True
        if not on_map:
            log.warning("怪物表沒有 %s 的出沒資料，改用全表過濾（誤判會變多）", map_name)
        return lut, bool(on_map)

    def open(self, pid: int) -> bool:
        try:
            self._scanner.open(pid)
        except OSError as exc:
            log.warning("開啟行程記憶體失敗：%s", exc)
            return False
        return True

    # ---- 快路徑：只讀已知位址 ---------------------------------------

    def read_one(self, addr: int, me: tuple[int, int]) -> MemoryEntity | None:
        """從**已知位址**直接讀一隻怪。驗不過（死了／走遠了／位址失效）回 None。

        只讀 0x14C bytes，不掃記憶體 —— 這才是每一拍該做的事。
        驗證條件與掃描那條路完全一樣（存活旗標、繪圖指標、class 在表裡、
        座標有限且在地圖內、站在可走格），所以不會因為走快路徑而放寬標準。
        """
        raw = self._scanner.read_region(addr + _ONE_START, _ONE_SIZE)
        if raw is None or len(raw) < _ONE_SIZE:
            return None
        buf = bytes(raw)
        alive, = struct.unpack_from("<I", buf, _B_ALIVE)
        render, = struct.unpack_from("<I", buf, _B_RENDER)
        gid, = struct.unpack_from("<I", buf, _B_GID)
        class_id, = struct.unpack_from("<I", buf, _B_CLASS)
        x, = struct.unpack_from("<f", buf, _B_X)
        y, = struct.unpack_from("<f", buf, _B_Y)
        if alive != 1 or render == 0:
            return None                      # 死掉的結構還在，但這兩個欄位會被清掉
        if not (0 < gid < _MAX_GID) or not (0 < class_id < 65536):
            return None
        if not self._lut[class_id]:
            return None
        if not (x == x and y == y):           # NaN
            return None
        cell_x, cell_y = int(round(x)), int(round(y))
        if not (0 < cell_x < self._terrain.width and 0 < cell_y < self._terrain.height):
            return None
        if max(abs(cell_x - me[0]), abs(cell_y - me[1])) > self._view:
            return None                      # 走出視野了
        if not self._terrain.is_walkable(cell_x, cell_y):
            return None
        return MemoryEntity(gid, class_id, x, y, addr)

    def read_known(self, me: tuple[int, int]) -> list[MemoryEntity]:
        """讀所有記著的怪，回傳還活著、還在視野內的那些。**很便宜。**

        讀不到的就從清單移除 —— 牠死了、走遠了，或那塊記憶體被回收了。
        新的怪由背景的 `start_discovery()` 補進來。
        """
        with self._known_lock:
            known = dict(self._known)
        out: list[MemoryEntity] = []
        dead: list[int] = []
        for gid, addr in known.items():
            entity = self.read_one(addr, me)
            if entity is None or entity.gid != gid:
                dead.append(gid)             # gid 對不上＝那塊記憶體換人住了
                continue
            out.append(entity)
        if dead:
            with self._known_lock:
                for gid in dead:
                    self._known.pop(gid, None)
        return out

    # ---- 背景發現 ---------------------------------------------------

    def start_discovery(self, position) -> None:  # noqa: ANN001 - 回目前座標的函式
        """在背景持續掃描記憶體找**新的**怪，把位址記起來。

        掃一輪整份記憶體要 1.5 秒級，放在主迴圈裡會讓 bot 每拍卡住 ——
        所以搬到背景執行緒。主迴圈只走 `read_known()`（只讀已知位址）。
        """
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._discover_loop, args=(position,),
            name="entity-discovery", daemon=True,
        )
        self._thread.start()

    def _discover_loop(self, position) -> None:  # noqa: ANN001
        while not self._stop.is_set():
            here = None
            try:
                here = position()
            except Exception:  # noqa: BLE001 - 背景執行緒不能讓例外逸出
                here = None
            if here is None:
                self._stop.wait(0.2)
                continue
            try:
                for entity in self.scan(here):
                    with self._known_lock:
                        self._known[entity.gid] = entity.addr
                self.discovered += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("背景找怪失敗：%s", exc)
            self._stop.wait(_DISCOVER_IDLE)

    def stop_discovery(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(3.0)
        self._thread = None

    @property
    def known_count(self) -> int:
        with self._known_lock:
            return len(self._known)

    def close(self) -> None:
        self.stop_discovery()
        self._scanner.close()

    # ---- 掃描 -------------------------------------------------------

    def scan(self, me: tuple[int, int]) -> list[MemoryEntity]:
        """回傳目前記憶體裡、角色附近的怪。失敗回空清單（安全退化）。

        每次只掃「熱區段」（實測 4 ms），另外**每次多掃一小段冷區段**把整份
        記憶體慢慢輪過一遍 —— 一次掃全部要 1.5 秒，直接做會讓 bot 定格。
        """
        start = self._now()
        regions = list(self._hot) + self._next_sweep_slice()

        found: dict[int, MemoryEntity] = {}
        hot: set[tuple[int, int]] = set(self._hot)
        for base, size in regions:
            entities = self._scan_region(base, size, me)
            if entities:
                hot.add((base, size))
            for entity in entities:
                previous = found.get(entity.gid)
                if previous is None or self._dist(entity, me) < self._dist(previous, me):
                    found[entity.gid] = entity

        self._hot = sorted(hot)
        self.last_cost = self._now() - start
        self.last_regions = len(regions)
        return list(found.values())

    def _next_sweep_slice(self) -> list[tuple[int, int]]:
        """輪掃冷區段：每次取一小段，掃完一輪就重新列舉（新配置的記憶體才看得到）。"""
        if self._sweep_index >= len(self._sweep):
            if self._now() - self._last_full < _FULL_RESCAN_SEC and self._hot:
                return []
            self._sweep = [r for r in self._all_regions() if r not in set(self._hot)]
            self._sweep_index = 0
            self._last_full = self._now()
        chunk = self._sweep[self._sweep_index : self._sweep_index + _SWEEP_CHUNK]
        self._sweep_index += _SWEEP_CHUNK
        return chunk

    def _all_regions(self) -> list[tuple[int, int]]:
        try:
            return self._scanner.regions(writable_only=True)
        except RuntimeError:
            return []

    @staticmethod
    def _dist(entity: MemoryEntity, me: tuple[int, int]) -> int:
        return max(abs(entity.x - me[0]), abs(entity.y - me[1]))

    def _scan_region(self, base: int, size: int, me: tuple[int, int]) -> list[MemoryEntity]:
        if size < _MIN_REGION:
            return []
        buf = self._scanner.read_region(base, size)
        if buf is None:
            return []
        count = len(buf) // 4
        if count * 4 < _SPAN + 16:
            return []
        words = np.frombuffer(buf, dtype=np.uint32, count=count)
        floats = words.view(np.float32)

        # class ID 欄位在 GID-0x4，所以先找 class 再往後算。
        # 開頭留 _HEAD 是因為存活旗標在 GID 前面，讀得到才判斷得了。
        head = _HEAD // 4
        stop = count - _SPAN // 4 - 2
        if stop <= head:
            return []
        classes = words[:stop]
        candidate = classes < 65536
        candidate[:head] = False
        if not candidate.any():
            return []
        candidate &= self._lut[np.where(candidate, classes, 0)]
        index = np.nonzero(candidate)[0]
        if not len(index):
            return []

        gid = words[index + 1]
        alive = words[index + 1 + OFF_ALIVE // 4]
        render = words[index + 1 + OFF_RENDER // 4]
        x = floats[index + (OFF_X - OFF_CLASS) // 4]
        y = floats[index + (OFF_Y - OFF_CLASS) // 4]
        with np.errstate(invalid="ignore"):
            ok = (
                (alive == 1)      # 死掉的結構還在，但旗標會被清掉
                & (render != 0)   # 沒有繪圖物件＝畫面上已經不存在
                & (gid > 0)
                & (gid < _MAX_GID)
                & np.isfinite(x)
                & np.isfinite(y)
                & (x > 0)
                & (x < self._terrain.width)
                & (y > 0)
                & (y < self._terrain.height)
                & (np.abs(x - me[0]) <= self._view)
                & (np.abs(y - me[1]) <= self._view)
            )
        out: list[MemoryEntity] = []
        for k in np.nonzero(ok)[0]:
            cell_x, cell_y = int(round(float(x[k]))), int(round(float(y[k])))
            if not self._terrain.is_walkable(cell_x, cell_y):
                continue  # 怪不會站在不可走的格子上 —— 擋掉剩下的垃圾值
            i = int(index[k])
            out.append(
                MemoryEntity(int(gid[k]), int(classes[i]), float(x[k]), float(y[k]),
                             base + i * 4 + 4)
            )
        return out
