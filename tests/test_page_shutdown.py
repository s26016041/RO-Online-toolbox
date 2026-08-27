"""分頁收尾：**所有背景執行緒都要停**，一條都不能漏。

⚠ 漏掉一條的後果不是「有點慢」，是 Qt 在解構時喊
「Destroyed while thread is still running」並用 0xC0000409 **中止整個行程**。

實際踩過（2026-08-27）：`AccountPage.shutdown()` 收了 `_offset_thread` 與
`_login_thread`，**漏了 `_link_thread`** —— 打包出來的 exe 自檢每一項都通過，
卻在收尾時崩掉、一個字都沒印，看起來像打包壞掉。原始碼版本一模一樣崩。
"""

from __future__ import annotations

from ro_toolbox.core.worker import Worker, WorkerThread
from ro_toolbox.ui.pages.base_page import BasePage


class _Sleeper(Worker):
    """跑到被叫停為止。"""

    def run(self) -> None:
        while not self.should_stop:
            self.msleep(10) if hasattr(self, "msleep") else None
            import time

            time.sleep(0.01)


class _Page(BasePage):
    title = "測試分頁"

    def build(self) -> None:
        self.thread_a = WorkerThread(_Sleeper())
        self.thread_b = WorkerThread(_Sleeper())


def test_shutdown_stops_every_worker_thread(qtbot):
    """⚠ 靠**掃描**而不是靠清單 —— 清單會漏，掃描不會。"""
    page = _Page()
    qtbot.addWidget(page)
    page.thread_a.start()
    page.thread_b.start()
    qtbot.waitUntil(lambda: page.thread_a.is_running and page.thread_b.is_running,
                    timeout=3000)

    page.shutdown()
    assert page.thread_a.is_running is False
    assert page.thread_b.is_running is False


def test_shutdown_is_safe_when_nothing_is_running(qtbot):
    page = _Page()
    qtbot.addWidget(page)
    page.shutdown()          # 不該拋例外
    page.shutdown()          # 收兩次也不該拋


def test_the_real_pages_call_super(qtbot):
    """覆寫 shutdown 的分頁要呼叫 super()，不然那道掃描等於不存在。"""
    import inspect

    from ro_toolbox.ui.main_window import PAGE_CLASSES

    for cls in PAGE_CLASSES:
        if "shutdown" not in cls.__dict__:
            continue          # 沒覆寫就直接吃到預設的掃描
        source = inspect.getsource(cls.__dict__["shutdown"])
        assert "super().shutdown()" in source, (
            f"{cls.__name__}.shutdown() 沒呼叫 super() —— "
            "漏收的執行緒會讓 Qt 中止整個行程"
        )
