"""RO 封包列表的 table model。"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from ro_toolbox.core.ro_packet import RoPacket

_HEADERS = ["#", "時間", "方向", "opcode", "長度", "內容"]
_MAX_ROWS = 5000

_OUTBOUND_COLOR = QColor("#2f6feb")
_INBOUND_COLOR = QColor("#3fa45b")


class RoPacketTableModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._packets: list[RoPacket] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._packets)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
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
                packet.seq,
                packet.time_text(),
                f"{packet.arrow} {packet.direction}",
                packet.opcode_hex,
                packet.length,
                packet.payload_hex(),
            )[column]

        if role == Qt.ItemDataRole.ForegroundRole and column == 2:
            return _OUTBOUND_COLOR if packet.outbound else _INBOUND_COLOR

        if role == Qt.ItemDataRole.TextAlignmentRole and column in (0, 4):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        return None

    def append_many(self, packets: list[RoPacket]) -> None:
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

    def packet_at(self, row: int) -> RoPacket | None:
        if 0 <= row < len(self._packets):
            return self._packets[row]
        return None

    @property
    def packets(self) -> list[RoPacket]:
        return list(self._packets)
