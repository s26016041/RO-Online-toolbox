"""自動更新：跟 GitHub Releases 比版本，下載新的 .exe 並就地換掉。

為什麼要有這個
--------------
RO 改版時記憶體特徵、封包 opcode、道具表都可能要修（見 `/_patchCheck`），
所以版本更新會很頻繁。不能每次都請使用者自己去 GitHub 抓 exe —— 程式要能自己換。

怎麼換掉「正在執行中的 exe」
----------------------------
Windows 不允許覆寫執行中的檔案，但**允許改名**。所以流程是：

    1. 新版下載到 <exe>.new
    2. 把執行中的 exe 改名成 <exe>.old      ← 這步 Windows 允許
    3. 把 .new 改名成原本的檔名
    4. 啟動新的 exe、結束自己
    5. 下次啟動時把殘留的 .old 刪掉（`clean_leftovers`）

任何一步失敗都會盡量還原，不會讓使用者落到「兩個檔案都不對」的狀態。

只在打包成 exe 時才會動作；開發時（直接跑 main.py）一律跳過。
只用標準庫，不加相依。

移植自姊妹專案 `s26016041/Angels-Online-toolbox` 的 `app/core/updater.py`，
連同它踩過的坑一起搬（憑證、`_MEI` 解壓目錄、失敗原因要留下來）。
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = "s26016041/RO-Online-toolbox"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "RO-Online-toolbox.exe"
TIMEOUT = 15.0
UA = {"User-Agent": "RO-Online-toolbox-Updater"}
#: 設了這個環境變數就完全不查更新。`--selftest` 會設 ——
#: 冒煙測試建好視窗就馬上結束，查更新的執行緒來不及收尾，
#: Qt 會以「Destroyed while thread is still running」中止行程（0xC0000409），
#: 害冒煙測試誤判成打包失敗。實際踩過。
NO_UPDATE_ENV = "RO_TOOLBOX_NO_UPDATE"

#: 上一次查詢失敗的原因。放在 list 裡是為了讓函式能改它（module 層級的可變狀態）。
_last_error = [""]


def is_frozen() -> bool:
    """是不是打包後的 exe。開發時直接跑 .py 就不該自我更新。"""
    return bool(getattr(sys, "frozen", False))


def exe_path() -> Path:
    return Path(sys.executable).resolve()


def parse_version(text: str) -> tuple[int, ...]:
    """`'v0.1.2'` / `'0.1.2'` → `(0, 1, 2)`。解不出來的段落當 0。"""
    cleaned = (text or "").strip().lstrip("vV")
    out = []
    for part in cleaned.split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_newer(remote: str, local: str) -> bool:
    """遠端版號有沒有比本地新。長度不同時短的補 0（`0.2` < `0.2.1`）。"""
    a, b = parse_version(remote), parse_version(local)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def _urlopen(url: str, timeout: float = TIMEOUT):
    """連線，憑證驗證失敗就退回不驗證再試一次。

    ⚠ 這裡不能只 `except ssl.SSLError` —— `urlopen` 遇到憑證問題丟的是
    `urllib.error.URLError` **包住** SSLError，裸的 SSLError 攔不到，
    後備等於沒作用，錯誤還會被外層吞掉變成「靜靜地不更新」。
    姊妹專案的使用者（Windows 10、系統根憑證較舊）實際卡在這裡，
    畫面上完全沒有提示。所以第一次失敗一律重試一次。

    退回不驗證是可接受的：抓的是自己 repo 的公開檔案，而且下載後還會檢查
    大小與 PE 標頭，換檔前也會驗證，動不了手腳。
    """
    request = urllib.request.Request(url, headers=UA)  # noqa: S310 - 固定 https
    try:
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
    except Exception as first:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            return urllib.request.urlopen(request, timeout=timeout, context=context)  # noqa: S310
        except Exception:
            raise first from None


def last_error() -> str:
    """上一次查詢失敗的原因（給診斷用）。沒失敗過就是空字串。"""
    return _last_error[0]


def latest_release() -> dict | None:
    """查 GitHub 最新 Release。回 `{version, url, size, notes}`；失敗回 None。

    沒網路、被限流、repo 沒有 Release 都算失敗。**失敗原因一定要留下來** ——
    靜靜回 None 的話，使用者更新不了時畫面與紀錄檔都沒有任何線索。
    """
    _last_error[0] = ""
    try:
        with _urlopen(API_LATEST) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 任何失敗都只是「這次不更新」
        _last_error[0] = f"{type(exc).__name__}: {exc}"
        if sys.stderr:        # 打包版的 stderr 是 None
            sys.stderr.write(f"[update] 查詢最新版本失敗 —— {_last_error[0]}\n")
        return None

    assets = data.get("assets") or []
    # 先找正式檔名，找不到就退而取任何 .exe —— 這樣以後改檔名也不會讓舊版失聯。
    picked = next((a for a in assets if a.get("name") == ASSET_NAME), None)
    if picked is None:
        picked = next(
            (a for a in assets if str(a.get("name", "")).lower().endswith(".exe")),
            None,
        )
    if picked is None:
        _last_error[0] = "最新的 Release 沒有附任何 .exe"
        return None
    return {
        "version": data.get("tag_name") or "",
        "url": picked.get("browser_download_url"),
        "size": int(picked.get("size") or 0),
        "notes": (data.get("body") or "").strip(),
    }


def check() -> dict | None:
    """有新版就回它的資訊，否則 None。開發模式一律 None。"""
    if not is_frozen():
        return None
    from ro_toolbox import __version__

    info = latest_release()
    if not info or not info.get("url"):
        return None
    return info if is_newer(info["version"], __version__) else None


def download(info: dict, dest: Path, progress=None) -> bool:
    """下載新版到 `dest`。`progress(已下載, 總量)` 可選。

    下載完會檢查大小與 PE 標頭（`MZ`）—— 抓到半截、或抓到 GitHub 的錯誤頁面時
    絕對不能拿去覆蓋，那會讓使用者連舊版都開不起來。
    """
    total = info.get("size") or 0
    try:
        with _urlopen(info["url"], timeout=60.0) as response, dest.open("wb") as handle:
            got = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
    except Exception:  # noqa: BLE001 - 下載失敗就是這次不更新
        dest.unlink(missing_ok=True)
        return False

    ok = dest.exists() and dest.stat().st_size > 1_000_000
    if ok and total:
        ok = dest.stat().st_size == total
    if ok:
        with dest.open("rb") as handle:
            ok = handle.read(2) == b"MZ"      # 確定是 Windows 執行檔
    if not ok:
        dest.unlink(missing_ok=True)
    return ok


def _child_env() -> dict:
    r"""啟動新版時要用的環境變數：把 PyInstaller 的解壓目錄指標清掉。

    onefile 的 exe 啟動時會把自己解壓到 `%TEMP%\_MEIxxxxxx`，並用 `_PYI_*` /
    `_MEIPASS2` 這些環境變數記住位置。整份環境傳給新行程的話，**新版會沿用
    舊版的解壓目錄**；舊版接著結束、要刪掉那個目錄時發現檔案還被新版開著，
    就會跳「Failed to remove temporary directory」的警告視窗（姊妹專案實際踩過）。
    """
    return {
        k: v for k, v in os.environ.items()
        if not (k.startswith(("_MEI", "_PYI")))
    }


def apply_and_restart(new_file: Path) -> bool:
    """用改名的方式換掉執行中的 exe，然後啟動新版。

    成功時呼叫端要自己結束程式。失敗回 False 並盡量還原 ——
    **絕不能讓使用者落到連舊版都開不起來的狀態**。
    """
    current = exe_path()
    old = current.with_suffix(current.suffix + ".old")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        os.replace(current, old)          # Windows 允許改名執行中的檔案
    except OSError:
        return False
    try:
        os.replace(new_file, current)
    except OSError:
        try:
            os.replace(old, current)      # 還原
        except OSError:
            pass
        return False
    try:
        subprocess.Popen(  # noqa: S603 - 執行的是我們自己剛驗證過的 exe
            [str(current)], cwd=str(current.parent), close_fds=True,
            env=_child_env(),
        )
    except OSError:
        return False
    return True


def clean_leftovers() -> None:
    """開場清理：刪掉上次更新留下的 `.old` 與別人留下的解壓目錄。

    刪不掉就算了（可能還被佔用），下次再試 —— 清理失敗不值得打擾使用者。
    """
    if not is_frozen():
        return
    old = exe_path()
    old = old.with_suffix(old.suffix + ".old")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass
    _clean_stale_mei()


def _clean_stale_mei() -> None:
    r"""清掉 `%TEMP%` 裡殘留的 `_MEIxxxxxx` 解壓目錄。

    onefile 的 exe 沒能正常收尾（更新換檔、當掉、被工作管理員砍掉）就會留下
    這種目錄，這支程式一個約 78 MB，累積起來很可觀。
    正在使用中的會刪失敗，直接跳過；自己這次的解壓目錄也不能刪。
    """
    mine = os.environ.get("_PYI_APPLICATION_HOME_DIR") or getattr(sys, "_MEIPASS", "")
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if not temp:
        return
    try:
        entries = list(Path(temp).glob("_MEI*"))
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        if mine and os.path.normcase(str(entry)) == os.path.normcase(str(mine)):
            continue
        try:
            shutil.rmtree(entry)
        except OSError:
            pass          # 還被別的行程開著 → 那個行程結束後自己會清
