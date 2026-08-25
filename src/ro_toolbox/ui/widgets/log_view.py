"""底部日誌面板。訂閱 LogBridge，顯示執行過程。"""

from __future__ import annotations

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

# 只有需要提醒的等級才指定顏色；DEBUG/INFO 留給 QSS 的文字色，
# 這樣淺色與深色主題都讀得清楚，不必為每個主題各寫一份。
_COLORS = {
    "WARNING": "#b26a00",
    "ERROR": "#c0392b",
    "CRITICAL": "#8e1b1b",
}
_MAX_BLOCKS = 2000


class LogView(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("logView")
        self.setReadOnly(True)
        self.setMaximumBlockCount(_MAX_BLOCKS)

    def append_record(self, level: str, text: str) -> None:
        color = _COLORS.get(level)
        if color is None:
            self.appendPlainText(text)
        else:
            escaped = (
                text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            self.appendHtml(f'<span style="color:{color};">{escaped}</span>')
        self.moveCursor(QTextCursor.MoveOperation.End)
