import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# ⚠ 已知雜訊：跑 tests/test_potion.py 時 faulthandler 偶爾會印
# 「Windows fatal exception: access violation」（間歇，同樣指令有時 0 次有時 2 次）。
# 那是**第一次例外**，行程繼續正常跑完、測試全過，實機也沒問題。
# 查過的方向都不是穩定成因：pytest 外掛（logging/capture/cacheprovider）全關掉照樣出現、
# 換 logging handler 也無法穩定消除、純 Event.wait 的最小重現不會發生。
# 目前當成環境層級的假警報，不為了消訊息去改動正常的執行緒收尾邏輯。
