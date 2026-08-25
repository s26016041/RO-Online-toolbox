"""版號有三處，必須一致。

為什麼要測：三處不同步**完全沒有徵兆** —— 視窗標題顯示一個版號、
封裝進去的是另一個、Release 標籤又是第三個，而程式照跑。
發版當下才發現的話，已經有人下載到對不上的東西了。

`VERSION` 是給發版流程讀的（人和腳本），程式本身讀 `__init__.py`。
兩份存在的理由不同，但值一定要一樣。
"""

from __future__ import annotations

import re
from pathlib import Path

import ro_toolbox

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _pyproject_version() -> str:
    """讀 `[project]` 那個 version，不是相依套件的版號。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    msg = "pyproject.toml 裡找不到頂層的 version"
    raise AssertionError(msg)


def test_all_three_sources_agree():
    versions = {
        "pyproject.toml": _pyproject_version(),
        "ro_toolbox.__version__": ro_toolbox.__version__,
        "VERSION": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
    }
    assert len(set(versions.values())) == 1, f"版號不同步：{versions}"


def test_it_looks_like_a_version():
    assert SEMVER.match(ro_toolbox.__version__), (
        f"版號要是 X.Y.Z，拿到 {ro_toolbox.__version__!r}"
    )


def test_the_version_file_has_no_trailing_newline():
    """發版腳本會直接把它拼進標籤名 —— 多一個換行就變成 `v0.1.0\\n`。"""
    raw = (ROOT / "VERSION").read_bytes()
    assert not raw.endswith((b"\n", b"\r")), "VERSION 結尾不要有換行"
