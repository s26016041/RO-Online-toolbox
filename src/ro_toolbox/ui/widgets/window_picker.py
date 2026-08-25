"""選擇目標視窗的共用元件。

封包頁與記憶體頁原本各有一份複本，行為容易走鐘（篩選欄只在按 Enter 時才更新、
列舉失敗會把清單清空），合併成一個元件以免同樣的 bug 修一邊漏一邊。

行為：

- **即時篩選**：打字當下就過濾，不必按 Enter；清空就立刻恢復完整清單。
  過濾的是已列舉的快取，不會每打一個字就重掃一次視窗。
- **列舉失敗不清空**：`enumerate_windows()` 偶發回空清單時保留上一次的結果，
  否則畫面會無聲清空（自動掛機頁踩過同樣的坑）。
- **選取以 PID 記憶**：不是用清單索引，篩選前後才不會選到別的視窗。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from ro_toolbox.config import ui_state
from ro_toolbox.services import window_list

log = logging.getLogger(__name__)

_EMPTY_TEXT = "（找不到符合的視窗）"


class WindowPicker(QWidget):
    """視窗下拉選單 + 即時篩選 + 重新整理。"""

    selection_changed = Signal()

    def __init__(
        self,
        label: str = "目標視窗",
        process_filter: str = "",
        combo_width: int = 420,
        state_key: str = "",
    ) -> None:
        super().__init__()
        self._state_key = state_key
        self._all: list[window_list.WindowInfo] = []
        self._shown: list[window_list.WindowInfo] = []
        self._process_filter = process_filter.lower()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.combo = QComboBox()
        self.combo.setMinimumWidth(combo_width)
        self.combo.currentIndexChanged.connect(lambda _i: self.selection_changed.emit())

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("輸入關鍵字篩選視窗標題…")
        # 150px 放不下中文關鍵字，打進去看不到自己輸入的內容
        self.filter_edit.setMinimumWidth(260)
        self.filter_edit.setMaximumWidth(320)
        self.filter_edit.setClearButtonEnabled(True)
        if state_key:
            self.filter_edit.setText(ui_state.get(f"{state_key}.filter", ""))
        # 即時篩選：打字當下就生效，不必按 Enter
        self.filter_edit.textChanged.connect(self._apply_filter)

        self.refresh_button = QPushButton("重新整理")
        self.refresh_button.clicked.connect(self.refresh)

        layout.addWidget(QLabel(label))
        layout.addWidget(self.combo)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.refresh_button)
        layout.addStretch(1)

        self.refresh()

    # ---- 資料 -------------------------------------------------------

    def refresh(self) -> None:
        """重新列舉視窗。列舉失敗時保留上一次的結果。"""
        windows = window_list.enumerate_windows()
        if not windows and self._all:
            # 一個視窗都列不到＝列舉本身失敗，不要把清單清空
            log.debug("視窗列舉回傳空清單，保留既有清單")
            return

        if self._process_filter:
            windows = [
                w for w in windows if w.process_name.lower() == self._process_filter
            ]
        self._all = windows
        self._apply_filter()

    def _apply_filter(self) -> None:
        if self._state_key:
            ui_state.set(f"{self._state_key}.filter", self.filter_edit.text())
        keyword = self.filter_edit.text().strip().lower()
        if keyword:
            self._shown = [w for w in self._all if keyword in w.title.lower()]
        else:
            self._shown = list(self._all)

        previous_pid = self.selected_pid()
        self.combo.blockSignals(True)
        self.combo.clear()
        for info in self._shown:
            self.combo.addItem(info.label, info.pid)
        if not self._shown:
            self.combo.addItem(_EMPTY_TEXT, None)
        elif previous_pid is not None:
            index = self.combo.findData(previous_pid)
            if index >= 0:
                self.combo.setCurrentIndex(index)
        self.combo.blockSignals(False)
        self.selection_changed.emit()

    # ---- 查詢 -------------------------------------------------------

    def selected_pid(self) -> int | None:
        data = self.combo.currentData()
        return int(data) if data is not None else None

    def selected(self) -> window_list.WindowInfo | None:
        pid = self.selected_pid()
        if pid is None:
            return None
        return next((w for w in self._shown if w.pid == pid), None)

    def count(self) -> int:
        return len(self._shown)

    def set_enabled(self, enabled: bool) -> None:
        self.combo.setEnabled(enabled)
        self.filter_edit.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
