"""把動作清單真的送進遊戲。**主程式與「送輸入的小 exe」共用這一份。**

## 為什麼要獨立成一個模組（[INP-023]）

送輸入的活必須交給一顆**小**的 exe：83 MB 的主 exe 送輸入會被 GameGuard
隨機整批擋掉（同一個視窗、同一時間交錯實測，主 exe `PostMessage` 5/10、
`SendInput` 6/10；同一台機器上 7 MB 的小 exe **20/20 全過**，
1.7 MB 的 onedir 也是 20/20）。

而小 exe **不能含 Qt 與 numpy**（那就變成 83 MB，繞回原點），
所以它不能 import `input_helper` —— 那一支會用到 `game_screen`，
而 PyInstaller 連函式裡面的 import 都會跟著收進來。

所以動作迴圈放在這裡，一份實作、兩個入口：

    主 exe   `input_helper.run_helper()`   —— 多支援「看畫面」那個動作（要 Qt）
    小 exe   `ro_toolbox.input_worker.main()` —— 只送輸入，7 MB
"""

from __future__ import annotations

import json
import sys
import time

#: 子行程回報「做完幾個動作」用的開頭。**重試安不安全全靠它。**
#: 見 `input_helper.send()`：只有「做完 0 個」才准換一個子行程重送。
DONE_PREFIX = "DONE "

#: 連按同一個鍵時，兩下之間的間隔（秒）。**這是節流，不是等待。**
#: 實機：一次灌 24 個 Backspace，客戶端只吃到 4 個。
_KEY_GAP = 0.02

#: 切輸入法用的 Win32 常數。
_WM_IME_CONTROL = 0x0283
_IMC_SETOPENSTATUS = 0x0006
_SMTO_ABORTIFHUNG = 0x0002


def speak_utf8() -> None:
    """子行程的輸出一律講 UTF-8。**打包之後這一步是必要的。**

    ⚠ 實機（2026-08-30，打包版）：主行程日誌裡的說明整段是亂碼 ——

        子行程在畫面上找不到同意按鈕（?e?? 1942x1256?...）

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


def switch_ime_off(hwnd: int) -> None:
    """把視窗的輸入法關掉（切英數）。做不到就記一筆，不要中斷輸入。

    ⚠ **打英文之前一定要做這一步。** 使用者的輸入法停在中文時，
    我們送進去的英文會被 IME 吃掉或轉成別的東西 —— 帳號密碼就變成垃圾，
    而且要等伺服器回「帳密錯誤」才會發現（實際踩過一整輪）。

    做法是 Windows 的標準跨行程手法：`ImmGetDefaultIMEWnd(hwnd)` 拿到那個
    視窗的 IME 視窗，再送 `WM_IME_CONTROL` / `IMC_SETOPENSTATUS` 關掉它。
    """
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


def perform(hwnd: int, actions: list[dict], on_look=None) -> int:
    """照順序做完動作清單，回傳結束碼（0 成功）。

    **只做被交代的動作，不做任何判斷** —— 要做什麼是主行程決定的，
    這裡多一個判斷就多一個會錯的地方。

    ★ **做完幾個動作一定要印出來**（`DONE n`）。GameGuard 會整批擋掉一個
    子行程的輸入（[INP-023]），主行程要靠這個數字判斷「重送安不安全」：
    0 個代表什麼都沒發生，重送不會打兩次字。
    """
    from ro_toolbox.services import input as game_input

    # ⚠ 一定要在碰任何視窗 API 之前宣告，否則座標會差一個縮放倍率（[INP-002]）。
    game_input.ensure_dpi_aware()
    done = 0
    try:
        for action in actions:
            if "focus" in action:
                if not game_input.focus_window(hwnd, 2.0):
                    print("搶不到前景，不敢打字（會打進別的視窗）", file=sys.stderr)
                    print(f"{DONE_PREFIX}{done}")
                    return 1
            elif "ime_off" in action:
                switch_ime_off(hwnd)
            elif "look" in action:
                if on_look is None:
                    # 小 exe 沒有 Qt，看不了畫面。這是**呼叫端派錯活**，
                    # 要大聲講 —— 安靜地跳過會讓主行程以為「畫面認不出來」。
                    print("這一顆 helper 不會看畫面（沒有 Qt）", file=sys.stderr)
                    print(f"{DONE_PREFIX}{done}")
                    return 2
                on_look(hwnd)
            elif "click" in action:
                x, y = action["click"]
                game_input.click_foreground(hwnd, int(x), int(y))
            elif "pause" in action:
                # ⚠ 這**不是**「睡一下等它穩定」，是**節流**：客戶端自己的訊息
                #   迴圈跟不上就會掉字（`input.type_background` 的註解量過：
                #   間隔 0.01 秒時 `s26016041` 會變成 `s26011034`）。
                #   清空那 48 個按鍵是一次爆量，緊接著的第一個字必掉 ——
                #   實機看到 ID 欄的 `PWaa1234` 變成 `Waa1234`。
                time.sleep(min(2.0, max(0.0, float(action["pause"]))))
            elif "click_msg" in action:
                rx, ry = action["click_msg"]
                game_input.click_message(hwnd, float(rx), float(ry))
            elif "text" in action:
                game_input.type_background(hwnd, action["text"])
            elif "text_fg" in action:
                game_input.type_foreground(action["text_fg"])
            elif "key_fg" in action:
                for _ in range(int(action.get("times", 1))):
                    game_input.press_foreground(
                        int(action["key_fg"]), bool(action.get("shift", False))
                    )
            elif "key" in action:
                char = action.get("char")
                # ⚠ **連按之間一定要留間隔。** 客戶端自己的訊息迴圈跟不上就
                #   會掉訊息，而且掉得很難看：實機把 24 個 Backspace 一次灌進去，
                #   欄位裡的 `s26016041` 只被刪掉 4 個字變成 `s2601`，
                #   接著打的 6 個字**一個都沒進去**。
                #   （同一條理由已經寫在 `input.type_background` 的註解裡。）
                gap = float(action.get("gap", _KEY_GAP))
                times = int(action.get("times", 1))
                for i in range(times):
                    game_input.press_background(
                        hwnd, int(action["key"]), None if char is None else int(char)
                    )
                    if gap and i + 1 < times:
                        time.sleep(gap)
            else:
                print(f"看不懂的動作：{action!r}", file=sys.stderr)
                print(f"{DONE_PREFIX}{done}")
                return 2
            done += 1
    except game_input.InputError as exc:
        print(str(exc), file=sys.stderr)
        print(f"{DONE_PREFIX}{done}")
        return 1
    print(f"{DONE_PREFIX}{done}")
    return 0


def run(argv: list[str], flag: str, on_look=None) -> int:
    """子行程本體：解析命令列、讀動作清單、做完。

    命令列長這樣：`<flag> <hwnd> <動作清單檔>`。
    動作清單走**暫存檔**不走命令列 —— 裡面有密碼，命令列在工作管理員看得到。
    """
    speak_utf8()
    try:
        index = argv.index(flag)
        hwnd = int(argv[index + 1])
        script = argv[index + 2]
    except (ValueError, IndexError):
        print(f"用法：{flag} <hwnd> <動作清單檔>", file=sys.stderr)
        return 2

    try:
        with open(script, encoding="utf-8") as fp:
            actions = json.load(fp)
    except (OSError, ValueError) as exc:
        print(f"讀不到動作清單：{exc}", file=sys.stderr)
        return 2

    return perform(hwnd, actions, on_look)
