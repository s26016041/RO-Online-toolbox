"""所有送進遊戲的輸入，一律交給**短命的小子行程**執行。

## 這是實測結論，不是設計偏好

失敗的樣子有兩種，都很難看懂（兩個都是「被攔截」，不是參數錯）：

- `PostMessage` 回 FALSE，`GetLastError` 是 0（pywin32 訊息寫「No error message」）
- `SendInput` 回 0，`GetLastError` 也是 0

**GameGuard 會整批擋掉某些行程的輸入。** 2026-08-30 量清楚了（[INP-023]）——
同一個遊戲視窗、同一時間交錯，各 10 次：

    主 exe（83 MB, onefile）   PostMessage 5/10 失敗、SendInput 4/10 失敗
    小 exe（7 MB, onefile）    0/10、0/10
    小 exe（1.7 MB, onedir）   0/10、0/10
    python.exe                 0/10、0/10

⇒ 不是 PyInstaller、不是自解壓、也不是簽章（小 exe 沒簽照樣過）——
**是那顆大的被擋**。而且是「這個行程能不能送」一次決定：能送的行程
連送 20 個動作都進得去（8 回裡 7 回整包成功）。

## 所以

- 主行程**只負責決定要做什麼**，不自己送。
- 送輸入交給**另外一顆小 exe**（`ro-input.exe`，見 `input_worker`）——
  打包版一定走它；找不到才退回主 exe（會被擋，但 `send()` 會換一個重送）。
- **看畫面**（找同意按鈕、判斷在哪一關）要 Qt，還是主 exe 的活 ——
  那件事只讀不送，不會被擋。
- 動作清單走**暫存檔**，不走命令列 —— 密碼會出現在工作管理員裡。

## 舊的說法已經作廢

[INP-009] 原本寫「一個行程送過第一次輸入之後就會被封鎖，所以每次都要開新的
子行程」，還有「送過 `SendInput` 之後視窗訊息會被封鎖，所以要拆批」。
**兩條實測都不成立**（[INP-022]、[INP-023]）：能送的行程可以一直送、
視窗訊息與按鍵可以在同一個行程混著送。拆批的唯一效果是**讓被擋的機率乘上去**。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ro_toolbox.services import input_actions

log = logging.getLogger(__name__)

#: 命令列旗標。`app.run()` 會在建 Qt 之前攔下來。
#: 送輸入的小 exe（`ro_toolbox.input_worker`）用同一個，主行程才不用記兩套。
HELPER_FLAG = "--send-input"

#: 子行程最多跑多久。它只送幾十個視窗訊息，正常不到一秒。
_TIMEOUT = 20.0


class InputHelperError(RuntimeError):
    """輸入送不出去。訊息是要直接給使用者看的。"""

    def __init__(self, message: str, done: int | None = None) -> None:
        super().__init__(message)
        #: 子行程在失敗前**做完了幾個動作**。
        #:
        #: `0` 代表整批一個都沒送出去 —— 那時候換一個子行程重送是**安全的**
        #: （不會重複打字）。`None` 代表問不出來（子行程根本沒回報），
        #: 那就當作「不知道做到哪」，不准重送。
        self.done = done


# ---- 動作 -------------------------------------------------------------------
#
# 動作是純資料（可以 JSON 化），這樣才能餵給子行程。


def click(x: int, y: int) -> dict:
    """前景點一下螢幕座標。**只有合約書需要**（那個畫面不吃視窗訊息）。"""
    return {"click": [int(x), int(y)]}


def text(value: str) -> dict:
    """對視窗打一段字（背景，不搶前景）。"""
    return {"text": value}


def pause(seconds: float) -> dict:
    """讓子行程停一下下。**這是節流，不是「等它穩定」。**

    清空欄位那 48 個按鍵是一次爆量，客戶端的訊息迴圈跟不上，
    緊接著的第一個字會被吃掉（實機：`PWaa1234` 進到欄位變成 `Waa1234`）。
    """
    return {"pause": float(seconds)}


def click_message(ratio_x: float, ratio_y: float) -> dict:
    """用**視窗訊息**點視窗裡的某一點（背景有效，不搶前景）。

    給「點某一格輸入框」用 —— 點完焦點就確定在哪一格，不必猜
    （見 `input.click_message`）。座標是**客戶區比例**。
    """
    return {"click_msg": [float(ratio_x), float(ratio_y)]}


def look() -> dict:
    """看一眼畫面：**現在在哪一關** ＋ 合約書的「同意」按鈕在哪。

    ⚠ **為什麼要在子行程裡做**：截圖與送輸入不能在同一個行程
    （[INP-009]，主行程送過輸入之後會被封鎖）。主行程只負責決定要不要看，
    真正碰視窗的事情一律在這裡做完。
    """
    return {"look": True}


#: 有對應字元碼的功能鍵。**這張表很重要**：
#:
#: 實測 RO 客戶端對 Enter 的反應（同一畫面、同一組帳密，只換 Enter 的送法）：
#:
#:     KEYDOWN + CHAR + KEYUP  → 送出 ★
#:     KEYDOWN                 → 送出 ★
#:     KEYDOWN + KEYUP（無 CHAR）→ **不送出**
#:     CHAR only               → **不送出**
#:
#: 少帶那個字元碼，帳密全部打對了也永遠不會送出去 —— 而且完全沒有錯誤訊息，
#: 症狀是「客戶端沒反應」，看起來像字沒進去（實際繞了很久才找到）。
_KEY_CHARS = {0x0D: 13, 0x09: 9}


def ime_off() -> dict:
    """把目標視窗的輸入法切成英數。

    ⚠ **打英文之前一定要做這一步。** 使用者的輸入法停在中文時，
    我們送進去的英文會被 IME 吃掉或轉成別的東西 —— 帳號密碼就變成垃圾，
    而且要等伺服器回「帳密錯誤」才會發現（實際踩過一整輪）。

    做法是 Windows 的標準跨行程手法：`ImmGetDefaultIMEWnd(hwnd)` 拿到那個
    視窗的 IME 視窗，再送 `WM_IME_CONTROL` / `IMC_SETOPENSTATUS` 關掉它。
    """
    return {"ime_off": True}


def focus() -> dict:
    """把遊戲視窗帶到最前面，並**確認真的到了**。

    ⚠ 前景輸入（`SendInput`）送到「當下的前景視窗」——不是我們指定的那個。
    沒有先確認就打字，帳號密碼有可能**打進使用者正在用的視窗**。
    確認不到就整批失敗，寧可不做也不要亂打。

    用 `SetForegroundWindow`（不是滑鼠點擊）—— 點擊會讓這個行程後續的輸入
    被封鎖（[INP-009]），而搶前景不會。
    """
    return {"focus": True}


def text_foreground(value: str) -> dict:
    """對**前景視窗**打一段字（`SendInput` Unicode）。

    ⚠ 送到哪裡取決於誰是前景，所以動作清單裡要先有一個 `click`（會順便搶前景），
    或呼叫端已經確保遊戲在最前面。合約書那類自繪對話框只吃這條路。
    """
    return {"text_fg": value}


def key_foreground(virtual_key: int, times: int = 1) -> dict:
    """對前景視窗按一個功能鍵幾次（`SendInput`）。"""
    return {"key_fg": int(virtual_key), "times": int(times)}


def key(virtual_key: int, times: int = 1) -> dict:
    """按一個鍵幾次（背景）。Enter／Tab 會自動帶上對應的字元碼。"""
    action = {"key": int(virtual_key), "times": int(times)}
    char = _KEY_CHARS.get(int(virtual_key))
    if char is not None:
        action["char"] = char
    return action


# ---- 主行程這一側 -----------------------------------------------------------


#: 送輸入的小 exe 的檔名（打包時收進主 exe 裡，見 `RO-Online-toolbox.spec`）。
INPUT_WORKER_EXE = "ro-input.exe"


def input_worker() -> Path | None:
    """送輸入用的**小 exe** 在哪。沒打包、或沒收進來就回 None。

    ⚠ **打包版一定要用它送輸入**（[INP-023]）：83 MB 的主 exe 送輸入會被
    GameGuard 隨機整批擋掉（實測 `PostMessage` 5/10、`SendInput` 4/10），
    同一台機器上 7 MB 的小 exe **20/20 全過**。

    找不到就退回主 exe —— 會慢、會被擋，但至少還會動（而且 `send()` 有重送）。
    """
    root = getattr(sys, "_MEIPASS", None)
    if not root:
        return None
    path = Path(root) / INPUT_WORKER_EXE
    return path if path.is_file() else None


def _command(hwnd: int, script: str, actions: list[dict]) -> list[str]:
    """要開哪一顆子行程。

    **看畫面走主 exe，送輸入走小 exe。**「看畫面」要 Qt（樣板比對），
    而 Qt 就是主 exe 那 83 MB 的來源；但看畫面**只讀不送**，不會被擋，
    所以那一顆大的照用。送輸入則一定要小的（[INP-023]）。
    """
    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "ro_toolbox", HELPER_FLAG, str(hwnd), script]
    worker = None if _needs_qt(actions) else input_worker()
    if worker is not None:
        return [str(worker), HELPER_FLAG, str(hwnd), script]
    return [sys.executable, HELPER_FLAG, str(hwnd), script]


def _needs_qt(actions: list[dict]) -> bool:
    """這批動作裡有沒有「看畫面」——那一個只有主 exe 做得到。"""
    return any("look" in action for action in actions)


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        # 開發時可能是 src 佈局；從套件自己的位置推上層目錄，不寫死路徑。
        parent = str(Path(__file__).resolve().parents[2])
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{parent}{os.pathsep}{existing}" if existing else parent
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


#: 一批輸入最多換幾個子行程重送。見 `send()`。
#:
#: 實機量到的擋掉率（2026-08-30，同一個視窗、同一時間交錯 A/B 各 10 次）：
#:
#:     打包版 exe 子行程   PostMessage 6 成功 / 4 失敗、SendInput 3 / 7
#:     venv python 子行程  PostMessage 10 / 0、        SendInput 10 / 0
#:
#: 每換一個子行程就是重擲一次骰子，擋掉率抓 0.5 的話 6 次還失敗是 1.6%。
_SEND_TRIES = 6

#: 子行程回報找到的按鈕座標時用的開頭。
_AGREE_PREFIX = "AGREE "
#: 子行程回報「畫面現在停在哪一關」時用的開頭（值是 `Stage` 的名字）。
_STAGE_PREFIX = "STAGE "
#: 子行程回報「為什麼找到／為什麼沒找到」時用的開頭。
#:
#: ⚠ 這一行不是裝飾。找按鈕跑在子行程裡（[INP-009]），它的 `log` **不會**
#: 進到主程式的日誌 —— 使用者朋友的機器上「找不到按鈕」查了兩輪都沒有線索，
#: 就是因為主行程這邊只看得到「找到了／沒找到」，看不到分數、倍率、畫面亮度。
_AGREE_NOTE = "AGREENOTE "


def send(hwnd: int, actions: list[dict], tries: int = _SEND_TRIES) -> None:
    """開一個子行程，照順序把動作送進遊戲。失敗丟 `InputHelperError`。

    ⚠ 回傳只代表「子行程說它送出去了」，**不代表遊戲收下了**。
    收沒收下要看封包（CLAUDE.md：等訊號，不等時間）。

    ## 整批被擋掉就換一個子行程再送（2026-08-30）

    實機量到：**GameGuard 會隨機把整個子行程的輸入擋掉**，而且是
    「這個行程能不能送」一次決定 —— 能送的行程連送 20 次都進得去
    （8 回裡 7 回整包成功），被擋的行程第一個動作就失敗。
    打包版 exe 的子行程被擋掉的機率高到 40~70%（同一時間交錯比對，
    venv python 的子行程 20/20 全過），所以**換一個子行程重送就是重擲骰子**。

    ⚠ **只有「一個動作都沒做」才准重送。** 做到一半才被擋的話，
    前面幾個動作已經生效了（欄位裡已經有字），整批重來會變成打兩次 ——
    那時候寧可讓呼叫端整輪重來（它會先清空欄位）。
    子行程回報的 `DONE n` 就是為了這個。
    """
    last: InputHelperError | None = None
    for attempt in range(1, max(1, tries) + 1):
        try:
            _run(actions, hwnd)
            return
        except InputHelperError as exc:
            last = exc
            if exc.done != 0 or attempt >= tries:
                raise
            log.info("第 %d 個子行程整批被擋掉（一個動作都沒送出）—— 換一個再送：%s",
                     attempt, exc)
    if last is not None:                     # pragma: no cover - 迴圈一定會 return/raise
        raise last


def _run(actions: list[dict], hwnd: int) -> str:
    """真的去開子行程，回傳它的標準輸出。"""
    if not actions:
        return ""
    # ⚠ 動作清單走**暫存檔**，不走命令列也不走 stdin。
    # 命令列：密碼會出現在工作管理員裡。
    # stdin：主行程的 stdin 是主控台時，`subprocess` 有時建不出管線
    #        （"Cannot open console input buffer for writing"，實際踩過）。
    handle, script = tempfile.mkstemp(prefix="ro-input-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fp:
            json.dump(actions, fp, ensure_ascii=False)
    except OSError as exc:
        raise InputHelperError(f"寫不出動作清單：{exc}") from exc

    try:
        done = subprocess.run(
            _command(hwnd, script, actions),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
            env=_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise InputHelperError(f"送輸入的子行程 {_TIMEOUT:.0f} 秒沒回應。") from exc
    except OSError as exc:
        raise InputHelperError(f"起不了送輸入的子行程：{exc}") from exc
    finally:
        # ⚠ 一定要刪掉 —— 裡面有密碼。
        try:
            os.unlink(script)
        except OSError:
            pass

    if done.returncode != 0:
        lines = [line for line in (done.stderr or "").strip().splitlines()
                 if not line.startswith(input_actions.DONE_PREFIX)]
        lines = lines or (done.stdout or "").strip().splitlines()
        detail = lines[-1] if lines else f"結束碼 {done.returncode}"
        raise InputHelperError(f"輸入沒送出去：{detail}", _actions_done(done.stdout))
    return done.stdout or ""


def _actions_done(output: str | None) -> int | None:
    """子行程說它做完幾個動作。問不出來回 None（＝不知道，不准重送）。"""
    for line in reversed((output or "").splitlines()):
        if line.startswith(input_actions.DONE_PREFIX):
            try:
                return int(line[len(input_actions.DONE_PREFIX):].strip())
            except ValueError:
                return None
    return None


def look_at_screen(hwnd: int):
    """問子行程「畫面現在停在哪一關、同意按鈕在螢幕的哪裡」。

    回傳 `game_screen.ScreenReport`（**永遠不回 None** —— 看不到畫面就是
    `Stage.UNKNOWN` 加一句說明，呼叫端才不用到處判斷 None）。

    ⚠ 認不出來不是錯誤：畫面可能還在載入。呼叫端要有退路，但**不准假裝
    知道自己在哪一關** —— 那正是使用者踩到的坑（合約書早就過了還在點同意）。
    """
    from ro_toolbox.services.game_screen import ScreenReport, Stage

    try:
        output = _run([look()], hwnd)
    except InputHelperError as exc:
        log.info("看畫面的子行程失敗：%s", exc)
        return ScreenReport(Stage.UNKNOWN, None, f"子行程失敗：{exc}")
    spot: tuple[int, ...] | None = None
    stage = Stage.UNKNOWN
    note = ""
    for line in (output or "").splitlines():
        if line.startswith(_AGREE_NOTE):
            note = line[len(_AGREE_NOTE):]
        elif line.startswith(_STAGE_PREFIX):
            name = line[len(_STAGE_PREFIX):].strip()
            stage = getattr(Stage, name, Stage.UNKNOWN)
        elif line.startswith(_AGREE_PREFIX):
            try:
                spot = tuple(int(v) for v in line[len(_AGREE_PREFIX):].split())
            except ValueError:
                spot = None
    found = (spot[0], spot[1]) if spot is not None and len(spot) == 2 else None
    # 每一輪都留下一行：認得出／認不出、憑什麼、畫面判定是什麼。
    # 別人的機器上這一句是唯一查得到的東西。
    log.info("子行程看畫面：%s、同意按鈕 %s（%s）",
             stage.value, found or "找不到", note or "沒有說明")
    return ScreenReport(stage, found, note or "沒有說明")


# ---- 子行程這一側 -----------------------------------------------------------


def _report_screen(hwnd: int) -> None:
    """看一眼畫面，把「在哪一關」與「同意按鈕在哪」印出來給主行程。

    認不出來**不算失敗**（畫面可能還在載入）—— 照樣把說明印出來，
    主行程要靠它決定下一步（而且那是別人的機器上唯一的線索）。
    """
    try:
        from ro_toolbox.services import game_screen

        report = game_screen.screen_report(hwnd)
    except Exception as exc:  # noqa: BLE001 - 看不到畫面不該讓整串動作失敗
        print(f"{_AGREE_NOTE}看畫面失敗（略過）：{exc}")
        return
    print(f"{_STAGE_PREFIX}{report.stage.name}")
    print(f"{_AGREE_NOTE}{report.note}")
    if report.agree is not None:
        print(f"{_AGREE_PREFIX}{report.agree[0]} {report.agree[1]}")


def run_helper(argv: list[str]) -> int:
    """子行程本體（**主 exe 這一顆**）。由 `app.run()` 在建 Qt 之前呼叫。

    動作迴圈與送輸入的小 exe 共用同一份（`services/input_actions.py`）——
    這一顆只多會一件事：**看畫面**（那要 Qt，見 `_report_screen`）。
    """
    return input_actions.run(argv, HELPER_FLAG, on_look=_report_screen)


def login_probe(pid: int, marker: str) -> bool:
    """去遊戲記憶體裡找剛剛打進去的探針字串。**唯讀。**

    自動登入用它判斷「客戶端收不收字」—— 收得到才敢把真的帳密打進去。
    找不到不代表壞掉，多半是客戶端還沒載完。
    """
    try:
        from ro_toolbox.services.memory_scan import MemoryScanner
    except ImportError:
        return True                      # 沒有掃描能力就別擋住流程
    scanner = MemoryScanner()
    try:
        scanner.open(pid)
        return bool(scanner.search_string(marker, ("ascii",)))
    except Exception as exc:  # noqa: BLE001
        log.debug("找探針失敗：%s", exc)
        return True
    finally:
        try:
            scanner.close()
        except Exception:  # noqa: BLE001
            pass


def submitted_account(pid: int) -> str | None:
    """讀出客戶端**上一次送出去的帳號**。讀不到或不合理回 `None`。

    自動登入用它做閉環驗證：按下送出之後，如果這裡不是我們的帳號，
    就代表字打到別的欄位去了（客戶端記住帳號時焦點會落在密碼欄），
    下一次先按一次 Tab 再打。

    **出處**（GAMEDATA [MEM-032]）：使用者在畫面上把帳號欄填 `s26011034`、
    密碼欄填 `wwe123`，掃記憶體時帳號那串同時出現在兩個靜態位置，
    而且**只有在送出之後才有值** —— 所以它記的就是「送出去的帳號」。

    位址用**程式碼特徵**定位（`SUBMITTED_ACCOUNT_SIGS`），不寫死。
    以前這裡是 `SUBMITTED_ACCOUNT_OFFSET = 0x11D2ACC`，那違反 CLAUDE.md 的
    最高原則：同一次改版已經讓角色座標的固定距離斷掉（[MEM-039]），
    這種寫死的偏移遲早輪到，而且壞了不會有任何徵兆。
    """
    try:
        from ro_toolbox.services.aob import locate_global
        from ro_toolbox.services.memory_scan import MemoryScanner
        from ro_toolbox.services.signatures import (
            SUBMITTED_ACCOUNT_MAX_BYTES,
            SUBMITTED_ACCOUNT_SIGS,
        )
    except ImportError:
        return None
    scanner = MemoryScanner()
    try:
        scanner.open(pid)
        address = locate_global(scanner, SUBMITTED_ACCOUNT_SIGS)
        if address is None:
            log.debug("帳號緩衝定位失敗（遊戲可能已改版），無法驗證送出的帳號")
            return None
        text = scanner.read_string(address, SUBMITTED_ACCOUNT_MAX_BYTES, "ascii")
        if not text or any(ord(c) < 0x20 or ord(c) > 0x7E for c in text):
            return None
        return text
    except Exception as exc:  # noqa: BLE001
        log.debug("讀送出的帳號失敗：%s", exc)
        return None
    finally:
        try:
            scanner.close()
        except Exception:  # noqa: BLE001
            pass


def field_addresses(pid: int, text: str) -> list[int]:
    """這串字目前落在哪些**欄位緩衝**（已排除共用緩衝與靜態副本）。

    登入畫面的兩格各有一塊堆積緩衝，位址每次重開都不同 ——
    所以用「找我們自己打進去的字」來認格子，不記位址（CLAUDE.md：存身分不存位置）。

    ⚠ **共用緩衝一定要排除**：帳號與密碼兩格都會寫進模組裡同一塊靜態緩衝，
    不排除的話兩格永遠分不出來（踩過）。這裡用「落在模組映像內就不算欄位」
    一次擋掉共用緩衝與所有靜態副本 —— 比記住某個偏移可靠，
    模組位移了也照樣成立（以前另外留了一個寫死的 `SHARED_INPUT_OFFSET`，
    實測**沒有任何程式碼引用得到它**，也沒有人用，已刪除）。
    """
    try:
        from ro_toolbox.services.memory_scan import MemoryScanner
    except ImportError:
        return []
    scanner = MemoryScanner()
    try:
        scanner.open(pid)
        base = scanner.image_base_by_scan()
        out = []
        for addr, _enc, _len in scanner.search_string(text, ("ascii",)):
            if base is not None and base <= addr < base + 0x2200000:
                continue          # 模組內的都是副本／共用緩衝，不是欄位
            out.append(addr)
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("找欄位位址失敗：%s", exc)
        return []
    finally:
        try:
            scanner.close()
        except Exception:  # noqa: BLE001
            pass
