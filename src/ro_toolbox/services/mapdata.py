"""地圖地形（.gat）解析。

地形是靜態資料，直接從 `RODATA/data/<地圖>.gat` 讀，不必碰遊戲記憶體
（依 CLAUDE.md 的優先序：這屬於「確認過不在記憶體裡就抄資源包」的第三順位，
 但地形本來就只有大更新才會變，而且從檔案讀是唯讀、零風險）。

格式（實測 moc_fild01 / payon / prt_fild05 三張，皆一致）：

    offset 0   magic  b"GRAT"
    offset 4   版本   1.2（兩個位元組）
    offset 6   width  uint32
    offset 10  height uint32
    offset 14  每格 20 bytes：4 個 float32 角落高度 + 1 個 uint32 地形類型

地形類型（依實測分佈判讀）：
    0 = 可行走          城鎮 36.6%、野外 51.9~65.3%
    1 = 不可行走        城鎮 63.4%（建築多）、野外 33.9~46.8%
    5 = 少量出現        只在野外（0.8~1.3%），城鎮沒有

⚠ 0 與 1 的語意有實測分佈支持；**5 的確切語意尚未驗證**（RO 慣例是
  「不可站但可穿越射擊」之類），要用到再確認，見 GAMEDATA 待驗證區。
"""

from __future__ import annotations

import gzip
import json
import logging
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from ro_toolbox.config.paths import ASSETS_DIR

log = logging.getLogger(__name__)

_RODATA_ROOT = Path(__file__).resolve().parents[3] / "RODATA"
# 解包工具與 grf 數量不同，地形檔會散在不同層。實測這次重新解包後
# `data/data` 有 47 個、`data0/data` 有 1042 個 —— 兩邊都要找，
# 先找 data.grf（比較新，會覆蓋 data0.grf 的同名檔）。
RODATA_DIRS = (
    _RODATA_ROOT / "data" / "data",
    _RODATA_ROOT / "data0" / "data",
    _RODATA_ROOT / "data",
)
#: 向後相容：舊程式碼直接用這個常數。指向第一個真的有地形檔的目錄。
RODATA_DIR = next((d for d in RODATA_DIRS if d.is_dir() and any(d.glob("*.gat"))),
                  RODATA_DIRS[0])

_MAGIC = b"GRAT"
_HEADER_SIZE = 14
_CELL_SIZE = 20
_TYPE_OFFSET_IN_CELL = 16

# ---- 打包用的地形資產 -----------------------------------------------
#
# `RODATA/` 只有開發機有，而且 1082 張 .gat 共 1800 MB，不可能打包進 exe。
# 沒有它的機器上「走路」整個不能用，症狀還是「讀不到地形」這種看起來像
# 資料壞掉的訊息 —— 使用者換一台電腦就踩到。
#
# 所以把「每格能不能站」壓成 1 bit（9,438 萬格 → 11.3 MB，gzip 後 1.5 MB）
# 做成資產隨 exe 一起走。產生方式見 `tools/build_terrain.py`。
TERRAIN_ASSET = ASSETS_DIR / "terrain.bin.gz"
TERRAIN_MAGIC = b"ROTR"
TERRAIN_VERSION = 1

WALKABLE_TYPES = frozenset({0})
"""可站立的地形類型。5 的語意未確認，保守起見不算可走。"""


class GatError(RuntimeError):
    """.gat 檔讀不成或格式不符。"""


