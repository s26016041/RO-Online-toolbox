"""煙霧測試：確認視窗能建立起來，不進事件迴圈。

在無顯示器環境（CI）需設定 QT_QPA_PLATFORM=offscreen。
"""

from __future__ import annotations

from ro_toolbox.app import create_app
from ro_toolbox.ui.main_window import PAGE_CLASSES


def test_main_window_builds():
    app, window = create_app([])
    try:
        assert window.stack.count() == len(PAGE_CLASSES)
        assert window.sidebar.count() == len(PAGE_CLASSES)
        window.show()
        app.processEvents()
        assert window.isVisible()
    finally:
        window.close()
