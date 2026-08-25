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


def load_settings() -> AppSettings:
    path = config_file()
    if not path.exists():
        return AppSettings()
    try:
        return AppSettings.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        log.warning("設定檔讀取失敗，改用預設值：%s", exc)
        return AppSettings()


def save_settings(settings: AppSettings) -> None:
    path = config_file()
    try:
        path.write_text(
            json.dumps(settings.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.error("設定檔寫入失敗：%s", exc)
