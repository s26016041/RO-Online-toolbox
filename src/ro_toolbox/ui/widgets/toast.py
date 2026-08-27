"""置頂通知窗：跑完一件會離開電腦的事（例：自動尋路到了）時，跳出來講一聲。

## ⚠ 為什麼不是 QMessageBox

`memory_page` 已經踩過並寫進註解：**QMessageBox 是強制回應（modal）的**，
而 RO 通常全螢幕或置頂 —— 對話框會跳到遊戲**後面**，使用者看不到也點不到，
整個工具箱看起來就像當機（實際回報過）。所以這裡用一個自己畫的置頂小窗。

## 兩件非做不可的事

1. **`WindowStaysOnTopHint`**：不置頂就等於沒有通知 —— 使用者按下自動尋路
   之後會切回遊戲，工具箱的視窗在後面，寫在裡面的字沒有人看得到。
2. **`WA_ShowWithoutActivating`（不搶焦點）**：這條比置頂更重要。
   跳出來的視窗如果搶走焦點，**全螢幕的遊戲會被切到背景甚至最小化** ——
   那比沒通知糟得多。所以：顯示、置頂、但**絕不 activateWindow()**。

點一下就關；沒人理它就自己收掉（`AUTO_CLOSE_SEC`）。
留在畫面上不收會擋住遊戲畫面，那也是一種騷擾。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

log = logging.getLogger(__name__)

#: 沒人點的話多久自己關掉。
AUTO_CLOSE_SEC = 20.0
#: 離螢幕上緣多遠。放上面是因為遊戲的重要資訊（血條、聊天）多半在下半部。
TOP_MARGIN = 48
WIDTH = 460

_STYLE = """
QWidget#toastBody {
    background-color: #1b2230;
    border: 2px solid #3b7bf5;
    border-radius: 10px;
}
QLabel#toastTitle { color: #ffffff; font-size: 16px; font-weight: 600; }
QLabel#toastText  { color: #d7dce3; font-size: 13px; }
QLabel#toastHint  { color: #8b95a5; font-size: 11px; }
"""

#: 已經跳出來的通知。**一定要留參考**：Qt 的頂層視窗沒人持有的話會被
#: Python 回收，視窗會在下一次 GC 時無聲消失（跳出來又立刻不見）。
_LIVE: list[QWidget] = []


class TopToast(QWidget):
    """一個置頂、不搶焦點、點一下就關的小通知窗。"""

    def __init__(self, title: str, message: str, seconds: float = AUTO_CLOSE_SEC) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        # ⚠ 不搶焦點：搶了的話全螢幕遊戲會被切到背景。
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setStyleSheet(_STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = QWidget(self)
        body.setObjectName("toastBody")
        outer.addWidget(body)

        box = QVBoxLayout(body)
        box.setContentsMargins(18, 14, 18, 12)
        box.setSpacing(6)

        head = QLabel(title, body)
        head.setObjectName("toastTitle")
        head.setWordWrap(True)
        box.addWidget(head)

        text = QLabel(message, body)
        text.setObjectName("toastText")
        text.setWordWrap(True)
        box.addWidget(text)

        hint = QLabel("點一下關閉", body)
        hint.setObjectName("toastHint")
        box.addWidget(hint)

        self.setFixedWidth(WIDTH)
        self.adjustSize()
        self._place()
        if seconds > 0:
            QTimer.singleShot(int(seconds * 1000), self.close)

    def _place(self) -> None:
        """放在滑鼠所在那面螢幕的上緣正中央。

        用滑鼠那面而不是主螢幕：多螢幕的人遊戲多半開在自己正在看的那一面。
        取不到螢幕就維持預設位置，不要因此不顯示 —— 通知不見比位置不對糟。
        """
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.center().x() - self.width() // 2, area.top() + TOP_MARGIN)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt 命名
        self.close()

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt 命名
        if self in _LIVE:
            _LIVE.remove(self)
        super().closeEvent(event)


def show_toast(title: str, message: str, seconds: float = AUTO_CLOSE_SEC) -> QWidget | None:
    """跳一個置頂通知。沒有 GUI（例如測試、無視窗環境）時回 None，不炸掉呼叫端。"""
    if QGuiApplication.instance() is None:
        log.info("通知（沒有視窗環境，只記日誌）：%s —— %s", title, message)
        return None
    toast = TopToast(title, message, seconds)
    _LIVE.append(toast)
    toast.show()
    toast.raise_()  # 只拉到最前面，**不** activateWindow()：那會搶走遊戲的焦點
    return toast
