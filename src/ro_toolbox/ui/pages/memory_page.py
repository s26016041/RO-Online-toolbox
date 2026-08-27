"""記憶體掃描分頁（Cheat Engine 風格）。

用途：找出遊戲中某個數值（經驗值 / HP / 金錢…）實際存放的記憶體位址。

操作流程：
  1. 選一個遊戲視窗 → 按「選定此程序」。
  2. 選型別（通常是 4 位元組整數），輸入你在遊戲裡看到的數字 → 首次搜尋。
     不知道確切數字就把條件改成「未知初始值」直接搜。
  3. 回遊戲讓數值改變，回來把條件改成「增加 / 減少 / 已改變」→ 再次搜尋。
  4. 重複第 3 步，候選會越縮越少，直到剩幾個位址。
  5. 選一列「加入觀察」，就能持續看它的即時值，也能寫入。

掃描在背景執行緒進行，不會卡住介面；全程只讀取選定的程序，不搶焦點。
掃描核心移植自 Angels-Online-toolbox，見 `services/memory_scan.py`。
"""

from __future__ import annotations

import ctypes
import logging

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ro_toolbox.config import ui_state
from ro_toolbox.services.memory_scan import (
    STRING_ENCODINGS,
    VALUE_REQUIRED,
    VALUE_TYPES,
    MemoryScanner,
    ScanCancelled,
)
from ro_toolbox.ui.pages.base_page import BasePage
from ro_toolbox.ui.widgets.window_picker import WindowPicker

log = logging.getLogger(__name__)

RESULT_DISPLAY_LIMIT = 2000
_WATCH_REFRESH_MS = 500

# 下拉選單裡的搜尋條件順序（含未知初始值）。
SCAN_TYPE_ORDER = [
    ("exact", "等於"),
    ("bigger", "大於"),
    ("smaller", "小於"),
    ("unknown", "未知初始值"),
    ("increased", "增加"),
    ("decreased", "減少"),
    ("changed", "已改變"),
    ("unchanged", "未改變"),
]
SCAN_TYPE_LABEL = dict(SCAN_TYPE_ORDER)

_ABOUT = (
    "找出遊戲數值（經驗、HP、金錢…）存在哪個記憶體位址："
    "輸入現在看到的數字搜尋一次，回遊戲讓它改變，再用「增加／減少」縮小範圍，"
    "重複幾次就會剩下幾個位址。只讀取你選定的程序，不搶滑鼠鍵盤。"
)


class ScanWorker(QThread):
    """在背景執行一次掃描（首次或再次），避免卡住介面。"""

    progress = Signal(int)
    done = Signal(int)
    failed = Signal(str)

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn
        self._cancel = False

    def cancel(self) -> None:
        """請掃描盡快收工（下一個區塊回呼時生效）。

        跨執行緒呼叫安全：只寫一個 bool，最壞多掃一個區塊。
        """
        self._cancel = True

    def run(self) -> None:
        # ⚠ 回呼是**每個記憶體區塊**呼叫一次（大目標有好幾千塊），而進度條只認得
        #   0~100。百分比沒變就不要送 —— 每次 emit 都是跨執行緒排隊事件，
        #   GUI 執行緒得一件一件處理完。
        last = -1

        def on_progress(fraction: float) -> None:
            nonlocal last
            # ⚠ 取消要在這裡查：回呼每個區塊都會被叫到，丟例外才能中斷整趟掃描。
            if self._cancel:
                raise ScanCancelled()
            percent = int(fraction * 100)
            if percent != last:
                last = percent
                self.progress.emit(percent)

        try:
            count = self._fn(on_progress)
            self.done.emit(int(count))
        except ScanCancelled:
            pass  # 使用者收工，不算失敗
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


