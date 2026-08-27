"""封包頁的方向處理。

## 為什麼要有這一支

`_on_packet` 曾經寫死 `if packet.outbound:` —— 伺服器推過來的封包在進 UI
之前就被丟掉了。症狀非常難查：

- 匯出永遠是「接收 0」，但擷取器其實兩個方向都收得到（實測 DNS 一來一回，
  送出 1 / 接收 3）。
- 序號會**跳號**（#1, #6, #9）—— 被丟掉的封包照樣佔了編號，
  看起來像「中間那幾包不見了」。
- **完全沒有徵兆**說東西被扔掉了。

而伺服器清單 `0x0069`、角色清單 `0x006B` 都是 inbound，那樣永遠看不到。

規則：**收集層不做過濾**。要不要只看某個方向是顯示層的選擇。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from ro_toolbox.core.ro_packet import RoPacket  # noqa: E402
from ro_toolbox.ui.pages.packet_page import PacketPage  # noqa: E402


def _packet(seq: int, outbound: bool, opcode: int = 0x0064) -> RoPacket:
    return RoPacket(
        seq=seq, timestamp=0.0, outbound=outbound, opcode=opcode, payload=b"\x01\x02"
    )


@pytest.fixture
def page(qtbot):
    widget = PacketPage()
    qtbot.addWidget(widget)
    yield widget
    widget.shutdown()


def test_both_directions_reach_the_table(page):
    """預設雙向都收。伺服器清單 0x0069 是 inbound，濾掉就永遠拿不到。"""
    page._on_packet(_packet(1, outbound=True))
    page._on_packet(_packet(2, outbound=False, opcode=0x0069))
    page._flush()

    directions = [p.outbound for p in page.model.packets]
    assert directions == [True, False]


def test_collector_never_filters(page):
    """收集層不准做方向判斷 —— 那是顯示層的事。"""
    page._on_packet(_packet(1, outbound=False))
    assert len(page._pending) == 1


def test_only_outbound_button_filters_at_display_time(page):
    page.only_outbound.setChecked(True)
    page._on_packet(_packet(1, outbound=True))
    page._on_packet(_packet(2, outbound=False))
    page._flush()

    assert [p.seq for p in page.model.packets] == [1]


def test_only_outbound_is_off_by_default(page):
    assert not page.only_outbound.isChecked()


def test_noise_filter_still_works(page):
    """隱藏心跳跟方向是兩件獨立的事，不要互相影響。"""
    from ro_toolbox.ui.pages.packet_page import _NOISE_OPCODES

    noisy = sorted(_NOISE_OPCODES)[0]
    page.hide_noise.setChecked(True)
    page._on_packet(_packet(1, outbound=False, opcode=noisy))
    page._on_packet(_packet(2, outbound=False, opcode=0x0069))
    page._flush()

    assert [p.opcode for p in page.model.packets] == [0x0069]


# ---- 空白畫面必須說得出原因 ------------------------------------------------


def test_says_when_capture_has_not_started(page):
    assert "尚未開始擷取" in page.stats_label.text()


class _FakeCapture:
    def __init__(self, server=""):
        self.server = server

    def stop(self, *_a, **_k):
        pass


def test_empty_because_the_game_is_not_connected(page):
    """登入畫面下遊戲沒有連線 —— 要講這件事，不是留一片空白。"""
    page._capture = _FakeCapture(server="")
    page._reset_stats()
    assert "還沒連上伺服器" in page.stats_label.text()


def test_empty_because_the_game_is_idle(page):
    """已連線但沒動作時只會有心跳（實測 15 秒 2 個）。要講清楚不是壞了。"""
    page._capture = _FakeCapture(server="219.84.200.98")
    page._reset_stats()
    text = page.stats_label.text()
    assert "219.84.200.98" in text
    assert "心跳" in text


def test_says_how_many_the_filters_ate(page):
    """收到了卻不顯示 —— 一定要說是被誰擋掉的，而且說得出數量。"""
    from ro_toolbox.ui.pages.packet_page import _NOISE_OPCODES

    page._capture = _FakeCapture(server="219.84.200.98")
    page._reset_stats()
    page.hide_noise.setChecked(True)
    noisy = sorted(_NOISE_OPCODES)[0]
    page._on_packet(_packet(1, outbound=True, opcode=noisy))
    page._on_packet(_packet(2, outbound=False, opcode=noisy))
    page._flush()

    text = page.stats_label.text()
    assert "收到 2 個（送出 1 / 接收 1）" in text
    assert "「隱藏心跳」擋掉 2 個" in text
    assert "全部被上面的篩選擋掉" in text


def test_noise_filter_is_off_by_default(page):
    """預設不藏 —— 心跳常常是唯一會出現的封包，藏了畫面就全白。"""
    assert not page.hide_noise.isChecked()
