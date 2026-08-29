"""所有送進遊戲的輸入，一律交給**短命的子行程**執行。

## 這是實測結論，不是設計偏好

最小化 A/B（同一個遊戲、同一份程式碼、同一瞬間交錯呼叫）：

    啟動遊戲的那個行程：第 1 次送得進去，之後**全部失敗**
    另外開的子行程    ：5/5 全部成功

失敗的樣子有兩種，都很難看懂：

- `PostMessage` 回 FALSE，`GetLastError` 是 0（pywin32 訊息寫「No error message」）
- `SendInput` 回 0，`GetLastError` 也是 0

多半是 GameGuard 認得「開這個遊戲的那個行程」，在它送出第一個輸入之後就封鎖它。
今天繞了很久的「點不動合約書」「打字打不進去」全部都是這一條。

## 所以

- 主行程**只負責決定要做什麼**，不自己送。
- 每次要送輸入，開一個子行程，把動作清單餵給它，做完就結束。
- 密碼走 **stdin**，不走命令列參數 —— 命令列在工作管理員裡看得到。

## 打包成單一 exe 也走同一套

凍結之後 `sys.executable` 就是我們自己，旗標由 `app.run()` 在建 Qt 之前攔下來。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

#: 命令列旗標。`app.run()` 會在建 Qt 之前攔下來。
HELPER_FLAG = "--send-input"

#: 切輸入法用的 Win32 常數。
_WM_IME_CONTROL = 0x0283
_IMC_SETOPENSTATUS = 0x0006
_SMTO_ABORTIFHUNG = 0x0002

#: 子行程最多跑多久。它只送幾十個視窗訊息，正常不到一秒。
_TIMEOUT = 20.0


class InputHelperError(RuntimeError):
    """輸入送不出去。訊息是要直接給使用者看的。"""


# ---- 動作 -------------------------------------------------------------------
#
# 動作是純資料（可以 JSON 化），這樣才能餵給子行程。


def click(x: int, y: int) -> dict:
    """前景點一下螢幕座標。**只有合約書需要**（那個畫面不吃視窗訊息）。"""
    return {"click": [int(x), int(y)]}


def text(value: str) -> dict:
    """對視窗打一段字（背景，不搶前景）。"""
    return {"text": value}


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


def _command(hwnd: int, script: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, HELPER_FLAG, str(hwnd), script]
    return [sys.executable, "-m", "ro_toolbox", HELPER_FLAG, str(hwnd), script]


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


def send(hwnd: int, actions: list[dict]) -> None:
    """開一個子行程，照順序把動作送進遊戲。失敗丟 `InputHelperError`。

    ⚠ 回傳只代表「子行程說它送出去了」，**不代表遊戲收下了**。
    收沒收下要看封包（CLAUDE.md：等訊號，不等時間）。
    """
    _run(actions, hwnd)


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
            _command(hwnd, script),
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
        lines = (done.stderr or done.stdout or "").strip().splitlines()
        detail = lines[-1] if lines else f"結束碼 {done.returncode}"
        raise InputHelperError(f"輸入沒送出去：{detail}")
    return done.stdout or ""


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


def _speak_utf8() -> None:
    """子行程的輸出一律講 UTF-8。**打包之後這一步是必要的。**

    ⚠ 實機（2026-08-30，打包版）：主行程日誌裡的說明整段是亂碼 ——

        子行程在畫面上找不到同意按鈕（�e�� 1942x1256�...）

    「畫面」的 Big5 是 `B5 65`，UTF-8 解不出 `B5` 就換成 `�`，`65` 剛好是 `e`
    —— 也就是子行程用 **cp950** 印出來、主行程用 utf-8 讀。
    主行程明明有設 `PYTHONIOENCODING=utf-8`，但 **PyInstaller 的啟動器用
    isolated config，`PYTHON*` 環境變數整批不生效**，所以凍結之後那一行等於沒設。
    唯一保險的做法是在子行程自己這一側把輸出串流轉成 UTF-8。

    這不是「只是難看」：那一句是別人的機器上唯一查得到的線索
    （[INP-009]，子行程的 `log` 不會進主程式日誌），看不懂就等於沒有。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - 轉不了就照舊，不該讓輸入失敗
            print(f"輸出轉不成 UTF-8（略過）：{exc}", file=sys.stderr)


def _switch_ime_off(hwnd: int) -> None:
    """把視窗的輸入法關掉（切英數）。做不到就記一筆，不要中斷輸入。"""
    import ctypes
    from ctypes import wintypes

    try:
        imm32 = ctypes.WinDLL("imm32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        imm32.ImmGetDefaultIMEWnd.argtypes = [wintypes.HWND]
        imm32.ImmGetDefaultIMEWnd.restype = wintypes.HWND
        ime = imm32.ImmGetDefaultIMEWnd(wintypes.HWND(hwnd))
        if not ime:
            print("找不到 IME 視窗，略過切換", file=sys.stderr)
            return
        result = wintypes.DWORD(0)
        user32.SendMessageTimeoutW(
            ime, _WM_IME_CONTROL, wintypes.WPARAM(_IMC_SETOPENSTATUS),
            wintypes.LPARAM(0), _SMTO_ABORTIFHUNG, 1000, ctypes.byref(result),
        )
    except Exception as exc:  # noqa: BLE001 - 切不掉不該讓整串輸入失敗
        print(f"切換輸入法失敗（略過）：{exc}", file=sys.stderr)


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
    """子行程本體。由 `app.run()` 在建 Qt 之前呼叫。

    只做被交代的動作，**不做任何判斷** —— 要做什麼是主行程決定的，
    這裡多一個判斷就多一個會錯的地方。
    """
    from ro_toolbox.services import input as game_input

    _speak_utf8()
    try:
        index = argv.index(HELPER_FLAG)
        hwnd = int(argv[index + 1])
        script = argv[index + 2]
    except (ValueError, IndexError):
        print(f"用法：{HELPER_FLAG} <hwnd> <動作清單檔>", file=sys.stderr)
        return 2

    try:
        with open(script, encoding="utf-8") as fp:
            actions = json.load(fp)
    except (OSError, ValueError) as exc:
        print(f"讀不到動作清單：{exc}", file=sys.stderr)
        return 2

    # ⚠ 一定要在碰任何視窗 API 之前宣告，否則座標會差一個縮放倍率（[INP-002]）。
    game_input.ensure_dpi_aware()
    try:
        for action in actions:
            if "focus" in action:
                if not game_input.focus_window(hwnd, 2.0):
                    print("搶不到前景，不敢打字（會打進別的視窗）", file=sys.stderr)
                    return 1
            elif "ime_off" in action:
                _switch_ime_off(hwnd)
            elif "look" in action:
                _report_screen(hwnd)
            elif "click" in action:
                x, y = action["click"]
                game_input.click_foreground(hwnd, int(x), int(y))
            elif "text" in action:
                game_input.type_background(hwnd, action["text"])
            elif "text_fg" in action:
                game_input.type_foreground(action["text_fg"])
            elif "key_fg" in action:
                for _ in range(int(action.get("times", 1))):
                    game_input.press_foreground(int(action["key_fg"]))
            elif "key" in action:
                char = action.get("char")
                for _ in range(int(action.get("times", 1))):
                    game_input.press_background(
                        hwnd, int(action["key"]), None if char is None else int(char)
                    )
            else:
                print(f"看不懂的動作：{action!r}", file=sys.stderr)
                return 2
    except game_input.InputError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


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
