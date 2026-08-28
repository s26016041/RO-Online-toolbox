"""分頁收尾：**所有背景執行緒都要停**，一條都不能漏。

⚠ 漏掉一條的後果不是「有點慢」，是 Qt 在解構時喊
「Destroyed while thread is still running」並用 0xC0000409 **中止整個行程**。

實際踩過（2026-08-27）：`AccountPage.shutdown()` 收了 `_offset_thread` 與
`_login_thread`，**漏了 `_link_thread`** —— 打包出來的 exe 自檢每一項都通過，
卻在收尾時崩掉、一個字都沒印，看起來像打包壞掉。原始碼版本一模一樣崩。
"""

from __future__ import annotations

import gc

from PySide6.QtCore import QThread

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


class _Stubborn(Worker):
    """叫不停的工作 —— 模擬卡在 GameGuard 擋住的系統呼叫上。"""

    def run(self) -> None:
        import time

        time.sleep(1.5)          # 不理會 request_stop


def test_a_thread_that_will_not_stop_is_kept_alive_not_destroyed(qtbot):
    """⚠ 停不下來的執行緒**殺不得**，唯一安全的做法是留著引用到行程結束。

    不留的話 Qt 會喊「Destroyed while thread is still running」並用
    0xC0000409 中止整個行程 —— 寧可洩漏一條，也不要把程式打掉（[ENV-005]）。
    """
    from ro_toolbox.core import worker as mod

    before = len(mod._STUCK)
    thread = WorkerThread(_Stubborn())
    thread.start()
    qtbot.waitUntil(lambda: thread.is_running, timeout=3000)
    thread.stop(timeout_ms=100)          # 故意等不到
    assert len(mod._STUCK) == before + 1, "停不下來的要被留著"
    kept_thread, kept_worker = mod._STUCK[-1]
    assert kept_thread is thread.thread
    assert kept_worker is thread.worker, "worker 也要留 —— 它在那條執行緒上"
    qtbot.waitUntil(lambda: not thread.is_running, timeout=5000)


# ---- 自檢不准去附加遊戲行程 ----------------------------------------------


def test_selftest_flag_stops_the_farm_page_from_scanning(qtbot, monkeypatch):
    """⚠ 自檢要驗的是「東西有沒有收進來」，不是「能不能操作遊戲」。

    附加遊戲的工作會卡在 GameGuard 擋住的系統呼叫上（列舉模組實測卡 3 秒
    以上），那條執行緒叫不停 —— 行程收尾時 DLL 開始卸載，它醒來就踩到已釋放
    的程式碼，整個行程被以 0xC0000409 中止（[ENV-005]）。
    **留著引用救不了：問題是它存在，不是它被解構。**
    """
    from ro_toolbox.config.paths import SELFTEST_ENV
    from ro_toolbox.ui.pages import farm_page as mod

    monkeypatch.setenv(SELFTEST_ENV, "1")
    scanned = []
    monkeypatch.setattr(mod.FarmPage, "_scan", lambda self: scanned.append(1))
    page = mod.FarmPage()
    qtbot.addWidget(page)
    assert scanned == [], "自檢時不該去掃遊戲視窗"
    assert page._scan_timer.isActive() is False
    assert page._read_timer.isActive() is False
    page.shutdown()


def test_without_the_flag_it_scans_as_usual(qtbot, monkeypatch):
    """一般啟動要照舊掃 —— 這道閘門只准影響自檢。"""
    from ro_toolbox.config.paths import SELFTEST_ENV
    from ro_toolbox.ui.pages import farm_page as mod

    monkeypatch.delenv(SELFTEST_ENV, raising=False)
    scanned = []
    monkeypatch.setattr(mod.FarmPage, "_scan", lambda self: scanned.append(1))
    page = mod.FarmPage()
    qtbot.addWidget(page)
    assert scanned == [1]
    assert page._scan_timer.isActive() is True
    page.shutdown()


# ---- 背景執行緒不准被「覆蓋掉」------------------------------------------
#
# 使用者實機：`py main.py` 登入完之後噴
#   QThread: Destroyed while thread '' is still running
# 呼叫端普遍寫成 `self._xxx_thread = WorkerThread(worker)`，下一次再起一條就把
# 上一條覆蓋掉；上一條還沒跑完的話 Python 這邊就沒人引用它了 → QThread 被解構
# → Qt 中止整個行程。這種事不該要求每個呼叫端自己記得。


def test_a_running_thread_is_not_dropped_when_it_is_replaced(qtbot):
    """⚠ 這是那句「Destroyed while thread is still running」的真正成因。"""
    from ro_toolbox.core import worker as worker_mod
    from ro_toolbox.core.worker import Worker, WorkerThread

    class _Slow(Worker):
        def run(self) -> None:
            while not self.should_stop:
                QThread.msleep(5)

    started = WorkerThread(_Slow())
    started.start()
    qtbot.waitUntil(lambda: started.is_running, timeout=2000)
    first_thread = started.thread

    # 呼叫端把引用蓋掉 —— 實際程式碼就是 `self._x = WorkerThread(...)`
    started = None
    gc.collect()

    assert any(t.thread is first_thread for t in worker_mod._RUNNING), \
        "還在跑的執行緒不准被丟掉"
    # 收尾：讓它正常停下來
    for t in list(worker_mod._RUNNING):
        if t.thread is first_thread:
            t.stop()


def test_a_finished_thread_is_let_go(qtbot):
    """跑完的就要放掉，不然一直累積等於漏記憶體。"""
    from ro_toolbox.core import worker as worker_mod
    from ro_toolbox.core.worker import Worker, WorkerThread

    class _Quick(Worker):
        def run(self) -> None:
            return

    done = WorkerThread(_Quick())
    done.start()
    qtbot.waitUntil(lambda: done.thread.isFinished(), timeout=2000)
    done.stop()
    assert done not in worker_mod._RUNNING


def test_no_page_hand_lists_its_threads_to_stop(qtbot):
    """⚠ 收尾一律靠 `BasePage.shutdown()` 的**全面掃描**，不准自己列清單。

    清單會漏，而漏掉一條的後果是整個行程被中止 ——
    `AccountPage` 就漏過 `_link_thread`（2026-08-27）。
    """
    import inspect

    from ro_toolbox.ui.pages.account_page import AccountPage

    body = inspect.getsource(AccountPage.shutdown)
    listed = [n for n in ("_link_thread", "_offset_thread", "_login_thread")
              if n in body]
    assert not listed, f"又在自己列清單了（{listed}）—— 交給 super().shutdown() 掃"