@dataclass(frozen=True)
class MapTerrain:
    name: str
    width: int
    height: int
    types: np.ndarray  # shape (height, width)，uint32
    #: 這份地形是哪來的。`"gat"` = 原始 .gat（types 是真的地形類型）；
    #: `"asset"` = 打包資產（**只有可走與否**，types 是 0/1 的合成值）。
    #: 要用 `cell_type()` 判斷「type 5 是什麼」之類的事情之前先看這個。
    source: str = "gat"

    @property
    def walkable(self) -> np.ndarray:
        """布林陣列，True = 可站。"""
        return np.isin(self.types, list(WALKABLE_TYPES))

    def is_walkable(self, x: int, y: int) -> bool:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return False
        return int(self.types[y, x]) in WALKABLE_TYPES

    def cell_type(self, x: int, y: int) -> int | None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        return int(self.types[y, x])

    def walkable_ratio(self) -> float:
        return float(self.walkable.mean())

    def find_path(
        self,
        start: tuple[int, int],
        goal: tuple[int, int],
        node_budget: int = 40000,
        blocked: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None,
    ) -> list[tuple[int, int]] | None:
        """A* 在可走格上找 start→goal 的**逐格**路徑（不含起點）。

        回傳逐格而不是抽稀過的走點：單次移動封包有距離上限
        （實測 ≤17 格接受、18 格被伺服器忽略，見 GAMEDATA [PKT-030]），
        所以要沿著路徑「邊走邊往前挑下一個目標」——挑目標時得知道中間每一格，
        才能保證每段都在上限內、而且真的沿著算好的路走。
        找不到路（被牆隔開／超出 node_budget）回 None。

        `blocked` 是「地形上可走，但我們不想踩」的格子（自動打怪用它避開傳點 ——
        踩上去會被傳到別張地圖）。**起點永遠不算被擋**：站在上面時要走得出來，
        不然會整個算不出路（等於自己把自己關在裡面）。
        """
        import heapq

        avoid = blocked or ()

        if not self.is_walkable(*goal) or not self.is_walkable(*start):
            return None
        if start == goal:
            return []

        # 8 方向；斜走成本 √2，用整數近似（14/10）
        moves = [(1, 0, 10), (-1, 0, 10), (0, 1, 10), (0, -1, 10),
                 (1, 1, 14), (1, -1, 14), (-1, 1, 14), (-1, -1, 14)]
        w, h = self.width, self.height
        walk = self.walkable

        def heuristic(x: int, y: int) -> int:
            dx, dy = abs(x - goal[0]), abs(y - goal[1])
            return 10 * (dx + dy) - 6 * min(dx, dy)  # 對角距離

        open_heap = [(heuristic(*start), 0, start)]
        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        best_g = {start: 0}
        expanded = 0

        while open_heap and expanded < node_budget:
            _f, g, cur = heapq.heappop(open_heap)
            if cur == goal:
                return self._trace(came_from, start, goal)
            expanded += 1
            cx, cy = cur
            for dx, dy, cost in moves:
                nx, ny = cx + dx, cy + dy
                if not (0 <= nx < w and 0 <= ny < h) or not walk[ny, nx]:
                    continue
                if (nx, ny) in avoid:
                    continue
                # 不允許穿角（斜走時兩側至少一格要可走，避免卡牆角）
                if dx and dy and not (walk[cy, nx] or walk[ny, cx]):
                    continue
                ng = g + cost
                nxt = (nx, ny)
                if ng < best_g.get(nxt, 1 << 30):
                    best_g[nxt] = ng
                    came_from[nxt] = cur
                    heapq.heappush(open_heap, (ng + heuristic(nx, ny), ng, nxt))
        return None

    def line_clear(self, start: tuple[int, int], goal: tuple[int, int]) -> bool:
        """從 `start` 直直走到 `goal`，中間**每一格都可走**嗎？

        用 Bresenham 走一遍那條直線（不含起點、含終點），有任何一格不可走
        就回 False。判斷「有沒有障礙物」用它，不要用 `find_path()` 的長度 ——
        A* 會安靜地繞過去，繞完長度可能還是一樣（8 方向格子裡斜著閃開一格
        石頭不會多花步數），於是「中間有牆」就被算成「直直走得過去」。

        斜走沿用 `find_path()` 的**不穿角**規則：兩側都是牆就過不去。
        兩邊規則不一致的話，會出現「這裡說走得過去、走路那邊說不行」。
        """
        x, y = start
        gx, gy = goal
        if (x, y) == (gx, gy):
            return True
        dx, dy = abs(gx - x), abs(gy - y)
        sx = 1 if gx > x else -1
        sy = 1 if gy > y else -1
        err = dx - dy
        while (x, y) != (gx, gy):
            e2 = 2 * err
            step_x = e2 > -dy
            step_y = e2 < dx
            if step_x:
                err -= dy
                x += sx
            if step_y:
                err += dx
                y += sy
            if not self.is_walkable(x, y):
                return False
            if step_x and step_y and not (
                self.is_walkable(x, y - sy) or self.is_walkable(x - sx, y)
            ):
                return False  # 穿角：斜走的兩側都是牆，實際過不去
        return True

    def reachable_from(self, start: tuple[int, int]) -> frozenset[tuple[int, int]]:
        """從 `start` **走得到**的所有格子。起點不可走就回空集合。

        規則跟 `find_path()` 完全一致（8 方向、斜走不穿角），否則會出現
        「泛洪說走得到、A* 說走不到」這種互相矛盾的答案。

        為什麼需要它：城鎮的室內圖是**一張地圖裡好幾個互不相連的房間** ——
        實測 `prt_in` 有 26 個區塊、22 道各自獨立通往 prontera 的門。
        站在藥水店裡時只有那一道門走得到；挑到別間的門，A* 就回「走不到」，
        整條路線因此被判失敗（使用者回報：主城商店裡面尋路不出來）。
        一次泛洪把「這間房間通到哪些格」算出來，之後挑門用查表，
        比一道一道去跑 A* 便宜得多，也不會漏掉真的走得到的那一道。
        """
        from collections import deque

        if not self.is_walkable(*start):
            return frozenset()
        walk = self.walkable          # 只算一次：`walkable` 每次呼叫都重建整個陣列
        w, h = self.width, self.height
        seen = np.zeros((h, w), dtype=bool)
        seen[start[1], start[0]] = True
        found = [start]
        queue = deque(found)
        moves = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
        while queue:
            cx, cy = queue.popleft()
            for dx, dy in moves:
                nx, ny = cx + dx, cy + dy
                if not (0 <= nx < w and 0 <= ny < h) or seen[ny, nx] or not walk[ny, nx]:
                    continue
                if dx and dy and not (walk[cy, nx] or walk[ny, cx]):
                    continue  # 不穿角，跟 find_path 同一條規則
                seen[ny, nx] = True
                found.append((nx, ny))
                queue.append((nx, ny))
        return frozenset(found)

    def walkable_cells(self) -> int:
        """整張圖有幾格可站。跟 `reachable_from()` 一比就知道這張圖分不分房間。"""
        return int(self.walkable.sum())

    @staticmethod
    def _trace(came_from, start, goal) -> list[tuple[int, int]]:
        """把 came_from 回溯成 start→goal 的逐格路徑（不含起點）。"""
        path = [goal]
        node = goal
        while node != start:
            node = came_from[node]
            path.append(node)
        path.reverse()
        return path[1:]

    @staticmethod
    def waypoints(cells: list[tuple[int, int]], max_step: int) -> list[tuple[int, int]]:
        """把逐格路徑抽稀成走點：每段都保證「路徑步數」與「直線距離」都 ≤ max_step。

        用步數而不只是直線距離來切，是因為伺服器要自己重算這一段的路；
        繞過障礙的一段可能直線只有 10 格、實際要走 25 步，這種請求會被無視。
        """
        out: list[tuple[int, int]] = []
        anchor: tuple[int, int] | None = None
        steps = 0
        for cell in cells:
            steps += 1
            reference = anchor or cell
            far = max(abs(cell[0] - reference[0]), abs(cell[1] - reference[1]))
            if steps >= max_step or far >= max_step:
                out.append(cell)
                anchor = cell
                steps = 0
        if cells and (not out or out[-1] != cells[-1]):
            out.append(cells[-1])
        return out

    def random_walkable(
        self, rng, near=None, radius=0, min_radius=0, tries=60
    ) -> tuple[int, int] | None:
        """隨機挑一個可走格。near+radius 限制在某點附近；min_radius 保證「夠遠」。

        夠遠很重要：目標挑太近，走沒幾步就要停下來重新規劃，看起來就是
        「走一小段、停一下」。挑遠一點就能一路走過去，中途遇怪再插隊處理。
        """
        for attempt in range(tries):
            if near is not None and radius:
                x = min(self.width - 2, max(1, near[0] + rng.randint(-radius, radius)))
                y = min(self.height - 2, max(1, near[1] + rng.randint(-radius, radius)))
            else:
                x = rng.randint(1, self.width - 2)
                y = rng.randint(1, self.height - 2)
            if not self.is_walkable(x, y):
                continue
            if near is not None and min_radius:
                far = max(abs(x - near[0]), abs(y - near[1]))
                # 最後 1/3 次數放寬距離要求，免得在死巷裡永遠挑不出點
                need = min_radius if attempt < tries * 2 // 3 else min_radius // 2
                if far < need:
                    continue
            return x, y
        return None

    def __repr__(self) -> str:
        return (
            f"MapTerrain({self.name} {self.width}x{self.height} "
            f"可走 {self.walkable_ratio() * 100:.1f}%)"
        )


