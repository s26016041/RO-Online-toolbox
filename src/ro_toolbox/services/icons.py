"""道具圖示：從解包資料找出每個道具的小圖檔。

路徑：`RODATA/.../texture/유저인터페이스/item/<資源名>.bmp`（24×24 BMP）。

兩個坑：
- **資源名是韓文**（`iteminfo` 的 `identifiedResourceName`，euc-kr），
  跟顯示名是兩回事（見 [DAT-001]）。
- 解包工具把 euc-kr 位元組當 latin-1 存成檔名，所以磁碟上的檔名是亂碼
  （`빨간포션` → `»§°£Æ÷¼Ç`）。要用同樣的方式轉回去才找得到檔案。

找不到就回 None —— 介面顯示沒有圖示的項目，不會拿別的圖來頂。
"""

from __future__ import annotations

import gzip
import json
import logging
from functools import lru_cache
from pathlib import Path

from ro_toolbox.config.paths import ASSETS_DIR

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3] / "RODATA"
#: 解包版面可能多一層 `data/`，兩種都試（跟 mapdata 同理）。
_TEXTURE_DIRS = (
    _ROOT / "data" / "data" / "texture",
    _ROOT / "data0" / "data" / "texture",
    _ROOT / "data" / "texture",
)
_UI_DIR = "유저인터페이스"
_SUBDIRS = ("item", "collection")


def _mangled(text: str) -> str:
    """把韓文轉成解包工具寫到磁碟上的那個亂碼檔名。"""
    return text.encode("euc-kr", errors="replace").decode("latin-1")


@lru_cache(maxsize=1)
def _resources() -> dict[int, str]:
    """道具 ID → 韓文資源名。"""
    try:
        with gzip.open(ASSETS_DIR / "items.json.gz", "rt", encoding="utf-8") as handle:
            table = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("載入道具表失敗：%s", exc)
        return {}
    table.pop("_meta", None)
    return {int(k): v["res"] for k, v in table.items() if v.get("res")}


@lru_cache(maxsize=1)
def _ui_root() -> Path | None:
    for base in _TEXTURE_DIRS:
        path = base / _mangled(_UI_DIR)
        if path.is_dir():
            return path
    return None


@lru_cache(maxsize=4096)
def icon_path(item_id: int) -> Path | None:
    """這個道具的圖示檔。找不到回 None。"""
    resource = _resources().get(item_id)
    root = _ui_root()
    if not resource or root is None:
        return None
    name = _mangled(resource) + ".bmp"
    for sub in _SUBDIRS:
        candidate = root / sub / name
        if candidate.is_file():
            return candidate
    return None


def available() -> bool:
    """解包資料在不在（不在的話介面就不顯示圖示，不是錯誤）。"""
    return _ui_root() is not None
