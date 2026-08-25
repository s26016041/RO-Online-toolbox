from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ro_toolbox.core.events import EngineState
from ro_toolbox.ui.pages.base_page import BasePage


class AutomationPage(BasePage):
    title = "自動化"
    subtitle = "腳本啟停、觸發條件、技能排程。"

    def build(self) -> None:
        controls = QWidget()
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.start_button = QPushButton("啟動")
        self.start_button.setObjectName("primaryButton")
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)

        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        row.addStretch(1)
        self.add(controls)

        self.state_label = QLabel(f"狀態：{EngineState.IDLE.label}")
        self.add(self.state_label)

        placeholder = QLabel("尚未實作：這裡會放觸發條件表格與腳本設定。")
        placeholder.setObjectName("placeholder")
        self.add(placeholder)
