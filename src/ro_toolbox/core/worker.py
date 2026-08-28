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


#: 叫不停的背景執行緒。**故意不放掉引用** —— 見 `WorkerThread.stop()`。
#: 只會在收尾時長大，行程結束就跟著消失。
_STUCK: list = []

#: **正在跑的**背景執行緒。這裡也故意留著引用。
#:
#: ⚠⚠ 為什麼需要：呼叫端通常是 `self._xxx_thread = WorkerThread(worker)`，
#: 下一次再起一條就把上一條**覆蓋掉**。上一條要是還沒跑完，Python 這邊就沒人
#: 引用它了 → `QThread` 被解構 → Qt 喊
#: 「**QThread: Destroyed while thread '' is still running**」→ 中止整個行程。
#: 使用者實機踩到（帳號頁的連線狀態每幾秒起一條，`shutdown()` 又沒收它）。
#:
#: 這種事**不該要求每一個呼叫端自己記得**：漏掉一個地方就是整個程式掛掉，
#: 而且症狀跟那個地方一點關係都沒有。所以擋在這一層，一次擋掉全部。
_RUNNING: list = []


class WorkerThread:
    """把 Worker 搬到 QThread 上跑，並負責收尾。"""

    def __init__(self, worker: Worker) -> None:
        self.worker = worker
        self.thread = QThread()
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker._execute)
        worker.finished.connect(self.thread.quit)

    def start(self) -> None:
        # 已經跑完的可以安全丟掉；還在跑的一律留著（見 `_RUNNING`）。
        _RUNNING[:] = [t for t in _RUNNING if not t.thread.isFinished()]
        _RUNNING.append(self)
        self.thread.start()

    def stop(self, timeout_ms: int = 3000) -> None:
        """叫停並等它結束。**等不到也絕不讓它被解構。**

        ⚠ 停不下來是真的會發生的：讀遊戲記憶體的工作可能卡在 GameGuard 擋住的
        系統呼叫上（列舉模組實測會卡 3 秒以上，[MEM-030]）。那種執行緒**殺不得**
        —— 唯一安全的做法是**留著它的引用**到行程結束。

        不留的話：Python 這邊沒人引用 → QThread 被解構 → Qt 喊
        「Destroyed while thread is still running」→ 用 0xC0000409
        **中止整個行程**。實際踩過（[ENV-005]）：exe 自檢每一項都通過，
        卻在收尾時無聲崩掉，看起來像打包壞掉。

        寧可洩漏一條執行緒（行程反正要結束了），也不要把整個程式打掉。
        """
        self.worker.request_stop()
        self.thread.quit()
        if self.thread.wait(timeout_ms):
            _RUNNING[:] = [t for t in _RUNNING if t is not self]
        else:
            log.warning("背景執行緒未在 %sms 內結束，留著它直到行程結束", timeout_ms)
            # ⚠ worker 也要一起留：它被 moveToThread 到那條執行緒上。
            _STUCK.append((self.thread, self.worker))

    @property
    def is_running(self) -> bool:
        return self.thread.isRunning()
