"""量出登入框的 Tab 走法（[INP-029] 就是這支量出來的）：焦點一開始在哪、按 Tab 會走到哪一格。

做法：進到登入畫面之後，依序打不同的記號再抓圖 —— 看畫面就知道每一格是誰。
全程前景 SendInput（不用 PostMessage），順便驗證「不靠視窗訊息也打得進去」。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from ro_toolbox.config.settings import current_settings  # noqa: E402
from ro_toolbox.services import game_launcher, game_screen, input_helper  # noqa: E402
from ro_toolbox.services.game_screen import Stage  # noqa: E402

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

paths = game_launcher.GamePaths(Path(current_settings().game_path))
pid = game_launcher.launch_game_directly(paths)
print("PID", pid, flush=True)

hwnd = None
deadline = time.time() + 120
while time.time() < deadline and hwnd is None:
    hwnd = game_screen.find_window(pid)
    time.sleep(0.5)
print("hwnd", hwnd, flush=True)

# 等到登入畫面（合約書就按同意）
stage = None
deadline = time.time() + 180
while time.time() < deadline:
    report = input_helper.look_at_screen(hwnd)
    stage = report.stage
    print("畫面：", stage, report.agree, flush=True)
    if stage is Stage.LOGIN:
        break
    if stage is Stage.EULA and report.agree:
        input_helper.send(hwnd, [input_helper.click(*report.agree)])
    time.sleep(1.5)

if stage is not Stage.LOGIN:
    print("沒等到登入畫面，放棄")
    sys.exit(1)


def shot(name):
    game_screen.capture(hwnd).save(str(OUT / f"{name}.png"))
    print("拍了", name, flush=True)


shot("00-login")

# ① 一開始的焦點：直接打 AAAAAA（不清空，看看它接在哪裡）
input_helper.send(hwnd, [input_helper.focus(), input_helper.ime_off(),
                         input_helper.text_foreground("AAAAAA")])
time.sleep(0.4)
shot("01-typed-A")

# ② Tab 一次再打 BBBBBB
input_helper.send(hwnd, [input_helper.focus(),
                         input_helper.key_foreground(0x09),
                         input_helper.pause(0.2),
                         input_helper.text_foreground("BBBBBB")])
time.sleep(0.4)
shot("02-tab-B")

# ③ 再 Tab 一次打 CCCCCC
input_helper.send(hwnd, [input_helper.focus(),
                         input_helper.key_foreground(0x09),
                         input_helper.pause(0.2),
                         input_helper.text_foreground("CCCCCC")])
time.sleep(0.4)
shot("03-tab-C")

# ④ 再 Tab 一次打 DDDDDD（看有沒有繞回第一格）
input_helper.send(hwnd, [input_helper.focus(),
                         input_helper.key_foreground(0x09),
                         input_helper.pause(0.2),
                         input_helper.text_foreground("DDDDDD")])
time.sleep(0.4)
shot("04-tab-D")

# ⑤ 前景清空能不能清掉（Home + Delete x24 + End + Backspace x24）
input_helper.send(hwnd, [input_helper.focus(),
                         input_helper.key_foreground(0x24),
                         input_helper.key_foreground(0x2E, 24),
                         input_helper.key_foreground(0x23),
                         input_helper.key_foreground(0x08, 24)])
time.sleep(0.4)
shot("05-cleared-foreground")

print("完成。圖在", OUT, flush=True)
