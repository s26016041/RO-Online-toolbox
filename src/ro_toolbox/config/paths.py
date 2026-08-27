"""集中管理所有路徑，避免程式各處自己拼 Path。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# src/ro_toolbox/
PACKAGE_DIR = Path(__file__).resolve().parents[1]


def _bundle_root() -> Path | None:
    """PyInstaller 解壓資料檔的位置；不是打包執行就回 None。

    打包成 exe 之後，程式碼跑在暫存解壓目錄裡，
    `PACKAGE_DIR.parents[1] / "assets"` 會指到一個**不存在的地方** ——
    而且 `gamedata` 讀不到表只會安靜地查不到道具名，選單變空白，
    沒有任何錯誤訊息。所以打包路徑一定要另外算。
    """
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if getattr(sys, "frozen", False) and base else None


def _resources_dir() -> Path:
    root = _bundle_root()
    return root / "ro_toolbox" / "ui" / "resources" if root else (
        PACKAGE_DIR / "ui" / "resources"
    )


def _assets_dir() -> Path:
    """道具表／怪物表／傳點表。打包時放在解壓根目錄的 `assets/`。"""
    root = _bundle_root()
    return root / "assets" if root else PACKAGE_DIR.parents[1] / "assets"


RESOURCES_DIR = _resources_dir()
ASSETS_DIR = _assets_dir()
STYLES_DIR = RESOURCES_DIR / "styles"

_APP_FOLDER = "RO-Online-toolbox"


def user_data_dir() -> Path:
    """使用者資料目錄（Windows 走 %APPDATA%，其他平台走 ~/.config）。"""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    path = root / _APP_FOLDER
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    return user_data_dir() / "settings.json"


def log_dir() -> Path:
    path = user_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def stylesheet_file(theme: str = "light") -> Path:
    """主題樣式檔。未知主題一律退回 light。"""
    name = theme if theme in {"light", "dark"} else "light"
    return STYLES_DIR / f"{name}.qss"


def icon_file() -> Path:
    """應用程式圖示（多尺寸 .ico）。用 `tools/make_icon.py` 產生。"""
    return RESOURCES_DIR / "icon.ico"


def capture_dir() -> Path:
    """封包擷取的匯出目錄。"""
    path = user_data_dir() / "captures"
    path.mkdir(parents=True, exist_ok=True)
    return path


#: 設了它 = 現在是冒煙測試（`--selftest`）。
#:
#: ⚠ 自檢要驗的是「資料檔與分頁有沒有收進來」，**不是「能不能操作遊戲」**。
#: 所以自檢時**一律不去附加遊戲行程**。原因是硬的：讀遊戲記憶體的工作會卡在
#: GameGuard 擋住的系統呼叫上（列舉模組實測卡 3 秒以上），那條執行緒叫不停 ——
#: 行程收尾時 DLL 開始卸載，它醒來就踩到已釋放的程式碼，
#: 整個行程被以 0xC0000409 中止（[ENV-005]，無主控台的版本才會踩到）。
#:
#: 留著執行緒的引用救不了這個 —— 問題是它**存在**，不是它被解構。
SELFTEST_ENV = "RO_TOOLBOX_SELFTEST"


def in_selftest() -> bool:
    import os

    return os.environ.get(SELFTEST_ENV) == "1"

