"""通知窗：跑完一件會離開電腦的事（例：自動尋路到了）時，跳出來講一聲。

## 長相：**Windows 自己那種驚嘆號框**

使用者 2026-08-30 指定：「全部通知換一下，不要這種很難看，Windows 那種
預設驚嘆號通知就好」。所以這裡用 `QMessageBox` ——
系統自己的驚嘆號圖示、系統自己的視窗外框與按鈕。

⚠ 顏色要**跳出程式的佈景**：整支程式套了一份深色／淺色 QSS，不擋的話
訊息框會被染成跟卡片一樣的顏色，就不是「Windows 那種」了。
所以在框上自己指定一份用 `palette(...)` 的樣式，讓它跟系統走。

## 兩件非做不可的事（沿用舊版，踩過才知道）

1. **`WindowStaysOnTopHint`**：不置頂就等於沒有通知 —— 使用者按下自動尋路
   之後會切回遊戲，工具箱的視窗在後面，寫在裡面的字沒有人看得到。
2. **不搶焦點（`WA_ShowWithoutActivating`）＋ 不是 modal**：
   跳出來的視窗如果搶走焦點，**全螢幕的遊戲會被切到背景甚至最小化** ——
   那比沒通知糟得多。所以：顯示、置頂、但**絕不 `activateWindow()`**，
   而且用 `show()` 不用 `exec()`（`exec()` 會卡住整個事件迴圈）。

## 「一定要按確定」的那一種（`show_notice`）

有些事**不能讓它自己消失**：抵達目的地、角色死亡。使用者離開電腦回來時
要看得到發生過什麼，自動收掉等於沒講。那一種沒有自動關閉，只有按「確定」才收。

⚠ **不是每件事都值得跳框。** 自動補給跑完走回練功點是背景流程的一部分
（使用者：「補給回去地圖要開始自動戰鬥，並且不用跳出通知或驚嘆號」）——
那種只記日誌，不打擾人。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QMessageBox, QWidget

log = logging.getLogger(__name__)

#: 沒人點的話多久自己關掉。
AUTO_CLOSE_SEC = 20.0
#: 離螢幕上緣多遠。放上面是因為遊戲的重要資訊（血條、聊天）多半在下半部。
TOP_MARGIN = 48

#: ⚠ 讓訊息框**跳出程式的佈景**，用系統自己的顏色 ——
#: 不寫的話 app 層的 QSS 會把它染成深色卡片的樣子。
_SYSTEM_LOOK = """
QMessageBox { background-color: palette(window); }
QMessageBox QLabel { color: palette(window-text); font-size: 13px; }
QMessageBox QPushButton {
    background-color: palette(button);
    color: palette(button-text);
    border: 1px solid palette(mid);
    border-radius: 4px;
    padding: 5px 20px;
    min-width: 68px;
}
QMessageBox QPushButton:hover { background-color: palette(light); }
QMessageBox QPushButton:default { border: 2px solid palette(highlight); }
"""

#: 已經跳出來的通知。**一定要留參考**：Qt 的頂層視窗沒人持有的話會被
#: Python 回收，視窗會在下一次 GC 時無聲消失（跳出來又立刻不見）。
_LIVE: list[QWidget] = []


class TopToast(QMessageBox):
    """置頂、不搶焦點、按「確定」關掉的系統訊息框。"""

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

        `icon` 只用來分「警示」還是「一般通知」—— 圖案本身交給系統畫
        （使用者要的就是 Windows 自己那顆驚嘆號）。
        """
        super().__init__(None)
        self.setWindowTitle(title)
        self.setText(message)
        self.setIcon(
            QMessageBox.Icon.Warning if icon else QMessageBox.Icon.Information
        )
        self.setStandardButtons(QMessageBox.StandardButton.Ok)
        self.ok_button = self.button(QMessageBox.StandardButton.Ok)
        self.ok_button.setText("確定")
        self.setStyleSheet(_SYSTEM_LOOK)

        # ⚠ 這幾行是命脈，見模組說明：不是 modal、置頂、不搶焦點。
        #
        # ⚠⚠ **順序不能換**：`setWindowModality()` 會把視窗旗標重新套一次，
        # 把剛設好的 `WindowStaysOnTopHint` 洗掉（實測：設完是 True，
        # 呼叫 setWindowModality 之後變 False）。置頂沒了＝通知躲在
        # 全螢幕遊戲後面，等於沒有通知。所以旗標一定要**最後**設。
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        self._need_ok = need_ok
        if seconds > 0 and not need_ok:
            QTimer.singleShot(int(seconds * 1000), self.close)

    def showEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt 命名
        """⚠ 位置要在 `showEvent` 裡算：訊息框的大小是 Qt 佈局完才知道的。"""
        super().showEvent(event)
        self._place()

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
