import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# ⚠ 已知雜訊：跑 tests/test_potion.py 時 faulthandler 偶爾會印
# 「Windows fatal exception: access violation」（間歇，同樣指令有時 0 次有時 2 次）。
# 那是**第一次例外**，行程繼續正常跑完、測試全過，實機也沒問題。
# 查過的方向都不是穩定成因：pytest 外掛（logging/capture/cacheprovider）全關掉照樣出現、
# 換 logging handler 也無法穩定消除、純 Event.wait 的最小重現不會發生。
# 目前當成環境層級的假警報，不為了消訊息去改動正常的執行緒收尾邏輯。


@pytest.fixture(autouse=True, scope="session")
def _keep_user_data_out_of_appdata(tmp_path_factory):
    """⚠⚠ **測試不准碰使用者真實的 `%APPDATA%\\RO-Online-toolbox\\`。**

    實機踩到（[DAT-079] 第二段，2026-09-05）：`test_farm_page.py` 有一條用真的
    角色名「狐狐狸」＋ `PotionSaved(hp_item=501, hp_percent=60)` 走到
    `_save_potion()`，而它沒有導開 `potion_store` 的路徑 —— 每跑一次 pytest
    就把使用者真的 `potion_settings.json` 裡狐狐狸的藥水**從 502 改成 501**
    （其他欄位被 `_keep_remembered` 保住，所以檔案看起來很正常）。
    使用者選的是背包裡有的 502，程式重開後拿到 501 → 背包裡沒有 →
    「紅色藥水 用完了 → 回程 → 買水」。一天兩次，日誌裡一行「記住…」都沒有。

    每個 store 各自 `monkeypatch` 是擋不完的（十個模組、每條測試都要記得）。
    `user_data_dir()` 每次呼叫都看 `APPDATA`，所以整個 session 把它指到暫存目錄，
    一次擋掉所有 store（補水、撿取黑名單、寄信、商店記憶、技能、帳號、設定）。
    """
    target = tmp_path_factory.mktemp("appdata")
    import os

    before = os.environ.get("APPDATA")
    os.environ["APPDATA"] = str(target)
    try:
        yield target
    finally:
        if before is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = before


@pytest.fixture(autouse=True, scope="session")
def _keep_logs_out_of_appdata(tmp_path_factory):
    """⚠ **測試不准寫進使用者真實的 app.log。**

    `tests/test_smoke.py` 會呼叫 `create_app()`，那會 `setup_logging()` 並在
    root logger 掛上一個指向使用者 AppData 底下那個 logs/app.log 的
    檔案 handler —— 而且**整個 pytest 過程都留著**。於是後面每一條測試的日誌
    都被寫進使用者的真實紀錄檔。

    實際造成的傷害：使用者把 app.log 貼過來診斷問題時，裡面混著
    `路線算好了：2 段 —— a → b → c`、`(5, 5) → (10, 10)` 這種**測試假地圖**的行，
    看起來像真的在跑圖 —— 診斷的人（含 AI）會直接讀錯（實際發生過）。

    所以把 `log_dir()` 整個 session 導到暫存目錄。
    """
    target = tmp_path_factory.mktemp("logs")
    from ro_toolbox.config import paths
    from ro_toolbox.utils import logging as logging_mod

    original_paths, original_logging = paths.log_dir, logging_mod.log_dir
    paths.log_dir = lambda: target
    logging_mod.log_dir = lambda: target
    try:
        yield target
    finally:
        paths.log_dir = original_paths
        logging_mod.log_dir = original_logging


@pytest.fixture(autouse=True)
def _our_socket_copy_is_alive(monkeypatch):
    """測試裡的 socket 是假的整數，`getpeername` 當然問不出東西來。

    ⚠ 沒有這一條的話 `game_socket.socket_alive()` 對每個假 socket 都回 False，
    於是每一支測試都會走「複本被遊戲關掉了 → 重綁」那條路（實測 22 支紅）。
    預設模擬**正常情況：我們手上那份還活著**；要測換地圖伺服器那條路的
    測試自己把它改掉（見 `tests/test_socket_gone_stale.py`）。
    """
    from ro_toolbox.services import game_socket

    monkeypatch.setattr(game_socket, "socket_alive", lambda sock: sock is not None)
