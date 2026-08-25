"""所有分頁的共同基底：統一標題排版與外距。"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class BasePage(QWidget):
    title: str = ""
    subtitle: str = ""
    # 內容需要撐滿整頁的分頁（表格、檢視器）設為 False
    stretch_at_end: bool = True

    def __init__(self) -> None:
        super().__init__()
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(24, 20, 24, 20)
        self._layout.setSpacing(12)

        heading = QLabel(self.title)
        heading.setObjectName("pageTitle")
        self._layout.addWidget(heading)

        if self.subtitle:
            sub = QLabel(self.subtitle)
            sub.setObjectName("pageSubtitle")
            sub.setWordWrap(True)
            self._layout.addWidget(sub)

        self.build()
        if self.stretch_at_end:
            self._layout.addStretch(1)

    def build(self) -> None:
        """子類別在這裡加入自己的內容。"""

    def add(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)

    def shutdown(self) -> None:
        """關閉程式前的收尾，需要停止背景工作的分頁覆寫這個方法。"""
