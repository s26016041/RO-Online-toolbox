"""封包分頁（網路層擷取，不碰遊戲行程）。

流程：選 RO 視窗 → 開始擷取 → 回遊戲做動作 → 下方即時列出封包 → 匯出。

**完全不寫遊戲記憶體、不注入** —— 之前的注入版會被 GameGuard 偵測並讓遊戲當機
（見 GAMEDATA [PKT-011]），已停用。

走 `services/packet_capture`（WinDivert，**雙向**：送出 + 伺服器推送）；
它起不來時才退回 `services/ro_capture` 的 raw socket
（**只收得到送出**，見 [PKT-003]）。

⚠ 這一頁曾經在 `_on_packet` 寫死「只留送出」，把伺服器推的封包在進 UI 之前
就丟光了 —— 匯出永遠「接收 0」、序號跳號，而且沒有任何徵兆。
**收集層不做過濾**，方向是顯示層的選擇（「只看送出」那顆按鈕）。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QWidget,
)

from ro_toolbox.config.paths import capture_dir
from ro_toolbox.core.ro_packet import RoPacket
from ro_toolbox.services import packet_capture
from ro_toolbox.services.process_monitor import is_admin
from ro_toolbox.services.ro_capture import RoPacketCapture, find_server
from ro_toolbox.ui.models.ro_packet_model import RoPacketTableModel
from ro_toolbox.ui.pages.base_page import BasePage
from ro_toolbox.ui.widgets.hex_view import HexView
from ro_toolbox.ui.widgets.window_picker import WindowPicker
from ro_toolbox.utils.hexdump import format_ro_packet, format_ro_packets

log = logging.getLogger(__name__)

_FLUSH_INTERVAL_MS = 200
_COLUMN_WIDTHS = (56, 108, 82, 84, 62)

_ADMIN_WARNING = "⚠ 目前不是以系統管理員身分執行 —— raw socket 會建立失敗，抓不到封包。"

_ABOUT_PCAP = (
    "抓遊戲與伺服器往來的封包（**雙向**）。只讀網路、不碰遊戲記憶體，"
    "GameGuard 看不到。做一個動作看跳出哪個 opcode，就知道那個動作的封包。"
)
_ABOUT_RAW = (
    "WinDivert 起不來（多半是沒有用系統管理員執行），退回 raw socket —— "
    "**只收得到「送出」方向**，看不到伺服器推過來的封包"
    "（伺服器清單、角色清單都在那一邊）。用系統管理員重跑就會改用雙向擷取。"
)

# 心跳等會一直自動送的封包，對照動作時是雜訊
_NOISE_OPCODES = {0x0360, 0x007D, 0x0187}


def _row(*widgets: QWidget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return container


class PacketPage(BasePage):
    title = "封包"
    subtitle = "選 RO 視窗，抓它與伺服器往來的封包，對照動作。"
    stretch_at_end = False

    def __init__(self) -> None:
        self._capture = None
        self._pending: list[RoPacket] = []
        # 擷取器**真的**交給我們幾個、畫面上留下幾個、各被誰擋掉幾個。
        # 沒有這組數字的話「表格空白」有五種可能的原因而畫面一種都不說。
        self._stats = {"received": 0, "out": 0, "in": 0, "noise": 0, "direction": 0}
        self._last_packet_at = 0.0
        super().__init__()

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)

        # 就算一個封包都沒來，狀態列也要繼續更新（「已經 N 秒沒有封包」
        # 本身就是重要資訊）。
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(500)
        self._stats_timer.timeout.connect(self._update_stats_label)
        self._stats_timer.start()

    # ---- 版面 -------------------------------------------------------

    def _about_text(self) -> str:
        """說明要跟**實際會用哪條擷取路徑**一致，不然使用者會以為抓不到是壞了。"""
        return _ABOUT_PCAP if packet_capture.available()[0] else _ABOUT_RAW

    def build(self) -> None:
        self.notice = QLabel(_ADMIN_WARNING)
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.setVisible(not is_admin())
        self.add(self.notice)

        self.about = QLabel(self._about_text())
        self.about.setObjectName("infoBar")
        self.about.setWordWrap(True)
        self.add(self.about)

        self.picker = WindowPicker(
            label="目標視窗",
            process_filter="Ragexe.exe",
            combo_width=380,
            state_key="packet",
        )
        self.picker.selection_changed.connect(self._update_server_label)
        self.add(self.picker)

        self._build_control_row()
        self._build_viewer()
        self._build_export_row()
        self._update_server_label()

    def _build_control_row(self) -> None:
        self.start_button = QPushButton("開始擷取")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start)

        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)

        self.clear_button = QPushButton("清除")
        self.clear_button.clicked.connect(self._clear)

        # ⚠ 預設**關閉**。心跳（0x0187 等）常常是角色站著不動時**唯一**會出現的
        # 封包 —— 預設藏起來的話畫面就是全白，而且不會說為什麼，看起來像壞掉。
        # 實測：已登入但站著不動，15 秒內遊戲連線只有 2 個封包，兩個都是 0x0187。
        self.hide_noise = QPushButton("隱藏心跳")
        self.hide_noise.setCheckable(True)
        self.hide_noise.setChecked(False)
        self.hide_noise.setToolTip(
            "心跳類封包（0x0187／0x0360／0x007D）會一直自動送，對照動作時是雜訊。\n"
            "藏起來的數量會顯示在下方狀態列，不會安靜消失。"
        )
        self.hide_noise.toggled.connect(self._update_stats_label)

        # ⚠ 預設**雙向都收**。這裡原本是寫死「只留送出」，於是伺服器推的封包
        # 在進 UI 之前就被丟光 —— 匯出永遠是「接收 0」，序號還會跳號
        # （被丟掉的那些照樣佔了編號）。伺服器清單 0x0069、角色清單 0x006B
        # 都是 inbound，那樣永遠看不到。要只看動作封包時再按這顆。
        self.only_outbound = QPushButton("只看送出")
        self.only_outbound.setCheckable(True)
        self.only_outbound.setChecked(False)
        self.only_outbound.setToolTip(
            "只留客戶端送出的封包。關掉（預設）會連伺服器推過來的一起收。"
        )
        self.only_outbound.toggled.connect(self._update_stats_label)

        self.server_label = QLabel()
        self.server_label.setObjectName("pageSubtitle")

        self.add(
            _row(
                self.start_button,
                self.stop_button,
                self.clear_button,
                self.hide_noise,
                self.only_outbound,
                self.server_label,
            )
        )

        self.stats_label = QLabel()
        self.stats_label.setObjectName("infoBar")
        self.stats_label.setWordWrap(True)
        self.add(self.stats_label)
        self._update_stats_label()

    def _build_viewer(self) -> None:
        self.model = RoPacketTableModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.selectionModel().currentRowChanged.connect(self._on_row_changed)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for column, width in enumerate(_COLUMN_WIDTHS):
            self.table.setColumnWidth(column, width)

        self.hex_view = HexView()
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.hex_view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self._layout.addWidget(splitter, 1)

    def _build_export_row(self) -> None:
        self.copy_selected_button = QPushButton("複製選取")
        self.copy_selected_button.clicked.connect(lambda: self._copy(True))
        self.copy_all_button = QPushButton("複製全部")
        self.copy_all_button.clicked.connect(lambda: self._copy(False))
        self.save_button = QPushButton("另存文字檔…")
        self.save_button.clicked.connect(self._save)

        self.export_hint = QLabel("匯出含 opcode 統計，可直接貼給 AI 分析。")
        self.export_hint.setObjectName("pageSubtitle")

        self.add(
            _row(
                self.copy_selected_button,
                self.copy_all_button,
                self.save_button,
                self.export_hint,
            )
        )

    # ---- 狀態 -------------------------------------------------------

    def _update_server_label(self) -> None:
        target = self.picker.selected()
        if target is None:
            self.server_label.setText("未選擇 RO 視窗")
            self.start_button.setEnabled(False)
            return
        # 有沒有登入**不影響能不能開始擷取**。擷取器就是擷取器，
        # 沒連線就先開著等 —— 登入交握（連線從無到有的那一刻）只有這樣才抓得到。
        self.start_button.setEnabled(is_admin())
        server = find_server(target.pid)
        if server:
            self.server_label.setText(f"伺服器 {server[0]}:{server[1]}")
        else:
            self.server_label.setText("尚未連線 —— 可以先開始擷取，再回遊戲按登入")

    # ---- 擷取控制 ---------------------------------------------------

    def _start(self) -> None:
        target = self.picker.selected()
        if target is None:
            QMessageBox.information(self, "未選擇目標", "請先選一個 RO 視窗。")
            return
        # WinDivert 抓雙向；它起不來才退回 raw socket（只有送出方向）
        if packet_capture.available()[0]:
            capture = packet_capture.PacketCapture(
                target.pid, self._on_packet, on_error=self._on_error
            )
        else:
            capture = RoPacketCapture(
                target.pid, self._on_packet, on_error=self._on_error
            )
        if not capture.start():
            return
        self._capture = capture
        self._reset_stats()
        self._flush_timer.start()
        self._set_running(True)
        where = capture.server or "（等連線出現）"
        self.server_label.setText(f"擷取中：{target.title} → {where}　回遊戲做動作")

    def _stop(self) -> None:
        self._flush_timer.stop()
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        self._flush()
        self._set_running(False)
        self._update_server_label()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.picker.set_enabled(not running)

    def _reset_stats(self) -> None:
        self._stats = {"received": 0, "out": 0, "in": 0, "noise": 0, "direction": 0}
        self._last_packet_at = time.monotonic()
        self._update_stats_label()

    def _clear(self) -> None:
        self.model.clear()
        self.hex_view.setPlainText("")
        self._reset_stats()

    def shutdown(self) -> None:
        self._stats_timer.stop()
        if self._capture is not None:
            self._stop()

    # ---- 封包流 -----------------------------------------------------

    def _on_packet(self, packet: RoPacket) -> None:
        """在擷取執行緒執行，只丟進暫存，由計時器整批送進 UI。

        ⚠ **這裡不做任何過濾。** 以前這裡寫死 `if packet.outbound`，把伺服器
        推過來的封包在進 UI 之前就丟掉了 —— 症狀是匯出永遠「接收 0」、
        序號還跳號（被丟掉的照樣佔編號），而且完全沒有徵兆說東西被扔了。
        方向要不要濾是**顯示層的選擇**（`_flush` 看「只看送出」那顆按鈕），
        不是收集層該做的決定。
        """
        self._pending.append(packet)

    def _flush(self) -> None:
        if not self._pending:
            return
        batch = self._pending
        self._pending = []

        self._stats["received"] += len(batch)
        self._stats["out"] += sum(1 for p in batch if p.outbound)
        self._stats["in"] += sum(1 for p in batch if not p.outbound)
        self._last_packet_at = time.monotonic()

        if self.only_outbound.isChecked():
            dropped = [p for p in batch if not p.outbound]
            self._stats["direction"] += len(dropped)
            batch = [p for p in batch if p.outbound]
        if self.hide_noise.isChecked():
            dropped = [p for p in batch if p.opcode in _NOISE_OPCODES]
            self._stats["noise"] += len(dropped)
            batch = [p for p in batch if p.opcode not in _NOISE_OPCODES]

        # ⚠ 狀態列要在 **append 之後**算。擺在前面的話 rowCount() 還是舊的，
        # 明明有收到、表格也有東西，狀態列卻會說「全部被篩選擋掉了」—— 騙人。
        if batch:
            scrollbar = self.table.verticalScrollBar()
            at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
            self.model.append_many(batch)
            if at_bottom:
                self.table.scrollToBottom()
        self._update_stats_label()

    def _update_stats_label(self) -> None:
        """讓「表格空白」永遠說得出原因。

        表格空白有五種可能：沒開始擷取、遊戲沒連線、遊戲真的沒送東西、
        被「隱藏心跳」擋掉、被「只看送出」擋掉。**畫面必須指出是哪一種** ——
        不然使用者只能得到「壞了」這個結論，而那通常是錯的。
        """
        stats = self._stats
        if self._capture is None:
            self.stats_label.setText("尚未開始擷取。")
            return

        parts = [
            f"收到 {stats['received']} 個（送出 {stats['out']} / 接收 {stats['in']}）",
            f"顯示 {self.model.rowCount()} 個",
        ]
        if stats["noise"]:
            parts.append(f"「隱藏心跳」擋掉 {stats['noise']} 個")
        if stats["direction"]:
            parts.append(f"「只看送出」擋掉 {stats['direction']} 個")
        text = "　".join(parts)

        if stats["received"] == 0:
            where = self._capture.server
            text += (
                "　—— 還沒有任何封包。"
                + (
                    f"目前鎖定 {where}；遊戲沒動作時本來就只會有心跳。"
                    if where
                    else "遊戲還沒連上伺服器，連上就會開始收。"
                )
            )
        elif self.model.rowCount() == 0:
            text += "　—— 收到的全部被上面的篩選擋掉了，把它們關掉就看得到。"
        else:
            idle = time.monotonic() - self._last_packet_at
            if idle > 5:
                text += f"　—— 已經 {idle:.0f} 秒沒有新封包。"
        self.stats_label.setText(text)

    def _on_row_changed(self, current, _previous) -> None:
        packet = self.model.packet_at(current.row())
        self.hex_view.setPlainText(format_ro_packet(packet) if packet else "")

    def _on_error(self, message: str) -> None:
        self._stop()
        QMessageBox.warning(self, "封包擷取", message)

    # ---- 匯出 -------------------------------------------------------

    def _collect(self, selected_only: bool) -> list:
        if not selected_only:
            return self.model.packets
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [p for p in (self.model.packet_at(row) for row in rows) if p is not None]

    def _title(self) -> str:
        target = self.picker.selected()
        return target.title if target else ""

    def _copy(self, selected_only: bool) -> None:
        packets = self._collect(selected_only)
        if not packets:
            QMessageBox.information(self, "沒有內容", "沒有可複製的封包。")
            return
        QApplication.clipboard().setText(format_ro_packets(packets, self._title()))
        self.export_hint.setText(f"已複製 {len(packets)} 筆到剪貼簿。")

    def _save(self) -> None:
        packets = self._collect(selected_only=False)
        if not packets:
            QMessageBox.information(self, "沒有內容", "沒有可儲存的封包。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "儲存封包",
            str(capture_dir() / "ro_packets.txt"),
            "文字檔 (*.txt);;所有檔案 (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(format_ro_packets(packets, self._title()))
        except OSError as exc:
            QMessageBox.warning(self, "儲存失敗", str(exc))
            return
        self.export_hint.setText(f"已儲存 {len(packets)} 筆至 {path}")
        log.info("封包已匯出：%s", path)
