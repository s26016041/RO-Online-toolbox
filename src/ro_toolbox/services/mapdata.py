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

import logging
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

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
    ) -> list[tuple[int, int]] | None:
        """A* 在可走格上找 start→goal 的**逐格**路徑（不含起點）。

        回傳逐格而不是抽稀過的走點：單次移動封包有距離上限
        （實測 ≤17 格接受、18 格被伺服器忽略，見 GAMEDATA [PKT-030]），
        所以要沿著路徑「邊走邊往前挑下一個目標」——挑目標時得知道中間每一格，
        才能保證每段都在上限內、而且真的沿著算好的路走。
        找不到路（被牆隔開／超出 node_budget）回 None。
        """
        import heapq

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


def load_terrain(map_name: str, data_dir: Path | None = None) -> MapTerrain:
    """讀取並解析地形。找不到或格式不符會拋 GatError。"""
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


def available_maps(data_dir: Path | None = None) -> int:
    """有幾張地圖的地形檔可用。同名只算一次（data.grf 會覆蓋 data0.grf）。"""
    directories = [data_dir] if data_dir is not None else list(RODATA_DIRS)
    names: set[str] = set()
    for directory in directories:
        if directory is not None and directory.is_dir():
            names.update(p.stem for p in directory.glob("*.gat"))
    return len(names)
