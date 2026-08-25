"""封包內容的 hex dump 檢視。"""

from __future__ import annotations

from PySide6.QtWidgets import QPlainTextEdit

from ro_toolbox.core.packet import CapturedPacket
from ro_toolbox.utils.hexdump import format_packet

_EMPTY_HINT = "選擇上方任一封包以檢視內容"


class HexView(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("hexView")
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setPlaceholderText(_EMPTY_HINT)

    def show_packet(self, packet: CapturedPacket | None) -> None:
        self.setPlainText(format_packet(packet) if packet else "")
