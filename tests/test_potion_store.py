"""補水設定的存檔：依角色名記在使用者本機。

守三件事：
  1. 存的是**道具編號**不是格號（格號會挪動，存了遲早喝錯東西）
  2. 鍵是**角色名**不是 PID（PID 每次開遊戲都變）
  3. 檔案內容不可信：壞掉、被手改過、舊版寫的，都要洗成安全值
"""

from __future__ import annotations

import json

import pytest

from ro_toolbox.services import potion_store
from ro_toolbox.services.potion_store import PotionSaved


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每個測試各自一個資料夾，不要碰到使用者真的設定檔。"""
    monkeypatch.setattr(potion_store, "user_data_dir", lambda: tmp_path)
    return tmp_path


def test_nothing_saved_yet():
    assert potion_store.get("狐狐狸") is None


def test_round_trip_per_character():
    potion_store.save("狐狐狸", PotionSaved(hp_item=501, hp_percent=50, enabled=True))
    potion_store.save("商狐", PotionSaved(sp_item=505, sp_percent=30))

    fox = potion_store.get("狐狐狸")
    assert (fox.hp_item, fox.hp_percent, fox.enabled) == (501, 50, True)
    shop = potion_store.get("商狐")
    assert (shop.sp_item, shop.sp_percent, shop.enabled) == (505, 30, False)
    assert potion_store.get("白雪狐") is None, "沒存過的角色不該拿到別人的設定"


def test_saving_again_overwrites_only_that_character():
    potion_store.save("狐狐狸", PotionSaved(hp_item=501, hp_percent=50))
    potion_store.save("商狐", PotionSaved(hp_item=502, hp_percent=40))
    potion_store.save("狐狐狸", PotionSaved(hp_item=503, hp_percent=60))
    assert potion_store.get("狐狐狸").hp_item == 503
    assert potion_store.get("商狐").hp_item == 502


def test_percent_is_clamped_below_100():
    """100 以上會在滿血時照喝 —— 實測 12 秒把 58 瓶灌到剩 0（[MEM-021]）。"""
    potion_store.save("狐狐狸", PotionSaved(hp_percent=150, sp_percent=-5))
    got = potion_store.get("狐狐狸")
    assert got.hp_percent == 99
    assert got.sp_percent == 0


def test_hand_edited_garbage_degrades_safely(_isolated):
    (_isolated / "potion_settings.json").write_text(
        json.dumps({"狐狐狸": {"hp_item": "紅色藥水", "hp_percent": None, "enabled": 1}}),
        encoding="utf-8",
    )
    got = potion_store.get("狐狐狸")
    assert got.hp_item is None, "道具編號不是數字就當沒選，不要拿字串去送封包"
    assert got.hp_percent == 0
    assert got.enabled is True


def test_broken_file_is_treated_as_no_settings(_isolated):
    """一個壞檔案不該擋住整頁。"""
    (_isolated / "potion_settings.json").write_text("{ 這不是 json", encoding="utf-8")
    assert potion_store.get("狐狐狸") is None
    potion_store.save("狐狐狸", PotionSaved(hp_item=501))   # 還要能存回去
    assert potion_store.get("狐狐狸").hp_item == 501


def test_blank_character_is_ignored():
    """選角畫面的殘留結構名字是空的（[MEM-003]）—— 不能拿它當鍵。"""
    potion_store.save("", PotionSaved(hp_item=501))
    potion_store.save("   ", PotionSaved(hp_item=502))
    assert potion_store.get("") is None
    assert potion_store.get("   ") is None


def test_forget_removes_only_that_character():
    potion_store.save("狐狐狸", PotionSaved(hp_item=501))
    potion_store.save("商狐", PotionSaved(hp_item=502))
    potion_store.forget("狐狐狸")
    assert potion_store.get("狐狐狸") is None
    assert potion_store.get("商狐").hp_item == 502


def test_go_home_settings_survive_a_restart():
    """「水用完回程」的開關與道具也要記住 —— 使用者要求除了自動戰鬥全都存。"""
    potion_store.save("狐狐狸", PotionSaved(
        hp_item=501, hp_percent=60, go_home=True, home_item=602,
    ))
    back = potion_store.get("狐狐狸")
    assert back.go_home is True
    assert back.home_item == 602


def test_a_bad_home_item_falls_back_to_nothing():
    """檔案被手改壞就退回「沒選」—— 亂猜一個道具編號會用錯東西。"""
    potion_store.save("狐狐狸", PotionSaved(hp_item=501, hp_percent=60))
    path = potion_store._path()
    path.write_text(
        '{"狐狐狸": {"hp_item": 501, "hp_percent": 60, '
        '"go_home": true, "home_item": "蝴蝶翅膀"}}', encoding="utf-8"
    )
    back = potion_store.get("狐狐狸")
    assert back.home_item is None
    assert back.go_home is True

