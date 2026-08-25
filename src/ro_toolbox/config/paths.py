"""集中管理所有路徑，避免程式各處自己拼 Path。"""

from __future__ import annotations

import os
from pathlib import Path

# src/ro_toolbox/
PACKAGE_DIR = Path(__file__).resolve().parents[1]
RESOURCES_DIR = PACKAGE_DIR / "ui" / "resources"
# 專案根目錄下的 assets/（道具表、怪物表）
ASSETS_DIR = PACKAGE_DIR.parents[1] / "assets"
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
