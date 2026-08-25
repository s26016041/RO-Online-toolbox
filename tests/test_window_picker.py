"""視窗選擇元件的行為測試。

用假的視窗清單，不依賴機器上實際開了什麼程式。
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from ro_toolbox.services import window_list
from ro_toolbox.ui.widgets import window_picker
from ro_toolbox.ui.widgets.window_picker import WindowPicker

_app = QApplication.instance() or QApplication([])


def make_window(pid: int, title: str, process: str = "Ragexe.exe"):
    return window_list.WindowInfo(hwnd=pid, pid=pid, title=title, process_name=process)


FAKE = [
    make_window(1, "Ragnarok"),
    make_window(2, "Ragnarok"),
    make_window(3, "記事本", "notepad.exe"),
    make_window(4, "Angels Online Global", "angel.dat"),
]


@pytest.fixture
def picker(monkeypatch):
    monkeypatch.setattr(window_picker.window_list, "enumerate_windows", lambda *a: list(FAKE))
    return WindowPicker()


def test_lists_everything_initially(picker):
    assert picker.count() == 4


def test_filter_applies_without_pressing_enter(picker):
    """打字當下就該生效——只按 Enter 才更新是使用者實際回報的 bug。"""
    picker.filter_edit.setText("Ragn")
    assert picker.count() == 2
    assert picker.selected().title == "Ragnarok"


def test_filter_is_case_insensitive(picker):
    for keyword in ("ragn", "RAGN", "RaGn"):
        picker.filter_edit.setText(keyword)
        assert picker.count() == 2, f"{keyword} 應該要匹配"


def test_clearing_filter_restores_full_list(picker):
    """清掉關鍵字要立刻恢復，不能整個清單消失。"""
    picker.filter_edit.setText("Ragn")
    assert picker.count() == 2
    picker.filter_edit.setText("")
    assert picker.count() == 4


def test_filter_with_no_match(picker):
    picker.filter_edit.setText("不存在的視窗")
    assert picker.count() == 0
    assert picker.selected() is None
    assert picker.selected_pid() is None


def test_selection_survives_filtering_by_pid(picker):
    """選取用 PID 記憶，篩選前後不能跳到別的視窗。"""
    index = picker.combo.findData(2)
    picker.combo.setCurrentIndex(index)
    assert picker.selected_pid() == 2

    picker.filter_edit.setText("Ragn")
    assert picker.selected_pid() == 2
    picker.filter_edit.setText("")
    assert picker.selected_pid() == 2


def test_empty_enumeration_keeps_previous_list(picker, monkeypatch):
    """列舉失敗（回空清單）不能把畫面清空——自動掛機頁踩過這個坑。"""
    assert picker.count() == 4
    monkeypatch.setattr(window_picker.window_list, "enumerate_windows", lambda *a: [])
    picker.refresh()
    assert picker.count() == 4, "列舉失敗時應保留上一次的清單"


def test_process_filter_limits_to_one_program(monkeypatch):
    monkeypatch.setattr(window_picker.window_list, "enumerate_windows", lambda *a: list(FAKE))
    picker = WindowPicker(process_filter="Ragexe.exe")
    assert picker.count() == 2
    assert all(w.process_name == "Ragexe.exe" for w in [picker.selected()])
