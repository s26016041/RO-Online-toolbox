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

## 「一定要按確定」的那一種（`show_notice`）

有些事**不能讓它自己消失**：抵達目的地、角色死亡。使用者離開電腦回來時
要看得到發生過什麼，自動收掉等於沒講（使用者指定：驚嘆號框、按確定才消失）。
那一種用 `show_notice()` —— 一樣置頂、一樣不搶焦點，但**沒有自動關閉、
點畫面也不會關**，只有按「確定」才收。

⚠ 仍然**不是** QMessageBox：modal 會跳到全螢幕遊戲後面，看不到也點不到。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

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
QLabel#toastIcon  { color: #ffcc44; font-size: 30px; }
QPushButton#toastOk {
    background-color: #3b7bf5;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 6px 26px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#toastOk:hover { background-color: #5590ff; }
"""

#: 已經跳出來的通知。**一定要留參考**：Qt 的頂層視窗沒人持有的話會被
#: Python 回收，視窗會在下一次 GC 時無聲消失（跳出來又立刻不見）。
_LIVE: list[QWidget] = []


class TopToast(QWidget):
    """一個置頂、不搶焦點、點一下就關的小通知窗。"""

    def __init__(
        self,
        title: str,
        message: str,
        seconds: float = AUTO_CLOSE_SEC,
        *,
        icon: str = "",
        need_ok: bool = False,
    ) -> None:
        """`need_ok=True` = **要按「確定」才會消失**（不自動關、點畫面也不關）。

        用在「不能自己消失」的事：抵達目的地、角色死亡。使用者離開電腦回來
        要看得到發生過什麼 —— 自動收掉等於沒講。
        """
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

        self._need_ok = need_ok

        box = QVBoxLayout(body)
        box.setContentsMargins(18, 14, 18, 12)
        box.setSpacing(6)

        # 有圖示時左邊放一欄（驚嘆號），文字靠右排 —— 一眼看得出是警示還是通知
        words = QWidget(body)
        words_box = QVBoxLayout(words)
        words_box.setContentsMargins(0, 0, 0, 0)
        words_box.setSpacing(6)

        head = QLabel(title, words)
        head.setObjectName("toastTitle")
        head.setWordWrap(True)
        words_box.addWidget(head)

        text = QLabel(message, words)
        text.setObjectName("toastText")
        text.setWordWrap(True)
        words_box.addWidget(text)

        if icon:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(14)
            mark = QLabel(icon, body)
            mark.setObjectName("toastIcon")
            mark.setAlignment(Qt.AlignmentFlag.AlignTop)
            row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(words, 1)
            box.addLayout(row)
        else:
            box.addWidget(words)

        if need_ok:
            # ⚠ 要按確定才消失，所以**不能**再寫「點一下關閉」——
            # 提示跟實際行為不一致比沒有提示更糟。
            buttons = QHBoxLayout()
            buttons.setContentsMargins(0, 6, 0, 0)
            buttons.addStretch(1)
            self.ok_button = QPushButton("確定", body)
            self.ok_button.setObjectName("toastOk")
            self.ok_button.clicked.connect(self.close)
            buttons.addWidget(self.ok_button)
            box.addLayout(buttons)
        else:
            hint = QLabel("點一下關閉", body)
            hint.setObjectName("toastHint")
            box.addWidget(hint)

        self.setFixedWidth(WIDTH)
        self.adjustSize()
        self._place()
        if seconds > 0 and not need_ok:
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
        if self._need_ok:
            return  # 要按「確定」才算看過了，點到旁邊不算
        self.close()

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt 命名
        if self in _LIVE:
            _LIVE.remove(self)
        super().closeEvent(event)


def show_notice(title: str, message: str) -> QWidget | None:
    """**要按「確定」才會消失**的置頂驚嘆號框（使用者指定的那一種）。

    用在抵達目的地與角色死亡：那兩件事發生時人多半不在電腦前，
    自動收掉的通知等於沒有發生過。
    """
    return show_toast(title, message, seconds=0, icon="⚠", need_ok=True)


def show_toast(
    title: str,
    message: str,
    seconds: float = AUTO_CLOSE_SEC,
    *,
    icon: str = "",
    need_ok: bool = False,
) -> QWidget | None:
    """跳一個置頂通知。沒有 GUI（例如測試、無視窗環境）時回 None，不炸掉呼叫端。"""
    if QGuiApplication.instance() is None:
        log.info("通知（沒有視窗環境，只記日誌）：%s —— %s", title, message)
        return None
    toast = TopToast(title, message, seconds, icon=icon, need_ok=need_ok)
    _LIVE.append(toast)
    toast.show()
    toast.raise_()  # 只拉到最前面，**不** activateWindow()：那會搶走遊戲的焦點
    return toast
