"""送輸入的小 exe（`ro-input.exe`）的進入點。**這顆不含 Qt，只有幾 MB。**

## 為什麼要單獨一顆（[INP-023]）

83 MB 的主 exe 送輸入會被 GameGuard **隨機整批擋掉**。同一個遊戲視窗、
同一時間交錯量（各 10 次）：

    主 exe（83 MB, onefile）   PostMessage 5/10 失敗、SendInput 4/10 失敗
    小 exe（7 MB, onefile）    0/10、0/10
    小 exe（1.7 MB, onedir）   0/10、0/10
    python.exe                 0/10、0/10

⇒ 不是 PyInstaller、不是自解壓、也不是簽章（小 exe 根本沒簽也照樣過），
是**那顆大的**被擋。所以輸入交給這顆小的送。

⚠ 這裡**不准 import 任何會拉到 Qt／numpy 的東西**（`game_screen`、
`input_helper`、`ro_toolbox.app`…）—— PyInstaller 連函式內的 import 都會收，
一不小心這顆就變回 83 MB，繞了一圈等於沒做。
只准 `ro_toolbox.services.input`（ctypes ＋ pywin32）與 `input_actions`。

看畫面（找同意按鈕、判斷在哪一關）還是主 exe 的活 —— 那個要 Qt，
而且它**只讀不送**，不會被擋。
"""

from __future__ import annotations

import sys

#: 命令列旗標。跟主 exe 用同一個，主行程才不用記兩套。
HELPER_FLAG = "--send-input"


def main(argv: list[str] | None = None) -> int:
    from ro_toolbox.services import input_actions

    return input_actions.run(argv if argv is not None else sys.argv, HELPER_FLAG)


if __name__ == "__main__":
    sys.exit(main())
