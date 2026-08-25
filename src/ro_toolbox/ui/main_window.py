"""主視窗：組裝側欄、分頁堆疊、日誌面板與狀態列。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QWidget,
)

from ro_toolbox import APP_NAME, __version__
from ro_toolbox.config.settings import AppSettings, save_settings
from ro_toolbox.core.events import EngineState
from ro_toolbox.ui.pages.automation_page import AutomationPage
from ro_toolbox.ui.pages.base_page import BasePage
from ro_toolbox.ui.pages.dashboard_page import DashboardPage
from ro_toolbox.ui.pages.farm_page import FarmPage
from ro_toolbox.ui.pages.memory_page import MemoryPage
from ro_toolbox.ui.pages.packet_page import PacketPage
from ro_toolbox.ui.pages.settings_page import SettingsPage
from ro_toolbox.ui.widgets.log_view import LogView
from ro_toolbox.ui.widgets.sidebar import Sidebar
from ro_toolbox.utils.logging import LogBridge

log = logging.getLogger(__name__)

PAGE_CLASSES: list[type[BasePage]] = [
    DashboardPage,
    PacketPage,
    MemoryPage,
    FarmPage,
    AutomationPage,
    SettingsPage,
]


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings, log_bridge: LogBridge | None = None) -> None:
        super().__init__()
        self._settings = settings

        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.resize(settings.window.width, settings.window.height)
        self.setMinimumSize(900, 560)

        self._build_body()
        self._build_log_dock(log_bridge)
        self._build_status_bar()
        self._build_menu()

        if settings.window.maximized:
            self.showMaximized()

        self.sidebar.setCurrentRow(0)
        log.info("%s 啟動完成", APP_NAME)

        # 自動更新：**只在這裡查一次**，用到一半不重查（見 update_ui 檔頭）。
        # 延後匯入是為了讓 update_ui 只在真的要開視窗時才被拉進來。
        from ro_toolbox.ui.update_ui import UpdateManager

        self._updater = UpdateManager(self)
        self._updater.start()

    # ---- 組裝 -------------------------------------------------------

    def _build_body(self) -> None:
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar()
        self.stack = QStackedWidget()

        for page_class in PAGE_CLASSES:
            self.sidebar.add_entry(page_class.title)
            self.stack.addWidget(page_class())

        self.sidebar.page_selected.connect(self.stack.setCurrentIndex)

        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

    def _build_log_dock(self, log_bridge: LogBridge | None) -> None:
        self.log_view = LogView()
        dock = QDockWidget("執行日誌", self)
        dock.setObjectName("logDock")
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        dock.setWidget(self.log_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [170], Qt.Orientation.Vertical)
        self.log_dock = dock

        if log_bridge is not None:
            log_bridge.message.connect(self.log_view.append_record)

    def _build_status_bar(self) -> None:
        self.state_label = QLabel(f"狀態：{EngineState.IDLE.label}")
        self.statusBar().addPermanentWidget(self.state_label)
        self.statusBar().showMessage("就緒")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("檔案(&F)")
        file_menu.addAction("結束(&X)", self.close)

        view_menu = self.menuBar().addMenu("檢視(&V)")
        toggle = self.log_dock.toggleViewAction()
        toggle.setText("執行日誌(&L)")
        view_menu.addAction(toggle)

    # ---- 生命週期 ---------------------------------------------------

    def closeEvent(self, event) -> None:
        # 更新的背景執行緒要等它收完，否則 Qt 會在解構時抱怨
        #「Destroyed while thread is still running」並中止行程。
        self._updater.stop()

        for index in range(self.stack.count()):
            page = self.stack.widget(index)
            if isinstance(page, BasePage):
                page.shutdown()

        self._settings.window.maximized = self.isMaximized()
        if not self.isMaximized():
            self._settings.window.width = self.width()
            self._settings.window.height = self.height()
        save_settings(self._settings)
        super().closeEvent(event)
