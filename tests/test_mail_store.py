"""自動寄信的設定與觸發條件。

使用者指定（2026-08-30）：
「只要**那樣**物品數量達到我選擇的就會寄信，**不需要全部湊齊才寄**」。
"""

from __future__ import annotations

import json

import pytest

from ro_toolbox.services import mail_store
from ro_toolbox.services.mail_store import MailRule, MailSaved


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_store, "user_data_dir", lambda: tmp_path)
    return tmp_path / "mail_settings.json"


# ---- 觸發條件 --------------------------------------------------------------


def test_any_single_item_reaching_its_own_target_is_enough():
    """⚠ 使用者指定的重點：**任一樣**達標就寄，不是全部湊齊才寄。"""
    config = MailSaved(
        receiver="商狐", enabled=True,
        rules=(MailRule(1010, 100), MailRule(909, 50)),
    )
    assert mail_store.due(config, {1010: 100, 909: 0}).item_id == 1010
    assert mail_store.due(config, {1010: 0, 909: 77}).item_id == 909


def test_below_the_target_does_not_send():
    config = MailSaved(receiver="商狐", enabled=True, rules=(MailRule(909, 50),))
    assert mail_store.due(config, {909: 49}) is None


def test_nothing_happens_while_it_is_switched_off():
    """「寄信設定要有個啟用」—— 沒啟用就完全不動作。"""
    config = MailSaved(receiver="商狐", enabled=False, rules=(MailRule(909, 1),))
    assert config.usable is False
    assert mail_store.due(config, {909: 999}) is None


def test_no_receiver_means_no_sending():
    """⛔ 沒填收件人就不准寄 —— 寄出那一包要帶收件人的角色 ID。"""
    config = MailSaved(receiver="", enabled=True, rules=(MailRule(909, 1),))
    assert config.usable is False
    assert mail_store.due(config, {909: 999}) is None


# ---- 存檔 ------------------------------------------------------------------


def test_settings_survive_a_restart(store):
    """「這些一切都要記錄」。"""
    config = MailSaved(
        receiver="商狐", enabled=True,
        rules=(MailRule(909, 50), MailRule(1010, 100)),
    )
    mail_store.save("白狐", config)
    assert mail_store.get("白狐") == config


def test_each_character_keeps_its_own(store):
    """⚠ 鍵是**角色名**不是 PID —— PID 每次開遊戲都不一樣。"""
    mail_store.save("白狐", MailSaved(receiver="商狐", rules=(MailRule(909, 5),)))
    mail_store.save("狐狐狸", MailSaved(receiver="白狐", rules=(MailRule(1010, 9),)))
    assert mail_store.get("白狐").receiver == "商狐"
    assert mail_store.get("狐狐狸").receiver == "白狐"


def test_an_unknown_character_gets_an_empty_config(store):
    assert mail_store.get("沒設定過的人") == MailSaved()


def test_a_broken_file_does_not_take_the_page_down(store):
    store.write_text("{ 這不是 JSON", encoding="utf-8")
    assert mail_store.get("白狐") == MailSaved()


# ---- 不信任檔案內容 --------------------------------------------------------


def test_rubbish_rules_are_dropped_not_guessed(store):
    """⚠ 寧可少一條規則，也不要留一條會寄錯東西的。"""
    store.write_text(json.dumps({"白狐": {
        "receiver": "商狐",
        "enabled": True,
        "rules": [
            {"item_id": 909, "amount": 50},      # 好的
            {"item_id": 0, "amount": 10},        # 編號不合理
            {"item_id": 1010, "amount": 0},      # 數量不合理
            {"item_id": 1011, "amount": -5},     # 負數
            {"item_id": 909, "amount": 99},      # 重複（只留第一條）
            "這不是一筆設定",
        ],
    }}, ensure_ascii=False), encoding="utf-8")
    saved = mail_store.get("白狐")
    assert saved.rules == (MailRule(909, 50),)


def test_a_receiver_that_cannot_fit_in_the_packet_is_dropped(store):
    """封包的名字欄位是 24 bytes（cp950）—— 塞不下就當沒設定，不要截斷。

    截斷會**寄給另一個人**，那比不寄糟糕得多。
    """
    store.write_text(json.dumps({"白狐": {
        "receiver": "超級無敵霹靂長的角色名字一二三四五六七八九十",
        "enabled": True,
        "rules": [{"item_id": 909, "amount": 1}],
    }}, ensure_ascii=False), encoding="utf-8")
    assert mail_store.get("白狐").receiver == ""
    assert mail_store.get("白狐").usable is False
