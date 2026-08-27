r"""把道具小圖打包成一份可以隨 exe 走的資產。

    .\.venv\Scripts\python.exe tools\build_icons.py

**為什麼要有這個**：`services/icons.py` 以前直接讀
`RODATA/.../texture/유저인터페이스/item/<資源名>.bmp`。那個資料夾只有開發機有，
所以在別人的電腦上道具圖示**全部空白** —— 功能不會壞，但看起來像壞了。
（CLAUDE.md：資料檔也一樣，不准依賴只有開發機有的東西。）

只收 `item/` 那 10,928 張 24×24 小圖（17.5 MB）。**不收 `collection/`**：
那是 9,541 張大圖、199 MB，介面上的 24×24 圖示根本用不到。

**分桶壓縮**（每桶 128 張各自 gzip）：
壓縮率跟整份 solid gzip 幾乎一樣（4.0 MB vs 3.8 MB），
但取一張圖只要解壓那一桶（約 0.2 MB），不必把 17.5 MB 全攤在記憶體裡。
整份 zip（每檔各自 deflate）要 8.6 MB，太大。

輸出 `assets/icons.bin`：

    "ROIC"        4 bytes
    version       uint32
    index_gz_len  uint32
    gzip(index)   JSON：{"b": [[桶位移, 桶長度], ...],
                         "i": {資源名: [第幾桶, 桶內位移, 長度]}}
    桶資料         各桶的 gzip 依序接起來

**索引的鍵是資源名不是道具編號**：多個道具會共用同一張圖，
用資源名當鍵才不會把同一份 BMP 存好幾次（道具編號→資源名由 `items.json.gz` 提供）。

⚠ 資源名是**韓文**（`iteminfo` 的 `identifiedResourceName`，euc-kr），
而解包工具把 euc-kr 位元組當 latin-1 寫成檔名，所以磁碟上是亂碼（見 [DAT-001]）。
這裡存進索引的是**還原後的韓文資源名**，讀取端不必再處理磁碟檔名的亂碼。
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
from ro_toolbox.services.icons import (  # noqa: E402
    ICON_ASSET,
    ICON_BUCKET,
    ICON_MAGIC,
    ICON_VERSION,
)


def main() -> int:
    root = icons_mod._ui_root()  # noqa: SLF001 - 這支就是要走解包目錄
    if root is None:
        print("找不到解包的 texture 目錄（RODATA 沒解包？）", file=sys.stderr)
        return 1
    item_dir = root / "item"
    if not item_dir.is_dir():
        print(f"找不到 {item_dir}", file=sys.stderr)
        return 1

    # 只收「道具表真的會用到」的資源名 —— 其餘是別的用途的圖，收了只是佔空間。
    wanted = sorted(set(icons_mod._resources().values()))  # noqa: SLF001
    print(f"道具表用到 {len(wanted):,} 個資源名")

    found: list[tuple[str, bytes]] = []
    missing = 0
    for resource in wanted:
        path = item_dir / (icons_mod._mangled(resource) + ".bmp")  # noqa: SLF001
        if not path.is_file():
            missing += 1
            continue
        found.append((resource, path.read_bytes()))
    print(f"找到圖 {len(found):,} 張，缺 {missing:,} 張（缺的介面就不顯示圖示）")

    buckets: list[list[int]] = []
    index: dict[str, list[int]] = {}
    blob = bytearray()
    for start in range(0, len(found), ICON_BUCKET):
        chunk = found[start : start + ICON_BUCKET]
        raw = bytearray()
        for resource, data in chunk:
            index[resource] = [len(buckets), len(raw), len(data)]
            raw += data
        packed = gzip.compress(bytes(raw), 9)
        buckets.append([len(blob), len(packed)])
        blob += packed

    head = gzip.compress(
        json.dumps({"b": buckets, "i": index}, separators=(",", ":")).encode("utf-8"), 9
    )
    ICON_ASSET.parent.mkdir(parents=True, exist_ok=True)
    with open(ICON_ASSET, "wb") as handle:
        handle.write(ICON_MAGIC)
        handle.write(struct.pack("<II", ICON_VERSION, len(head)))
        handle.write(head)
        handle.write(bytes(blob))

    size = ICON_ASSET.stat().st_size
    raw_total = sum(len(d) for _r, d in found)
    print(
        f"{len(buckets):,} 桶（每桶 {ICON_BUCKET} 張），"
        f"原始 {raw_total / 1024 / 1024:.1f} MB → {size / 1024 / 1024:.1f} MB"
    )
    print(f"輸出：{ICON_ASSET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
