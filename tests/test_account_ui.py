"""帳號頁與新增帳號對話框的介面邏輯。

盯的是「不准安靜地做錯事」那幾條：時鐘偏掉時不給碼、帳號檔解不開時整頁唯讀、
沒驗證過的種子存不下去、換了種子舊的驗證就作廢。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402

from ro_toolbox.services import accounts as account_store  # noqa: E402
from ro_toolbox.services import servers, totp  # noqa: E402
from ro_toolbox.services.accounts import Account, AccountStore, AccountStoreError  # noqa: E402
from ro_toolbox.ui.pages import account_page as page_module  # noqa: E402
from ro_toolbox.ui.pages.account_page import AccountPage  # noqa: E402
from ro_toolbox.ui.widgets.account_dialog import AccountDialog  # noqa: E402

DEMO_B32 = "JBSWY3DPEHPK3PXP"
DEMO_URI = f"otpauth://totp/GRAVITY:RO1-abc?secret={DEMO_B32}&issuer=GRAVITY"


def _account(name: str = "主帳-騎士") -> Account:
    return Account(name, "demo01", "pw", totp.parse(DEMO_URI)[0])


@pytest.fixture
def offline(monkeypatch):
    """別讓測試去打 NTP，也別碰使用者真正的帳號檔。"""
    monkeypatch.setattr(AccountPage, "_check_time", lambda self: None)
    monkeypatch.setattr(account_store, "save", lambda *a, **k: None)


@pytest.fixture
def page(qtbot, offline, monkeypatch):
    monkeypatch.setattr(account_store, "load", lambda *a, **k: AccountStore([_account()]))
    widget = AccountPage()
    qtbot.addWidget(widget)
    yield widget
    widget.shutdown()


def _code_cell(page) -> str:
    """驗證碼欄（欄位順序見 account_page._COLUMNS）。"""
    return page.table.item(0, page_module._CODE).text()


# ---- 頁面 ------------------------------------------------------------------


def test_lists_accounts_with_a_live_code(page):
    assert page.table.rowCount() == 1
    assert page.table.item(0, page_module._NAME).text() == "主帳-騎士"
    assert _code_cell(page) == totp.generate(page._store.accounts[0].secret)
    assert page.table.cellWidget(0, page_module._LEFT).maximum() == 30


def test_bad_clock_hides_the_code_instead_of_showing_a_wrong_one(page):
    """偏移超過半個週期，碼欄顯示「──」。顯示錯的碼會被使用者抄去用。"""
    page._offset = 20.0          # 30 秒週期，偏 20 秒 → 一定算錯
    page._tick()
    assert _code_cell(page) == page_module._UNAVAILABLE

    page._offset = 3.0
    page._tick()
    assert _code_cell(page) == totp.generate(page._store.accounts[0].secret)


def test_copy_is_disabled_when_the_clock_is_bad(page):
    page.table.selectRow(0)
    assert page.copy_btn.isEnabled()
    page._offset = 25.0
    page._refresh_buttons()
    assert not page.copy_btn.isEnabled()


def test_unknown_offset_does_not_block(page):
    """問不到 NTP（沒網路）不該停用功能 —— 沒網路本來就登入不了。"""
    page._on_offset_failed("連不上")
    page.table.selectRow(0)
    page._refresh_buttons()
    assert page._offset is None
    assert page.copy_btn.isEnabled()
    assert _code_cell(page) != page_module._UNAVAILABLE


def test_login_button_needs_a_checked_account(page):
    """沒勾任何帳號就不能按登入 —— 按了不知道要登誰。"""
    page._refresh_buttons()
    assert not page.login_btn.isEnabled()

    page.table.item(0, page_module._CHECK).setCheckState(Qt.CheckState.Checked)
    assert page._checked == {"主帳-騎士"}
    assert page.login_btn.isEnabled()


def test_broken_store_switches_to_readonly(qtbot, offline, monkeypatch):
    """帳號檔解不開時不能讓使用者按存檔，否則會蓋掉原本的資料。"""

    def boom(*_args, **_kwargs):
        raise AccountStoreError("DPAPI 解密失敗（錯誤碼 13）。")

    monkeypatch.setattr(account_store, "load", boom)
    widget = AccountPage()
    qtbot.addWidget(widget)

    assert widget._readonly is True
    # 用 isHidden 而不是 isVisible：整個視窗沒 show 出來的時候，
    # 沒被隱藏的子元件 isVisible() 一樣是 False。
    assert not widget.notice.isHidden()
    assert "唯讀" in widget.notice.text()
    assert not widget.add_btn.isEnabled()
    assert not widget.edit_btn.isEnabled()
    assert not widget.remove_btn.isEnabled()
    widget.shutdown()


# ---- 對話框 ----------------------------------------------------------------


@pytest.fixture
def dialog(qtbot):
    widget = AccountDialog()
    qtbot.addWidget(widget)
    return widget


def _save_button(dialog):
    from PySide6.QtWidgets import QDialogButtonBox

    return dialog.box.button(QDialogButtonBox.StandardButton.Save)


def _fill(dialog, seed: str = DEMO_URI) -> None:
    dialog.name_edit.setText("主帳-騎士")
    dialog.user_edit.setText("demo01")
    dialog.pass_edit.setText("pw")
    dialog.seed_edit.setPlainText(seed)
    dialog._from_text()


def test_save_stays_locked_until_verified(dialog):
    _fill(dialog)
    assert not _save_button(dialog).isEnabled()
    assert "驗證" in _save_button(dialog).toolTip()

    dialog.code_edit.setText(totp.generate(dialog._secret))
    dialog._verify()
    assert dialog._verified is True
    assert _save_button(dialog).isEnabled()


def test_wrong_code_keeps_it_locked(dialog):
    _fill(dialog)
    dialog.code_edit.setText("000000")
    dialog._verify()
    assert dialog._verified is False
    assert not _save_button(dialog).isEnabled()


def test_changing_the_seed_invalidates_the_verification(dialog):
    """換了種子，先前那次驗證證明的是別把鑰匙。"""
    _fill(dialog)
    dialog.code_edit.setText(totp.generate(dialog._secret))
    dialog._verify()
    assert dialog._verified

    dialog.seed_edit.setPlainText("GEZDGNBVGY3TQOJQ")
    dialog._from_text()
    assert dialog._verified is False
    assert not _save_button(dialog).isEnabled()


def test_plain_base32_gets_its_params_measured(dialog):
    """只貼字串時，參數是從手機那組碼實測出來的，不是預設值。"""
    _fill(dialog, seed=DEMO_B32)
    assert dialog._secret.params_confirmed is False

    truth = totp.OtpSecret("", "", dialog._secret.key, "SHA256", 6, 60, params_confirmed=True)
    dialog.code_edit.setText(totp.generate(truth))
    dialog._verify()

    assert dialog._secret.params_confirmed is True
    assert (dialog._secret.algorithm, dialog._secret.period) == ("SHA256", 60)
    assert _save_button(dialog).isEnabled()


def test_rebuild_leaves_no_stale_cell_widgets(page):
    """重建清單不能留下上一輪的進度條 —— 它會卡在別的欄位跟文字疊在一起。"""
    page._store.accounts.append(_account("小帳-商人"))
    page._rebuild()
    page._rebuild()
    for row in range(page.table.rowCount()):
        for column in range(page_module._LEFT):
            assert page.table.cellWidget(row, column) is None
        assert page.table.cellWidget(row, page_module._LEFT) is not None


def test_login_destination_fields_round_trip(dialog):
    """二次密碼、伺服器、角色（登入流程剩下的三關，見 GAMEDATA [PKT-046]）。"""
    _fill(dialog)
    dialog.pin_edit.setText("7342")
    dialog.server_box.setCurrentText("波利")
    dialog.slot_box.setCurrentText("4")
    dialog.code_edit.setText(totp.generate(dialog._secret))
    dialog._verify()
    dialog._accept()

    account = dialog.account
    # 還沒登入過這個帳號 → 只有格號，角色名稱要等登入抓到清單才知道
    assert (account.pin, account.server, account.character) == ("7342", "波利", "")
    assert account.char_slot == 4
    assert account.destination == "波利"


def test_unknown_char_slot_is_none_not_zero(dialog):
    """「未知」不能存成 0 —— 0 是合法的第一格，會安靜地選錯角色。"""
    _fill(dialog)
    dialog.code_edit.setText(totp.generate(dialog._secret))
    dialog._verify()
    dialog._accept()
    assert dialog.account.char_slot is None


def test_accept_builds_the_account(dialog):
    _fill(dialog)
    dialog.code_edit.setText(totp.generate(dialog._secret))
    dialog._verify()
    dialog._accept()

    assert dialog.account is not None
    assert dialog.account.name == "主帳-騎士"
    assert dialog.account.secret.key == totp.decode_base32(DEMO_B32)


def test_editing_an_existing_account_starts_verified(qtbot):
    """只是改個顯示名稱，不該逼使用者再掏一次手機。"""
    widget = AccountDialog(account=_account())
    qtbot.addWidget(widget)
    assert widget._verified is True
    assert _save_button(widget).isEnabled()


def test_seed_box_is_cleared_after_a_successful_parse(dialog):
    """種子收進去之後不留在輸入框裡 —— 畫面上只該看得到遮蔽版。"""
    _fill(dialog)
    assert dialog.seed_edit.toPlainText() == ""
    assert DEMO_B32 not in dialog.seed_label.text()
    assert dialog._secret.masked in dialog.seed_label.text()


def test_bad_seed_text_is_reported_not_swallowed(dialog):
    _fill(dialog, seed="這不是種子!!!")
    assert dialog._secret is None
    assert dialog.seed_label.text()
    assert not _save_button(dialog).isEnabled()


def test_slot_dropdown_shows_slot_and_name(qtbot):
    """登入過之後格號下拉長這樣：「1 雪色狐狸」。但**實際值是數字**。"""
    from ro_toolbox.services.accounts import KnownCharacter

    account = _account()
    account.remember_characters(
        [KnownCharacter("雪色狐狸", 1), KnownCharacter("雪狐u", 2)]
    )
    account.character = "雪狐u"
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)

    # 下拉列表看得到名字
    labels = [dialog.slot_box.itemText(i) for i in range(dialog.slot_box.count())]
    assert labels == ["1 雪色狐狸", "2 雪狐u"]
    # 但收起來只顯示格號 —— 名字是下面那格的事
    assert dialog.slot_box.currentText() == "2"
    assert dialog._slot_value() == 2
    assert dialog.char_name.text() == "雪狐u"


def test_the_character_name_cannot_be_typed(qtbot):
    """角色名稱是查出來的，不給改 —— 打得出清單裡沒有的名字就是安靜的錯。"""
    dialog = AccountDialog()
    qtbot.addWidget(dialog)
    assert dialog.char_name.isReadOnly()


def test_the_server_is_a_fixed_list(qtbot):
    """伺服器只能選現有的台，不給自己打。"""
    dialog = AccountDialog()
    qtbot.addWidget(dialog)
    assert not dialog.server_box.isEditable()
    assert [
        dialog.server_box.itemText(i) for i in range(dialog.server_box.count())
    ] == list(servers.names())


def test_an_unknown_server_in_an_old_file_is_still_shown(qtbot):
    """舊存檔的台別不在清單裡也要列出來 —— 不能安靜地換成別台。"""
    account = _account()
    account.server = "已經關掉的台"
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)
    assert dialog.server_box.currentText() == "已經關掉的台"


def test_saved_character_is_the_name_not_the_label(qtbot):
    """存進去的不能是「1 雪色狐狸」—— 那之後拿去比對角色名會永遠對不上。"""
    from ro_toolbox.services.accounts import KnownCharacter

    account = _account()
    account.remember_characters([KnownCharacter("雪色狐狸", 1)])
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)
    dialog.slot_box.setCurrentIndex(0)
    dialog._accept()
    assert dialog.account.character == "雪色狐狸"
    assert dialog.account.char_slot == 1


def _two_server_account() -> Account:
    """同一個帳號在兩台各有角色 —— 而且**同一個格號是不同的人**（實測如此）。"""
    from ro_toolbox.services.accounts import KnownCharacter

    account = _account()
    account.remember_characters(
        [KnownCharacter("雪色狐狸", 3), KnownCharacter("夜神狐", 0)], "查爾斯"
    )
    account.remember_characters([KnownCharacter("波利小獵人", 3)], "波利")
    return account


def test_character_dropdown_follows_the_selected_server(qtbot):
    """換伺服器，角色清單要跟著換 —— 列出別台的角色會讓人選到別人。"""
    account = _two_server_account()
    account.server = "查爾斯"
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)

    assert [
        dialog.slot_box.itemText(i) for i in range(dialog.slot_box.count())
    ] == ["3 雪色狐狸", "0 夜神狐"]

    dialog.server_box.setCurrentText("波利")
    assert [
        dialog.slot_box.itemText(i) for i in range(dialog.slot_box.count())
    ] == ["3 波利小獵人"]


def test_switching_server_drops_the_old_character(qtbot):
    """換台之後原本那隻不在新清單裡 → 名字要清掉，格號也要丟掉。

    留著名字就等於說「這一台有這隻」，而那是假的；
    留著格號更糟 —— 兩台的同一格是不同的人（實測），照著送就是換成別人。
    """
    account = _two_server_account()
    account.server = "查爾斯"
    account.character = "雪色狐狸"
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)
    assert dialog.char_name.text() == "雪色狐狸"

    dialog.server_box.setCurrentText("波利")
    assert dialog.char_name.text() == ""
    # 波利的第 3 格是「波利小獵人」—— 絕對不能因為格號一樣就選過去
    assert dialog._slot_value() is None


def test_picking_a_slot_fills_in_the_character(qtbot):
    """挑一格，角色名稱跟著查出來。"""
    account = _two_server_account()
    account.server = "查爾斯"
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)

    dialog.slot_box.setCurrentIndex(0)          # 3 雪色狐狸
    assert dialog.slot_box.currentText() == "3"
    assert dialog.char_name.text() == "雪色狐狸"
    dialog.slot_box.setCurrentIndex(1)          # 0 夜神狐
    assert dialog.slot_box.currentText() == "0"
    assert dialog.char_name.text() == "夜神狐"


def test_no_list_for_this_server_says_so(qtbot):
    """沒清單就講清楚，不要生一份猜的選單。"""
    account = _two_server_account()
    account.server = "波利"
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)
    assert dialog.slot_box.count() == 1

    dialog.server_box.addItem("還沒開的新台")
    dialog.server_box.setCurrentText("還沒開的新台")
    assert dialog.slot_box.count() == 0
    assert "還沒登入過" in dialog.char_hint.text()


def test_changing_the_slot_changes_the_character_name(qtbot):
    """換格號，上面的角色名稱要跟著換 —— 不能留著上一隻。"""
    account = _two_server_account()
    account.server = "查爾斯"
    account.character = "雪色狐狸"
    account.char_slot = 3
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)
    assert dialog.char_name.text() == "雪色狐狸"

    dialog.slot_box.setCurrentText("0 夜神狐")
    assert dialog.char_name.text() == "夜神狐"


def test_slot_with_nobody_on_it_shows_unknown(qtbot):
    """填一個沒人的格號 → 角色顯示「未知」，而且不准存成角色名。"""
    account = _two_server_account()
    account.server = "查爾斯"
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)

    dialog.slot_box.setCurrentText("9")
    assert dialog.char_name.text() == "未知"

    dialog._secret = account.secret
    dialog._verified = True
    dialog.name_edit.setText("n")
    dialog.user_edit.setText("u")
    dialog.pass_edit.setText("p")
    dialog._accept()
    # 「未知」是給人看的字，不是身分 —— 存成空字串，登入時照格號現查。
    assert dialog.account.character == ""
    assert dialog.account.char_slot == 9


def test_typed_slot_with_no_list_shows_unknown(qtbot):
    """第一次用還沒有角色清單：格號自己打，名字就顯示「未知」。

    這是正常狀態不是錯誤 —— 登入一次抓到清單之後就會自己填上。
    """
    dialog = AccountDialog()
    qtbot.addWidget(dialog)
    dialog.slot_box.setCurrentText("4")
    assert dialog._slot_value() == 4
    assert dialog.char_name.text() == "未知"


# ---- 連線欄 --------------------------------------------------------------


def test_link_column_starts_as_a_red_cross(page):
    """還沒查到之前一律是紅叉 —— 不准預設綠勾（那等於騙人說連上了）。"""
    from ro_toolbox.ui.pages.account_page import _LINK

    item = page.table.item(0, _LINK)
    assert item.text() == "✘"
    assert item.toolTip() == "沒有連線"


def test_link_column_turns_green_when_the_account_is_online(page):
    """查到那個帳號有連線中的實例就變綠勾。"""
    from ro_toolbox.ui.pages.account_page import _LINK

    page._on_links({page._store.accounts[0].username})
    item = page.table.item(0, _LINK)
    assert item.text() == "✔"
    assert item.toolTip() == "連線中"


def test_link_column_goes_back_to_red_when_it_drops(page):
    """被踢下線、或使用者自己關掉，欄位要跟著變回紅叉 ——
    它顯示的是**現在的事實**，不是「我們剛剛登入成功過」。"""
    from ro_toolbox.ui.pages.account_page import _LINK

    page._on_links({page._store.accounts[0].username})
    page._on_links(set())
    assert page.table.item(0, _LINK).text() == "✘"


def test_finishing_a_batch_does_not_pop_a_dialog(page, monkeypatch):
    """登入完不要彈窗（使用者要求）—— 狀態在表格上看得到。"""
    from PySide6.QtWidgets import QMessageBox

    popped = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: popped.append(a))
    page._on_batch_done("成功 1 個")
    assert popped == []


# ---- 對話框的「更新」按鈕 ------------------------------------------------


def test_refresh_button_is_off_without_the_login_blob(qtbot):
    """沒有登入密文就按不了 —— 而且要說清楚要先做什麼。"""
    account = _account()
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)
    assert not dialog.refresh_btn.isEnabled()
    assert "自動登入" in dialog.refresh_btn.toolTip()


def test_refresh_button_is_on_once_we_have_the_blob(qtbot):
    account = _account()
    account.password_blob = "00" * 24
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)
    assert dialog.refresh_btn.isEnabled()


def test_refreshing_replaces_only_that_server(qtbot):
    """更新「查爾斯」不可以把「波利」的清單洗掉 —— 每台各自獨立。"""
    from ro_toolbox.services.accounts import KnownCharacter

    account = _account()
    account.password_blob = "00" * 24
    account.remember_characters([KnownCharacter("波利小獵人", 3)], "波利")
    account.remember_characters([KnownCharacter("舊的", 0)], "查爾斯")
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)

    dialog._on_characters(
        "查爾斯", [KnownCharacter("夜神狐", 0, "查爾斯")], ""
    )
    names = {(c.server, c.name) for c in dialog._known_characters}
    assert ("查爾斯", "夜神狐") in names
    assert ("查爾斯", "舊的") not in names
    assert ("波利", "波利小獵人") in names, "別台的不准被洗掉"


def test_refreshing_tells_the_page_to_save(qtbot):
    """更新完要**立刻**通知外面存檔 —— 使用者按取消也不該白登一次。"""
    from ro_toolbox.services.accounts import KnownCharacter

    account = _account()
    account.password_blob = "00" * 24
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)
    seen = []
    dialog.characters_updated.connect(
        lambda user, server, chars: seen.append((user, server, list(chars)))
    )

    dialog._on_characters("波利", [KnownCharacter("光狐", 3, "波利")], "")

    assert len(seen) == 1
    user, server, chars = seen[0]
    assert (user, server) == (account.username, "波利")
    assert [c.name for c in chars] == ["光狐"]


def test_a_failed_refresh_says_why_and_changes_nothing(qtbot):
    account = _account()
    account.password_blob = "00" * 24
    dialog = AccountDialog(account=account)
    qtbot.addWidget(dialog)
    before = list(dialog._known_characters)
    seen = []
    dialog.characters_updated.connect(lambda *a: seen.append(a))

    dialog._on_characters("", [], "連不上伺服器")

    assert "連不上伺服器" in dialog.char_hint.text()
    assert dialog._known_characters == before
    assert seen == [], "失敗不准通知存檔"


def test_the_page_writes_the_refreshed_list_back(page):
    """帳號頁收到通知就寫回本機那個帳號 + 那一台的清單。"""
    from ro_toolbox.services.accounts import KnownCharacter

    account = page._store.accounts[0]
    page._store_characters(
        account.username, "查爾斯", [KnownCharacter("夜神狐", 0, "查爾斯")]
    )
    assert [(c.server, c.slot, c.name) for c in account.known_characters] == [
        ("查爾斯", 0, "夜神狐")
    ]
    assert account.server == "查爾斯"


def test_login_stores_the_password_blob(page, monkeypatch):
    """自動登入要把登入密文存回帳號 —— 沒存的話「更新角色清單」永遠按不了。"""
    from ro_toolbox.ui.pages import account_page as page_mod

    account = page._store.accounts[0]
    account.password_blob = ""

    class _FakeBot:
        characters = []
        server_name = "波利"
        learned_character = ""
        password_blob = "ab" * 24

        def __init__(self, *a, **k):
            pass

        def run(self):
            class _P:
                ok = True
                summary = "好了"
            return _P()

    worker = page_mod._BatchLoginWorker([account], object(), page._store)
    monkeypatch.setattr(page_mod, "_BatchLoginWorker", page_mod._BatchLoginWorker)
    import ro_toolbox.services.auto_login as auto_login_mod
    import ro_toolbox.services.game_census as census_mod
    import ro_toolbox.services.game_launcher as launcher_mod
    monkeypatch.setattr(auto_login_mod, "AutoLogin", _FakeBot)
    monkeypatch.setattr(census_mod, "take", lambda: [])
    monkeypatch.setattr(census_mod, "close_idle", lambda snap: [])
    monkeypatch.setattr(census_mod, "close_stale_launchers", lambda: None)
    monkeypatch.setattr(census_mod, "account_in_use", lambda name: False)
    monkeypatch.setattr(launcher_mod, "launch_game_directly", lambda paths: 4242)

    worker.run()

    assert account.password_blob == "ab" * 24


# ---- 勾選要記住（使用者要求）---------------------------------------------


def test_ticking_an_account_is_remembered_on_the_account_itself(page, monkeypatch):
    """⚠ 勾選存在 `Account.selected`，**不是另外存一份名字清單**。

    專案鐵則是「存身分，不存位置」；而另存一份名字清單等於又多一個要同步的
    東西 —— 改名、刪帳號、換順序都得記得去修它，漏掉就是安靜地勾錯人。
    """
    saved = []
    monkeypatch.setattr(page, "_persist", lambda: saved.append(True) or True)
    page.table.item(0, page_module._CHECK).setCheckState(Qt.CheckState.Checked)
    assert page._store.accounts[0].selected is True
    assert saved, "勾了要當場存檔，不然關掉就沒了"


def test_unticking_is_remembered_too(page, monkeypatch):
    monkeypatch.setattr(page, "_persist", lambda: True)
    item = page.table.item(0, page_module._CHECK)
    item.setCheckState(Qt.CheckState.Checked)
    item.setCheckState(Qt.CheckState.Unchecked)
    assert page._store.accounts[0].selected is False


def test_a_saved_tick_comes_back_after_a_restart(qtbot, offline, monkeypatch):
    """下次開程式，勾好的還是勾好的。"""
    account = _account()
    account.selected = True
    monkeypatch.setattr(
        account_store, "load", lambda *a, **k: AccountStore([account])
    )
    widget = AccountPage()
    qtbot.addWidget(widget)
    try:
        assert (
            widget.table.item(0, page_module._CHECK).checkState()
            == Qt.CheckState.Checked
        )
        assert [a.name for a in widget.checked_accounts()] == [account.name]
    finally:
        widget.shutdown()


def test_the_selection_survives_a_round_trip_through_the_file():
    """存檔／讀檔要帶得過去，舊存檔沒有這個欄位就是沒勾。"""
    account = _account()
    account.selected = True
    assert Account.from_dict(account.to_dict()).selected is True

    old = account.to_dict()
    del old["selected"]
    assert Account.from_dict(old).selected is False
