"""哪些格子**踩到就會被傳到別張地圖** —— 自動打怪與自動尋路共用同一份定義。

## 為什麼要抽出來共用

這份定義本來只有 `farm_bot` 有，`travel` 完全沒有 —— A* 不知道傳點的存在，
算出來的路徑就大方地穿過去。使用者實測的災難鏈就是這樣開始的：

    15:40:02  伺服器說我被移到 s_atelier (13, 119)      ← 走進一間店
    15:40:04  伺服器說我被移到 prontera  (268, 108)     ← 走出來，落在門邊
    15:40:04  正在計算 prontera 上從 (271,108) 到 (289,203) 的路徑…
    15:40:06  （連線換到 s_atelier 那台）                ← 第一步就踩回腳下那道門
    15:40:09  send 失敗，WSA 錯誤 10054                 ← 來回刷到被伺服器斷線

**出門就在門邊**，所以只要目標在門的另一側，路徑第一格就是那道門。
擋傳點不是「走得漂亮一點」，是**不擋就會把自己刷到斷線**。

## 傳點資料只有取樣點，所以要補

`assets/warps.json.gz`（來自 `navi_link_tw.lub`）每個傳點只給**一格**，
實際的傳點是一片區域，而且一條傳點帶只被取樣幾次 —— 見 `warp_strips`。
照資料原封不動繞開，中間會留下十幾格寬的洞，走過去照樣被傳走。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from ro_toolbox.services.gamedata import warps_on_map

log = logging.getLogger(__name__)

#: 傳點周圍幾格內一律不踩。
#:
#: ⚠ 傳點資料只給一格、實際是一片區域（見上），所以要留餘裕。
#: 這是**同一件事的唯一定義**：自動打怪的禁區與自動尋路的繞行都用它。
KEEP_OUT = 3
#: 同一張圖上通往**同一張地圖**、又共線、又靠得這麼近的兩個傳點，
#: 當成**同一條傳點帶**，中間整段都不准踩。
STRIP_MAX = 60


def warp_strips(
    by_dest: dict[str, list[tuple[int, int]]],
) -> set[tuple[int, int]]:
    """把「同一張圖上通往同一個目的地、又共線、又靠得夠近」的傳點連成一條帶。

    ⚠ 這不是猜的，是**資料形狀本身**告訴我們的：`navi_link_tw.lub` 對一條
    傳點帶只取樣幾個點。實測 `moc_fild01` 往 `moc_fild02` 有三筆
    (301,16)/(321,16)/(341,16) **指向同一個目的地格** —— 那顯然是一條約 40 格
    寬的傳點帶，只被取樣三次。只擋取樣點周圍 3 格的話，中間留了兩個 14 格的洞，
    人走過去照樣被傳走（使用者實測回報「自動打怪走一走被傳到別的地圖」）。

    只連**共線**且距離 `STRIP_MAX` 以內的兩點：距離遠的多半是兩個各自
    獨立、剛好通往同一張圖的傳點（實測 `ayo_dun02` 有兩個相隔 252 格的），
    連起來會擋掉一整條沒事的路。
    """
    strip: set[tuple[int, int]] = set()
    for cells in by_dest.values():
        spots = sorted(set(cells))
        for i, a in enumerate(spots):
            for b in spots[i + 1:]:
                if a[0] != b[0] and a[1] != b[1]:
                    continue  # 不共線 = 不是同一條帶
                if max(abs(a[0] - b[0]), abs(a[1] - b[1])) > STRIP_MAX:
                    continue
                if a[0] == b[0]:
                    strip.update(
                        (a[0], y) for y in range(min(a[1], b[1]), max(a[1], b[1]) + 1)
                    )
                else:
                    strip.update(
                        (x, a[1]) for x in range(min(a[0], b[0]), max(a[0], b[0]) + 1)
                    )
    return strip


@lru_cache(maxsize=64)
def warp_cells(map_name: str) -> frozenset[tuple[int, int]]:
    """這張圖上「踩到就會被傳走」的格子：取樣點 ＋ 補起來的傳點帶。

    查不到就是空的 —— **安全退化成「跟以前一樣會踩到」**，不會因此走不了路。
    """
    cells: set[tuple[int, int]] = set()
    by_dest: dict[str, list[tuple[int, int]]] = {}
    for x, y, dest, _dx, _dy in warps_on_map(map_name):
        cells.add((x, y))
        by_dest.setdefault(dest, []).append((x, y))
    return frozenset(cells | warp_strips(by_dest))


def keep_out(
    cells: frozenset[tuple[int, int]] | set[tuple[int, int]],
    radius: int = KEEP_OUT,
) -> frozenset[tuple[int, int]]:
    """把傳點格擴成禁區（每格往外 `radius` 格）。"""
    zone: set[tuple[int, int]] = set()
    for x, y in cells:
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                zone.add((x + dx, y + dy))
    return frozenset(zone)


__all__ = ["KEEP_OUT", "STRIP_MAX", "keep_out", "warp_cells", "warp_strips"]
