"""記憶體分頁：訊息一律就地顯示，不准用強制回應對話框擋住整個工具箱。

## 為什麼要有這一支

實際回報過的災情：在記憶體分頁按「選定此程序」之後整個工具箱像當機一樣不動，
同時噴出 `QFont::setPointSize: Point size <= 0 (-1)`。那行警告正是 Qt 在建
對話框時算字級噴的（本專案樣式表用 px 定字型大小，`pointSize()` 是 -1）。

真正發生的事：`QMessageBox` 是**強制回應**的，而遊戲通常全螢幕或置頂，
對話框跳到遊戲**後面** —— 使用者看不到、也點不到，程式就停在那裡等回應。
症狀跟當機一模一樣，但其實只是一個看不見的對話框。

所以規則是：**提示與錯誤一律寫在頁面上**。只有非用不可的（要打字、要確認
破壞性寫入）才准跳窗，而且跳之前要把主視窗拉到最前面。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

from ro_toolbox.ui.pages import memory_page as page_module  # noqa: E402

SOURCE = Path(page_module.__file__).read_text(encoding="utf-8")

#: 這些會擋住整個程式又可能藏在遊戲後面，一律不准用。
FORBIDDEN = ("QMessageBox.information", "QMessageBox.warning", "QMessageBox.critical")


@pytest.mark.parametrize("call", FORBIDDEN)
def test_no_blocking_message_boxes(call):
    assert call not in SOURCE, (
        f"記憶體分頁不准用 {call}：它會跳到全螢幕遊戲後面，看起來就是當機。"
        "改用 self._notice(訊息, error=True)。"
    )


def test_destructive_confirmation_raises_the_window_first():
    """確認框可以留（破壞性寫入該問），但一定要先把視窗拉到前面。"""
    body = SOURCE[SOURCE.index("def _write_string_entry") :]
    question = body.index("QMessageBox.question")
    before = body[:question]
    assert "self._front()" in before, "跳確認框之前沒有先把主視窗拉到最前面"


def test_input_dialogs_raise_the_window_first():
    """要打字的對話框同理 —— 躲在遊戲後面就等於當機。"""
    for match in re.finditer(r"QInputDialog\.getText", SOURCE):
        window = SOURCE[max(0, match.start() - 200) : match.start()]
        assert "self._front()" in window, "QInputDialog 之前沒有先把主視窗拉到最前面"


def test_notice_shows_and_logs(qtbot, monkeypatch):
    """就地提示要真的看得到；空字串要收起來，不要留一條空白列。"""
    monkeypatch.setattr(page_module, "_is_admin", lambda: True)
    page = page_module.MemoryPage()
    qtbot.addWidget(page)
    try:
        assert not page.notice_label.isVisible() or not page.notice_label.text()

        page._notice("開不了這個程序")
        assert page.notice_label.text() == "開不了這個程序"

        page._notice("")
        assert page.notice_label.text() == ""
    finally:
        page.shutdown()


def test_warns_when_not_admin(qtbot, monkeypatch):
    """權限不夠時要一進頁面就講，不要等使用者按下去才撞牆。"""
    monkeypatch.setattr(page_module, "_is_admin", lambda: False)
    page = page_module.MemoryPage()
    qtbot.addWidget(page)
    try:
        assert "系統管理員" in page.notice_label.text()
    finally:
        page.shutdown()
