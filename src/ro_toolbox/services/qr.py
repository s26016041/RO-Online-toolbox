"""QR 解碼：把圖片變成 QR 裡的文字。

只負責「圖片 → 文字」，不管那串文字是什麼意思 —— 解讀交給 `services/totp.py`。

## 選型（會影響打包出來的 exe 大小，所以記一下）

- 解碼用 **zxing-cpp**（1MB wheel，自帶原生 DLL）。
  **不要換成 opencv**：為了解一張 QR 會讓單一 exe 多幾十 MB。
- 讀圖與剪貼簿用 **PySide6 的 QImage**，所以不需要 Pillow。
- **刻意不用 numpy**：numpy 在本專案是選用相依（記憶體掃描才裝），
  QR 這條不該把它變成必要。zxing-cpp 收得下一般的 buffer，
  用 `memoryview.cast` 給它形狀就好。

沒裝 zxing-cpp 時 `available()` 回 False，UI 把按鈕停用並說原因 ——
不是靜靜地讓按鈕沒反應（照專案鐵則：失效要大聲）。
"""

from __future__ import annotations

import logging

from PySide6.QtGui import QImage

try:
    import zxingcpp
except ImportError:  # pragma: no cover - 取決於安裝方式
    zxingcpp = None

log = logging.getLogger(__name__)

MISSING_MESSAGE = "沒有安裝 zxing-cpp，無法解 QR 圖片。請執行：pip install zxing-cpp"


class QrError(RuntimeError):
    """解碼失敗。訊息是要直接給使用者看的。"""


def available() -> bool:
    return zxingcpp is not None


def decode_image(image: QImage) -> list[str]:
    """解一張圖裡的所有 QR，回文字清單（可能是空的）。"""
    if zxingcpp is None:
        raise QrError(MISSING_MESSAGE)
    if image.isNull():
        raise QrError("圖片是空的或格式讀不出來。")

    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    width, height, stride = gray.width(), gray.height(), gray.bytesPerLine()
    raw = bytes(gray.constBits())

    # QImage 每一列會補到 4 byte 對齊，stride 通常大於 width。
    # 不把 padding 切掉的話影像會每列往右斜一點點，QR 就再也對不上了 ——
    # 而且不會報錯，只會「掃不到」。
    if stride != width:
        raw = b"".join(raw[y * stride : y * stride + width] for y in range(height))

    try:
        results = zxingcpp.read_barcodes(
            memoryview(raw).cast("B", (height, width)),
            formats=zxingcpp.BarcodeFormat.QRCode | zxingcpp.BarcodeFormat.MicroQRCode,
        )
    except Exception as exc:  # noqa: BLE001 - 原生函式庫的例外型別不保證
        raise QrError(f"解碼時出錯：{exc}") from exc

    return [r.text for r in results if r.text]


def decode_file(path: str) -> list[str]:
    image = QImage(path)
    if image.isNull():
        raise QrError(f"讀不到圖檔或格式不支援：{path}")
    return decode_image(image)


def describe_failure(image: QImage) -> str:
    """掃不到時給一句有用的話，而不是「失敗」兩個字。"""
    if image.isNull():
        return "剪貼簿裡沒有圖片。用 Win+Shift+S 框選 QR 之後再按一次。"
    side = min(image.width(), image.height())
    if side < 120:
        return (
            f"圖片只有 {image.width()}x{image.height()}，太小了。"
            "框選時把整個 QR 含四個角框進來。"
        )
    return (
        f"這張 {image.width()}x{image.height()} 的圖裡找不到 QR。"
        "確認框選範圍完整包含四個角的定位方塊，而且沒有被縮放糊掉。"
    )
