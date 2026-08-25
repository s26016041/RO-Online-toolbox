"""應用組裝點：建立 QApplication、載入設定與樣式、開主視窗。"""

from __future__ import annotations

import logging
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ro_toolbox import APP_ID, APP_NAME, ORG_NAME
from ro_toolbox.config.paths import icon_file, stylesheet_file
from ro_toolbox.config.settings import load_settings
from ro_toolbox.ui.main_window import MainWindow
from ro_toolbox.utils.logging import setup_logging

log = logging.getLogger(__name__)


def _apply_stylesheet(app: QApplication, theme: str) -> None:
    path = stylesheet_file(theme)
    if not path.exists():
        log.warning("找不到樣式檔：%s", path)
        return
    app.setStyleSheet(path.read_text(encoding="utf-8"))


def _claim_taskbar_identity() -> None:
    """跟 Windows 宣告自己是誰，工作列才會用我們的圖示。

    **這件事跟 `setWindowIcon` 是兩回事。** 視窗左上角吃的是視窗圖示，
    但工作列按鈕是依 AppUserModelID 分組的：不設的話，用 `python.exe`
    跑起來的視窗會被歸到 python.exe 底下，工作列顯示 **Python 的圖示**，
    改視窗圖示完全影響不到。打包成 exe 之後才會自動用 exe 內嵌的圖示。

    必須在**建立任何視窗之前**呼叫，設晚了那一輪不會生效。
    失敗只記一行 —— 圖示不對不影響任何功能，不值得擋住啟動。
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError) as exc:   # 非 Windows shell 或權限受限
        log.debug("設定 AppUserModelID 失敗（工作列圖示可能不對）：%s", exc)


def _apply_icon(app: QApplication) -> None:
    """設在 QApplication 上，所有視窗與工作列都會跟著用。

    找不到圖示檔只記一行 —— 沒有圖示不影響任何功能，不值得擋住啟動。
    """
    path = icon_file()
    if not path.exists():
        log.warning("找不到圖示檔：%s（跑 tools/make_icon.py 產生）", path)
        return
    app.setWindowIcon(QIcon(str(path)))


def create_app(argv: list[str] | None = None) -> tuple[QApplication, MainWindow]:
    """建立 app 與主視窗但不進入事件迴圈，方便測試直接呼叫。"""
    _claim_taskbar_identity()      # 一定要在建立視窗之前
    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    _apply_icon(app)

    settings = load_settings()
    log_bridge = setup_logging(settings.log_level)
    _apply_stylesheet(app, settings.theme)

    window = MainWindow(settings, log_bridge)
    return app, window


def run(argv: list[str] | None = None) -> int:
    app, window = create_app(argv)
    window.show()
    return app.exec()
