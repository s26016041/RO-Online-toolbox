r"""把 RODATA 的 .gat 地形壓成一份可以打包進 exe 的資產。

    .\.venv\Scripts\python.exe tools\build_terrain.py

**為什麼要有這個**：走路類功能（自動打怪的漫遊、自動尋路）都要地形才能算路徑，
而地形以前是從 `RODATA/data/**.gat` 讀的 —— 那個資料夾只有開發機有，
`RO-Online-toolbox.spec` 也沒打包它（1082 張、**1800 MB**，也不可能打包）。
結果就是：在開發機上好好的，**換一台電腦裝 exe 就整個不會走路**，
而且症狀是「讀不到地形」這種看起來像資料壞掉的訊息。

**只留一個 bit**：路徑規劃只問「這格能不能站」，不需要四個角落高度。
1082 張、9,438 萬格 → 1 bit/格 = 11.3 MB，gzip 之後 **1.5 MB**。

輸出 `assets/terrain.bin.gz`，格式（整份 gzip）：

    "ROTR"            4 bytes  魔數
    version           uint32   目前 1
    index_len         uint32
    index             JSON，{地圖名: [寬, 高, 位移, 位元組數]}
    blob              各地圖的 np.packbits(可走) 依序接起來

⚠ **只有可走與否，沒有原始地形類型**。`.gat` 的 type 5 語意還沒確認
（[DAT-008]），目前 `WALKABLE_TYPES` 只收 0，所以 5 在這裡會被壓成「不可走」——
與現行行為完全一致。日後若確認 5 可走，要**重跑這支腳本**，不是改讀取端。

改版新增地圖時也要重跑。抽不到的地圖會安全退化成「讀不到地形」（大聲停用）。
"""

from __future__ import annotations

import gzip
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from ro_toolbox.services.mapdata import (  # noqa: E402
    RODATA_DIRS,
    TERRAIN_ASSET,
    TERRAIN_MAGIC,
    TERRAIN_VERSION,
    WALKABLE_TYPES,
    _CELL_SIZE,
    _HEADER_SIZE,
    _MAGIC,
    _TYPE_OFFSET_IN_CELL,
)


def _gat_files() -> dict[str, Path]:
    """所有地形檔。同名只收一次 —— data.grf 比較新，會蓋掉 data0.grf 的。"""
    found: dict[str, Path] = {}
    for directory in RODATA_DIRS:
        if directory.is_dir():
            for path in sorted(directory.glob("*.gat")):
                found.setdefault(path.stem.lower(), path)
    return found


def _walkable_bits(path: Path) -> tuple[int, int, bytes] | None:
    """回 (寬, 高, 打包後的可走位元)。檔案不合格式就回 None（跳過，不猜）。"""
    raw = path.read_bytes()
    if len(raw) < _HEADER_SIZE or raw[:4] != _MAGIC:
        return None
    width, height = struct.unpack_from("<II", raw, 6)
    need = _HEADER_SIZE + width * height * _CELL_SIZE
    if width <= 0 or height <= 0 or len(raw) < need:
        return None
    cells = np.frombuffer(
        raw, dtype=np.uint8, count=width * height * _CELL_SIZE, offset=_HEADER_SIZE
    ).reshape(width * height, _CELL_SIZE)
    types = (
        cells[:, _TYPE_OFFSET_IN_CELL : _TYPE_OFFSET_IN_CELL + 4]
        .copy()
        .view("<u4")
        .reshape(height, width)
    )
    walkable = np.isin(types, list(WALKABLE_TYPES))
    return width, height, np.packbits(walkable.reshape(-1)).tobytes()


def main() -> int:
    files = _gat_files()
    if not files:
        print(f"找不到任何 .gat（找過 {[str(d) for d in RODATA_DIRS]}）", file=sys.stderr)
        return 1
    print(f"地形檔 {len(files):,} 張")

    index: dict[str, list[int]] = {}
    blob = bytearray()
    skipped: list[str] = []
    for stem, path in files.items():
        parsed = _walkable_bits(path)
        if parsed is None:
            skipped.append(stem)
            continue
        width, height, packed = parsed
        index[stem] = [width, height, len(blob), len(packed)]
        blob += packed

    head = json.dumps(index, separators=(",", ":")).encode("utf-8")
    TERRAIN_ASSET.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(TERRAIN_ASSET, "wb", compresslevel=9) as handle:
        handle.write(TERRAIN_MAGIC)
        handle.write(struct.pack("<II", TERRAIN_VERSION, len(head)))
        handle.write(head)
        handle.write(bytes(blob))

    size = TERRAIN_ASSET.stat().st_size
    print(f"收錄 {len(index):,} 張，未壓縮 {len(blob) / 1024 / 1024:.1f} MB")
    if skipped:
        print(f"⚠ 跳過 {len(skipped)} 張格式不合的：{skipped[:5]}")
    for name in ("prontera", "prt_fild08", "mjolnir_06", "payon"):
        if name in index:
            w, h, _off, _n = index[name]
            print(f"  {name}：{w}x{h}")
    print(f"輸出：{TERRAIN_ASSET}（{size / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
