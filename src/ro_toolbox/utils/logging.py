"""日誌設定：同時輸出到檔案、主控台，以及 UI 的 log 面板。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QObject, Signal

from ro_toolbox.config.paths import log_dir

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"


class LogBridge(QObject):
    """把 logging 記錄轉成 Qt signal，讓 UI 可以訂閱。"""

    message = Signal(str, str)  # (levelname, formatted_text)


class QtLogHandler(logging.Handler):
    """背景執行緒也能安全使用：Signal 會自動排到 UI 執行緒。"""

    def __init__(self, bridge: LogBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._bridge.message.emit(record.levelname, self.format(record))
        except RuntimeError:
            # 視窗已銷毀時忽略，不要讓 logging 反過來炸掉程式
            pass


def setup_logging(level: str = "INFO") -> LogBridge:
    """初始化 root logger，回傳供 UI 綁定的 bridge。"""
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir() / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    bridge = LogBridge()
    qt_handler = QtLogHandler(bridge)
    qt_handler.setFormatter(formatter)
    root.addHandler(qt_handler)

    return bridge
