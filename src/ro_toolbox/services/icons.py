"""道具圖示（24×24）。

**優先讀打包資產 `assets/icons.bin`**，讀不到才退回解包目錄
`RODATA/.../texture/유저인터페이스/item/<資源名>.bmp`。
使用者的電腦沒有 `RODATA/`，只靠解包目錄的話圖示會全部空白
（CLAUDE.md：資料檔也一樣，不准依賴只有開發機有的東西）。
資產怎麼產生見 `tools/build_icons.py`。

兩個坑：
- **資源名是韓文**（`iteminfo` 的 `identifiedResourceName`，euc-kr），
  跟顯示名是兩回事（見 [DAT-001]）。
- 解包工具把 euc-kr 位元組當 latin-1 存成檔名，所以磁碟上的檔名是亂碼
  （`빨간포션` → `»§°£Æ÷¼Ç`）。要用同樣的方式轉回去才找得到檔案。
  **資產裡存的是還原後的韓文資源名**，讀取端不必再碰那層亂碼。

找不到就回 None —— 介面顯示沒有圖示的項目，不會拿別的圖來頂。
"""

from __future__ import annotations

import gzip
import json
import logging
import struct
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

#: 打包資產。格式與產生方式見 `tools/build_icons.py`。
ICON_ASSET = ASSETS_DIR / "icons.bin"
#: 技能圖示。同一個格式、同一個 `item/` 目錄，只是**索引的鍵是技能英文代號**
#: （`SM_BASH`，磁碟上的檔名是小寫）。分成兩份資產是因為 `icons.bin` 只收
#: 道具表用得到的資源名，技能圖不在裡面。產生方式見 `tools/build_skill_icons.py`。
SKILL_ICON_ASSET = ASSETS_DIR / "skill_icons.bin"
ICON_MAGIC = b"ROIC"
ICON_VERSION = 1
#: 每桶幾張圖。128 張時整份約 4.0 MB（接近 solid gzip 的 3.8 MB），
#: 而取一張圖只要解壓那一桶（約 0.2 MB）—— 不必把 17.5 MB 全攤在記憶體裡。
ICON_BUCKET = 128


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
    """這個道具的圖示檔（**只在有解包目錄的機器上**才有值）。找不到回 None。

    一般顯示請用 `icon_bytes()` —— 使用者的電腦沒有 `RODATA/`，這裡永遠回 None。
    """
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


# ---- 打包資產 -------------------------------------------------------


@lru_cache(maxsize=4)
def _asset_at(path: Path) -> tuple[dict, bytes] | None:
    """(索引, 桶區塊)。沒有資產或格式不符就回 None，讓呼叫端退回解包目錄。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) < 12 or raw[:4] != ICON_MAGIC:
        log.warning("%s 不是圖示資產（開頭是 %r）", path.name, raw[:4])
        return None
    version, head_len = struct.unpack_from("<II", raw, 4)
    if version != ICON_VERSION or len(raw) < 12 + head_len:
        log.warning("%s 版本或長度不符（version=%s）", path.name, version)
        return None
    try:
        index = json.loads(gzip.decompress(raw[12 : 12 + head_len]).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        log.warning("%s 的索引解不開：%s", path.name, exc)
        return None
    return index, raw[12 + head_len :]


@lru_cache(maxsize=16)
def _bucket_at(path: Path, number: int) -> bytes:
    """解壓第 `number` 桶。只留幾桶在快取裡，記憶體不會一直長。"""
    loaded = _asset_at(path)
    if loaded is None:
        return b""
    index, blob = loaded
    try:
        offset, size = index["b"][number]
        return gzip.decompress(blob[offset : offset + size])
    except (IndexError, KeyError, OSError, ValueError) as exc:
        log.warning("%s 第 %d 桶解不開：%s", path.name, number, exc)
        return b""


def _packed(path: Path, key: str) -> bytes | None:
    """從資產取一張圖。找不到回 None。"""
    loaded = _asset_at(path)
    if loaded is None:
        return None
    entry = loaded[0]["i"].get(key)
    if entry is None:
        return None
    number, offset, size = entry
    data = _bucket_at(path, number)[offset : offset + size]
    return data if len(data) == size else None


def _asset() -> tuple[dict, bytes] | None:
    return _asset_at(ICON_ASSET)


def _bucket(number: int) -> bytes:
    return _bucket_at(ICON_ASSET, number)


def icon_bytes(item_id: int) -> bytes | None:
    """這個道具的圖示位元組（BMP）。找不到回 None。

    先查打包資產（使用者的電腦上唯一的來源），再退回解包目錄。
    """
    resource = _resources().get(item_id)
    if not resource:
        return None
    data = _packed(ICON_ASSET, resource)
    if data is not None:
        return data
    path = icon_path(item_id)
    if path is not None:
        try:
            return path.read_bytes()
        except OSError:
            return None
    return None


def skill_icon_path(key: str) -> Path | None:
    """解包目錄裡這個技能的圖示。磁碟上的檔名是**小寫**的英文代號。"""
    root = _ui_root()
    if root is None or not key:
        return None
    path = root / "item" / (key.lower() + ".bmp")
    return path if path.is_file() else None


def skill_icon_bytes(key: str) -> bytes | None:
    """技能圖示（BMP）。`key` 是英文代號（`SM_BASH`）。找不到回 None。

    1,605 個技能裡有 1,317 個有圖，其餘（多半是不會出現在技能欄的內部技能）
    介面就不顯示圖示 —— **外觀降級不是錯誤**。
    """
    if not key:
        return None
    data = _packed(SKILL_ICON_ASSET, key)
    if data is not None:
        return data
    path = skill_icon_path(key)
    if path is not None:
        try:
            return path.read_bytes()
        except OSError:
            return None
    return None


def available() -> bool:
    """有沒有圖示可用（打包資產或解包目錄，兩者有一就行）。

    都沒有的話介面就不顯示圖示 —— 那是**外觀降級不是錯誤**。
    """
    return _asset() is not None or _ui_root() is not None
