"""所有分頁的共同基底：統一標題排版與外距。"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

log = logging.getLogger(__name__)


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
        """關閉程式前的收尾。覆寫的子類別**要記得呼叫 `super().shutdown()`**。

        預設會把這個分頁身上**所有還在跑的 `WorkerThread` 都停掉**。

        ⚠ 為什麼要這道全面掃描，而不是各自列一份清單：漏掉一條的後果是
        Qt 在解構時喊「Destroyed while thread is still running」並用
        0xC0000409 **中止整個行程**。實際踩過（2026-08-27）：
        `AccountPage.shutdown()` 收了 `_offset_thread` 與 `_login_thread`，
        **漏了 `_link_thread`** —— 打包出來的 exe 自檢每一項都通過，
        卻在收尾時崩掉、一個字都沒印，看起來像打包壞掉。
        清單會漏，掃描不會。
        """
        from ro_toolbox.core.worker import WorkerThread

        for name, value in list(vars(self).items()):
            if isinstance(value, WorkerThread) and value.is_running:
                log.info("%s：停掉還在跑的 %s", type(self).__name__, name)
                value.stop()
