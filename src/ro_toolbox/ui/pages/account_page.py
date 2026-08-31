"""自動登入頁：勾幾個帳號，按一顆按鈕，剩下的自動做完。

## 這一頁刻意做的四件事

- **驗證碼旁邊一定有倒數。** 使用者一眼就能拿它跟手機對照。出事的時候
  （登不進去）才分得出是種子錯還是時間錯 —— 沒有倒數就只能瞎猜。
- **時鐘偏掉就不給碼看。** 偏移超過半個週期時，碼欄顯示「──」而不是一個
  錯的數字。顯示錯的碼會讓使用者手動抄去用，那是安靜地做錯事。
- **帳號檔解不開時，整頁改成唯讀。** 不能讓使用者在「看起來沒有帳號」的
  狀態下按存檔，那一下會把原本的檔案整個蓋掉。
- **勾選存的是帳號名稱不是列號。** 清單排序或增刪之後列號會挪動，
  勾的東西不能綁在位置上（專案鐵則：存身分，不存位置）。

## 目前做到哪

開遊戲 → 合約書 → 帳號密碼 → OTP，**已經串起來了**（`services/auto_login`）。
選伺服器、二次密碼、選角還沒接上（封包版面已知，見 GAMEDATA [PKT-046]）。

⚠ **全程只有合約書那一下會佔用畫面**（約一秒，那個畫面不吃背景訊息，
見 [INP-001]）。其餘都是背景完成，不占鍵盤滑鼠。

登入跑在 worker 執行緒上：整段會等好幾十秒，放 UI 執行緒就是整個程式凍住。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ro_toolbox.config.paths import in_selftest
from ro_toolbox.config.settings import current_settings, save_settings
from ro_toolbox.core.worker import Worker, WorkerThread
from ro_toolbox.services import accounts as account_store
from ro_toolbox.services import game_launcher, timesync, totp
from ro_toolbox.ui.pages.base_page import BasePage
from ro_toolbox.ui.widgets.account_dialog import AccountDialog

log = logging.getLogger(__name__)

_COLUMNS = ("", "顯示名稱", "遊戲帳號", "登入到", "驗證碼", "剩餘", "連線")
_CHECK, _NAME, _USER, _DEST, _CODE, _LEFT, _LINK = range(len(_COLUMNS))

#: 連線欄的兩種樣子。**用讀得到的事實決定**：那個帳號有沒有連線中的遊戲實例
#: （`game_census.account_in_use`），不是「我們剛剛有沒有登入成功」——
#: 登入完之後被踢下線、或使用者自己關掉，欄位都要跟著變。
_LINK_ON = ("✔", "#1f7a4d", "連線中")
_LINK_OFF = ("✘", "#b3261e", "沒有連線")
#: 連線狀態多久查一次。要列舉行程並讀記憶體，不必每 0.2 秒做一次。
_LINK_EVERY_SEC = 3.0
_UNAVAILABLE = "──"
# 偏移超過半個週期，算出來的碼一定是錯的（伺服器也放寬一個窗，但別賭那個）。
_OFFSET_TOLERANCE = 0.5


class _LinkWorker(Worker):
    """查哪些帳號現在有**連線中**的遊戲實例。

    放背景執行緒是因為它要列舉行程、讀每個行程的記憶體認帳號 ——
    放 UI 執行緒上每三秒卡一下，畫面就會鈍（[MEM-031] 那類查詢還可能被擋住）。
    """

    done = Signal(object)      # set[str]：線上的遊戲帳號

    def __init__(self, usernames: list[str]) -> None:
        super().__init__()
        self._usernames = list(usernames)

    def run(self) -> None:
        from ro_toolbox.services import game_census

        online = set()
        try:
            for name in self._usernames:
                if self.should_stop:
                    return
                if game_census.account_in_use(name):
                    online.add(name)
        except Exception as exc:  # noqa: BLE001 - 查不到就當作沒連線，別讓它炸掉整頁
            log.debug("查連線狀態失敗：%s", exc)
        self.done.emit(online)


class _RefreshCharactersWorker(Worker):
    """不開遊戲，直接跟官方伺服器要角色清單（見 services/login_client）。

    ⚠ **這是一次真的登入。** 帳號正在線上的話會被踢下線 —— 所以先問
    `game_census.account_in_use()`，在線上的一律跳過。
    """

    step = Signal(str)
    done = Signal(str)

    def __init__(self, accounts_to_refresh: list, store) -> None:
        super().__init__()
        self._accounts = accounts_to_refresh
        self._store = store

    def run(self) -> None:
        from ro_toolbox.services import accounts as account_store
        from ro_toolbox.services import game_census, login_client

        ok, skipped, failed = 0, [], []
        for account in self._accounts:
            if self.should_stop:
                break
            if not account.password_blob:
                failed.append(f"{account.name}（還沒有登入密文 —— 先用自動登入跑一次）")
                continue
            if game_census.account_in_use(account.username):
                skipped.append(account.name)
                self.step.emit(f"「{account.name}」正在線上，跳過（免得把它踢下線）")
                continue
            self.step.emit(f"更新「{account.name}」的角色清單…")
            try:
                server, characters = login_client.fetch_characters(
                    account, on_step=self.step.emit
                )
            except Exception as exc:  # noqa: BLE001 - 訊息要給使用者看
                failed.append(f"{account.name}（{exc}）")
                continue
            account.remember_characters(characters, server)
            if not account.server:
                account.server = server
            ok += 1
            try:
                account_store.save(self._store)
            except Exception as exc:  # noqa: BLE001 - 存不了不該讓整批失敗
                log.warning("角色清單存檔失敗：%s", exc)

        parts = [f"更新 {ok} 個"]
        if skipped:
            parts.append(f"跳過 {len(skipped)} 個（在線上）")
        if failed:
            parts.append("失敗：" + "、".join(failed))
        if not self.should_stop:
            self.done.emit("；".join(parts))


#: 一個帳號最多開幾次遊戲。第一次沒登進去就**關掉重開**再試一次 ——
#: 客戶端的狀態（記不記得帳號、焦點在哪一格、卡在哪個對話框）重開就乾淨了。
#: 三次還不成就報失敗，不要無限重開（那是使用者說的「無意義等待」）。
_LOGIN_TRIES = 3


class _BatchLoginWorker(Worker):
    """在背景把勾選的帳號**一個一個**登入。

    ⚠ 一定要跑在 worker 執行緒：一個帳號就要三十秒，放 UI 執行緒等於整個程式凍住。

    ## 三條規則（都用讀得到的事實，不猜狀態）

    1. **有連線的實例一律不碰。** 硬登會把使用者正在玩的那個踢下線。
    2. **該帳號已經有連線中的實例 → 跳過。**（用客戶端記下的「送出去的帳號」比對）
    3. **沒有連線的實例（沒登入或已斷線）→ 關掉。**
       我們無法知道它斷在哪一步，接續是賭博、重開是確定的。
    """

    step = Signal(str)
    account_done = Signal(str, bool, str)     # 顯示名稱, 成功?, 說明
    done = Signal(str)                        # 整批的總結

    def __init__(self, accounts_to_login: list, paths, store) -> None:
        super().__init__()
        self._accounts = accounts_to_login
        self._paths = paths
        self._store = store

    def run(self) -> None:
        from ro_toolbox.services import accounts as account_store
        from ro_toolbox.services import game_census, game_launcher
        from ro_toolbox.services.auto_login import AutoLogin

        snapshot = game_census.take()
        for instance in snapshot:
            self.step.emit(f"現況：{instance.label}")
        closed = game_census.close_idle(snapshot)
        if closed:
            self.step.emit(f"關掉沒有連線的實例：{'、'.join(str(p) for p in closed)}")
        game_census.close_stale_launchers()

        ok_count = 0
        skipped = []
        failed = []
        for account in self._accounts:
            if self.should_stop:
                break
            if game_census.account_in_use(account.username):
                self.step.emit(f"「{account.name}」已經在跑，跳過（不強制登入）")
                skipped.append(account.name)
                self.account_done.emit(account.name, True, "已經在跑，跳過")
                continue

            progress = None
            for attempt in range(1, _LOGIN_TRIES + 1):
                if self.should_stop:
                    break
                self.step.emit(
                    f"開始登入「{account.name}」"
                    + (f"（第 {attempt} 次）" if attempt > 1 else "")
                )
                try:
                    pid = game_launcher.launch_game_directly(self._paths)
                except game_launcher.LaunchError as exc:
                    self.step.emit(f"開遊戲失敗：{exc}")
                    progress = None
                    break

                bot = AutoLogin(account, pid, self.step.emit)
                progress = bot.run()
                if progress.ok or attempt >= _LOGIN_TRIES:
                    break
                # ⚠ **失敗就關掉重開，不要在壞掉的畫面上繼續試。**
                #   使用者訂的規則（2026-08-31）：「如果沒有成功連線那就是
                #   官方問題，就直接關閉重開遊戲登入」。實機踩過：帳密打到
                #   別的欄位之後客戶端已經送出去了，畫面回不到登入框，
                #   舊版在那裡重打了 60 秒，最後跳出「請你手動按一次同意」。
                #   留著那個半死的客戶端還會擋住下一次（多開判定、封包混淆）。
                self.step.emit(
                    f"「{account.name}」沒登進去（{progress.summary}）—— 關掉重開再試一次"
                )
                game_census.close(pid)

            if progress is None:
                failed.append(account.name)
                self.account_done.emit(account.name, False, "開遊戲失敗")
                continue
            if bot.password_blob and bot.password_blob != account.password_blob:
                # ⚠ **每次都覆蓋**。這串是密碼轉出來的，改了遊戲密碼舊的就過期；
                # 靠這裡自動換新，使用者不必知道有這回事（見 login_client 檔頭）。
                # 這一段以前漏掉，於是「更新角色清單」那顆按鈕永遠是灰的 ——
                # 而且測試腳本存進去的還會被這裡的存檔蓋掉（踩過）。
                account.password_blob = bot.password_blob
                self.step.emit("記下登入密文（之後可以不開遊戲更新角色清單）")
            if bot.characters or bot.password_blob:
                # 角色清單記到本機，下次就能直接選名字（不必背格號）。
                # ⚠ **要記是哪一台的** —— 每台的角色各自獨立，混在一起會選錯人。
                account.remember_characters(bot.characters, bot.server_name or "")
                if bot.server_name and not account.server:
                    # 設定裡沒填台別的話，把**實際連到**那一台記起來 ——
                    # 這不是猜的，是從連線 IP 認出來的（servers.name_for_ip）。
                    # 帳號＋伺服器＋角色清單要湊成一組，少了台別下次就不知道
                    # 該拿哪一份清單來比對。
                    account.server = bot.server_name
                    self.step.emit(f"記住這個帳號在「{bot.server_name}」")
                if bot.learned_character and not account.character:
                    # 第一次登入是照手填的格號進去的；現在知道那一格是誰了，
                    # 就把**名字**存回設定，之後改用身分查位置。
                    account.character = bot.learned_character
                    self.step.emit(
                        f"記住角色「{bot.learned_character}」——"
                        "下次就從下拉選，不必再背格號"
                    )
                try:
                    account_store.save(self._store)
                except Exception as exc:  # noqa: BLE001 - 存不了不該讓登入算失敗
                    log.warning("角色清單存檔失敗：%s", exc)
                self.step.emit(
                    f"記住「{bot.server_name or '不明伺服器'}」的角色："
                    + "、".join(f"{c.slot} {c.name}" for c in bot.characters)
                )
            if progress.ok:
                ok_count += 1
            else:
                failed.append(account.name)
            self.account_done.emit(account.name, progress.ok, progress.summary)

        parts = [f"成功 {ok_count} 個"]
        if skipped:
            parts.append(f"跳過 {len(skipped)} 個（已在跑）")
        if failed:
            parts.append(f"沒完成 {len(failed)} 個：{'、'.join(failed)}")
        if not self.should_stop:
            self.done.emit("；".join(parts))


class _OffsetWorker(Worker):
    """背景問 NTP。對時要等網路，不能卡在 UI 執行緒上。"""

    result = Signal(float, str)

    def run(self) -> None:
        offset, host = timesync.query_any()
        if not self.should_stop:
            self.result.emit(offset, host)


class AccountPage(BasePage):
    title = "自動登入"
    subtitle = "勾要登入的帳號，按「登入」。帳密與 OTP 種子只存在本機，用 DPAPI 加密。"
    stretch_at_end = False

    # 連線欄的狀態放**類別層級**當預設值：`build()` 中途會跑 `_load()`／`_check_time()`，
    # 它們可能就觸發一次 `_tick`，那時候實例屬性還沒指派（踩過：AttributeError）。
    _link_at = 0.0
    _link_busy = False
    _link_thread = None
    _refresh_thread = None

    def build(self) -> None:
        self._store = account_store.AccountStore()
        self._readonly = False       # 帳號檔解不開時整頁唯讀，防止蓋掉舊資料
        self._offset: float | None = None   # 本機時鐘偏移（秒），None 代表還不知道
        self._offset_thread: WorkerThread | None = None
        self._login_thread: WorkerThread | None = None
        # ⚠ 共用那一份，不要自己 load 一份 —— 主視窗關閉時會把它那份整檔寫回去，
        # 兩份不一致的話這裡存的遊戲路徑會被安靜地蓋掉（見 current_settings）。
        self._settings = current_settings()
        # 勾選存**帳號名稱**不存列號 —— 增刪或排序之後列號會挪動。
        #: 目前勾起來的帳號名稱。**真正的存檔在 `Account.selected`** ——
        #: 這裡只是為了查得快，每次重建清單都從帳號重新算出來。
        self._checked: set[str] = set()

        self.notice = QLabel()
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        self.add(self.notice)

        self.add(self._build_game_path())

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.itemChanged.connect(self._on_item_changed)
        header = self.table.horizontalHeader()
        for column in (_NAME, _USER, _DEST):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        # 勾選格、驗證碼、倒數條給固定寬度。用 ResizeToContents 的話欄寬會縮到
        # 標題那麼窄，裡面的進度條就會撐出去蓋到隔壁欄。
        for column, width in ((_CHECK, 34), (_CODE, 110), (_LEFT, 110),
                              (_LINK, 56)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(column, width)
        self.table.itemSelectionChanged.connect(self._refresh_buttons)
        self.table.doubleClicked.connect(self._edit)
        self.add(self.table)

        self.add(self._build_buttons())

        self._load()
        if not in_selftest():
            # 自檢不對時（那要連外，而且會起背景執行緒）—— 只驗東西在不在。
            self._check_time()

        # 200ms 而不是 1s：碼是在整秒邊界跳的，用秒級輪詢看起來會慢半拍。
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(200)

    def _build_buttons(self) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)

        self.add_btn = QPushButton("新增帳號")
        self.add_btn.setObjectName("primaryButton")
        self.add_btn.clicked.connect(self._add)
        self.edit_btn = QPushButton("編輯")
        self.edit_btn.clicked.connect(self._edit)
        self.remove_btn = QPushButton("刪除")
        self.remove_btn.clicked.connect(self._remove)
        self.copy_btn = QPushButton("複製驗證碼")
        self.copy_btn.clicked.connect(self._copy_code)
        self.resync_btn = QPushButton("重新對時")
        self.resync_btn.clicked.connect(self._check_time)
        self.refresh_btn = QPushButton("更新角色清單")
        self.refresh_btn.setToolTip(
            "不開遊戲，直接跟伺服器要角色清單（約 5 秒）。\n"
            "⚠ 這是一次真的登入 —— 正在線上的帳號會自動跳過，不會把它踢下線。"
        )
        self.refresh_btn.clicked.connect(self._refresh_characters)
        self.login_btn = QPushButton("登入")
        self.login_btn.setObjectName("primaryButton")
        self.login_btn.clicked.connect(self._login)

        # 自動回連：斷線就關遊戲、重開、重新登入，再把斷線前在跑的東西接回去。
        # ⚠ 分得出「你的網路斷了」跟「遊戲斷線」——前者什麼都不做
        #   （關遊戲重開是幫倒忙，重開照樣連不上，人還被登出了）。
        self.auto_reconnect = QCheckBox("自動回連")
        self.auto_reconnect.setToolTip(
            "斷線就自動關遊戲、重開、重新登入，並把斷線前在跑的\n"
            "自動打怪／自動補水／自動尋路接回去。\n"
            "⚠ 你自己的網路斷線時不會動遊戲，只會等它回來。\n"
            "重連失敗會退避（間隔越來越長），不會無腦一直重開。"
        )
        self.auto_reconnect.setChecked(bool(self._settings.auto_reconnect))
        self.auto_reconnect.toggled.connect(self._on_auto_reconnect)

        for button in (
            self.add_btn,
            self.edit_btn,
            self.remove_btn,
            self.copy_btn,
            self.resync_btn,
            self.refresh_btn,
        ):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(self.auto_reconnect)
        row.addWidget(self.login_btn)

        box = QWidget()
        box.setLayout(row)
        return box

    def _on_auto_reconnect(self, on: bool) -> None:
        """開關記在設定檔（跟著程式走，不是跟著角色）。"""
        self._settings.auto_reconnect = bool(on)
        save_settings(self._settings)
        log.info("自動回連：%s", "開啟" if on else "關閉")

    def _build_game_path(self) -> QWidget:
        """遊戲路徑。指到**啟動器**（Ragnarok.exe），遊戲本體從同一層推出來。"""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)

        self.path_edit = QLineEdit(self._settings.game_path)
        self.path_edit.setPlaceholderText(r"D:\ro\RagnarokOnline\Ragnarok.exe")
        self.path_edit.editingFinished.connect(self._save_game_path)

        browse = QPushButton("瀏覽…")
        browse.clicked.connect(self._browse_game_path)

        row.addWidget(QLabel("遊戲路徑"))
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)

        box = QWidget()
        box.setLayout(row)
        return box

    def _browse_game_path(self) -> None:
        start = self._settings.game_path or ""
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇遊戲啟動器（Ragnarok.exe）", start, "執行檔 (*.exe)"
        )
        if path:
            self.path_edit.setText(path)
            self._save_game_path()

    def _save_game_path(self) -> None:
        text = self.path_edit.text().strip().strip('"')
        if text == self._settings.game_path:
            return
        self._settings.game_path = text
        save_settings(self._settings)
        problem = game_launcher.GamePaths(Path(text)).problem() if text else ""
        if problem:
            self._warn(f"遊戲路徑有問題：{problem}")
        else:
            self.notice.hide()

    # ---- 資料 -------------------------------------------------------

    def _load(self) -> None:
        try:
            self._store = account_store.load()
        except account_store.AccountStoreError as exc:
            # 解不開時**不能**當成「沒有帳號」。整頁轉唯讀，讓使用者無法覆蓋。
            self._readonly = True
            self._warn(
                f"帳號檔讀不出來，已切換成唯讀避免蓋掉原本的資料：{exc}　"
                f"檔案位置：{account_store.store_path()}"
            )
            log.error("帳號檔讀取失敗：%s", exc)
        self._rebuild()

    def _persist(self) -> bool:
        try:
            account_store.save(self._store)
        except account_store.AccountStoreError as exc:
            QMessageBox.critical(self, "存檔失敗", str(exc))
            log.error("帳號存檔失敗：%s", exc)
            return False
        return True

    def _rebuild(self) -> None:
        # ⚠ 先歸零再設列數。只呼叫 setRowCount(n) 的話**既有列的 cell widget 會留著** ——
        # 欄位順序一改，上一輪那根進度條就卡在別的欄位上跟文字疊在一起。
        self.table.setRowCount(0)
        self.table.setRowCount(len(self._store.accounts))
        # ⚠ **要在填格子之前算好。** 勾選狀態的真身是 `Account.selected`；
        # 這個集合只是查得快的快取。放到迴圈後面算的話，第一次建表時它還是空的
        # —— 每一格都會畫成沒勾（踩過）。順便把已刪掉的帳號自然排除。
        self._checked = {a.name for a in self._store.accounts if a.selected}
        # 填表期間 itemChanged 會一直觸發，先擋掉才不會把勾選狀態洗掉。
        self.table.blockSignals(True)
        for row, account in enumerate(self._store.accounts):
            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            check.setCheckState(
                Qt.CheckState.Checked
                if account.selected
                else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, _CHECK, check)

            self.table.setItem(row, _NAME, QTableWidgetItem(account.name))
            self.table.setItem(row, _USER, QTableWidgetItem(account.username))

            target = QTableWidgetItem(account.destination)
            if not account.character:
                # 沒填角色不是錯誤（選角還沒實作），但要看得出來還缺東西。
                target.setToolTip("還沒指定伺服器與角色。")
            self.table.setItem(row, _DEST, target)

            code = QTableWidgetItem("")
            font = QFont("Consolas")
            font.setStyleHint(QFont.StyleHint.Monospace)
            font.setPointSize(12)
            font.setBold(True)
            code.setFont(font)
            code.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            code.setToolTip(f"{account.secret.display_name}　{account.secret.params_text}")
            self.table.setItem(row, _CODE, code)

            link = QTableWidgetItem(_LINK_OFF[0])
            link.setForeground(QColor(_LINK_OFF[1]))
            link.setToolTip(_LINK_OFF[2])
            link.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, _LINK, link)

            bar = QProgressBar()
            bar.setTextVisible(True)
            bar.setFormat("%v 秒")
            bar.setFixedHeight(20)
            bar.setMaximum(account.secret.period)
            self.table.setCellWidget(row, _LEFT, bar)
        self.table.blockSignals(False)
        self._tick()
        self._refresh_buttons()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != _CHECK:
            return
        row = item.row()
        if row >= len(self._store.accounts):
            return
        account = self._store.accounts[row]
        checked = item.checkState() == Qt.CheckState.Checked
        if checked:
            self._checked.add(account.name)
        else:
            self._checked.discard(account.name)
        # ⚠ 使用者要求「要記住我勾選的紀錄」。存在帳號自己身上，
        # 下次開程式就照樣是勾好的（見 `Account.selected`）。
        if account.selected != checked:
            account.selected = checked
            self._persist()
        self._refresh_buttons()

    def checked_accounts(self) -> list:
        """勾起來的帳號，照清單順序。用名稱比對，不是列號。"""
        return [a for a in self._store.accounts if a.name in self._checked]

    # ---- 連線狀態 ---------------------------------------------------

    def _tick_links(self) -> None:
        """定期更新「連線」欄。查得慢一點沒關係，但**不能卡住畫面**。"""
        now = time.monotonic()
        if now - self._link_at < _LINK_EVERY_SEC or self._link_busy:
            return
        self._link_at = now
        names = [a.username for a in self._store.accounts]
        if not names:
            return
        self._link_busy = True
        worker = _LinkWorker(names)
        worker.done.connect(self._on_links)
        worker.finished.connect(lambda: setattr(self, "_link_busy", False))
        self._link_thread = WorkerThread(worker)
        self._link_thread.start()

    def _on_links(self, online: object) -> None:
        online = set(online or ())
        for row, account in enumerate(self._store.accounts):
            item = self.table.item(row, _LINK)
            if item is None:
                continue
            mark, colour, tip = (
                _LINK_ON if account.username in online else _LINK_OFF
            )
            item.setText(mark)
            item.setForeground(QColor(colour))
            item.setToolTip(tip)

    # ---- 每一拍 -----------------------------------------------------

    def _tick(self) -> None:
        self._tick_links()
        for row, account in enumerate(self._store.accounts):
            secret = account.secret
            item = self.table.item(row, _CODE)
            bar = self.table.cellWidget(row, _LEFT)
            if item is None or bar is None:
                continue
            if self._clock_bad(secret):
                # 寧可不給，也不給一個錯的。使用者看到數字就會拿去用。
                item.setText(_UNAVAILABLE)
                item.setToolTip("本機時間偏移過大，這組碼一定是錯的。")
                bar.setValue(0)
                continue
            item.setText(totp.generate(secret))
            item.setToolTip("")
            bar.setValue(totp.remaining_seconds(secret))

    def _clock_bad(self, secret: totp.OtpSecret) -> bool:
        if self._offset is None:
            return False  # 還沒對到時就不擋，沒網路的人本來也登入不了
        return abs(self._offset) > secret.period * _OFFSET_TOLERANCE

    # ---- 對時 -------------------------------------------------------

    def _check_time(self) -> None:
        if self._offset_thread is not None and self._offset_thread.is_running:
            return
        self.resync_btn.setEnabled(False)
        worker = _OffsetWorker()
        worker.result.connect(self._on_offset)
        worker.failed.connect(self._on_offset_failed)
        worker.finished.connect(lambda: self.resync_btn.setEnabled(True))
        self._offset_thread = WorkerThread(worker)
        self._offset_thread.start()

    def _on_offset(self, offset: float, host: str) -> None:
        self._offset = offset
        worst = min((a.secret.period for a in self._store.accounts), default=30)
        if abs(offset) > worst * _OFFSET_TOLERANCE:
            self._warn(
                f"{timesync.describe(offset)}（對 {host}）。"
                "驗證碼一定會被打回票，請先校正 Windows 時間再登入。"
            )
        else:
            self.notice.hide()
        self._tick()
        self._refresh_buttons()

    def _on_offset_failed(self, message: str) -> None:
        # 問不到 NTP 不該停用功能：沒網路本來就登入不了，
        # 在這裡多擋一層只會讓錯誤訊息更難懂。
        self._offset = None
        log.info("無法對時：%s", message)
        self._tick()

    def _warn(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.show()

    # ---- 動作 -------------------------------------------------------

    def _selected_row(self) -> int:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else -1

    def _add(self) -> None:
        dialog = AccountDialog(self)
        dialog.characters_updated.connect(self._store_characters)
        if dialog.exec() != AccountDialog.DialogCode.Accepted or dialog.account is None:
            return
        if self._store.index_of(dialog.account.name) >= 0:
            QMessageBox.warning(
                self, "名稱重複", f"已經有一個叫「{dialog.account.name}」的帳號了。"
            )
            return
        self._store.accounts.append(dialog.account)
        if not self._persist():
            self._store.accounts.pop()
        self._rebuild()

    def _edit(self) -> None:
        row = self._selected_row()
        if row < 0 or self._readonly:
            return
        original = self._store.accounts[row]
        dialog = AccountDialog(self, account=original)
        dialog.characters_updated.connect(self._store_characters)
        if dialog.exec() != AccountDialog.DialogCode.Accepted or dialog.account is None:
            return
        self._store.accounts[row] = dialog.account
        if not self._persist():
            self._store.accounts[row] = original
        self._rebuild()

    def _remove(self) -> None:
        row = self._selected_row()
        if row < 0 or self._readonly:
            return
        account = self._store.accounts[row]
        confirm = QMessageBox.question(
            self,
            "刪除帳號",
            f"要刪掉「{account.name}」嗎？\n"
            "OTP 種子也會一起刪除，之後要重新綁定才拿得回來。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._store.accounts.pop(row)
        if not self._persist():
            self._store.accounts.insert(row, account)
        self._rebuild()

    def _copy_code(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        secret = self._store.accounts[row].secret
        if self._clock_bad(secret):
            QMessageBox.warning(
                self, "時間偏移過大", "本機時間偏太多，現在算出來的碼是錯的，不給複製。"
            )
            return
        QGuiApplication.clipboard().setText(totp.generate(secret))

    def _login(self) -> None:
        """把勾選的帳號一個一個登入。

        全程只有**合約書那一下**會佔用畫面（約一秒），其餘都是背景完成。
        已經在跑的帳號會自動跳過（**不強制登入**，免得把正在玩的踢下線）；
        沒有連線的實例會被關掉（我們無法知道它斷在哪一步，重開才是確定的）。
        """
        chosen = self.checked_accounts()
        if not chosen:
            QMessageBox.information(self, "沒有勾選", "先在最左邊勾要登入的帳號。")
            return

        paths = game_launcher.GamePaths(Path(self._settings.game_path))
        problem = paths.problem()
        if problem:
            QMessageBox.warning(self, "遊戲路徑", f"{problem}\n請先在上面設好遊戲路徑。")
            return

        blocked = [a.name for a in chosen if self._clock_bad(a.secret)]
        if blocked:
            QMessageBox.warning(
                self,
                "時間偏移過大",
                "本機時間偏太多，這些帳號的驗證碼一定會被打回票：\n"
                + "、".join(blocked)
                + "\n請先校正 Windows 時間。",
            )
            return

        self.login_btn.setEnabled(False)
        self._warn(f"開始登入 {len(chosen)} 個帳號…")
        worker = _BatchLoginWorker(chosen, paths, self._store)
        worker.step.connect(lambda text: self._warn(f"登入中：{text}"))
        worker.account_done.connect(self._on_account_done)
        worker.done.connect(self._on_batch_done)
        worker.finished.connect(lambda: self.login_btn.setEnabled(True))
        self._login_thread = WorkerThread(worker)
        self._login_thread.start()

    def _store_characters(self, username: str, server: str, characters: object) -> None:
        """對話框在更新完角色清單時發出來的 —— **立刻寫回本機帳號檔**。

        為什麼不等使用者按儲存：他按取消的話那份清單就沒了，
        而那是真的登入一次換來的（見 services/login_client）。
        `remember_characters` 只會換掉**那一台**的清單，別台的保留。
        """
        target = next(
            (a for a in self._store.accounts if a.username == username), None
        )
        if target is None or not server:
            return
        target.remember_characters(list(characters), server)
        if not target.server:
            target.server = server
        try:
            account_store.save(self._store)
        except account_store.AccountStoreError as exc:
            self._warn(f"角色清單存不起來：{exc}")
            return
        self._warn(f"已更新並存檔：{target.name} 在「{server}」的角色清單")

    def _refresh_characters(self) -> None:
        """不開遊戲，直接跟伺服器要勾起來那些帳號的角色清單。"""
        chosen = self.checked_accounts()
        if not chosen:
            QMessageBox.information(self, "沒有勾選", "先在最左邊勾要更新的帳號。")
            return
        blocked = [a.name for a in chosen if self._clock_bad(a.secret)]
        if blocked:
            QMessageBox.warning(
                self, "時間偏移過大",
                "本機時間偏太多，這些帳號的驗證碼一定會被打回票：\n"
                + "、".join(blocked) + "\n請先校正 Windows 時間。",
            )
            return
        self.refresh_btn.setEnabled(False)
        self._warn(f"更新 {len(chosen)} 個帳號的角色清單…")
        worker = _RefreshCharactersWorker(chosen, self._store)
        worker.step.connect(lambda text: self._warn(f"更新中：{text}"))
        worker.done.connect(self._on_refresh_done)
        worker.finished.connect(lambda: self.refresh_btn.setEnabled(True))
        self._refresh_thread = WorkerThread(worker)
        self._refresh_thread.start()

    def _on_refresh_done(self, summary: str) -> None:
        self._warn(f"角色清單更新完成 —— {summary}")
        self._rebuild()

    def _on_account_done(self, name: str, ok: bool, summary: str) -> None:
        self._warn(f"{'✔' if ok else '✘'} {name}：{summary}")

    def _on_batch_done(self, summary: str) -> None:
        # ⚠ **不要彈窗。** 結果就在表格最右邊那一欄（連線中／沒有連線），
        # 而且它是持續更新的事實，比一個要按確定的快照有用。
        self._warn(f"登入結束 —— {summary}")
        self._rebuild()          # 角色清單可能更新了
        self._link_at = 0.0      # 馬上重查一次連線狀態

    def _on_login_done(self, ok: bool, summary: str, steps: str) -> None:
        if ok:
            self._warn(f"登入流程完成：{summary}")
            QMessageBox.information(self, "登入", f"{summary}\n\n{steps}")
        else:
            self._warn(f"登入沒完成 —— {summary}")
            # 失敗時把每一步都給出來，這樣看得出卡在哪一關，不是只有「失敗」兩個字。
            QMessageBox.warning(self, "登入沒完成", f"{summary}\n\n走到哪：\n{steps}")

    def _refresh_buttons(self) -> None:
        has_selection = self._selected_row() >= 0
        self.add_btn.setEnabled(not self._readonly)
        self.refresh_btn.setEnabled(not self._readonly)
        self.edit_btn.setEnabled(has_selection and not self._readonly)
        self.remove_btn.setEnabled(has_selection and not self._readonly)
        copyable = has_selection and not self._clock_bad(
            self._store.accounts[self._selected_row()].secret
        )
        self.copy_btn.setEnabled(copyable)
        self.login_btn.setEnabled(bool(self._checked))

    # ---- 收尾 -------------------------------------------------------

    def shutdown(self) -> None:
        super().shutdown()
        self._timer.stop()
        # 收尾靠 `super().shutdown()` 的**全面掃描**（`BasePage`）——
        # 這裡不再另外列清單：清單會漏，掃描不會。
        # ⚠ 掃描救不了的是「**還在跑就被新的蓋掉**」那種，那個擋在
        # `WorkerThread` 自己身上（`core/worker._RUNNING`）。
