r"""把一張圖做成應用程式圖示（多尺寸 `.ico` ＋ 一張 png）。

    .\.venv\Scripts\python.exe tools\make_icon.py 22.png

輸出到 `src/ro_toolbox/ui/resources/`：

- `icon.ico` —— 內含 16/24/32/48/64/128/256 七種尺寸。
  Windows 會依場合自己挑（工作列 32、桌面 48、檔案總管大圖示 256），
  **只放一種尺寸的話，其他場合會由系統硬縮，邊緣糊掉。**
- `icon.png` —— 256×256，給不吃 ico 的地方用。

為什麼自己組 ICO 而不是讓 Qt 直接存：Qt 的 ico 寫出器一個檔只放一張圖。
ICO 的容器格式很單純（表頭 + 每張圖一筆目錄 + 資料），每一格直接塞
PNG 位元組（Vista 以後支援），所以自己組反而最省事、也不必多裝 Pillow。

縮圖一律用 `SmoothTransformation`：這張圖細節多（毛、字），
用最近鄰縮到 16px 會變成雜訊。
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, Qt
from PySide6.QtGui import QImage

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "src" / "ro_toolbox" / "ui" / "resources"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def _png_bytes(image: QImage) -> bytes:
    # ⚠ 一定要用不帶參數的 QBuffer（它自己持有緩衝）。寫成
    # `QBuffer(QByteArray())` 的話，那個暫時的 QByteArray 會被 Python 回收，
    # QBuffer 留著懸空指標，`save()` 當場 segfault（實測 PySide6 6.11）。
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        msg = "PNG 編碼失敗"
        raise RuntimeError(msg)
    return bytes(buffer.data())


def build_ico(source: QImage, sizes=SIZES) -> bytes:
    """把一張圖縮成多個尺寸，組成一個 .ico 的位元組。"""
    frames = []
    for size in sizes:
        scaled = source.scaled(
            size, size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        frames.append((size, _png_bytes(scaled)))

    header = struct.pack("<HHH", 0, 1, len(frames))   # reserved, type=1(ICO), count
    offset = len(header) + 16 * len(frames)
    entries, blobs = b"", b""
    for size, data in frames:
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,   # 256 要寫成 0（欄位只有一個位元組）
            size if size < 256 else 0,
            0,        # 調色盤色數，全彩填 0
            0,        # 保留
            1,        # planes
            32,       # 每像素位元數
            len(data),
            offset,
        )
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main() -> int:
    ap = argparse.ArgumentParser(description="把一張圖做成應用程式圖示")
    ap.add_argument("source", type=Path, help="來源圖片（建議正方形）")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    if not args.source.exists():
        print(f"找不到 {args.source}")
        return 2

    # 不建立 QGuiApplication：QImage 是純資料類別，縮放與存檔都不需要 GUI。
    # （順帶一提，`QGuiApplication([])` 用空的 argv 會直接讓 Qt 存取違規當掉。）
    image = QImage(str(args.source))
    if image.isNull():
        print(f"讀不到圖片：{args.source}")
        return 2
    if image.width() != image.height():
        # 不自己裁 —— 裁掉哪一邊是設計決定，要由人決定，不是工具偷偷做。
        print(f"⚠ 來源不是正方形（{image.width()}×{image.height()}），"
              "縮放後比例會變。要裁請先裁好再跑這支。")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    ico = args.out_dir / "icon.ico"
    png = args.out_dir / "icon.png"
    ico.write_bytes(build_ico(image))
    image.scaled(256, 256, Qt.AspectRatioMode.IgnoreAspectRatio,
                 Qt.TransformationMode.SmoothTransformation).save(str(png), "PNG")

    print(f"{ico}　{ico.stat().st_size / 1024:.0f} KB（{len(SIZES)} 種尺寸）")
    print(f"{png}　{png.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
