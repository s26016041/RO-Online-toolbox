"""封包列表的 table model。

用 model/view 而非 QTableWidget，是因為擷取時每 100ms 就會整批插入，
逐格建立 item 在幾千筆之後會明顯卡頓。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from ro_toolbox.core.packet import CapturedPacket

_HEADERS = ["#", "時間", "方向", "來源", "目的", "長度", "資料預覽"]
_MAX_ROWS = 5000

_OUTBOUND_COLOR = QColor("#7fb2ff")
_INBOUND_COLOR = QColor("#8fd694")


class PacketTableModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._packets: list[CapturedPacket] = []

    # ---- Qt 介面 ----------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._packets)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return _HEADERS[section]

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        packet = self._packets[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return (
                packet.index,
                packet.time_text(),
                f"{packet.arrow} {packet.direction}",
                packet.source,
                packet.destination,
                packet.length,
                packet.preview(),
            )[column]

        if role == Qt.ItemDataRole.ForegroundRole and column == 2:
            return _OUTBOUND_COLOR if packet.outbound else _INBOUND_COLOR

        if role == Qt.ItemDataRole.TextAlignmentRole and column in (0, 5):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        return None

    # ---- 資料操作 ---------------------------------------------------

    def append_many(self, packets: list[CapturedPacket]) -> None:
        if not packets:
            return

        overflow = len(self._packets) + len(packets) - _MAX_ROWS
        if overflow > 0:
            drop = min(overflow, len(self._packets))
            self.beginRemoveRows(QModelIndex(), 0, drop - 1)
            del self._packets[:drop]
            self.endRemoveRows()

        start = len(self._packets)
        self.beginInsertRows(QModelIndex(), start, start + len(packets) - 1)
        self._packets.extend(packets)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self._packets.clear()
        self.endResetModel()

    def packet_at(self, row: int) -> CapturedPacket | None:
        if 0 <= row < len(self._packets):
            return self._packets[row]
        return None

    @property
    def packets(self) -> list[CapturedPacket]:
        return list(self._packets)
