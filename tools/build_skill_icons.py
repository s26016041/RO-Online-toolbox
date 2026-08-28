r"""把技能小圖打包成一份可以隨 exe 走的資產。

    .\.venv\Scripts\python.exe tools\build_skill_icons.py

技能圖跟道具圖放在**同一個資料夾**（`texture/유저인터페이스/item/`），但
`icons.bin` 只收「道具表用得到的資源名」，技能圖不在裡面 —— 所以另外收一份。

差別只有索引的鍵：道具用韓文資源名，技能用**英文代號**（`SM_BASH`）。
磁碟上的檔名是小寫（`sm_bash.bmp`），這裡存進索引的是**大寫代號**，
讀取端不必知道磁碟怎麼命名。

格式與 `icons.bin` 完全相同（見 `tools/build_icons.py`）：

    "ROIC" + version + index_gz_len + gzip(index) + 各桶 gzip

⚠ 為什麼要有這個：使用者的電腦沒有 `RODATA/`（CLAUDE.md）。
改版重新解包後重跑這支。
"""

from __future__ import annotations

import gzip
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ro_toolbox.services import icons as icons_mod  # noqa: E402
from ro_toolbox.services.gamedata import skill_table  # noqa: E402
from ro_toolbox.services.icons import (  # noqa: E402
    ICON_BUCKET,
    ICON_MAGIC,
    ICON_VERSION,
    SKILL_ICON_ASSET,
)


def main() -> int:
    root = icons_mod._ui_root()  # noqa: SLF001 - 這支就是要走解包目錄
    if root is None:
        print("找不到解包的 texture 目錄（RODATA 沒解包？）", file=sys.stderr)
        return 1
    item_dir = root / "item"
    if not item_dir.is_dir():
        print("找不到 item 圖示目錄", file=sys.stderr)
        return 1

    table = skill_table()
    if not table:
        print("技能表是空的 —— 先跑 tools/build_skill_table.py", file=sys.stderr)
        return 1
    wanted = sorted({entry["key"] for entry in table.values() if entry.get("key")})
    print(f"技能表有 {len(wanted):,} 個代號")

    found: list[tuple[str, bytes]] = []
    missing: list[str] = []
    for key in wanted:
        path = item_dir / (key.lower() + ".bmp")
        if not path.is_file():
            missing.append(key)
            continue
        found.append((key, path.read_bytes()))
    print(f"找到圖 {len(found):,} 張，缺 {len(missing):,} 張（缺的介面就不顯示圖示）")

    buckets: list[list[int]] = []
    index: dict[str, list[int]] = {}
    blob = bytearray()
    for start in range(0, len(found), ICON_BUCKET):
        chunk = found[start : start + ICON_BUCKET]
        raw = bytearray()
        for key, data in chunk:
            index[key] = [len(buckets), len(raw), len(data)]
            raw += data
        packed = gzip.compress(bytes(raw), 9)
        buckets.append([len(blob), len(packed)])
        blob += packed

    head = gzip.compress(
        json.dumps({"b": buckets, "i": index}, separators=(",", ":")).encode("utf-8"), 9
    )
    SKILL_ICON_ASSET.parent.mkdir(parents=True, exist_ok=True)
    with open(SKILL_ICON_ASSET, "wb") as handle:
        handle.write(ICON_MAGIC)
        handle.write(struct.pack("<II", ICON_VERSION, len(head)))
        handle.write(head)
        handle.write(bytes(blob))

    size = SKILL_ICON_ASSET.stat().st_size
    raw_total = sum(len(d) for _k, d in found)
    print(
        f"{len(buckets):,} 桶（每桶 {ICON_BUCKET} 張），"
        f"原始 {raw_total / 1024 / 1024:.1f} MB → {size / 1024:.0f} KB"
    )
    print(f"輸出：{SKILL_ICON_ASSET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
