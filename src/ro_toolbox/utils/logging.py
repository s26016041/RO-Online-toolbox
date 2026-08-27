"""日誌設定：同時輸出到檔案、主控台，以及 UI 的 log 面板。"""

from __future__ import annotations

import faulthandler
import logging
import time
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QObject, Signal

from ro_toolbox.config.paths import log_dir

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"

#: 崩潰紀錄檔要一直開著給 faulthandler 用，關掉就寫不進去了。
_crash_file = None


def _enable_crash_log() -> None:
    """把硬當機（access violation 之類）的堆疊寫進 crash.log。

    ⚠ 為什麼需要：程式硬當時 Python 來不及跑任何 `except`，`app.log` 只會
    停在最後一行正常訊息 —— 看起來像「什麼都沒發生就當掉」，完全沒有線索。
    `faulthandler` 是在**訊號／例外層**接的，硬當也寫得出堆疊。

    注意 access violation 有兩種：被別人接手處理掉的（first-chance，程式照跑）
    也會寫進來。所以看的時候要以**最後一筆**與後面有沒有正常訊息為準。
    """
    global _crash_file
    if _crash_file is not None:
        return
    try:
        _crash_file = open(log_dir() / "crash.log", "a", encoding="utf-8")
        _crash_file.write(
            "\n===== " + time.strftime("%Y-%m-%d %H:%M:%S") + " 這一次啟動 =====\n"
        )
        _crash_file.flush()
        faulthandler.enable(file=_crash_file, all_threads=True)
    except Exception as exc:  # noqa: BLE001 - 記錄崩潰失敗不該讓程式起不來
        logging.getLogger(__name__).warning("開不了崩潰紀錄檔：%s", exc)


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
    _enable_crash_log()
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


class StateLog:
    """同一件事一直發生時只講一次，狀態變了才再講。

    背景輪詢的東西（角色狀態每 12 秒、背包每 1.5 秒）失敗時照實記，
    幾分鐘就是幾百行一模一樣的字 —— 真正該看的錯誤全被洗掉，
    使用者也會以為程式壞了（實際發生過）。

    ⚠ 這是**降噪，不是消音**：第一次照原本的層級大聲講，
    重複的降到 DEBUG（打開除錯還是看得到），恢復時再講一句。
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger
        self._last: str | None = None

    def problem(self, key: str, level: int, msg: str, *args) -> None:
        """回報一個問題。`key` 一樣就當成「還是同一件事」。"""
        if self._last == key:
            self._log.debug(msg, *args)
            return
        self._last = key
        self._log.log(level, msg, *args)

    def ok(self, msg: str | None = None, *args) -> None:
        """回報恢復正常。之前有講過問題才需要說一聲。"""
        if self._last is not None and msg:
            self._log.info(msg, *args)
        self._last = None
