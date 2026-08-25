from __future__ import annotations

from PySide6.QtWidgets import QLabel

from ro_toolbox.ui.pages.base_page import BasePage


class SettingsPage(BasePage):
    title = "設定"
    subtitle = "熱鍵、擷取來源、日誌等級。"

    def build(self) -> None:
        placeholder = QLabel("尚未實作：這裡會放設定表單，寫回 settings.json。")
        placeholder.setObjectName("placeholder")
        self.add(placeholder)
