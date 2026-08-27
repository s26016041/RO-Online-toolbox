"""新增／編輯帳號的對話框：貼種子 → 解析 → **驗證** → 才准存。

## 驗證閘門是這支的重點，不是裝飾

「現在手機上顯示的驗證碼」那一格不驗過，儲存鍵就不會亮。它擋三件事：

1. **參數猜錯**：使用者只貼得到一串 base32 時，SHA1/6 碼/30 秒只是最常見，
   不是保證。這裡拿他手機上的碼**實測反查**出真正的參數（`totp.search_params`）。
2. **選錯帳號**：匯出 QR 常常一次包好幾個帳號，點錯一個不會有任何徵兆。
3. **時鐘偏掉**：本機時間偏超過半個週期，算出來的碼永遠不會對。

三件事的共同點是**沒有這道閘門就完全沒有徵兆** —— 使用者要到幾天後登入一直
失敗、還以為是密碼錯，才會發現。這正是專案鐵則裡「安靜地做錯事」那一類。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ro_toolbox.core.worker import Worker, WorkerThread
from ro_toolbox.services import qr, servers, totp
from ro_toolbox.services.accounts import Account, characters_on

log = logging.getLogger(__name__)

#: 格號在清單裡查不到人時，角色那格顯示這個。**不是角色名**，存檔時會清掉。
_UNKNOWN = "未知"

#: `_refresh_characters(keep_slot=...)` 的哨兵：**明確地丟掉**現在的格號。
#: 不能用 None 表示 —— None 已經是「沿用現在這格」的意思了。
_DROP_SLOT = object()

_HINT = (
    "貼上綁定畫面那串英數字、整條 otpauth:// 網址，或直接匯入 QR 圖片。\n"
    "提醒：綁定用的 QR 只會出現那一次，離開頁面就看不到了。"
)


class _FetchCharactersWorker(Worker):
    """背景跟伺服器要角色清單。**不能放 UI 執行緒** —— 要連兩次網路，約 5 秒。"""

    done = Signal(str, object, str)      # 伺服器名稱, 角色清單, 錯誤訊息

    def __init__(self, account) -> None:
        super().__init__()
        self._account = account

    def run(self) -> None:
        from ro_toolbox.services import login_client

        try:
            server, characters = login_client.fetch_characters(self._account)
        except Exception as exc:  # noqa: BLE001 - 訊息要直接給使用者看
            self.done.emit("", [], str(exc))
            return
        self.done.emit(server, characters, "")


class AccountDialog(QDialog):
    """回傳 `account` 屬性；使用者按取消時為 None。

    另外會在角色清單更新完的當下發出 `characters_updated`，讓帳號頁**立刻寫回
    本機的帳號檔** —— 使用者按取消也不該把剛抓回來的清單丟掉。
    """

    #: (遊戲帳號, 伺服器名稱, 角色清單)。帳號頁接到就存檔。
    characters_updated = Signal(str, str, object)

    def __init__(self, parent: QWidget | None = None, account: Account | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("編輯帳號" if account else "新增帳號")
        self.setMinimumWidth(520)

        self.account: Account | None = None
        self._secret: totp.OtpSecret | None = account.secret if account else None
        # 編輯既有帳號時，種子先前已經驗證過，不必為了改個顯示名稱再驗一次。
        # 但只要種子被換掉，這個旗標就會被打回 False（見 _apply_secret）。
        self._verified = bool(account and account.secret.params_confirmed)
        # search_params 回超過一組時，把候選存著等第二次輸入取交集。
        self._pending: list[totp.OtpSecret] = []
        self._known_characters = list(account.known_characters) if account else []
        # `0x0064` 的密碼密文。有它才能不開遊戲跟伺服器要角色清單
        # （見 services/login_client）；新帳號還沒有，要先用自動登入跑一次。
        self._password_blob = account.password_blob if account else ""
        self._refresh_thread = None

        self._build(account)
        self._refresh_state()

    # ---- 版面 -------------------------------------------------------

    def _build(self, account: Account | None) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.name_edit = QLineEdit(account.name if account else "")
        self.name_edit.setPlaceholderText("自己看得懂就好，例如「主帳-騎士」")
        self.user_edit = QLineEdit(account.username if account else "")
        # ⚠ **密碼欄位不遮罩**（使用者要求）。
        # 這是本機的設定畫面，遮起來只會讓人打錯了看不出來 ——
        # 而打錯密碼的症狀是「伺服器說帳密錯誤」，最難查。
        self.pass_edit = QLineEdit(account.password if account else "")

        show_pass = QCheckBox("顯示")
        show_pass.setMinimumWidth(56)   # 不給下限的話「顯示」兩個字會被切掉
        show_pass.setChecked(True)
        show_pass.toggled.connect(
            lambda on: self.pass_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        pass_row = QHBoxLayout()
        pass_row.setContentsMargins(0, 0, 0, 0)
        pass_row.addWidget(self.pass_edit, 1)
        pass_row.addWidget(show_pass)
        pass_box = QWidget()
        pass_box.setLayout(pass_row)

        form.addRow("顯示名稱", self.name_edit)
        form.addRow("遊戲帳號", self.user_edit)
        form.addRow("密碼", pass_box)
        layout.addLayout(form)

        layout.addWidget(self._build_destination(account))

        # ---- OTP 種子 ----
        seed_title = QLabel("OTP 種子")
        seed_title.setObjectName("cardTitle")
        layout.addWidget(seed_title)

        hint = QLabel(_HINT)
        hint.setObjectName("pageSubtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        self.paste_btn = QPushButton("從剪貼簿貼上 QR")
        self.paste_btn.clicked.connect(self._from_clipboard)
        self.file_btn = QPushButton("選圖檔…")
        self.file_btn.clicked.connect(self._from_file)
        if not qr.available():
            # 沒有解碼器就把按鈕停用並說原因，不要讓它按了沒反應。
            for btn in (self.paste_btn, self.file_btn):
                btn.setEnabled(False)
                btn.setToolTip(qr.MISSING_MESSAGE)
        buttons.addWidget(self.paste_btn)
        buttons.addWidget(self.file_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.seed_edit = QPlainTextEdit()
        self.seed_edit.setPlaceholderText("JBSW Y3DP EHPK 3PXP　或　otpauth://totp/...")
        self.seed_edit.setFixedHeight(56)
        layout.addWidget(self.seed_edit)

        parse_row = QHBoxLayout()
        self.parse_btn = QPushButton("解析")
        self.parse_btn.clicked.connect(self._from_text)
        parse_row.addWidget(self.parse_btn)
        parse_row.addStretch(1)
        layout.addLayout(parse_row)

        self.seed_label = QLabel()
        self.seed_label.setWordWrap(True)
        layout.addWidget(self.seed_label)

        # ---- 驗證閘門 ----
        verify_title = QLabel("驗證")
        verify_title.setObjectName("cardTitle")
        layout.addWidget(verify_title)

        verify_row = QHBoxLayout()
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("現在手機上顯示的驗證碼")
        self.code_edit.setMaxLength(8)
        self.code_edit.returnPressed.connect(self._verify)
        self.verify_btn = QPushButton("驗證")
        self.verify_btn.clicked.connect(self._verify)
        verify_row.addWidget(self.code_edit, 1)
        verify_row.addWidget(self.verify_btn)
        layout.addLayout(verify_row)

        self.verify_label = QLabel()
        self.verify_label.setWordWrap(True)
        layout.addWidget(self.verify_label)

        self.box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save = self.box.button(QDialogButtonBox.StandardButton.Save)
        save.setText("儲存")
        save.setObjectName("primaryButton")
        self.box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.box.accepted.connect(self._accept)
        self.box.rejected.connect(self.reject)
        layout.addWidget(self.box)

        for edit in (self.name_edit, self.user_edit, self.pass_edit):
            edit.textChanged.connect(self._refresh_state)

    def _build_destination(self, account: Account | None) -> QWidget:
        """登入流程剩下的三關：二次密碼、伺服器、角色（見 GAMEDATA [PKT-046]）。

        ## 一組是「帳號＋伺服器＋角色清單」，不是「帳號＋角色清單」

        每一台伺服器的角色**各自獨立**：實測同一個帳號在查爾斯與波利看到完全
        不同的角色，而且**同一個格號在兩台是不同的人**。所以角色清單一定要跟著
        上面選的伺服器變 —— 列出別台的角色讓人選，選下去就是安靜地登入到別人。

        ## 三格各自能做什麼

        - **伺服器**：只能從清單選（`servers.KNOWN`）。打字沒有意義 ——
          打出一個不存在的台別，登入時只會停在選角，不如一開始就不給打。
        - **角色格號**：可以下拉、也可以自己打數字（第一次登入還沒有清單，
          那時候只填得出格號）。
        - **角色名稱**：**唯讀**。它是拿格號去清單裡查出來的結果，不是輸入 ——
          能打字就代表能打出一個清單裡沒有的名字，那是安靜的錯。
        """
        box = QGroupBox("登入目標")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # 二次密碼同樣不遮罩（使用者要求）。
        self.pin_edit = QLineEdit(account.pin if account else "")
        self.pin_edit.setMaxLength(8)
        self.pin_edit.setPlaceholderText("進遊戲前那道數字密碼（實測四位）")

        self.server_box = QComboBox()          # ⚠ 不可編輯：只能選現有的台
        self.server_box.addItems(servers.names())
        current_server = (account.server if account else "") or ""
        if current_server and self.server_box.findText(current_server) < 0:
            # 舊存檔裡的台別不在現在的清單裡（改版換名？）——
            # 還是要列出來讓人看得到自己設定的是什麼，不能安靜地換成別台。
            self.server_box.addItem(current_server)
        if current_server:
            self.server_box.setCurrentIndex(self.server_box.findText(current_server))
        elif not servers.known():
            self.server_box.setToolTip(servers.UNKNOWN_HINT)

        # 格號下拉顯示成「1 雪狐」，實際值是數字。可編輯 —— 第一次登入只能自己打。
        self.slot_box = QComboBox()
        self.slot_box.setEditable(True)
        self.slot_box.lineEdit().setPlaceholderText("第幾格（0 起算）")
        # 自動補完會把打進去的「3」補成「3 雪色狐狸」，那正是我們不要的顯示。
        self.slot_box.setCompleter(None)
        self.slot_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.slot_box.setToolTip(
            "第一次登入還沒有角色清單，只能自己填格號。\n"
            "送出前會拿它跟伺服器當場給的清單對一次，對不上就停在選角畫面。\n"
            "之後改用角色名稱現查 —— 存位置會選錯角色。"
        )

        # 角色名稱是**查出來的**，不給改。存進帳號的身分就是這一格。
        self.char_name = QLineEdit()
        self.char_name.setReadOnly(True)
        self.char_name.setPlaceholderText("選了格號就會自己填上")
        self.char_name.setToolTip(
            "拿左邊的格號去這一台的角色清單裡查出來的，不給手動改 ——\n"
            "打得出清單裡沒有的名字，登入時只會停在選角畫面。"
        )

        self.char_hint = QLabel()
        self.char_hint.setObjectName("pageSubtitle")
        self.char_hint.setWordWrap(True)

        form.addRow("二次密碼", self.pin_edit)
        form.addRow("伺服器", self.server_box)
        self.refresh_btn = QPushButton("更新")
        self.refresh_btn.setToolTip(
            "不開遊戲，直接跟伺服器要這個帳號的角色清單（約 5 秒）。\n"
            "⚠ 這是一次真的登入 —— 帳號正在線上時會拒絕，不會把它踢下線。"
        )
        self.refresh_btn.setEnabled(bool(self._password_blob))
        if not self._password_blob:
            self.refresh_btn.setToolTip(
                "還沒有這個帳號的登入密文 —— 先用「登入」跑一次自動登入，\n"
                "工具會順手把它記下來，之後就能直接更新清單了。"
            )
        self.refresh_btn.clicked.connect(self._refresh_characters)

        slot_row = QHBoxLayout()
        slot_row.setContentsMargins(0, 0, 0, 0)
        slot_row.addWidget(self.slot_box, 1)
        slot_row.addWidget(self.refresh_btn)
        slot_box_holder = QWidget()
        slot_box_holder.setLayout(slot_row)
        form.addRow("角色格號", slot_box_holder)
        form.addRow("角色名稱", self.char_name)
        form.addRow("", self.char_hint)

        self._refresh_characters_list(
            account.character if account else "",
            account.char_slot if account else None,
        )
        self.server_box.currentTextChanged.connect(self._server_changed)
        self.slot_box.currentTextChanged.connect(lambda _: self._slot_changed())
        return box

    def _refresh_characters(self) -> None:
        """跟伺服器要角色清單（不開遊戲）。跑在背景，按鈕先鎖起來。

        ⚠ 這是一次**真的登入**：帳號在線上時直接拒絕，不去踢它下線。
        """
        from ro_toolbox.services import game_census
        from ro_toolbox.services.accounts import Account

        username = self.user_edit.text().strip()
        if not username or self._secret is None:
            self._say_verify("要先有遊戲帳號與 OTP 種子才能更新清單。", ok=False)
            return
        if game_census.account_in_use(username):
            self._say_verify(
                "這個帳號正在線上 —— 現在更新會把它踢下線，所以不做。", ok=False
            )
            return

        probe = Account(
            name=self.name_edit.text().strip() or username,
            username=username,
            password=self.pass_edit.text(),
            secret=self._secret,
            password_blob=self._password_blob,
            server=self._value_of(self.server_box),
        )
        self.refresh_btn.setEnabled(False)
        self.char_hint.setText("跟伺服器要角色清單中…")
        worker = _FetchCharactersWorker(probe)
        worker.done.connect(self._on_characters)
        worker.finished.connect(lambda: self.refresh_btn.setEnabled(True))
        self._refresh_thread = WorkerThread(worker)
        self._refresh_thread.start()

    def _on_characters(self, server: str, characters: object, error: str) -> None:
        """更新回來了。**只換那一台的清單**，別台的保留。"""
        if error:
            self.char_hint.setText(f"更新失敗：{error}")
            return
        from ro_toolbox.services.accounts import characters_on

        keep = [
            c for c in self._known_characters
            if server and c.server and c.server != server
        ]
        self._known_characters = keep + list(characters)
        if server and self.server_box.findText(server) >= 0:
            self.server_box.setCurrentIndex(self.server_box.findText(server))
        self._refresh_characters_list()
        got = characters_on(self._known_characters, server)
        self.char_hint.setText(
            f"已更新「{server}」：" + ("、".join(c.name for c in got) or "沒有角色")
        )
        # ⚠ **立刻通知外面存檔。** 只更新對話框裡那份的話，使用者按取消就沒了 ——
        # 而他剛剛才為了這份清單真的登入了一次，丟掉等於白登。
        self.characters_updated.emit(
            self.user_edit.text().strip(), server, list(characters)
        )

    def _server_changed(self, _text: str) -> None:
        """換了伺服器：名字（身分）留著試查，**舊格號一律丟掉**。

        格號是位置，跨台完全沒有意義 —— 實測兩台的同一格是不同的人。
        照著舊格號在新台上挑一個出來，就是安靜地換成別人。
        """
        self._refresh_characters_list(keep_slot=_DROP_SLOT)

    def _refresh_characters_list(self, keep: str | None = None, keep_slot=None) -> None:
        """依照現在選的伺服器重建格號下拉，並把角色名稱查出來。

        `keep` 是要保留的角色名稱（預設沿用現在顯示的）—— 換台之後那隻通常
        不在新清單裡，這時**留著名字但不對應到任何一格**，讓使用者看得出
        「這個名字在這一台查不到」，而不是被安靜地換成同格號的另一個人。
        """
        server = self._value_of(self.server_box)
        known = characters_on(self._known_characters, server)
        wanted = self._character_name() if keep is None else keep
        if keep_slot is None:
            slot = self._slot_value()
        elif keep_slot is _DROP_SLOT:
            slot = None
        else:
            slot = keep_slot

        blocked = self.slot_box.blockSignals(True)
        self.slot_box.clear()
        for entry in known:
            self.slot_box.addItem(f"{entry.slot} {entry.name}", entry.slot)

        # 先拿**名字**查（身分優先）；名字對不上才退回格號（第一次登入只有它）。
        match = next((e for e in known if e.name == wanted), None) if wanted else None
        if match is not None:
            self.slot_box.setCurrentIndex(self.slot_box.findData(match.slot))
            self.slot_box.setEditText(str(match.slot))
        else:
            target = self.slot_box.findData(slot) if slot is not None else -1
            if target >= 0:
                self.slot_box.setCurrentIndex(target)
            self.slot_box.setEditText("" if slot is None else str(slot))
        self.slot_box.blockSignals(blocked)

        where = f"「{server}」" if server else "這個帳號"
        if known:
            self.char_hint.setText(
                f"{where}記到 {len(known)} 隻：" + "、".join(e.name for e in known)
            )
        else:
            self.char_hint.setText(
                f"還沒登入過{where} —— 先自己填角色格號，"
                "登入一次就會抓到清單，之後直接下拉選。"
            )
        self._sync_character()

    def _slot_changed(self) -> None:
        """格號那一格變了。

        選好之後**只留數字**（「3」），名字交給下面的「角色名稱」顯示 ——
        下拉打開時列的仍然是「3 雪色狐狸」，那時候才需要看名字。
        Qt 選完會把整串塞進輸入框，所以這裡收一次；收完會再進來一次，
        那時候文字已經相等，就往下走去查名字（不會無限繞）。
        """
        # ⚠ 一律看**文字**，不要看 currentIndex：自己打字的時候 Qt 不一定會把
        # 舊的選取清掉，照著它收就會把使用者剛打的字改回上一個選擇。
        text = self.slot_box.currentText().strip()
        slot = self._slot_value()
        if slot is not None and text != str(slot):
            self.slot_box.setEditText(str(slot))
            return
        self._sync_character()

    def _known_here(self):
        return characters_on(self._known_characters, self._value_of(self.server_box))

    def _character_name(self) -> str:
        """角色名稱那一格的**實際值**。「未知」是顯示用的字，不是身分。"""
        text = self.char_name.text().strip()
        return "" if text == _UNKNOWN else text

    def _slot_value(self) -> int | None:
        """格號的**實際值**。打的字不是數字就回 None。

        None 代表「沒指定」—— 登入時會停在選角畫面，不會拿 0 去頂替。
        0 是合法的第一格，把「不知道」寫成 0 就是安靜地選錯角色。
        """
        # 收起來的時候輸入框是純數字（「3」），下拉列表才是「3 雪色狐狸」——
        # 兩種都要吃得下，所以一律取第一段來看是不是數字。
        text = self.slot_box.currentText().strip()
        digits = text.split()[0] if text else ""
        return int(digits) if digits.isdigit() else None

    def _sync_character(self) -> None:
        """把角色名稱查出來填進去。查不到就是「未知」。

        名稱那格是唯讀的，唯一的來源就是「這一台的清單 + 現在的格號」。
        所以查不到時**一定要寫「未知」**，不能留著上一次的名字 ——
        留著就等於說「這一台的這一格是那隻」，而那是假的。
        還沒登入過這一台（根本沒有清單）也算查不到：第一次用本來就是這樣，
        使用者自己打格號，名字等登入抓到清單再說。
        """
        slot = self._slot_value()
        if slot is None:
            self.char_name.setText("")
            return
        entry = next((e for e in self._known_here() if e.slot == slot), None)
        self.char_name.setText(entry.name if entry is not None else _UNKNOWN)

    @staticmethod
    def _value_of(combo: QComboBox) -> str:
        """拿下拉的**實際值**（不是顯示文字）。

        伺服器那格顯示什麼就是什麼，但格號那格顯示的是「1 雪狐」而值是 1 ——
        直接拿顯示文字去比對台別／角色名，會永遠對不上。
        """
        text = combo.currentText().strip()
        index = combo.currentIndex()
        if index >= 0 and combo.itemText(index) == text:
            return str(combo.itemData(index) or text)
        return text

    # ---- 種子來源 ---------------------------------------------------

    def _from_clipboard(self) -> None:
        clipboard = QGuiApplication.clipboard()
        image = clipboard.image()
        if image.isNull():
            # 使用者也可能是複製了一段文字（otpauth 網址）而不是圖。
            text = clipboard.text().strip()
            if text:
                self.seed_edit.setPlainText(text)
                self._from_text()
                return
            self._say_seed(qr.describe_failure(image), ok=False)
            return
        self._decode(lambda: qr.decode_image(image), lambda: qr.describe_failure(image))

    def _from_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇 QR 圖檔", "", "圖片 (*.png *.jpg *.jpeg *.bmp *.gif *.webp)"
        )
        if not path:
            return
        self._decode(lambda: qr.decode_file(path), lambda: f"「{path}」裡找不到 QR。")

    def _decode(self, decode, on_empty) -> None:
        try:
            texts = decode()
        except qr.QrError as exc:
            self._say_seed(str(exc), ok=False)
            return
        if not texts:
            self._say_seed(on_empty(), ok=False)
            return
        self.seed_edit.setPlainText(texts[0])
        self._from_text()

    def _from_text(self) -> None:
        raw = self.seed_edit.toPlainText().strip()
        if not raw:
            self._say_seed("還沒貼上任何種子。", ok=False)
            return
        try:
            secrets = totp.parse(raw)
        except totp.OtpError as exc:
            self._say_seed(str(exc), ok=False)
            return

        if len(secrets) > 1:
            # 匯出 QR 一張可以包好幾個帳號。**不准自作主張挑第一個** ——
            # 挑錯了畫面上一切正常，只有登入會失敗。
            names = [f"{s.display_name}（{s.params_text}）" for s in secrets]
            chosen, ok = QInputDialog.getItem(
                self, "這張 QR 裡有多個帳號", "選擇要匯入哪一個：", names, 0, False
            )
            if not ok:
                return
            secret = secrets[names.index(chosen)]
        else:
            secret = secrets[0]
        self._apply_secret(secret)

    def _apply_secret(self, secret: totp.OtpSecret) -> None:
        changed = self._secret is None or secret.key != self._secret.key
        self._secret = secret
        self._pending = []
        if changed:
            # 換了種子就得重驗，先前那次驗證證明的是別把鑰匙。
            self._verified = False
            self.code_edit.clear()
            self.verify_label.clear()

        if not self.name_edit.text().strip() and secret.label:
            self.name_edit.setText(secret.label)
        if not self.user_edit.text().strip() and secret.label:
            self.user_edit.setText(secret.label)

        # 解析完就把輸入框清掉：種子已經收進 `_secret` 了，沒有理由讓它以明文
        # 留在畫面上（截圖、直播、旁邊有人看都算）。摘要那行顯示的是遮蔽版。
        self.seed_edit.clear()

        detail = f"已讀取：{secret.display_name}　種子 {secret.masked}"
        if secret.params_confirmed:
            self._say_seed(f"{detail}　參數 {secret.params_text}（來源：{secret.source}）", ok=True)
        else:
            self._say_seed(
                f"{detail}　**參數未知** —— 這串沒有帶參數，"
                "要用你手機上現在的驗證碼實測出來。",
                ok=True,
            )
        self.code_edit.setFocus()
        self._refresh_state()

    # ---- 驗證 -------------------------------------------------------

    def _verify(self) -> None:
        if self._secret is None:
            self._say_verify("還沒有種子可以驗。", ok=False)
            return
        code = "".join(ch for ch in self.code_edit.text() if ch.isdigit())
        if not code:
            self._say_verify("請輸入手機上現在顯示的那組數字。", ok=False)
            return

        if self._pending:
            self._resolve_pending(code)
            return

        if self._secret.params_confirmed:
            if totp.verify(self._secret, code):
                self._pass()
            else:
                self._say_verify(
                    "對不上。可能是：QR 選到別的帳號、碼打錯了，"
                    "或本機時間偏掉超過半個週期。工具算出來的是 "
                    f"{totp.generate(self._secret)}。",
                    ok=False,
                )
            return

        # 參數不明：拿這組碼反查。
        matches = totp.search_params(self._secret, code)
        if not matches:
            self._say_verify(
                "所有常見的參數組合都對不上。種子可能貼錯，或本機時間偏掉太多。", ok=False
            )
        elif len(matches) == 1:
            self._secret = matches[0]
            self._pass(f"實測確認參數為 {matches[0].params_text}。")
        else:
            # 撞到多組的機率很低但不是零。這時**不准挑一個用**。
            self._pending = matches
            self.code_edit.clear()
            self._say_verify(
                f"有 {len(matches)} 組參數都對得上，還分不出來："
                + "、".join(m.params_text for m in matches)
                + "。請等手機跳下一組碼，再輸入一次。",
                ok=False,
            )

    def _resolve_pending(self, code: str) -> None:
        """第二組碼：拿它跟上一輪的候選取交集。"""
        survivors = [m for m in self._pending if totp.verify(m, code, window=0)]
        if len(survivors) == 1:
            self._secret = survivors[0]
            self._pending = []
            self._pass(f"兩次實測後確認參數為 {survivors[0].params_text}。")
        elif not survivors:
            self._pending = []
            self._say_verify("第二組碼一組都對不上，重來一次。", ok=False)
        else:
            self.code_edit.clear()
            self._say_verify(
                f"還是有 {len(survivors)} 組對得上，再等下一組碼輸入一次。", ok=False
            )
            self._pending = survivors

    def _pass(self, extra: str = "") -> None:
        self._verified = True
        self._say_verify(f"驗證通過。{extra}".strip(), ok=True)
        self._refresh_state()

    # ---- 狀態 -------------------------------------------------------

    def _say_seed(self, text: str, ok: bool) -> None:
        self.seed_label.setText(text)
        self.seed_label.setStyleSheet("color:#1f7a4d;" if ok else "color:#b3261e;")

    def _say_verify(self, text: str, ok: bool) -> None:
        self.verify_label.setText(text)
        self.verify_label.setStyleSheet("color:#1f7a4d;" if ok else "color:#b3261e;")

    def _refresh_state(self) -> None:
        ready = bool(
            self.name_edit.text().strip()
            and self.user_edit.text().strip()
            and self.pass_edit.text()
            and self._secret is not None
            and self._verified
        )
        save = self.box.button(QDialogButtonBox.StandardButton.Save)
        save.setEnabled(ready)
        if ready:
            save.setToolTip("")
        elif self._secret is None:
            save.setToolTip("還沒有 OTP 種子。")
        elif not self._verified:
            save.setToolTip("OTP 還沒驗證。先用手機上的驗證碼確認一次。")
        else:
            save.setToolTip("顯示名稱、遊戲帳號、密碼都要填。")

    def _accept(self) -> None:
        if self._secret is None or not self._verified:
            return
        self.account = Account(
            name=self.name_edit.text().strip(),
            username=self.user_edit.text().strip(),
            password=self.pass_edit.text(),
            secret=self._secret,
            pin=self.pin_edit.text().strip(),
            server=self._value_of(self.server_box),
            # 「未知」不是角色名，`_character_name` 會把它換成空字串 ——
            # 登入時就走「照格號當場現查」那條路。
            character=self._character_name(),
            char_slot=self._slot_value(),
            # 角色快取跟著帳號走，編輯對話框不動它 —— 它只由登入時的
            # 伺服器清單整份覆蓋（見 Account.remember_characters）。
            known_characters=list(self._known_characters),
        )
        self.accept()
