from __future__ import annotations

from PySide6.QtWidgets import QLabel

from ro_toolbox.ui.pages.base_page import BasePage


class DashboardPage(BasePage):
    title = "總覽"
    subtitle = "執行狀態、目標視窗、即時統計。"

    def build(self) -> None:
        placeholder = QLabel("尚未實作：這裡會放引擎狀態卡片與即時數據。")
        placeholder.setObjectName("placeholder")
        self.add(placeholder)
