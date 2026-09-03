"""道具小圖（QIcon）。

⚠ **只有一份**：補水的下拉選單、黑名單視窗…都用這一支。以前這段長在
`ui/pages/farm_page.py` 裡，第二個要用的地方只能 import 一整頁的 UI
（而那一頁又會 import 回來，繞成一圈）。搬出來之後兩邊都只依賴這裡。

圖示的來源是打包資產 `assets/icons.bin`（`services/icons`）——
使用者的電腦**沒有 RODATA**，那是唯一的來源。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap

from ro_toolbox.services import icons

_ICON_CACHE: dict[int, QIcon] = {}
#: RO 的道具圖示用洋紅當透明色（實測 501 的左上角像素就是 #ff00ff）。
_TRANSPARENT = "#ff00ff"


def item_icon(item_id: int) -> QIcon:
    """道具小圖。找不到就回空 QIcon（介面照樣顯示文字，不拿別的圖來頂）。"""
    got = _ICON_CACHE.get(item_id)
    if got is not None:
        return got
    # 走 `icon_bytes` 不走 `icon_path`：使用者的電腦沒有 RODATA，
    # 圖示的唯一來源是打包資產 `assets/icons.bin`。
    data = icons.icon_bytes(item_id)
    icon = QIcon()
    if data is not None:
        pixmap = QPixmap()
        if pixmap.loadFromData(data) and not pixmap.isNull():
            image = pixmap.toImage()
            image.setAlphaChannel(image.createMaskFromColor(
                QColor(_TRANSPARENT).rgb(), Qt.MaskMode.MaskOutColor
            ))
            icon = QIcon(QPixmap.fromImage(image))
    _ICON_CACHE[item_id] = icon
    return icon
