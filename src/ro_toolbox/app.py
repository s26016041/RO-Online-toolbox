"""應用組裝點：建立 QApplication、載入設定與樣式、開主視窗。"""

from __future__ import annotations

import logging
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ro_toolbox import APP_ID, APP_NAME, ORG_NAME
from ro_toolbox.config.paths import RESOURCES_DIR, icon_file, stylesheet_file
from ro_toolbox.config.settings import load_settings
from ro_toolbox.services import input_helper
from ro_toolbox.ui.main_window import MainWindow
from ro_toolbox.utils.logging import setup_logging

log = logging.getLogger(__name__)


def _apply_stylesheet(app: QApplication, theme: str) -> None:
    path = stylesheet_file(theme)
    if not path.exists():
        log.warning("找不到樣式檔：%s", path)
        return
    # QSS 裡的 url() 是相對於**工作目錄**解析的，不是相對於 qss 檔案本身 ——
    # 寫相對路徑的話，從別的目錄啟動就找不到圖（而且 Qt 不會報錯，
    # 只會安靜地不畫）。所以在這裡換成絕對路徑。
    sheet = path.read_text(encoding="utf-8").replace(
        "@RESOURCES@", RESOURCES_DIR.as_posix()
    )
    app.setStyleSheet(sheet)


def _claim_taskbar_identity() -> None:
    """跟 Windows 宣告自己是誰，工作列才會用我們的圖示。

    **這件事跟 `setWindowIcon` 是兩回事。** 視窗左上角吃的是視窗圖示，
    但工作列按鈕是依 AppUserModelID 分組的：不設的話，用 `python.exe`
    跑起來的視窗會被歸到 python.exe 底下，工作列顯示 **Python 的圖示**，
    改視窗圖示完全影響不到。打包成 exe 之後才會自動用 exe 內嵌的圖示。

    必須在**建立任何視窗之前**呼叫，設晚了那一輪不會生效。
    失敗只記一行 —— 圖示不對不影響任何功能，不值得擋住啟動。
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError) as exc:   # 非 Windows shell 或權限受限
        log.debug("設定 AppUserModelID 失敗（工作列圖示可能不對）：%s", exc)


def _apply_icon(app: QApplication) -> None:
    """設在 QApplication 上，所有視窗與工作列都會跟著用。

    找不到圖示檔只記一行 —— 沒有圖示不影響任何功能，不值得擋住啟動。
    """
    path = icon_file()
    if not path.exists():
        log.warning("找不到圖示檔：%s（跑 tools/make_icon.py 產生）", path)
        return
    app.setWindowIcon(QIcon(str(path)))


def create_app(argv: list[str] | None = None) -> tuple[QApplication, MainWindow]:
    """建立 app 與主視窗但不進入事件迴圈，方便測試直接呼叫。"""
    _claim_taskbar_identity()      # 一定要在建立視窗之前
    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    _apply_icon(app)

    settings = load_settings()
    log_bridge = setup_logging(settings.log_level)
    _apply_stylesheet(app, settings.theme)

    window = MainWindow(settings, log_bridge)
    return app, window


def selftest() -> int:
    """冒煙測試：把整個程式組起來但不進事件迴圈，回 0 代表沒問題。

    這支存在的唯一理由是**驗證打包好的 exe**。`--windowed` 的 exe 出事時
    不會有任何訊息，只會開出一個怪怪的視窗，而最容易漏的正是資料檔：
    `assets/*.json.gz` 沒收進去的話，道具名一律查不到、補水選單整個空白，
    程式本身完全不會報錯（見 `config/paths.py` 的 `_bundle_root`）。
    所以這裡不只建視窗，還要**真的查一筆資料**。
    """
    import os

    from ro_toolbox.config.paths import icon_file, stylesheet_file
    from ro_toolbox.services.gamedata import item_name, mob_name
    from ro_toolbox.services.updater import NO_UPDATE_ENV
    from ro_toolbox.ui.main_window import PAGE_CLASSES

    # 自檢是短命的無頭執行：建好視窗就結束，查更新的執行緒來不及收尾
    # 會讓 Qt 直接中止行程。一定要在建視窗**之前**設。
    os.environ[NO_UPDATE_ENV] = "1"

    # 主控台是 cp950，`✓` 這種字元編不進去會直接拋 UnicodeEncodeError ——
    # 冒煙測試自己因為印字而失敗是最蠢的失敗方式，所以輸出一律 ASCII 記號，
    # 並且把 stdout 轉成不會因為編碼而炸的模式。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    problems: list[str] = []
    try:
        _app, window = create_app(["selftest"])
    except Exception as exc:  # noqa: BLE001 - 冒煙測試要把任何失敗變成訊息
        print(f"[NG] 組不起來：{exc}")
        return 1

    pages = window.stack.count()
    if pages != len(PAGE_CLASSES):
        problems.append(f"分頁只載入 {pages}/{len(PAGE_CLASSES)} 個")

    # 資料表：查得到名字才算真的收進去了（501 紅色藥水、1002 波利）
    if item_name(501) in ("", "501", None):
        problems.append("道具表沒收進來（item_name(501) 查不到）")
    if mob_name(1002) in ("", "1002", None):
        problems.append("怪物表沒收進來（mob_name(1002) 查不到）")

    if not icon_file().exists():
        problems.append(f"圖示沒收進來：{icon_file()}")
    if not stylesheet_file("light").exists():
        problems.append(f"樣式表沒收進來：{stylesheet_file('light')}")
    # 漏收箭頭圖不會有任何錯誤訊息，下拉就只是**安靜地沒有箭頭**。
    for arrow in ("arrow-down.svg", "arrow-down-dark.svg"):
        if not (RESOURCES_DIR / arrow).exists():
            problems.append(f"下拉箭頭圖沒收進來：{RESOURCES_DIR / arrow}")

    # 合約書按鈕的樣板。漏收的話自動登入會退回「用視窗大小算比例」——
    # 在別的解析度或對話框被拖過之後就會點空（見 game_screen.find_agree_button）。
    from ro_toolbox.services.game_screen import AGREE_TEMPLATE_FILE
    if not (RESOURCES_DIR / AGREE_TEMPLATE_FILE).exists():
        problems.append(f"合約書按鈕樣板沒收進來：{RESOURCES_DIR / AGREE_TEMPLATE_FILE}")

    # WinDivert 的驅動檔漏收的話，抓封包整個不能用（自動登入的二次密碼、
    # 角色清單全靠它），而錯誤訊息會長得像「相依沒裝好」，很難查。
    from ro_toolbox.services import packet_capture
    ok, why = packet_capture.available()
    if not ok:
        problems.append(f"封包擷取不可用：{why}")
    else:
        import pathlib

        import pydivert
        root = pathlib.Path(pydivert.__file__).parent
        drivers = [p.name for p in root.rglob("*") if p.suffix.lower() in (".dll", ".sys")]
        if not any(name.lower().endswith(".sys") for name in drivers):
            problems.append(f"WinDivert 驅動沒收進來（{root}）")

    for line in problems:
        print(f"[NG] {line}")
    if problems:
        return 1
    print(f"[OK] 分頁 {pages} 個、道具表、怪物表、圖示、樣式表、下拉箭頭、"
          "同意按鈕樣板、WinDivert 都在")
    return 0


def run(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv
    # ⚠ 這一支要在**建 Qt 之前**攔下來：所有送進遊戲的輸入都由它執行。
    # 為什麼要獨立行程：啟動遊戲的那個行程送出第一個輸入之後就會被封鎖
    #（實測，見 services/input_helper 的說明）。
    if input_helper.HELPER_FLAG in args:
        return input_helper.run_helper(args)
    if "--selftest" in args:
        return selftest()
    app, window = create_app(args)
    window.show()
    return app.exec()
