"""應用程式圖示：檔案在、格式對、尺寸齊、真的掛到 QApplication 上。

為什麼值得測：圖示壞掉不會有任何錯誤訊息 —— 視窗照開，只是變成
Qt 的預設灰方塊，而且沒人會馬上發現。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import make_icon  # noqa: E402

from ro_toolbox.config.paths import icon_file  # noqa: E402

pytest.importorskip("PySide6.QtGui")

EXPECTED = set(make_icon.SIZES)


def _entries(raw: bytes) -> list[tuple[int, int, int, int]]:
    """讀 ICO 目錄，回傳 [(寬, 高, 資料長度, 位移)]。"""
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    assert reserved == 0, "ICO 保留欄位應為 0"
    assert kind == 1, f"應該是 ICO（type=1），拿到 type={kind}"
    out = []
    for i in range(count):
        w, h, _colors, _res, _planes, _bits, size, offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + i * 16
        )
        out.append((w or 256, h or 256, size, offset))
    return out


def test_the_icon_ships_with_the_package():
    assert icon_file().exists(), (
        f"{icon_file()} 不存在。跑 "
        r".\.venv\Scripts\python.exe tools\make_icon.py assets\icon-source.png"
    )


def test_it_has_every_size_windows_asks_for():
    """只放一種尺寸的話，其他場合會被系統硬縮，邊緣會糊。"""
    got = {w for w, _h, _n, _o in _entries(icon_file().read_bytes())}
    assert got == EXPECTED, f"缺了 {sorted(EXPECTED - got)}"


def test_every_frame_is_square_and_inside_the_file():
    raw = icon_file().read_bytes()
    for w, h, size, offset in _entries(raw):
        assert w == h, f"{w}×{h} 不是正方形"
        assert offset + size <= len(raw), f"{w}px 那格指到檔案外面（{offset}+{size}）"


def test_every_frame_actually_decodes():
    """目錄寫得對不代表資料是好的 —— 每一格都真的解一次。"""
    from PySide6.QtGui import QImage

    raw = icon_file().read_bytes()
    for w, _h, size, offset in _entries(raw):
        image = QImage.fromData(raw[offset : offset + size])
        assert not image.isNull(), f"{w}px 那格解不開"
        assert (image.width(), image.height()) == (w, w)


def test_build_ico_rejects_nothing_and_keeps_order():
    """產生器本身：尺寸順序要照傳進去的，位移要接得起來。"""
    from PySide6.QtGui import QImage

    source = QImage(64, 64, QImage.Format.Format_ARGB32)
    source.fill(0xFF3366CC)
    raw = make_icon.build_ico(source, sizes=(16, 32))
    got = _entries(raw)
    assert [w for w, _h, _n, _o in got] == [16, 32]
    first, second = got
    assert first[3] + first[2] == second[3], "第二格的位移應該緊接在第一格之後"


def test_the_running_app_actually_has_it(qapp):
    """檔案在不等於有掛上去。"""
    from ro_toolbox.app import _apply_icon

    _apply_icon(qapp)
    assert not qapp.windowIcon().isNull()


@pytest.mark.skipif(sys.platform != "win32", reason="AppUserModelID 是 Windows 專屬")
def test_windows_taskbar_gets_our_identity():
    """工作列**不看視窗圖示**，看 AppUserModelID。

    沒設的話，用 python.exe 跑起來的視窗會被歸到 python.exe 底下，
    工作列顯示的是 Python 的圖示 —— 而且完全沒有徵兆，視窗左上角還是對的。
    """
    import ctypes

    from ro_toolbox import APP_ID
    from ro_toolbox.app import _claim_taskbar_identity

    _claim_taskbar_identity()
    buf = ctypes.c_wchar_p()
    hr = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(buf))
    assert hr == 0, f"讀不回 AppUserModelID（HRESULT {hr:#x}）"
    assert buf.value == APP_ID


def test_the_app_id_has_no_spaces():
    """AppUserModelID 的格式是 CompanyName.ProductName，不能有空白。"""
    from ro_toolbox import APP_ID

    assert " " not in APP_ID
    assert "." in APP_ID


def test_a_missing_icon_does_not_stop_the_app(qapp, monkeypatch, caplog):
    """沒有圖示只是難看，不該擋住啟動 —— 記一行就好。"""
    from ro_toolbox import app as app_module

    monkeypatch.setattr(app_module, "icon_file", lambda: Path("不存在的圖示.ico"))
    with caplog.at_level("WARNING"):
        _apply_icon = app_module._apply_icon
        _apply_icon(qapp)
    assert "找不到圖示檔" in caplog.text
