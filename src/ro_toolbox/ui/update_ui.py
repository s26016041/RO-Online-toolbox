"""自動更新的介面：啟動時背景檢查、下載進度、換檔重啟。

**強制更新，不詢問** —— 查到新版就直接下載、換檔、重啟。只顯示進度，沒有選項。
理由是 RO 改版後記憶體特徵與封包 opcode 會失效（見 `/_patchCheck`），
舊版留在使用者手上只會顯示錯的資料或整個抓不到，讓人以為程式壞了。

⚠⚠ **只在啟動時檢查一次，用到一半不重查。**
每隔一段時間重查的話，發新版的當下所有開著的工具箱會在幾分鐘內全部強制重啟 ——
自動打怪、自動補水做到一半被硬生生打斷。剛啟動那一刻什麼都還沒跑，
這時換檔不會打斷任何事。代價（開著不關的人停在舊版直到重開）是可以接受的。

檢查在背景執行緒做（連 GitHub 可能要幾秒），不擋住視窗開啟。
沒網路 / 查不到 / 已是最新 → 完全安靜。
更新失敗 → 講清楚原因就繼續用舊版，**不能因為更新不成功就讓人沒得用**。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from ro_toolbox import __version__
from ro_toolbox.services import updater

log = logging.getLogger(__name__)

_CLEAN_DELAY_MS = 3000
_STATUS_MS = 30000


class CheckThread(QThread):
    """背景查有沒有新版。"""

    done = Signal(object)   # dict 或 None

    def run(self) -> None:
        try:
            self.done.emit(updater.check())
        except Exception:  # noqa: BLE001 - 背景執行緒不能讓例外逸出
            log.debug("檢查更新時發生例外", exc_info=True)
            self.done.emit(None)


class DownloadThread(QThread):
    """背景下載新版 exe。"""

    progress = Signal(int, int)
    done = Signal(bool)

    def __init__(self, info: dict, dest) -> None:
        super().__init__()
        self._info = info
        self._dest = dest

    def run(self) -> None:
        try:
            ok = updater.download(
                self._info, self._dest,
                progress=lambda got, total: self.progress.emit(got, total),
            )
        except Exception:  # noqa: BLE001 - 同上
            log.debug("下載更新時發生例外", exc_info=True)
            ok = False
        self.done.emit(ok)


class UpdateManager:
    """掛在主視窗上：**只在開場檢查一次**，有新版就直接更新（見檔頭）。

    ⛔ 別把「每 N 分鐘重查」加回來 —— 那會在發新版的當下，
      把所有正在自動打怪／自動補水的工具箱強制重啟。
    """

    def __init__(self, parent) -> None:
        self._parent = parent
        self._check: CheckThread | None = None
        self._download: DownloadThread | None = None
        self._dialog: QProgressDialog | None = None
        self._info: dict | None = None
        # ⚠⚠ 收工的執行緒先擱這裡，**不要直接把參考丟掉**。
        #   `done` 是排隊送過來的，槽跑到時 run() 往往還在收尾；那一刻要是
        #   Python 把最後一個參考回收掉，C++ 端的 QThread 就在執行中被解構
        #   →「Destroyed while thread is still running」→ 原生當機。
        self._retired: list[QThread] = []

    # ---- 對外 -------------------------------------------------------

    def start(self) -> None:
        """開場呼叫。開發模式與無頭模式不做，其餘一律檢查並強制更新。"""
        # ⚠ clean_leftovers() 會 rmtree 每個殘留的 _MEIxxxxxx（一個約 78 MB）——
        #   那是**開視窗前**的同步磁碟操作，會拖慢啟動。純清理，晚三秒做完全沒差。
        QTimer.singleShot(_CLEAN_DELAY_MS, updater.clean_leftovers)
        if not updater.is_frozen():
            return
        # 無頭模式（--selftest 會跑 offscreen）不要查：冒煙測試建好視窗就馬上結束，
        # 這條執行緒還連著網路沒收完，Qt 會丟「Destroyed while running」並中止行程，
        # 害冒煙測試誤判成打包失敗。
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        self._run_check()

    def stop(self) -> None:
        """關閉程式前呼叫：等背景執行緒收完，別讓 Qt 在解構時抱怨。"""
        for thread in (self._check, self._download, *self._retired):
            if thread is not None and thread.isRunning():
                thread.wait(3000)
        self._check = None
        self._download = None
        self._retired.clear()

    # ---- 檢查 -------------------------------------------------------

    def _run_check(self) -> None:
        if self._check is not None or self._download is not None:
            return
        self._check = CheckThread()
        self._check.done.connect(self._on_checked)
        self._check.start()

    def _on_checked(self, info) -> None:
        """查到新版就直接更新，不問使用者。"""
        self._retire(self._check)
        self._check = None
        if not info:
            # 查不到有兩種：已是最新（正常）、連線／憑證出問題（要讓人看得到）。
            # 兩種都靜靜略過的話，更新不了時完全沒有線索。
            error = updater.last_error()
            if error:
                self._show_status(f"⚠ 檢查更新失敗（{error}）—— 目前使用 {__version__}")
            return
        self._info = info
        self._start_download()

    # ---- 下載 -------------------------------------------------------

    def _start_download(self) -> None:
        destination = updater.exe_path().with_suffix(updater.exe_path().suffix + ".new")
        version = self._info["version"] if self._info else ""
        # 強制更新：沒有取消鈕（第二個參數傳 None）、應用程式層級 modal，
        # 避免使用者在換檔途中去操作視窗。
        self._dialog = QProgressDialog(
            f"正在更新到 {version}…\n完成後會自動重新啟動。",
            None, 0, 100, self._parent,
        )
        self._dialog.setWindowTitle("更新中")
        self._dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._dialog.setMinimumDuration(0)
        self._dialog.setAutoClose(False)
        self._dialog.setAutoReset(False)
        # 拿掉標題列的關閉鈕：關掉對話框會讓人以為取消了，其實還在下載。
        self._dialog.setWindowFlags(
            (self._dialog.windowFlags() | Qt.WindowType.CustomizeWindowHint)
            & ~Qt.WindowType.WindowCloseButtonHint
        )
        self._dialog.setValue(0)

        self._download = DownloadThread(self._info, destination)
        self._download.progress.connect(self._on_progress)
        self._download.done.connect(lambda ok: self._on_downloaded(ok, destination))
        self._download.start()

    def _on_progress(self, got: int, total: int) -> None:
        if self._dialog is None:
            return
        version = self._info["version"] if self._info else ""
        if total:
            self._dialog.setValue(int(got / total * 100))
            self._dialog.setLabelText(
                f"正在更新到 {version}…　{got / 1e6:.1f} / {total / 1e6:.1f} MB\n"
                "完成後會自動重新啟動。"
            )
        else:
            self._dialog.setLabelText(
                f"正在更新到 {version}…　{got / 1e6:.1f} MB\n完成後會自動重新啟動。"
            )

    def _on_downloaded(self, ok: bool, destination) -> None:
        self._retire(self._download)
        self._download = None
        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None
        version = self._info["version"] if self._info else ""

        # 更新失敗不能讓人沒得用 —— 講清楚原因就讓他繼續用舊版。
        if not ok:
            QMessageBox.warning(
                self._parent, "更新失敗",
                f"下載 {version} 沒有完成或檔案不完整，這次先用目前的版本。\n"
                "下次開啟會再試一次，也可以到 GitHub Releases 手動下載。",
            )
            return
        if not updater.apply_and_restart(destination):
            QMessageBox.warning(
                self._parent, "更新失敗",
                "換檔時失敗，已保留原本的版本。\n"
                "若程式放在唯讀資料夾（例如 Program Files），"
                "請改用系統管理員身分執行，或手動下載。",
            )
            return
        QApplication.quit()

    # ---- 雜項 -------------------------------------------------------

    def _retire(self, thread: QThread | None) -> None:
        """把執行緒移出「現役」但**留著參考**，等它自己結束才放掉。"""
        if thread is None:
            return
        self._retired.append(thread)
        thread.finished.connect(lambda t=thread: self._forget(t))
        if thread.isFinished():
            self._forget(thread)

    def _forget(self, thread: QThread) -> None:
        if thread in self._retired:
            self._retired.remove(thread)
            thread.deleteLater()

    def _show_status(self, text: str) -> None:
        """把訊息寫到主視窗狀態列（沒有狀態列就算了，不能因此壞掉）。"""
        try:
            bar = self._parent.statusBar()
        except Exception:  # noqa: BLE001 - 顯示訊息失敗不值得中斷任何事
            return
        if bar is not None:
            bar.showMessage(text, _STATUS_MS)
