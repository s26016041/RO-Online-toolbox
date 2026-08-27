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
    if ok and not has_signature(dest):
        # ⚠ 沒簽章的版本讀不到遊戲（[ENV-006]）。寧可不更新，也不要
        # 把一顆「開得起來但什麼都讀不到」的 exe 換上去。
        sys.stderr.write("[update] 新版沒有簽章 —— 那種版本讀不到遊戲，不更新\n")
        _last_error[0] = "新版沒有簽章（讀不到遊戲），已略過這次更新"
        ok = False
    if not ok:
        dest.unlink(missing_ok=True)
    return ok


def has_signature(path: Path) -> bool:
    """這顆 exe 帶著 Authenticode 簽章嗎？（只看有沒有，不驗信任鏈）

    ⚠ **為什麼非檢查不可**：未簽章的 exe 會被 GameGuard 擋掉對遊戲的大量
    記憶體讀取（`ERROR_ACCESS_DENIED`，見 GAMEDATA [ENV-006]）。
    程式看起來會動、視窗也開得起來，但讀不到角色、讀不到背包 ——
    而那個症狀完全不像「忘了簽」，會被當成別的問題追很久（實際追了兩小時）。

    自動更新如果把一顆沒簽的 exe 推給所有人，每個人都會踩到那個坑。
    所以**寧可不更新，也不要換成沒簽的**。

    做法：讀 PE 的資料目錄第 4 項（`IMAGE_DIRECTORY_ENTRY_SECURITY`），
    大小不為 0 就代表有憑證表。純讀位元組，不叫外部工具。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if len(raw) < 0x40 or raw[:2] != b"MZ":
        return False
    pe = int.from_bytes(raw[0x3C:0x40], "little")
    if len(raw) < pe + 0x18 or raw[pe:pe + 4] != b"PE\x00\x00":
        return False
    magic = int.from_bytes(raw[pe + 0x18:pe + 0x1A], "little")
    if magic == 0x10B:          # PE32
        dirs = pe + 0x18 + 96
    elif magic == 0x20B:        # PE32+（64 位元）
        dirs = pe + 0x18 + 112
    else:
        return False
    entry = dirs + 4 * 8        # 第 4 項：憑證表
    if len(raw) < entry + 8:
        return False
    size = int.from_bytes(raw[entry + 4:entry + 8], "little")
    return size > 0


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


def _live_bundle_dirs() -> set[str]:
    r"""現在**還有行程在用**的 onefile 解壓目錄。

    onefile 的啟動器把自己解壓到 `%TEMP%\_MEIxxxxxx` 之後，會把位置寫進自己的
    環境變數 `_PYI_APPLICATION_HOME_DIR`（舊版叫 `_MEIPASS2`），而且**父子行程
    都有**。所以「這個目錄有沒有人在用」是讀得到的訊號，不必用時間或檔案鎖去猜：
    把所有行程的環境變數掃一遍，撈出來的那些就是不能碰的。

    掃的是**全部**行程，不是只有我們自己的 —— 姊妹專案（Angels-Online-toolbox）
    也是 onefile，它的解壓目錄同樣不能被我們清掉（[ENV-007] 就是反過來被它清掉）。

    讀不到某個行程的環境（系統行程、提權的行程）就跳過那一個；psutil 沒收進來
    就回空集合 —— 這時全靠 `_looks_abandoned()` 那道關卡把關。
    """
    try:
        import psutil
    except ImportError:                    # 開發環境沒裝就退回只靠刪除探測
        return set()
    live: set[str] = set()
    try:
        procs = list(psutil.process_iter())
    except Exception:                      # noqa: BLE001 - 清理失敗不值得打擾使用者
        return live
    for proc in procs:
        try:
            env = proc.environ()
        except Exception:                  # noqa: BLE001 - 讀不到就跳過這個行程
            continue
        home = env.get("_PYI_APPLICATION_HOME_DIR") or env.get("_MEIPASS2")
        if home:
            live.add(os.path.normcase(os.path.normpath(home)))
    return live


def _looks_abandoned(entry: Path) -> bool:
    r"""第二道關卡：**先刪掉 `python3XX.dll`**，刪得掉才算真的沒人在用。

    為什麼要這樣做，而不是直接 `rmtree` 看它成不成功：
    `rmtree` 是**一路刪下去**的，遇到刪不掉的檔案只會在最後拋例外 ——
    在那之前它已經把所有刪得掉的東西刪光了。對一個**還在執行**的解壓目錄來說，
    被鎖住的 `.dll`／`.pyd` 會留著（所以那個程式不會當場死），
    但 `assets/*.gz` 這種純資料檔會被刪乾淨，於是那支程式從此讀不到自己的資料表，
    而且**沒有任何錯誤**，只會安靜地少一塊功能（[ENV-007] 實際踩到的就是這個）。

    所以要有一個「刪得掉嗎」的探測，而且它必須挑**一定被載入**的檔案：
    行程活著就一定映射著自己的 `python3XX.dll`，映射中的檔案刪不掉
    （改名可以、獨佔開啟也可以，只有刪除會失敗 —— 兩種都試過了，見 [ENV-007]）。
    `python3.dll`（穩定 ABI 的轉送層）不保證被載入，不能拿來探測。

    探測本身是破壞性的，但破壞的是「探測通過＝馬上要整個刪掉」的目錄，
    而且它是整個清理流程動的**第一個**檔案：不通過就等於什麼都沒發生。
    """
    probes = sorted(entry.glob("python3[0-9][0-9].dll"))
    if not probes:
        # 不像 onefile 的解壓目錄（或已經被清到一半）→ 不歸我們處理，別碰。
        return False
    for dll in probes:
        try:
            dll.unlink()
        except OSError:
            return False                   # 還映射著 → 有人在跑 → 收手
    return True


def _clean_stale_mei() -> None:
    r"""清掉 `%TEMP%` 裡**已經沒人在用**的 `_MEIxxxxxx` 解壓目錄。

    onefile 的 exe 沒能正常收尾（更新換檔、當掉、被工作管理員砍掉）就會留下
    這種目錄，這支程式一個約 78 MB，累積起來很可觀。

    ⚠ 「刪不掉的自然會失敗」**不是**安全網（[ENV-007]）：`rmtree` 會先把刪得掉的
    刪光才報錯，等於把還在跑的程式的資料檔挖掉。所以動手前要先過兩道關卡 ——
    `_live_bundle_dirs()`（有沒有行程說這是它的家）與 `_looks_abandoned()`
    （鎖住的 DLL 刪不刪得掉）。兩道都過才 `rmtree`。
    """
    mine = os.environ.get("_PYI_APPLICATION_HOME_DIR") or getattr(sys, "_MEIPASS", "")
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if not temp:
        return
    try:
        entries = list(Path(temp).glob("_MEI*"))
    except OSError:
        return
    busy = _live_bundle_dirs()
    if mine:
        busy.add(os.path.normcase(os.path.normpath(str(mine))))
    for entry in entries:
        if not entry.is_dir():
            continue
        if os.path.normcase(os.path.normpath(str(entry))) in busy:
            continue                       # 有行程正拿它當家
        if not _looks_abandoned(entry):
            continue                       # 檔案還鎖著 → 有人在跑
        try:
            shutil.rmtree(entry)
        except OSError:
            pass          # 剩下的下次再清，清理失敗不值得打擾使用者
