"""從遊戲自己的記憶體讀「附近有哪些怪、牠們站在哪」。

**這是主來源。** 誠實的 A/B（2026-09-01、狐狐狸 @ mjolnir_07、60 秒 177 拍、
用伺服器封包當對照，30 格以內）：

    封包                      1.44 隻
    記憶體（**修好之前**）      0.44 隻   ← 三個欄位都解錯，見下
    記憶體（修好）             1.72 隻   ← 兩邊都有 1.24、只有記憶體 0.48、只有封包 0.20

同一份樣本裡「同一隻怪的座標差幾格」：
**整數格 `+0x5C/+0x60` 中位 0 格**、舊版用的 float `+0x120/+0x124` **中位 181 格**。

## 修好之前錯在哪（[MEM-058]，三個都是「欄位意義猜錯」）

1. **座標讀錯欄位**：`+0x120/+0x124` 是移動的插值座標，
   **從沒走過路的怪那裡是 (0,0)** —— 而 (0,0) 過不了可走格驗證，
   所以整批被丟掉。真正的「現在站哪」是 `+0x5C/+0x60`（走路中則是路徑節點），
   `player_position.py` 早就量過並寫在文件裡了。
2. **`-0x24` 當存活旗標**：它在活著的怪身上會閃爍。實機量到就站在角色旁邊
   2 格的野豬 `-0x24 == 0`。（角色自己身上會閃爍這件事也早就寫在
   `player_position.py` 了。）
3. **`+0x110` 當「繪圖物件指標」**：那是**路徑陣列 begin**，
   沒走過路的怪一律是 0 —— 站著不動的怪（噬人花那種完全不會動的更是）
   100% 被擋掉。使用者回報的「明明有怪卻說沒怪」「走不到怪那邊」就是這條。

現在改用**動作 tick `+0x134`**判斷「還在不在世界上」：活的實體它一直在更新，
被丟掉的實體它就停住（實機殘留物 tickΔ 5 秒~9 分鐘，活的在 ±0.5 秒內）。
版面與門檻的完整出處見 `services/actor.py`。

**全程唯讀**（ReadProcessMemory），不寫、不注入，GameGuard 看不到。
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from ro_toolbox.services import actor
from ro_toolbox.services.actor import ActorView
from ro_toolbox.services.gamedata import mob_names, mobs_on_map
from ro_toolbox.services.mapdata import MapTerrain
from ro_toolbox.services.memory_scan import MemoryScanner

log = logging.getLogger(__name__)

_MAX_GID = 0x7FFF_FFFF
_FULL_RESCAN_SEC = 5.0  # 冷區段輪掃一輪結束後，至少隔這麼久才重新輪一次
_SWEEP_CHUNK = 60  # 每次多掃幾個冷區段（把全掃攤平成每拍幾十毫秒）
_MIN_REGION = 0x1000
#: 背景發現執行緒兩輪之間睡多久。掃描本身就會花時間，這只是別把 CPU 佔滿 ——
#: 佔滿的代價是主迴圈那一拍變慢，走路看起來就一卡一卡。
_DISCOVER_IDLE = 0.08
#: 掃描階段的視野放寬幾格。掃描用的是「移動終點」（省一次讀取），
#: 走路中的怪終點會比現在位置遠一點，不放寬就會在牠跑過來的路上漏掉牠。
#: 真正的視野判斷在 `read_known()`，那裡用的是精確位置。
_SCAN_SLACK = 10

# ---- 以 class 欄位為錨的 dword 索引（class 在 GID-0x04，所以 GID 在 +1）----
_D_GID = 1
_D_VTABLE = _D_GID + actor.VTABLE // 4
_D_STATE = _D_GID + actor.STATE // 4
_D_DEST_X = _D_GID + actor.DEST_X // 4
_D_DEST_Y = _D_GID + actor.DEST_Y // 4
_D_TICK = _D_GID + actor.TICK // 4
_D_DEAD = _D_GID + actor.DEAD // 4
#: 掃描時 class 欄位前面要留多少 dword 讀得到（vtable 在最前面）
_HEAD_DWORDS = actor.HEAD // 4
#: class 欄位後面要留多少 dword 讀得到
_TAIL_DWORDS = _D_DEAD + 2


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

      1. **vtable 落在 Ragexe.exe 的模組映像內** —— 實體物件的第一個欄位。
         列舉不到模組時（GameGuard 偶爾會擋，[MEM-031]）這一關自動略過，
         其餘四關照常，屬安全退化。
      2. **死亡旗標 `+0x1A0` 是 0** —— 伺服器的 `0x0080 type=1` 同一拍它就變 1，
         而**屍體的 GID 不會失效**、座標也還在。少了這道就是對著屍體送攻擊
         （使用者說的「打空氣」）。封包對照：11 次死亡全中、
         活體 1007 筆取樣裡 1006 筆是 0。
      3. **動作 tick `+0x134` 還在更新**（`actor.FRESH_MS` 內）——
         前兩道都沒觸發時的最後一道網子（例如換圖後被丟下的整批實體）。
         走出視野的實體 GID 會被寫成 `0xFFFFFFFF`，由下面第 5 條擋掉。
      4. class ID 在**這張地圖的出沒表**裡（[DAT-016]）
      5. GID 在合理範圍（**離開視野的實體 GID 是 `0xFFFFFFFF`，這裡就擋掉了**）
      6. **座標**（`+0x5C/+0x60`，走路中讀路徑節點）落在地圖內、
         而且站在 .gat 的可走格上
      7. 離角色不超過 view（超出視野的不可能是現在看得到的怪）

    每次掃「曾經掃到過怪」的熱區段（實測 4 ms），再加掃一小段冷區段慢慢輪完
    整份記憶體 —— 一次掃全部要 0.5~1.5 秒，直接做會讓 bot 每隔幾秒定格一下。
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
        #: 模組映像範圍（vtable 驗證用）。None = 列舉不到，那一關略過。
        self._module: tuple[int, int] | None = None
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
        self._module = self._main_image()
        return True

    def _main_image(self) -> tuple[int, int] | None:
        """主程式映像的範圍。查不到回 None —— vtable 那一關就略過（安全退化）。"""
        try:
            base = self._scanner.main_module_base()
            if base:
                for module in self._scanner.list_modules():
                    if module.base == base and module.size:
                        return base, base + module.size
        except (RuntimeError, OSError) as exc:  # noqa: BLE001 - 查不到不是錯誤
            log.debug("列舉模組失敗（vtable 驗證停用）：%s", exc)
        log.info("列舉不到主程式映像，實體驗證少一道 vtable（其餘照常）")
        return None

    # ---- 快路徑：只讀已知位址 ---------------------------------------

    def read_actor(self, addr: int, now_tick: int | None = None) -> MemoryEntity | None:
        """從**已知位址**直接讀一隻怪，**不看距離**。驗不過回 None。

        驗證條件與掃描那條路完全一樣（vtable、動作 tick、class 在表裡、
        座標有效且站在可走格），所以不會因為走快路徑而放寬標準。
        距離由呼叫端決定 —— 走出視野的怪位址還是好的，不該因此忘掉它
        （忘掉就要等背景輪掃好幾秒才找得回來）。
        """
        view = ActorView.read(self._scanner, addr)
        if view is None:
            return None
        if self._module is not None and not (
            self._module[0] <= view.vtable < self._module[1]
        ):
            return None                      # 這塊記憶體已經不是實體物件了
        gid = view.gid
        class_id = view.class_id
        if not (0 < gid < _MAX_GID) or not (0 < class_id < 65536):
            return None
        if not self._lut[class_id]:
            return None
        if view.dead:
            return None                      # 屍體：GID 還在、座標還在，但打不到
        if not view.fresh(now_tick):
            return None                      # 動作 tick 停了＝已經不在世界上
        cell = view.cell(self._scanner)
        if cell is None:
            return None
        cell_x, cell_y = cell
        if not (0 < cell_x < self._terrain.width and 0 < cell_y < self._terrain.height):
            return None
        if not self._terrain.is_walkable(cell_x, cell_y):
            return None
        return MemoryEntity(gid, class_id, cell_x, cell_y, addr)

    def read_one(self, addr: int, me: tuple[int, int]) -> MemoryEntity | None:
        """`read_actor()` 再加一道「還在視野內」。"""
        entity = self.read_actor(addr)
        if entity is None:
            return None
        if max(abs(entity.x - me[0]), abs(entity.y - me[1])) > self._view:
            return None
        return entity

    def read_known(self, me: tuple[int, int]) -> list[MemoryEntity]:
        """讀所有記著的怪，回傳還活著、還在視野內的那些。**很便宜。**

        ⚠ **走出視野不等於忘掉牠**：位址還是好的，牠走回來的下一拍就又看得到。
        只有「讀不到／GID 換人／動作 tick 停了／座標驗不過」才從清單移除 ——
        那才是真的「那塊記憶體不再是牠」。舊版一律移除，於是怪一走遠就要
        等背景輪掃好幾秒才找得回來。
        """
        with self._known_lock:
            known = dict(self._known)
        now_tick = actor.now_tick()
        out: list[MemoryEntity] = []
        dead: list[int] = []
        for gid, addr in known.items():
            entity = self.read_actor(addr, now_tick)
            if entity is None or entity.gid != gid:
                dead.append(gid)             # gid 對不上＝那塊記憶體換人住了
                continue
            if max(abs(entity.x - me[0]), abs(entity.y - me[1])) <= self._view:
                out.append(entity)
        if dead:
            with self._known_lock:
                for gid in dead:
                    self._known.pop(gid, None)
        return out

    # ---- 背景發現 ---------------------------------------------------

    def start_discovery(self, position) -> None:  # noqa: ANN001 - 回目前座標的函式
        """在背景持續掃描記憶體找**新的**怪，把位址記起來。

        掃一輪整份記憶體要 0.5~1.5 秒，放在主迴圈裡會讓 bot 每拍卡住 ——
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
        記憶體慢慢輪過一遍 —— 一次掃全部要 0.5~1.5 秒，直接做會讓 bot 定格。

        ⚠ 這條路是**為了發現位址**，座標用的是「移動終點」（少讀一次路徑節點）。
        精確位置一律由 `read_known()` 回答。
        """
        start = self._now()
        regions = list(self._hot) + self._next_sweep_slice()
        now_tick = actor.now_tick()

        found: dict[int, MemoryEntity] = {}
        hot: set[tuple[int, int]] = set(self._hot)
        for base, size in regions:
            entities = self._scan_region(base, size, me, now_tick)
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

    def _scan_region(
        self, base: int, size: int, me: tuple[int, int], now_tick: int
    ) -> list[MemoryEntity]:
        if size < _MIN_REGION:
            return []
        buf = self._scanner.read_region(base, size)
        if buf is None:
            return []
        count = len(buf) // 4
        if count < _HEAD_DWORDS + _TAIL_DWORDS + 4:
            return []
        words = np.frombuffer(buf, dtype=np.uint32, count=count)

        # class ID 欄位在 GID-0x4，所以先找 class 再往前後算。
        # 開頭留 _HEAD_DWORDS 是因為 vtable 在 GID-0x110，讀得到才驗得了。
        stop = count - _TAIL_DWORDS
        if stop <= _HEAD_DWORDS:
            return []
        classes = words[:stop]
        candidate = classes < 65536
        candidate[:_HEAD_DWORDS] = False
        if not candidate.any():
            return []
        candidate &= self._lut[np.where(candidate, classes, 0)]
        index = np.nonzero(candidate)[0]
        if not len(index):
            return []

        gid = words[index + _D_GID]
        dest_x = words[index + _D_DEST_X].astype(np.int32)
        dest_y = words[index + _D_DEST_Y].astype(np.int32)
        # tick 差用無號算再折回有號 —— 走路中的實體 tick 是未來的值（差是負的）。
        age = (np.uint32(now_tick) - words[index + _D_TICK]).astype(np.int64)
        age = np.where(age > 0x7FFF_FFFF, age - 0x1_0000_0000, age)
        reach = self._view + _SCAN_SLACK
        ok = (
            (gid > 0)
            & (gid < _MAX_GID)
            & (age < actor.FRESH_MS)          # 動作 tick 停了＝已經不在世界上
            & (words[index + _D_DEAD] == 0)   # 死亡旗標：屍體不算怪
            & (dest_x > 0)
            & (dest_x < self._terrain.width)
            & (dest_y > 0)
            & (dest_y < self._terrain.height)
            & (np.abs(dest_x - me[0]) <= reach)
            & (np.abs(dest_y - me[1]) <= reach)
        )
        if self._module is not None:
            vtable = words[index + _D_VTABLE]
            ok &= (vtable >= self._module[0]) & (vtable < self._module[1])
        out: list[MemoryEntity] = []
        for k in np.nonzero(ok)[0]:
            cell_x, cell_y = int(dest_x[k]), int(dest_y[k])
            if not self._terrain.is_walkable(cell_x, cell_y):
                continue  # 怪不會站在不可走的格子上 —— 擋掉剩下的垃圾值
            i = int(index[k])
            out.append(
                MemoryEntity(int(gid[k]), int(classes[i]), cell_x, cell_y,
                             base + i * 4 + 4)
            )
        return out
