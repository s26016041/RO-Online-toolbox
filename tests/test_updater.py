"""自動更新。

這支程式會**覆寫使用者的執行檔**，壞掉的方式比別的功能惡劣得多：
抓到半截檔案就換上去 → 使用者連舊版都開不起來。所以這裡的測試重點不是
「更新成功」，而是**每一種失敗都要安全**。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from ro_toolbox.services import updater


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _release(tag="v9.9.9", name=updater.ASSET_NAME, size=80_000_000):
    return {
        "tag_name": tag,
        "body": "重點變更",
        "assets": [{"name": name, "size": size,
                    "browser_download_url": f"https://example/{name}"}],
    }


def _serve(monkeypatch, payload: bytes):
    monkeypatch.setattr(updater, "_urlopen",
                        lambda *_a, **_k: FakeResponse(payload))


# ---- 版號比較 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("remote", "local", "expected"),
    [
        ("v0.1.3", "0.1.2", True),
        ("0.1.2", "0.1.2", False),
        ("v0.1.1", "0.1.2", False),
        ("v0.2", "0.1.9", True),
        ("v0.2.1", "0.2", True),      # 長度不同要補 0，不能只比前兩段
        ("v1.0.0", "0.9.9", True),
        ("v0.10.0", "0.9.0", True),   # 字串比較會說 "0.10" < "0.9"，數字比較才對
    ],
)
def test_version_comparison(remote, local, expected):
    assert updater.is_newer(remote, local) is expected


def test_garbage_version_does_not_crash():
    """解不出來的段落當 0 —— 遠端 tag 亂寫也不該讓程式炸掉。"""
    assert updater.parse_version("v1.beta.3") == (1, 0, 3)
    assert updater.parse_version("") == (0,)
    assert updater.is_newer("亂寫", "0.1.2") is False


# ---- 查詢 --------------------------------------------------------------


def test_it_finds_the_exe_asset(monkeypatch):
    import json

    _serve(monkeypatch, json.dumps(_release()).encode())
    info = updater.latest_release()
    assert info["version"] == "v9.9.9"
    assert info["url"].endswith(".exe")
    assert updater.last_error() == ""


def test_it_falls_back_to_any_exe(monkeypatch):
    """改過檔名的 Release 也要認得出來，否則舊版會永遠找不到更新。"""
    import json

    _serve(monkeypatch, json.dumps(_release(name="改了名字.exe")).encode())
    assert updater.latest_release()["url"].endswith("改了名字.exe")


def test_a_release_without_an_exe_is_reported(monkeypatch):
    """沒有 .exe 要留下原因 —— 靜靜回 None 的話沒人查得出為什麼不更新。"""
    import json

    _serve(monkeypatch, json.dumps(_release(name="說明.txt")).encode())
    assert updater.latest_release() is None
    assert "沒有附任何 .exe" in updater.last_error()


def test_a_network_failure_is_reported(monkeypatch):
    def boom(*_a, **_k):
        msg = "getaddrinfo failed"
        raise OSError(msg)

    monkeypatch.setattr(updater, "_urlopen", boom)
    assert updater.latest_release() is None
    assert "getaddrinfo failed" in updater.last_error()


def test_it_never_updates_in_development(monkeypatch):
    """直接跑 .py 時不該自我更新（也沒有 exe 可以換）。"""
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    assert updater.check() is None


# ---- 下載驗證（最要緊的部分）-------------------------------------------


def _info(size: int) -> dict:
    return {"url": "https://example/x.exe", "size": size}


def test_a_good_download_is_accepted(monkeypatch, tmp_path):
    payload = b"MZ" + b"\x00" * 2_000_000
    _serve(monkeypatch, payload)
    # 假 payload 不是真的簽過的 PE —— 這條測的是「下載完整性」，
    # 簽章那道閘門另外有測（見檔尾）。
    monkeypatch.setattr(updater, "has_signature", lambda _p: True)
    dest = tmp_path / "new.exe"
    assert updater.download(_info(len(payload)), dest) is True
    assert dest.exists()


def test_a_truncated_download_is_thrown_away(monkeypatch, tmp_path):
    """大小對不上就丟掉 —— 半截的 exe 換上去等於把使用者的程式弄壞。"""
    payload = b"MZ" + b"\x00" * 2_000_000
    _serve(monkeypatch, payload)
    dest = tmp_path / "new.exe"
    assert updater.download(_info(len(payload) + 1), dest) is False
    assert not dest.exists()


def test_an_html_error_page_is_thrown_away(monkeypatch, tmp_path):
    """抓到錯誤頁面（不是 PE 檔）也要丟掉。"""
    payload = b"<html>404</html>" + b"\x00" * 2_000_000
    _serve(monkeypatch, payload)
    dest = tmp_path / "new.exe"
    assert updater.download(_info(len(payload)), dest) is False
    assert not dest.exists()


def test_a_suspiciously_small_file_is_thrown_away(monkeypatch, tmp_path):
    payload = b"MZ" + b"\x00" * 10
    _serve(monkeypatch, payload)
    dest = tmp_path / "new.exe"
    assert updater.download(_info(len(payload)), dest) is False


def test_a_failed_download_leaves_nothing_behind(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        msg = "connection reset"
        raise OSError(msg)

    monkeypatch.setattr(updater, "_urlopen", boom)
    dest = tmp_path / "new.exe"
    assert updater.download(_info(100), dest) is False
    assert not dest.exists()


# ---- 換檔 --------------------------------------------------------------


def test_swapping_keeps_the_old_one_as_backup(monkeypatch, tmp_path):
    current = tmp_path / "app.exe"
    current.write_bytes(b"MZ old")
    new = tmp_path / "app.exe.new"
    new.write_bytes(b"MZ new")

    launched = []
    monkeypatch.setattr(updater, "exe_path", lambda: current)
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **_k: launched.append(cmd))

    assert updater.apply_and_restart(new) is True
    assert current.read_bytes() == b"MZ new"
    assert (tmp_path / "app.exe.old").read_bytes() == b"MZ old"
    assert launched == [[str(current)]]


def test_a_failed_swap_restores_the_old_one(monkeypatch, tmp_path):
    """第二步失敗就把舊的搬回來 —— 絕不能讓使用者兩個檔案都拿不到。"""
    current = tmp_path / "app.exe"
    current.write_bytes(b"MZ old")

    real_replace = updater.os.replace
    calls = []

    def flaky(src, dst):
        calls.append((src, dst))
        if len(calls) == 2:          # 第二次（.new → 正式檔名）失敗
            msg = "denied"
            raise OSError(msg)
        return real_replace(src, dst)

    monkeypatch.setattr(updater, "exe_path", lambda: current)
    monkeypatch.setattr(updater.os, "replace", flaky)

    assert updater.apply_and_restart(tmp_path / "app.exe.new") is False
    assert current.read_bytes() == b"MZ old", "舊版沒有被還原回來"


def test_the_child_does_not_inherit_the_extraction_dir(monkeypatch):
    """新版不能沿用舊版的 _MEI 解壓目錄，否則舊版收尾時會跳警告視窗。"""
    monkeypatch.setenv("_MEIPASS2", r"C:\Temp\_MEI123")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\Temp\_MEI123")
    monkeypatch.setenv("PATH", "keep-me")
    env = updater._child_env()
    assert "_MEIPASS2" not in env
    assert "_PYI_APPLICATION_HOME_DIR" not in env
    assert env["PATH"] == "keep-me"


# ---- 開場清理 ----------------------------------------------------------


def test_cleanup_does_nothing_in_development(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    leftover = tmp_path / "app.exe.old"
    leftover.write_bytes(b"x")
    updater.clean_leftovers()
    assert leftover.exists(), "開發模式不該去動任何檔案"


def test_cleanup_removes_the_old_exe(monkeypatch, tmp_path):
    current = tmp_path / "app.exe"
    old = tmp_path / "app.exe.old"
    old.write_bytes(b"MZ old")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "exe_path", lambda: current)
    monkeypatch.setattr(updater, "_clean_stale_mei", lambda: None)
    updater.clean_leftovers()
    assert not old.exists()


def test_cleanup_never_removes_its_own_extraction_dir(monkeypatch, tmp_path):
    mine = tmp_path / "_MEI_mine"
    other = tmp_path / "_MEI_other"
    mine.mkdir()
    other.mkdir()
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(sys, "_MEIPASS", str(mine), raising=False)
    monkeypatch.delenv("_PYI_APPLICATION_HOME_DIR", raising=False)
    updater._clean_stale_mei()
    assert mine.exists(), "把自己的解壓目錄刪了會當場自殺"
    assert not other.exists()


def test_cleanup_survives_a_locked_directory(monkeypatch, tmp_path):
    """刪不掉（還被別的行程開著）就跳過，不能讓啟動失敗。"""
    (tmp_path / "_MEI_busy").mkdir()
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.delenv("_PYI_APPLICATION_HOME_DIR", raising=False)

    def denied(*_a, **_k):
        msg = "in use"
        raise OSError(msg)

    monkeypatch.setattr(updater.shutil, "rmtree", denied)
    updater._clean_stale_mei()          # 不該拋例外


# ---- 介面層 ------------------------------------------------------------


def test_headless_runs_never_check(monkeypatch, qtbot):
    """`--selftest` 是 offscreen：查更新的執行緒會來不及收尾，害冒煙測試誤判。"""
    from PySide6.QtWidgets import QMainWindow

    from ro_toolbox.ui.update_ui import UpdateManager

    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    window = QMainWindow()
    qtbot.addWidget(window)
    manager = UpdateManager(window)
    manager.start()
    assert manager._check is None, "無頭模式不該起檢查執行緒"


def test_development_runs_never_check(monkeypatch, qtbot):
    from PySide6.QtWidgets import QMainWindow

    from ro_toolbox.ui.update_ui import UpdateManager

    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    window = QMainWindow()
    qtbot.addWidget(window)
    manager = UpdateManager(window)
    manager.start()
    assert manager._check is None


def test_a_check_failure_is_shown_to_the_user(monkeypatch, qtbot):
    """更新不了要看得見 —— 靜靜略過的話沒人知道自己停在舊版。"""
    from PySide6.QtWidgets import QMainWindow

    from ro_toolbox.ui.update_ui import UpdateManager

    monkeypatch.setattr(updater, "last_error", lambda: "SSLError: 憑證過期")
    window = QMainWindow()
    qtbot.addWidget(window)
    manager = UpdateManager(window)
    manager._on_checked(None)
    assert "檢查更新失敗" in window.statusBar().currentMessage()


def test_being_up_to_date_is_silent(monkeypatch, qtbot):
    from PySide6.QtWidgets import QMainWindow

    from ro_toolbox.ui.update_ui import UpdateManager

    monkeypatch.setattr(updater, "last_error", lambda: "")
    window = QMainWindow()
    qtbot.addWidget(window)
    window.statusBar().showMessage("就緒")
    manager = UpdateManager(window)
    manager._on_checked(None)
    assert window.statusBar().currentMessage() == "就緒"


def test_the_repo_matches_the_release_asset():
    """更新指到的 repo 與檔名要跟發布流程一致，否則永遠抓不到。"""
    spec = (Path(__file__).resolve().parents[1] / "RO-Online-toolbox.spec").read_text(
        encoding="utf-8"
    )
    assert f'APP_NAME = "{updater.ASSET_NAME[:-4]}"' in spec
    assert updater.REPO == "s26016041/RO-Online-toolbox"


def test_the_explicit_flag_stops_the_check(monkeypatch, qtbot):
    """打包後的 exe 跑 --selftest 時平台是正常的，所以要有明確旗標。

    只靠 QT_QPA_PLATFORM 判斷會漏掉那條路 —— 實際踩過：
    冒煙測試以 0xC0000409（Qt 的「執行緒還在跑就被解構」）中止。
    """
    from PySide6.QtWidgets import QMainWindow

    from ro_toolbox.ui.update_ui import UpdateManager

    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.setenv(updater.NO_UPDATE_ENV, "1")
    window = QMainWindow()
    qtbot.addWidget(window)
    manager = UpdateManager(window)
    manager.start()
    assert manager._check is None


def test_selftest_sets_the_flag_before_building_the_window():
    """旗標要在**建視窗之前**設，設晚了那一輪還是會起執行緒。"""
    source = (Path(__file__).resolve().parents[1]
              / "src" / "ro_toolbox" / "app.py").read_text(encoding="utf-8")
    body = source[source.index("def selftest"):]
    flag_at = body.index("NO_UPDATE_ENV] = ")
    build_at = body.index("create_app([")
    assert flag_at < build_at, "設旗標必須排在 create_app 之前"