def _is_admin() -> bool:
    """本行程是不是系統管理員。

    遊戲掛 GameGuard 且通常以較高權限執行；權限不夠時 `OpenProcess` 會直接失敗，
    症狀是「選定程序」按下去什麼都讀不到。與其等使用者撞牆，一進頁面就講清楚。
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 查不到就不要嚇使用者
        return True


def _row(*widgets: QWidget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return container


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class MemoryPage(BasePage):
    title = "記憶體"
    subtitle = "搜尋遊戲數值所在的記憶體位址，觀察並寫入。"
    stretch_at_end = False

    def __init__(self) -> None:
        self._scanner = MemoryScanner()
        self._worker: ScanWorker | None = None
        self._watch: list[dict] = []
        # search_string 回傳 [(位址, 編碼, 位元組長度), ...]，字串再次篩選要沿用
        self._string_hits: list[tuple[int, str, int]] = []
        self._string_meta: dict[int, tuple[str, int]] = {}
        super().__init__()

        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(_WATCH_REFRESH_MS)
        self._watch_timer.timeout.connect(self._refresh_watch_values)

        self.refresh_windows()
        self._update_enabled()

    # ---- 版面 -------------------------------------------------------

    def build(self) -> None:
        self.about = QLabel(_ABOUT)
        self.about.setObjectName("infoBar")
        self.about.setWordWrap(True)
        self.add(self.about)

        self._build_target_row()
        self._build_scan_row()
        self._build_string_row()
        self._build_tables()

    def _build_target_row(self) -> None:
        self.picker = WindowPicker(label="目標視窗", combo_width=420, state_key="memory")
        self.attach_button = QPushButton("選定此程序")
        self.attach_button.setObjectName("primaryButton")
        self.attach_button.clicked.connect(self.attach_selected)
        self.picker.layout().addWidget(self.attach_button)

        self.add(self.picker)

        self.attached_label = QLabel("尚未選定程序")
        self.attached_label.setObjectName("pageSubtitle")
        self.add(self.attached_label)

        # ⚠ 所有提示／錯誤都寫在這裡，**不准用 QMessageBox**。
        # QMessageBox 是強制回應（modal）的：遊戲通常是全螢幕或置頂，
        # 對話框會跳到遊戲**後面** —— 使用者看不到也點不到，整個工具箱就
        # 「沒有回應」，看起來就是當機（實際回報：選定程序後直接當掉，
        # 同時出現 QFont::setPointSize 警告 —— 那行正是 Qt 在建對話框時噴的）。
        self.notice_label = QLabel("")
        self.notice_label.setObjectName("pageSubtitle")
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(False)
        self.add(self.notice_label)

        if not _is_admin():
            self._notice(
                "目前**不是**以系統管理員身分執行。遊戲行程多半開不起來（開啟失敗）——"
                "請以系統管理員身分重開本工具。",
                error=True,
            )

    def _build_scan_row(self) -> None:
        self.type_combo = QComboBox()
        for key, vt in VALUE_TYPES.items():
            self.type_combo.addItem(vt.label, key)
        self.type_combo.currentIndexChanged.connect(self._on_value_type_changed)

        self.scan_combo = QComboBox()
        for key, label in SCAN_TYPE_ORDER:
            self.scan_combo.addItem(label, key)
        self.scan_combo.currentIndexChanged.connect(self._on_scan_type_changed)

        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("數值（可用 0x）")
        self.value_edit.setText(ui_state.get("memory.value", ""))
        self.value_edit.textChanged.connect(
            lambda t: ui_state.set("memory.value", t)
        )
        self.value_edit.setMinimumWidth(160)
        self.value_edit.setMaximumWidth(160)
        self.value_edit.returnPressed.connect(self._first_or_next)

        self.first_button = QPushButton("首次搜尋")
        self.first_button.setObjectName("primaryButton")
        self.first_button.clicked.connect(self.do_first_scan)

        self.next_button = QPushButton("再次搜尋")
        self.next_button.clicked.connect(self.do_next_scan)

        self.reset_button = QPushButton("重設")
        self.reset_button.clicked.connect(self.reset_scan)

        self.add(
            _row(
                QLabel("型別"),
                self.type_combo,
                QLabel("條件"),
                self.scan_combo,
                self.value_edit,
                self.first_button,
                self.next_button,
                self.reset_button,
            )
        )

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(240)
        self.progress.setValue(0)

        self.scan_status = QLabel("請先選定程序")
        self.scan_status.setObjectName("pageSubtitle")

        self.add(_row(self.progress, self.scan_status))

    def _build_string_row(self) -> None:
        self.string_edit = QLineEdit()
        self.string_edit.setPlaceholderText("搜尋文字（角色名、地圖名…）")
        self.string_edit.setText(ui_state.get("memory.string", ""))
        self.string_edit.textChanged.connect(
            lambda t: ui_state.set("memory.string", t)
        )
        self.string_edit.setMinimumWidth(260)
        self.string_edit.setMaximumWidth(260)
        self.string_edit.returnPressed.connect(self.do_string_scan)

        self.encoding_combo = QComboBox()
        for key, label in STRING_ENCODINGS.items():
            self.encoding_combo.addItem(label, key)

        self.string_button = QPushButton("搜尋字串")
        self.string_button.clicked.connect(self.do_string_scan)

        self.string_next_button = QPushButton("再次篩選")
        self.string_next_button.clicked.connect(self.do_string_next_scan)

        self.add(
            _row(
                QLabel("字串"),
                self.string_edit,
                self.encoding_combo,
                self.string_button,
                self.string_next_button,
            )
        )

    def _build_tables(self) -> None:
        self.result_table = _table(["位址", "數值"])
        self.result_table.setColumnWidth(0, 150)
        self.watch_table = _table(["位址", "型別", "目前值"])
        self.watch_table.setColumnWidth(0, 130)
        self.watch_table.setColumnWidth(1, 140)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(QLabel("搜尋結果"))
        left_layout.addWidget(self.result_table, 1)

        self.watch_button = QPushButton("加入觀察")
        self.watch_button.clicked.connect(self.add_selected_to_watch)
        self.manual_button = QPushButton("手動位址…")
        self.manual_button.clicked.connect(self.add_manual_address)
        left_layout.addWidget(_row(self.watch_button, self.manual_button))

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addWidget(QLabel("觀察清單（每 0.5 秒更新）"))
        right_layout.addWidget(self.watch_table, 1)

        self.write_button = QPushButton("寫入數值…")
        self.write_button.clicked.connect(self.write_selected)
        self.remove_button = QPushButton("移除")
        self.remove_button.clicked.connect(self.remove_selected_watch)
        right_layout.addWidget(_row(self.write_button, self.remove_button))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self._layout.addWidget(splitter, 1)

    # ---- 訊息 -------------------------------------------------------

    def _notice(self, message: str, error: bool = False) -> None:
        """把提示寫在頁面上。

        這裡刻意**不用 QMessageBox**：它是強制回應的，而遊戲通常全螢幕或置頂，
        對話框會跳到遊戲後面 —— 使用者看不到、也點不到，整個工具箱就停在那裡，
        看起來完全就是當機（實際回報過，當下伴隨 QFont::setPointSize 警告，
        那正是 Qt 建對話框時噴的）。頁面上的一行字看得到、也不會擋住操作。
        """
        self.notice_label.setText(message)
        self.notice_label.setVisible(bool(message))
        self.notice_label.setStyleSheet("color: #d64545;" if error else "")
        if message:
            (log.warning if error else log.info)("記憶體分頁：%s", message)

    def _front(self) -> None:
        """把工具箱視窗拉到最前面。

        只有**非用要不可**的對話框（要打字、要確認破壞性寫入）才會用到；
        不先拉到前面的話，對話框會躲在全螢幕遊戲後面。
        """
        window = self.window()
        if window is not None:
            window.raise_()
            window.activateWindow()

    # ---- 目標選擇 ---------------------------------------------------

    def refresh_windows(self) -> None:
        self.picker.refresh()

    def attach_selected(self) -> None:
        # ⚠ 掃描進行中不准換程序：scanner.open() 會先 close() 舊 handle，
        #   在掃描執行緒腳下抽走；而 Windows 常把同一個 handle 值配給緊接著的
        #   OpenProcess，掃描後半段就會靜默讀到**另一個程序**的記憶體。
        if self._worker is not None and self._worker.isRunning():
            self._notice("掃描進行中，請等它結束再切換程序。")
            return

        target = self.picker.selected()
        if target is None:
            self._notice("請先選一個視窗。")
            return
        try:
            self._scanner.open(target.pid)
        except Exception as exc:  # noqa: BLE001
            self._notice(str(exc), error=True)
            self._update_enabled()
            return

        bits = 32 if self._scanner.pointer_size == 4 else 64
        write_note = "" if self._scanner.can_write else "（唯讀，無法寫入）"
        self.attached_label.setText(
            f"已選定：PID {target.pid}（{bits} 位元）— {target.title}{write_note}"
        )
        self._clear_results()
        self._scanner.reset()
        self.scan_status.setText("已選定程序，可開始首次搜尋。")
        self._watch_timer.start()
        self._update_enabled()

    # ---- 條件連動 ---------------------------------------------------

    def _on_value_type_changed(self) -> None:
        # 換型別代表要重新搜尋（不同大小的候選不相容）。
        if self._scanner.has_results:
            self._scanner.reset()
            self._clear_results()
            self.scan_status.setText("已切換型別，請重新首次搜尋。")
        self._update_enabled()

    def _on_scan_type_changed(self) -> None:
        self.value_edit.setEnabled(self.scan_combo.currentData() in VALUE_REQUIRED)

    def _current_vt(self):
        return VALUE_TYPES[self.type_combo.currentData()]

    def _parse_value(self, vt):
        text = self.value_edit.text().strip()
        if not text:
            return None, "請先輸入要搜尋的數值。"
        try:
            if vt.is_float:
                return float(text), None
            return int(text, 0), None  # 支援 0x 十六進位
        except ValueError:
            kind = "浮點" if vt.is_float else "整數"
            return None, f"「{text}」不是有效的{kind}數值。"

    def _first_or_next(self) -> None:
        if self._scanner.has_results:
            self.do_next_scan()
        else:
            self.do_first_scan()

    # ---- 掃描 -------------------------------------------------------

    def do_first_scan(self) -> None:
        if not self._scanner.attached:
            self._notice("請先選定程序。")
            return

        vt = self._current_vt()
        scan_type = self.scan_combo.currentData()
        value = None

        if scan_type in VALUE_REQUIRED:
            value, error = self._parse_value(vt)
            if error:
                self._notice(error, error=True)
                return
        elif scan_type != "unknown":
            self._notice(
                "首次搜尋只能用「等於 / 大於 / 小於 / 未知初始值」；"
                "「增加 / 減少 / 改變」要先有一輪結果才能比較。",
                error=True,
            )
            return

        self._run_scan(
            lambda progress: self._scanner.first_scan(vt, scan_type, value, False, progress),
            f"首次搜尋（{SCAN_TYPE_LABEL.get(scan_type, scan_type)}）…",
        )

    def do_next_scan(self) -> None:
        if not self._scanner.has_results:
            self._notice("請先做一次首次搜尋。")
            return

        scan_type = self.scan_combo.currentData()
        if scan_type == "unknown":
            self._notice("「未知初始值」只用於首次搜尋。", error=True)
            return

        value = None
        if scan_type in VALUE_REQUIRED:
            value, error = self._parse_value(self._current_vt())
            if error:
                self._notice(error, error=True)
                return

        self._run_scan(
            lambda progress: self._scanner.next_scan(scan_type, value, progress),
            f"再次搜尋（{SCAN_TYPE_LABEL.get(scan_type, scan_type)}）…",
        )

    def _run_scan(self, fn, status_text: str) -> None:
        self._set_scanning(True)
        self.scan_status.setText(status_text)
        self.progress.setValue(0)
        self._worker = ScanWorker(fn)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.done.connect(self._on_scan_done)
        self._worker.failed.connect(self._on_scan_failed)
        self._worker.start()

    def _on_scan_done(self, count: int) -> None:
        self.progress.setValue(100)
        self._set_scanning(False)
        self._populate_results(count)

    def _on_scan_failed(self, message: str) -> None:
        self._set_scanning(False)
        self.progress.setValue(0)
        self.scan_status.setText("搜尋失敗")
        self._notice(f"搜尋失敗：{message}", error=True)

    def reset_scan(self) -> None:
        self._scanner.reset()
        self._clear_results()
        self.scan_status.setText("已重設，可重新首次搜尋。")
        self._update_enabled()

    # ---- 字串搜尋 ---------------------------------------------------

    def do_string_scan(self) -> None:
        if not self._scanner.attached:
            self._notice("請先選定程序。")
            return
        text = self.string_edit.text()
        if not text:
            self._notice("請輸入要搜尋的文字。")
            return

        encodings = [self.encoding_combo.currentData()]

        def job(progress):
            # 一律搜尋全部記憶體（不分可否寫入）
            self._string_hits = self._scanner.search_string(text, encodings, False, progress)
            return len(self._string_hits)

        self._run_string_scan(job, f"搜尋字串「{text}」…")

    def do_string_next_scan(self) -> None:
        """在既有命中裡，只留下「現在內容仍等於新輸入文字」的位址。

        典型用法：先搜舊文字得到很多命中 → 在遊戲裡把它改掉 → 輸入新文字再篩，
        只有你真正在改的那個位址會留下來。
        """
        if not self._string_hits:
            self._notice("請先做一次字串搜尋。")
            return
        text = self.string_edit.text()
        if not text:
            return

        previous = list(self._string_hits)

        def job(progress):
            self._string_hits = self._scanner.filter_string_hits(previous, text, progress)
            return len(self._string_hits)

        self._run_string_scan(job, f"再次篩選「{text}」…（在 {len(previous)} 筆中縮小）")

    def _run_string_scan(self, job, status_text: str) -> None:
        self._set_scanning(True)
        self.scan_status.setText(status_text)
        self.progress.setValue(0)
        self._worker = ScanWorker(job)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.done.connect(self._on_string_done)
        self._worker.failed.connect(self._on_scan_failed)
        self._worker.start()

    def _on_string_done(self, count: int) -> None:
        self.progress.setValue(100)
        self._set_scanning(False)
        self._populate_string_results(count)

    # ---- 結果 -------------------------------------------------------

    def _populate_results(self, count: int) -> None:
        # 這是數值結果 → 之前的字串命中作廢
        self._string_meta = {}
        self._string_hits = []

        rows = self._scanner.results(limit=RESULT_DISPLAY_LIMIT)
        self.result_table.setRowCount(len(rows))
        for index, (address, value) in enumerate(rows):
            item = QTableWidgetItem(f"0x{address:X}")
            item.setData(Qt.ItemDataRole.UserRole, address)
            self.result_table.setItem(index, 0, item)
            self.result_table.setItem(index, 1, QTableWidgetItem(str(value)))

        note = f"（候選太多，只顯示前 {len(rows)} 筆）" if count > len(rows) else ""
        self.scan_status.setText(f"目前候選：{count} 筆{note}")
        self._update_enabled()

    def _populate_string_results(self, count: int) -> None:
        shown = self._string_hits[:RESULT_DISPLAY_LIMIT]
        self._string_meta = {}

        self.result_table.setRowCount(len(shown))
        for index, (address, encoding, byte_length) in enumerate(shown):
            self._string_meta[address] = (encoding, byte_length)
            text = self._scanner.read_string(address, byte_length, encoding) or ""
            item = QTableWidgetItem(f"0x{address:X}")
            item.setData(Qt.ItemDataRole.UserRole, address)
            self.result_table.setItem(index, 0, item)
            self.result_table.setItem(index, 1, QTableWidgetItem(text))

        note = f"（只顯示前 {len(shown)} 筆）" if count > len(shown) else ""
        self.scan_status.setText(f"字串命中：{count} 筆{note}")
        self._update_enabled()

    def _clear_results(self) -> None:
        self.result_table.setRowCount(0)
        self._string_hits = []
        self._string_meta = {}

    # ---- 觀察清單 ---------------------------------------------------

    def add_selected_to_watch(self) -> None:
        rows = self.result_table.selectionModel().selectedRows()
        if not rows:
            self._notice("請先在結果中選取要觀察的列。")
            return

        vt = self._current_vt()
        for index in rows:
            item = self.result_table.item(index.row(), 0)
            address = item.data(Qt.ItemDataRole.UserRole)
            if address in self._string_meta:
                encoding, byte_length = self._string_meta[address]
                self._add_watch_string(address, encoding, byte_length)
            else:
                self._add_watch(address, vt)
        self._rebuild_watch_table()

    def add_manual_address(self) -> None:
        self._front()
        text, ok = QInputDialog.getText(
            self, "手動加入位址", "輸入記憶體位址（可用 0x 十六進位）："
        )
        if not ok or not text.strip():
            return
        try:
            address = int(text.strip(), 0)
        except ValueError:
            self._notice(f"「{text}」不是有效的位址。", error=True)
            return
        self._add_watch(address, self._current_vt())
        self._rebuild_watch_table()

    def _add_watch(self, address: int, vt) -> None:
        for entry in self._watch:
            if entry["addr"] == address and entry.get("vt") and entry["vt"].key == vt.key:
                return  # 避免重複
        self._watch.append({"addr": address, "vt": vt})

    def _add_watch_string(self, address: int, encoding: str, byte_length: int) -> None:
        for entry in self._watch:
            if entry["addr"] == address and entry.get("str_enc"):
                return
        self._watch.append(
            {"addr": address, "vt": None, "str_enc": encoding, "str_len": byte_length}
        )

    def remove_selected_watch(self) -> None:
        rows = sorted(
            (index.row() for index in self.watch_table.selectionModel().selectedRows()),
            reverse=True,
        )
        for row in rows:
            if 0 <= row < len(self._watch):
                del self._watch[row]
        self._rebuild_watch_table()

    def _rebuild_watch_table(self) -> None:
        self.watch_table.setRowCount(len(self._watch))
        for row, entry in enumerate(self._watch):
            self.watch_table.setItem(row, 0, QTableWidgetItem(f"0x{entry['addr']:X}"))
            if entry.get("str_enc"):
                label = f"字串（{STRING_ENCODINGS[entry['str_enc']]}）"
            else:
                label = entry["vt"].label
            self.watch_table.setItem(row, 1, QTableWidgetItem(label))
            self.watch_table.setItem(row, 2, QTableWidgetItem("…"))
        self._refresh_watch_values()

    def _refresh_watch_values(self) -> None:
        if not self._scanner.attached or not self._watch:
            return
        for row, entry in enumerate(self._watch):
            if row >= self.watch_table.rowCount():
                break
            if entry.get("str_enc"):
                value = self._scanner.read_string(
                    entry["addr"], entry["str_len"], entry["str_enc"]
                )
            else:
                value = self._scanner.read_value(entry["addr"], entry["vt"])
            text = "讀取失敗" if value is None else str(value)
            self.watch_table.setItem(row, 2, QTableWidgetItem(text))

    # ---- 寫入 -------------------------------------------------------

    def write_selected(self) -> None:
        if not self._scanner.attached:
            return
        rows = self.watch_table.selectionModel().selectedRows()
        if not rows:
            self._notice("請先在觀察清單選一列。")
            return

        entry = self._watch[rows[0].row()]
        if entry.get("str_enc"):
            self._write_string_entry(entry)
            return

        vt = entry["vt"]
        self._front()
        text, ok = QInputDialog.getText(
            self, "寫入數值", f"對位址 0x{entry['addr']:X} 寫入新的 {vt.label}："
        )
        if not ok or not text.strip():
            return

        try:
            value = float(text) if vt.is_float else int(text, 0)
        except ValueError:
            self._notice(f"「{text}」不是有效的數值。", error=True)
            return

        try:
            self._scanner.write_value(entry["addr"], vt, value)
        except Exception as exc:  # noqa: BLE001
            self._notice(f"寫入失敗：{exc}", error=True)
            return
        self._refresh_watch_values()

    def _write_string_entry(self, entry: dict) -> None:
        encoding = entry["str_enc"]
        self._front()
        text, ok = QInputDialog.getText(
            self, "寫入字串", f"對位址 0x{entry['addr']:X} 寫入新文字："
        )
        if not ok:
            return

        try:
            new_bytes = text.encode(encoding)
        except (UnicodeEncodeError, LookupError) as exc:
            self._notice(
                f"這段文字無法用 {STRING_ENCODINGS[encoding]} 編碼：{exc}", error=True
            )
            return

        # 比原長度長 → 會覆蓋相鄰記憶體，先問過
        null_terminate = True
        if len(new_bytes) > entry["str_len"]:
            self._front()
            reply = QMessageBox.question(
                self,
                "字串較長",
                f"新字串 {len(new_bytes)} 位元組，比原本 {entry['str_len']} 位元組長，"
                "寫入會覆蓋後面相鄰的記憶體，可能造成遊戲異常。\n確定要寫入嗎？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            null_terminate = False  # 已經在冒險，不再多寫一個 null 多覆蓋一格

        try:
            self._scanner.write_string(entry["addr"], text, encoding, null_terminate)
        except Exception as exc:  # noqa: BLE001
            self._notice(f"寫入失敗：{exc}", error=True)
            return
        self._refresh_watch_values()

    # ---- 狀態 -------------------------------------------------------

    def _set_scanning(self, scanning: bool) -> None:
        # 掃描期間暫停即時刷新，避免與背景執行緒同時動用同一個控制代碼。
        if scanning:
            self._watch_timer.stop()
        else:
            self._watch_timer.start()

        attached = self._scanner.attached
        self.first_button.setEnabled(not scanning and attached)
        self.next_button.setEnabled(not scanning and self._scanner.has_results)
        self.reset_button.setEnabled(not scanning)
        self.type_combo.setEnabled(not scanning)
        self.picker.set_enabled(not scanning)
        self.string_button.setEnabled(not scanning and attached)
        self.string_next_button.setEnabled(
            not scanning and attached and bool(self._string_hits)
        )

    def _update_enabled(self) -> None:
        attached = self._scanner.attached
        self.first_button.setEnabled(attached)
        self.next_button.setEnabled(attached and self._scanner.has_results)
        self.string_button.setEnabled(attached)
        self.string_next_button.setEnabled(attached and bool(self._string_hits))
        self.write_button.setEnabled(attached and self._scanner.can_write)
        self._on_scan_type_changed()

    def shutdown(self) -> None:
        super().shutdown()
        self._watch_timer.stop()
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()  # 下一個區塊回呼就會丟 ScanCancelled 收工
            worker.wait(5000)
            if worker.isRunning():
                # ⚠ 逾時代表執行緒還卡在 ReadProcessMemory 裡。這時**不能**關
                #   handle（等於在它腳下抽走），只能把回收交給行程收尾。
                log.warning("掃描執行緒未在 5 秒內結束，不關閉控制代碼")
                return
        self._scanner.close()
