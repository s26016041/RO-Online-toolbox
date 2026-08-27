"""設定檔：全程式只能有**一份** AppSettings。

這一支盯的是實際踩過的坑：帳號頁存了遊戲路徑，關視窗時主視窗拿它自己那份
（`game_path` 還是空的）整檔覆蓋回去 —— 路徑安靜地不見，沒有任何錯誤訊息。
"""

from __future__ import annotations

import json

import pytest

from ro_toolbox.config import settings as settings_module
from ro_toolbox.config.settings import (
    AppSettings,
    current_settings,
    load_settings,
    save_settings,
)

GAME_PATH = r"D:\ro\RagnarokOnline\Ragnarok.exe"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把設定檔導到暫存目錄，別碰使用者真正的設定。"""
    target = tmp_path / "settings.json"
    monkeypatch.setattr(settings_module, "config_file", lambda: target)
    monkeypatch.setattr(settings_module, "_current", None)
    return target


def test_everyone_shares_one_instance(store):
    """`current_settings()` 拿到的必須是**同一個物件**，不是各自一份。"""
    first = load_settings()
    assert current_settings() is first
    assert current_settings() is current_settings()


def test_a_stale_copy_cannot_wipe_the_game_path(store):
    """重現原本的 bug：主視窗那份較舊，關視窗存檔時不可以把路徑蓋掉。"""
    main_window_copy = load_settings()          # app.py 啟動時拿的那份

    page_copy = current_settings()              # 帳號頁拿的那份
    page_copy.game_path = GAME_PATH
    save_settings(page_copy)                    # 使用者選好路徑

    main_window_copy.window.maximized = True    # 關視窗時只動視窗大小
    save_settings(main_window_copy)

    assert json.loads(store.read_text(encoding="utf-8"))["game_path"] == GAME_PATH


def test_the_path_survives_a_restart(store):
    """關掉再打開要看得到上次選的路徑（使用者的原話）。"""
    first = load_settings()
    first.game_path = GAME_PATH
    save_settings(first)

    monkey = settings_module
    monkey._current = None                      # 模擬重開程式
    assert load_settings().game_path == GAME_PATH


def test_a_broken_file_does_not_lose_the_shared_instance(store):
    """設定檔壞掉要退回預設值，但仍然是**大家共用的那一份**。"""
    store.write_text("{ 這不是 json", encoding="utf-8")
    loaded = load_settings()
    assert loaded == AppSettings()
    assert current_settings() is loaded
