"""左側導覽列。只負責發出選取事件，不知道頁面內容是什麼。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem


class Sidebar(QListWidget):
    page_selected = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(180)
        self.setSpacing(2)
        self.setIconSize(QSize(18, 18))
        self.currentRowChanged.connect(self.page_selected)

    def add_entry(self, title: str) -> None:
        item = QListWidgetItem(title)
        item.setSizeHint(QSize(0, 40))
        self.addItem(item)
