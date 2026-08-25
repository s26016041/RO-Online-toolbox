"""注入攔截封包的 table model。"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from ro_toolbox.core.intercept import InterceptedPacket

_HEADERS = ["#", "時間", "長度", "亂度", "呼叫鏈", "資料預覽"]
_MAX_ROWS = 5000

_ENCRYPTED_COLOR = QColor("#e0b341")
_PLAIN_COLOR = QColor("#8fd694")
_ENCRYPTED_THRESHOLD = 7.5


class InterceptTableModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._packets: list[InterceptedPacket] = []

    # ---- Qt 介面 ----------------------------------------------------

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
            length = f"{packet.length}*" if packet.truncated else str(packet.length)
            return (
                packet.seq,
                datetime.fromtimestamp(packet.timestamp).strftime("%H:%M:%S.%f")[:-3],
                length,
                f"{packet.entropy():.2f}",
                packet.chain_text(),
                packet.preview(),
            )[column]

        if role == Qt.ItemDataRole.ForegroundRole and column == 3:
            high = packet.entropy() > _ENCRYPTED_THRESHOLD
            return _ENCRYPTED_COLOR if high else _PLAIN_COLOR

        if role == Qt.ItemDataRole.TextAlignmentRole and column in (0, 2, 3):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ToolTipRole and column == 2 and packet.truncated:
            return f"實際送出 {packet.length} bytes，只記錄了前 {len(packet.data)} bytes"

        return None

    # ---- 資料操作 ---------------------------------------------------

    def append_many(self, packets: list[InterceptedPacket]) -> None:
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

    def packet_at(self, row: int) -> InterceptedPacket | None:
        if 0 <= row < len(self._packets):
            return self._packets[row]
        return None

    @property
    def packets(self) -> list[InterceptedPacket]:
        return list(self._packets)
