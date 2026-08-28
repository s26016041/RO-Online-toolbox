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


#: 「執行日誌」面板與 app.log **至少**收到這個層級。
#:
#: ⚠⚠ **設定裡的 `log_level` 不准壓過它。** 那個設定預設是 `WARNING`，
#: 而所有功能的進度都是 `INFO`（自動尋路的 `TravelBot._note()`、自動登入的
#: 每一步、自動打怪的回報…）—— 於是使用者的面板與 app.log **一行進度都沒有**。
#:
#: 實際踩過兩次：
#:   1. 自動登入卡住時去撈 app.log，登入步驟全空，完全看不出卡在哪。
#:   2. 使用者回報「自動尋路都沒有提示文字出現，他在計算還是壞掉我都不知道」。
#:
#: 面板存在的唯一理由就是給人看進度；被記錄層級關掉等於這個功能不存在。
#: `log_level` 現在的意思是「**主控台**的層級，以及要不要更囉唆（DEBUG）」。
_UI_FLOOR = logging.INFO


def setup_logging(level: str = "INFO") -> LogBridge:
    """初始化 root logger，回傳供 UI 綁定的 bridge。"""
    _enable_crash_log()
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    asked = getattr(logging, level.upper(), logging.INFO)
    # 面板與檔案至少 INFO；使用者想要更囉唆（DEBUG）就照他的。
    kept = min(asked, _UI_FLOOR)

    root = logging.getLogger()
    root.setLevel(kept)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(asked)          # 主控台照設定，安靜就安靜
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir() / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(kept)      # 檔案是唯一的事後線索，不准比 INFO 安靜
    root.addHandler(file_handler)

    bridge = LogBridge()
    qt_handler = QtLogHandler(bridge)
    qt_handler.setFormatter(formatter)
    qt_handler.setLevel(kept)        # 面板就是給人看進度的
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

    def changed(self, key: str, level: int, msg: str, *args) -> None:
        """**狀態變了才講一次**；`key` 一樣就當成「還是同一件事」，降到 DEBUG。

        成功與失敗都適用：被輪詢的東西（背包每秒多一次）照實記的話，
        真正該看的訊息會被自己洗掉。
        """
        if self._last == key:
            self._log.debug(msg, *args)
            return
        self._last = key
        self._log.log(level, msg, *args)

    def problem(self, key: str, level: int, msg: str, *args) -> None:
        """`changed()` 的別名（早期命名，只用在失敗路徑）。"""
        self.changed(key, level, msg, *args)

    def ok(self, msg: str | None = None, *args) -> None:
        """回報恢復正常。之前有講過問題才需要說一聲。"""
        if self._last is not None and msg:
            self._log.info(msg, *args)
        self._last = None
