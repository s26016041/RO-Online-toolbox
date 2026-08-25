"""背景工作基底類別。

自動化主迴圈一律跑在 worker 執行緒，UI 只透過 signal 溝通，
絕對不要在背景執行緒直接碰任何 QWidget。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger(__name__)


class Worker(QObject):
    """繼承後覆寫 run()，用 self._stop_requested 做中斷點檢查。"""

    started = Signal()
    finished = Signal()
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    @property
    def should_stop(self) -> bool:
        return self._stop_requested

    def run(self) -> None:  # pragma: no cover - 由子類別實作
        raise NotImplementedError

    def _execute(self) -> None:
        self.started.emit()
        try:
            self.run()
        except Exception as exc:
            log.exception("背景工作發生例外")
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class WorkerThread:
    """把 Worker 搬到 QThread 上跑，並負責收尾。"""

    def __init__(self, worker: Worker) -> None:
        self.worker = worker
        self.thread = QThread()
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker._execute)
        worker.finished.connect(self.thread.quit)

    def start(self) -> None:
        self.thread.start()

    def stop(self, timeout_ms: int = 3000) -> None:
        self.worker.request_stop()
        self.thread.quit()
        if not self.thread.wait(timeout_ms):
            log.warning("背景執行緒未在 %sms 內結束", timeout_ms)

    @property
    def is_running(self) -> bool:
        return self.thread.isRunning()
