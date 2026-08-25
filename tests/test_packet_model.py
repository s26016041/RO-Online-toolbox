from __future__ import annotations

from PySide6.QtWidgets import QApplication
from test_hexdump import make_packet

from ro_toolbox.ui.models.packet_model import _MAX_ROWS, PacketTableModel

_app = QApplication.instance() or QApplication([])


def test_append_and_read_back():
    model = PacketTableModel()
    model.append_many([make_packet(1), make_packet(2)])

    assert model.rowCount() == 2
    assert model.packet_at(0).index == 1
    assert model.packet_at(5) is None


def test_clear_resets_rows():
    model = PacketTableModel()
    model.append_many([make_packet(i) for i in range(1, 4)])
    model.clear()
    assert model.rowCount() == 0


def test_row_cap_drops_oldest():
    model = PacketTableModel()
    model.append_many([make_packet(i) for i in range(1, _MAX_ROWS + 1)])
    model.append_many([make_packet(_MAX_ROWS + 1)])

    assert model.rowCount() == _MAX_ROWS
    # 最舊的被丟掉，最新的留著
    assert model.packet_at(0).index == 2
    assert model.packet_at(_MAX_ROWS - 1).index == _MAX_ROWS + 1
