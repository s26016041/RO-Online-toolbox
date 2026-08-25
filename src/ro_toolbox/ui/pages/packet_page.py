"""封包分頁（網路層擷取，不碰遊戲行程）。

流程：選 RO 視窗 → 開始擷取 → 回遊戲做動作 → 下方即時列出送出的封包 → 匯出。

用 raw socket 抓遊戲送往伺服器的封包（見 services/ro_capture.py），
**完全不寫遊戲記憶體、不注入** —— 之前的注入版會被 GameGuard 偵測並讓遊戲當機
（見 GAMEDATA [PKT-011]），已停用。

限制：raw socket 只收得到送出（outbound）的封包。要看伺服器推送需改用 Npcap。
"""

from __future__ import annotations

import logging

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
from ro_toolbox.services import pcap_capture
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

_ABOUT = (
    "用 raw socket 抓遊戲送往伺服器的封包，只讀網路、不碰遊戲記憶體，"
    "GameGuard 看不到。目前只收得到「送出」方向；要看伺服器推送需另裝 Npcap。"
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
    subtitle = "選 RO 視窗，抓它送出的封包，對照動作。"
    stretch_at_end = False

    def __init__(self) -> None:
        self._capture = None
        self._pending: list[RoPacket] = []
        super().__init__()

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)

    # ---- 版面 -------------------------------------------------------

    def _about_text(self) -> str:
        return (
            "只顯示「送出」的封包（動作對照用）。只讀網路、不碰遊戲記憶體，"
            "GameGuard 看不到。做一個動作看跳出哪個 opcode，就知道那個動作的封包。"
        )

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

        self.hide_noise = QPushButton("隱藏心跳")
        self.hide_noise.setCheckable(True)
        self.hide_noise.setChecked(True)

        self.server_label = QLabel()
        self.server_label.setObjectName("pageSubtitle")

        self.add(
            _row(
                self.start_button,
                self.stop_button,
                self.clear_button,
                self.hide_noise,
                self.server_label,
            )
        )

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
        server = find_server(target.pid)
        if server:
            self.server_label.setText(f"伺服器 {server[0]}:{server[1]}")
            self.start_button.setEnabled(is_admin())
        else:
            self.server_label.setText("這個行程還沒連到伺服器（尚未登入？）")
            self.start_button.setEnabled(False)

    # ---- 擷取控制 ---------------------------------------------------

    def _start(self) -> None:
        target = self.picker.selected()
        if target is None:
            QMessageBox.information(self, "未選擇目標", "請先選一個 RO 視窗。")
            return
        # 有 Npcap 就抓雙向，沒有就退回 raw socket（只有送出方向）
        if pcap_capture.available()[0]:
            capture = pcap_capture.PcapCapture(
                target.pid, self._on_packet, on_error=self._on_error
            )
        else:
            capture = RoPacketCapture(
                target.pid, self._on_packet, on_error=self._on_error
            )
        if not capture.start():
            return
        self._capture = capture
        self._flush_timer.start()
        self._set_running(True)
        self.server_label.setText(
            f"擷取中：{target.title} → {capture.server}　回遊戲做動作"
        )

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

    def _clear(self) -> None:
        self.model.clear()
        self.hex_view.setPlainText("")

    def shutdown(self) -> None:
        if self._capture is not None:
            self._stop()

    # ---- 封包流 -----------------------------------------------------

    def _on_packet(self, packet: RoPacket) -> None:
        """在擷取執行緒執行，只丟進暫存，由計時器整批送進 UI。

        只保留「送出」方向：這頁是給人看動作封包用的，伺服器推送的 inbound
        對這用途沒意義（怪物/掉落由 farm bot 內部處理）。
        """
        if packet.outbound:
            self._pending.append(packet)

    def _flush(self) -> None:
        if not self._pending:
            return
        batch = self._pending
        self._pending = []
        if self.hide_noise.isChecked():
            batch = [p for p in batch if p.opcode not in _NOISE_OPCODES]
        if not batch:
            return
        scrollbar = self.table.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        self.model.append_many(batch)
        if at_bottom:
            self.table.scrollToBottom()

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
