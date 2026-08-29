"""「寄信設定」按鈕與它下面那段說明。

使用者 2026-08-30 指定：「自動寄信我要放在自動尋路選地圖的下面，然後自動寄信
啟動並選好之後他下面還要有文字說明：我的設定什麼東西、幾個、寄給誰、
目前那樣東西有幾個」。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services.mail_store import MailRule, MailSaved
from ro_toolbox.ui.pages.farm_page import CharacterCard

pytest.importorskip("PySide6.QtWidgets")

RED = 501          # 紅色藥水
LEG = 940          # 蝗蟲後腿


@pytest.fixture
def card(qtbot):
    widget = CharacterCard()
    qtbot.addWidget(widget)
    return widget


def test_the_button_sits_under_the_destination_picker(card):
    """位置就是使用者指定的：目的地選單的**下面**，同一欄。"""
    column = card.destination.parentWidget().layout()
    order = [column.itemAt(i).widget() for i in range(column.count())]
    assert card.destination in order
    assert card.mail_button.parentWidget() in order
    assert order.index(card.mail_button.parentWidget()) > order.index(card.destination)


def test_nothing_is_shown_before_it_is_set_up(card):
    card.set_mail_summary(MailSaved(), {})
    assert not card.mail_summary.isVisibleTo(card)


def test_switched_off_hides_it_again(card):
    """沒啟用就整塊收起來 —— 不要留一段看起來像在跑的字。"""
    card.set_mail_summary(
        MailSaved(receiver="商狐", enabled=False, rules=(MailRule(LEG, 10),)), {})
    assert not card.mail_summary.isVisibleTo(card)


def test_it_says_what_how_many_to_whom_and_how_many_now(card):
    """四件事一件都不能少 —— 少了「現在有幾個」就看不出還差多遠。"""
    card.set_mail_summary(
        MailSaved(receiver="商狐", enabled=True,
                  rules=(MailRule(LEG, 10), MailRule(RED, 100))),
        {LEG: 7, RED: 120},
    )
    text = card.mail_summary.text()
    assert "商狐" in text                      # 寄給誰
    assert "蝗蟲後腿" in text and "10" in text   # 什麼東西、幾個
    assert "現有 <b>7</b>" in text             # 目前有幾個
    assert "還差 3" in text
    assert "可以寄了" in text                  # 紅色藥水已經夠了


def test_an_item_that_is_gone_still_shows_up_as_zero(card):
    """背包裡暫時沒有的，也要列出來 —— 設定沒有消失，只是還沒湊到。"""
    card.set_mail_summary(
        MailSaved(receiver="商狐", enabled=True, rules=(MailRule(LEG, 10),)), {})
    assert "現有 <b>0</b>" in card.mail_summary.text()


def test_colours_follow_the_theme(card, monkeypatch):
    """⚠ 明暗兩套佈景 —— 顏色不能寫死一組，深色底要用亮一階的。"""
    dark = card._mail_colours()
    monkeypatch.setattr(type(card), "_mail_colours", lambda _self: ("#1", "#2", "#3"))
    assert card._MAIL_WHO == "#1"
    assert len(dark) == 3
