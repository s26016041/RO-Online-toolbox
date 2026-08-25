"""記住使用者上次在 UI 裡輸入／選的東西（篩選字、搜尋值…）。

刻意跟 settings.json 分開存成一個小檔：這些是「方便」用途，
壞了或被刪掉頂多回到空白，不該影響正式設定。
存取用 key（慣例 "頁面.欄位"），寫入即存檔，容錯不拋例外。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ro_toolbox.config.paths import user_data_dir

log = logging.getLogger(__name__)

_cache: dict[str, Any] | None = None


def _file():
    return user_data_dir() / "ui_state.json"


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    path = _file()
    if path.exists():
        try:
            _cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    else:
        _cache = {}
    return _cache


def get(key: str, default: Any = "") -> Any:
    return _load().get(key, default)


def set(key: str, value: Any) -> None:  # noqa: A001 - 就是要叫 set，語意最清楚
    cache = _load()
    if cache.get(key) == value:
        return
    cache[key] = value
    try:
        _file().write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log.debug("UI 狀態寫入失敗：%s", exc)
