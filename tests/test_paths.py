"""打包成 exe 之後，資料檔的路徑要算對。

為什麼值得測：算錯**完全沒有錯誤訊息**。`assets/*.json.gz` 找不到時
`gamedata` 只會回空表，道具名一律查不到、補水選單整個空白，
程式照跑、log 也不會抱怨 —— 使用者只覺得「這程式壞掉了」。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from ro_toolbox.config import paths


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """假裝自己是 PyInstaller 解壓出來的 exe。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    return tmp_path


def test_normal_run_uses_the_repo_layout():
    """沒打包時就是原本的專案版面。"""
    assert paths.ASSETS_DIR == paths.PACKAGE_DIR.parents[1] / "assets"
    assert paths.RESOURCES_DIR == paths.PACKAGE_DIR / "ui" / "resources"


def test_frozen_run_looks_inside_the_bundle(frozen):
    """打包時要指到解壓目錄，不是 `parents[1]`（那會指到不存在的地方）。"""
    assert paths._bundle_root() == frozen
    assert paths._assets_dir() == frozen / "assets"
    assert paths._resources_dir() == frozen / "ro_toolbox" / "ui" / "resources"


def test_frozen_needs_both_flags(monkeypatch, tmp_path):
    """只有 `_MEIPASS` 沒有 `frozen`（或反過來）都不算打包執行。"""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths._bundle_root() is None


def test_the_bundle_layout_matches_the_spec():
    """spec 裡的 datas 目的地要跟 paths.py 算的一致 —— 兩邊對不上就是空選單。"""
    spec = (Path(__file__).resolve().parents[1] / "RO-Online-toolbox.spec").read_text(
        encoding="utf-8"
    )
    assert '("assets", "assets")' in spec, "spec 沒把 assets 收到 `assets/`"
    assert '"ro_toolbox/ui/resources"' in spec, "spec 沒把 resources 收到對的位置"


def test_the_tables_are_actually_readable():
    """三張表在版控裡，任何 clone 都要讀得到（不需要 RODATA）。"""
    from ro_toolbox.services.gamedata import item_name, mob_name

    importlib.reload(paths)
    assert item_name(501) == "紅色藥水"
    assert mob_name(1002) != "1002", "波利查不到，怪物表沒載到"