def gat_path(map_name: str, data_dir: Path | None = None) -> Path:
    """地圖名可帶或不帶副檔名，一律轉成 .gat 路徑。

    沒指定目錄時會依序找 `RODATA_DIRS`；都找不到就回第一個候選，
    讓 `load_terrain` 拋出帶路徑的錯誤（要能看出它去哪裡找了）。
    """
    stem = map_name.rsplit(".", 1)[0] if "." in map_name else map_name
    if data_dir is not None:
        return data_dir / f"{stem}.gat"
    for directory in RODATA_DIRS:
        candidate = directory / f"{stem}.gat"
        if candidate.is_file():
            return candidate
    return RODATA_DIRS[0] / f"{stem}.gat"


@lru_cache(maxsize=1)
def _terrain_asset() -> tuple[dict[str, list[int]], bytes]:
    """載入打包的地形資產（索引, 位元組）。沒有或壞掉就回空的，讓呼叫端退回 .gat。"""
    try:
        raw = gzip.decompress(TERRAIN_ASSET.read_bytes())
    except (OSError, ValueError) as exc:
        log.warning("載入 %s 失敗：%s", TERRAIN_ASSET.name, exc)
        return {}, b""
    if len(raw) < 12 or raw[:4] != TERRAIN_MAGIC:
        log.warning("%s 不是地形資產（開頭是 %r）", TERRAIN_ASSET.name, raw[:4])
        return {}, b""
    version, head_len = struct.unpack_from("<II", raw, 4)
    if version != TERRAIN_VERSION or len(raw) < 12 + head_len:
        log.warning("%s 版本或長度不符（version=%s）", TERRAIN_ASSET.name, version)
        return {}, b""
    try:
        index = json.loads(raw[12 : 12 + head_len].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("%s 的索引解不開：%s", TERRAIN_ASSET.name, exc)
        return {}, b""
    log.debug("地形資產：%d 張地圖", len(index))
    return index, raw[12 + head_len :]


def _terrain_from_asset(stem: str) -> MapTerrain | None:
    """從打包資產取一張地形。資產裡沒有這張就回 None。"""
    index, blob = _terrain_asset()
    entry = index.get(stem)
    if entry is None:
        return None
    width, height, offset, size = entry
    chunk = blob[offset : offset + size]
    if len(chunk) != size:
        log.warning("地形資產裡 %s 的資料不完整", stem)
        return None
    bits = np.unpackbits(np.frombuffer(chunk, dtype=np.uint8), count=width * height)
    # 資產只存「可不可走」，所以把它還原成 types 的 0/1 —— 0 是可走、1 是不可走，
    # 與 .gat 的語意一致（[DAT-008]）。**但這不是真的地形類型**，見 `source`。
    types = np.where(bits.reshape(height, width) == 1, 0, 1).astype(np.uint32)
    return MapTerrain(name=stem, width=width, height=height, types=types, source="asset")


def load_terrain(map_name: str, data_dir: Path | None = None) -> MapTerrain:
    """讀取並解析地形。找不到或格式不符會拋 GatError。

    **先查打包資產，再退回 RODATA 的 .gat**：資產隨 exe 一起走，
    所以換一台電腦、重灌、沒有 RODATA 都能走路（`tools/build_terrain.py`）。
    .gat 留著是給開發機用的 —— 改版新增地圖時可以先解包測，再重跑腳本更新資產。

    `data_dir` 有指定就**只**讀那個目錄的 .gat（測試與工具用，不吃資產）。
    """
    if data_dir is None:
        stem = map_name.rsplit(".", 1)[0].lower() if "." in map_name else map_name.lower()
        terrain = _terrain_from_asset(stem)
        if terrain is not None:
            return terrain

    path = gat_path(map_name, data_dir)
    if not path.is_file():
        raise GatError(f"找不到地形檔：{path}")

    raw = path.read_bytes()
    if len(raw) < _HEADER_SIZE or raw[:4] != _MAGIC:
        raise GatError(f"{path.name} 不是 GRAT 檔（開頭是 {raw[:4]!r}）")

    width, height = struct.unpack_from("<II", raw, 6)
    expected = _HEADER_SIZE + width * height * _CELL_SIZE
    if width <= 0 or height <= 0 or len(raw) < expected:
        raise GatError(
            f"{path.name} 尺寸不合理：{width}x{height}，"
            f"需要 {expected} bytes 但只有 {len(raw)}"
        )

    cells = np.frombuffer(
        raw, dtype=np.uint8, count=width * height * _CELL_SIZE, offset=_HEADER_SIZE
    ).reshape(width * height, _CELL_SIZE)
    types = (
        cells[:, _TYPE_OFFSET_IN_CELL : _TYPE_OFFSET_IN_CELL + 4]
        .copy()
        .view("<u4")
        .reshape(height, width)
    )

    terrain = MapTerrain(name=path.stem, width=width, height=height, types=types)
    log.debug("載入地形 %r", terrain)
    return terrain


def has_terrain(map_name: str) -> bool:
    """走得到這張圖嗎（地形拿得到嗎）？

    給「決定要不要接受一個目的地」用。**不要**改回去問 `.gat` 檔在不在 ——
    使用者的電腦上沒有 `RODATA/`，那樣問等於全部拒絕。
    """
    stem = map_name.rsplit(".", 1)[0].lower() if "." in map_name else map_name.lower()
    if stem in _terrain_asset()[0]:
        return True
    return gat_path(stem).is_file()


def available_maps(data_dir: Path | None = None) -> int:
    """有幾張地圖的地形可用。同名只算一次（data.grf 會覆蓋 data0.grf）。

    沒指定目錄時把**打包資產**也算進來 —— 那才是使用者機器上真正的來源。
    """
    names: set[str] = set()
    if data_dir is None:
        names.update(_terrain_asset()[0])
    directories = [data_dir] if data_dir is not None else list(RODATA_DIRS)
    for directory in directories:
        if directory is not None and directory.is_dir():
            names.update(p.stem.lower() for p in directory.glob("*.gat"))
    return len(names)
