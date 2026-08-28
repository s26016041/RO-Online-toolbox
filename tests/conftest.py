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
