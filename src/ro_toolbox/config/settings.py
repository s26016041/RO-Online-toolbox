"""應用設定：dataclass 定義 + JSON 讀寫。

刻意不依賴 QSettings，讓設定檔可以直接手改、可以進版控。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from ro_toolbox.config.paths import config_file

log = logging.getLogger(__name__)


@dataclass
class WindowSettings:
    width: int = 1180
    height: int = 720
    maximized: bool = False


@dataclass
class AppSettings:
    """全域設定。新增欄位時給預設值，舊設定檔才不會讀失敗。"""

    theme: str = "light"
    log_level: str = "WARNING"
    # 遊戲啟動器的完整路徑，例如 D:\ro\RagnarokOnline\Ragnarok.exe。
    # 遊戲本體與工作目錄都從它推（見 services/game_launcher.GamePaths）。
    # 這不是機密，可以留在這個檔；帳密與 OTP 種子在加密過的 accounts.dat。
    game_path: str = ""
    #: 合約書「同意」按鈕在**視窗內的比例位置**（x, y），例如 [0.5628, 0.621]。
    #:
    #: 為什麼存比例不存座標：視窗會移動、大小會變、DPI 縮放也會變，
    #: 存絕對座標的那一刻它就已經是壞的。比例只跟「版面」有關。
    #:
    #: 為什麼要存：那個畫面**只吃滑鼠**（鍵盤全試過都沒反應，[INP-001]），
    #: 而按鈕位置會隨客戶端的解析度設定跑掉。內建的預設值是在 1280x800
    #: 的客戶端上量的；別人的解析度不同時，自動登入會請他手動按一次同意，
    #: **然後把他按的位置學起來**（見 auto_login._learn_agree_button）。
    #: 空的代表還沒學過，用內建預設值。
    agree_button: list[float] | None = None
    #: 自動回連：斷線就關遊戲、重開、重新登入，再把斷線前在跑的東西接回去。
    #:
    #: 預設**關閉** —— 它會關掉並重開你的遊戲，那種事不該預設發生。
    #: ⚠ 只在「你的網路正常但遊戲沒有連線」時才動作；你自己的網路斷了
    #: 一律什麼都不做（見 services/reconnect.py）。
    #: 登入畫面的焦點預設在哪一格：True＝密碼欄、False＝帳號欄、None＝還不知道。
    #:
    #: 使用者實測的規則：**客戶端記住帳號時焦點在密碼欄**，沒記住時在帳號欄。
    #: 這是那台客戶端的性質（存檔勾了沒），登入幾次都一樣 ——
    #: 所以**問一次就好**：第一次登入用「清乾淨→打進去→問記憶體」查出來
    #: （[INP-026]，要 5~6 秒），查到就記在這裡，之後直接走快路（1~2 秒）。
    #: 猜錯不會出事：送出後的 `0x0064` 明文帳號會抓到，翻面重打並改掉這一格。
    login_focus_password: bool | None = None
    auto_reconnect: bool = False
    window: WindowSettings = field(default_factory=WindowSettings)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppSettings:
        known = {f.name for f in fields(cls)}
        payload = {k: v for k, v in data.items() if k in known}
        window = payload.pop("window", None)
        settings = cls(**payload)
        if isinstance(window, dict):
            settings.window = WindowSettings(
                **{k: v for k, v in window.items() if k in {f.name for f in fields(WindowSettings)}}
            )
        return settings


#: 全程式共用的那一份。**不准各拿各的** —— 見 `current_settings`。
_current: AppSettings | None = None


def current_settings() -> AppSettings:
    """拿全程式共用的那一份設定。

    ⚠ **不要自己再 `load_settings()` 一份。** 兩份在記憶體裡各改各的，
    誰最後存檔誰贏，而且輸的那邊是**安靜地**消失。

    實際踩過：帳號頁自己 load 了一份、把遊戲路徑存進檔案；關視窗時主視窗
    拿它那份（`game_path` 還是空的）整檔覆蓋回去 —— 使用者選好的路徑，
    下次開程式就不見了，沒有任何錯誤訊息。
    """
    return _current if _current is not None else load_settings()


def load_settings() -> AppSettings:
    global _current
    path = config_file()
    if not path.exists():
        _current = AppSettings()
        return _current
    try:
        _current = AppSettings.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        log.warning("設定檔讀取失敗，改用預設值：%s", exc)
        _current = AppSettings()
    return _current


def save_settings(settings: AppSettings) -> None:
    global _current
    # 存下去的這一份就是往後大家共用的那一份。
    _current = settings
    path = config_file()
    try:
        path.write_text(
            json.dumps(settings.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.error("設定檔寫入失敗：%s", exc)
