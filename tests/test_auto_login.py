"""自動登入狀態機：每一步都要等**真的訊號**，失敗要說得出卡在哪。

這一支不需要遊戲：把畫面判定與封包流換成假的，測流程與失敗訊息。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

pytest.importorskip("PySide6.QtGui")

from ro_toolbox.services import (
    auto_login,  # noqa: E402
    game_screen,  # noqa: E402
)
from ro_toolbox.services import input as game_input  # noqa: E402
from ro_toolbox.services.accounts import Account  # noqa: E402
from ro_toolbox.services.auto_login import AutoLogin  # noqa: E402
from ro_toolbox.services.game_screen import Stage  # noqa: E402
from ro_toolbox.services.totp import parse  # noqa: E402

SECRET = "otpauth://totp/GRAVITY:demo?secret=JBSWY3DPEHPK3PXP&issuer=GRAVITY"


#: 0x0064 的內容：version(4) + 帳號[24] + 密碼[24] + clienttype(1)。
#: 帳號欄是明文，狀態機會拿它驗證「字有沒有打到對的欄位」。
def _login_payload(account: str = "demo01") -> bytes:
    name = account.encode("ascii")
    return bytes(4) + name + bytes(24 - len(name)) + bytes(24) + bytes(1)


@dataclass
class FakePacket:
    opcode: int
    payload: bytes = b""


def _account() -> Account:
    return Account("測試", "demo01", "pw", parse(SECRET)[0])


@pytest.fixture
def fast(monkeypatch):
    """把等待縮短，測試才不會真的等幾十秒。"""
    for name in (
        "_PACKET_TIMEOUT",
        "_CREDENTIAL_TIMEOUT",
        "_INPUT_TIMEOUT",
        "_OTP_TIMEOUT",
        "_OTP_STEP_TIMEOUT",
        "_PIN_SEED_TIMEOUT",
        "_PIN_SOCKET_TIMEOUT",
        "_PIN_REPLY_TIMEOUT",
        "_ZONE_TIMEOUT",
        "_CHAR_LIST_TIMEOUT",
        "_CHAR_LIST_QUIET",
        "_BLOB_TIMEOUT",
    ):
        if hasattr(auto_login, name):
            monkeypatch.setattr(auto_login, name, 0.4)
    monkeypatch.setattr(auto_login, "_POLL", 0.02)
    monkeypatch.setattr(auto_login, "_OTP_MIN_SECONDS", 0)


@pytest.fixture
def wired(monkeypatch, fast):
    """把外部相依全部換成假的，並記錄送出去的輸入。"""
    sent: list[str] = []
    monkeypatch.setattr(game_input, "ensure_dpi_aware", lambda: True)
    monkeypatch.setattr(game_screen, "find_window", lambda pid: 0x1234)
    monkeypatch.setattr(game_screen, "is_minimised", lambda hwnd: False)
    monkeypatch.setattr(
        game_screen, "agree_button_position",
        lambda hwnd, ratio=None: (10, 20),
    )
    # 所有輸入都走 input_helper 子行程（實測：主行程送第一次之後就被封鎖）。
    def _fake_send(hwnd, actions):
        for action in actions:
            if "click" in action:
                sent.append("click")
            elif "text" in action:
                sent.append(f"type:{action['text']}")
            elif "text_fg" in action:
                sent.append(f"type:{action['text_fg']}")
            elif "key" in action or "key_fg" in action:
                vk = action.get("key", action.get("key_fg"))
                sent.append("enter" if vk == 0x0D else f"key:{vk}")

    monkeypatch.setattr(auto_login.input_helper, "send", _fake_send)

    # 流程的兩個訊號都是「客戶端連到哪」：
    #   帳密送出 → 連上登入伺服器
    #   OTP 過了 → 換到角色伺服器
    # 測試裡照這個順序給。
    # 兩個訊號都是「客戶端連到哪」：
    #   帳密送出 → 連上登入伺服器
    #   OTP 過了 → 換到角色伺服器
    # ⚠ 只有**送出六位數字**才算 OTP —— 早期版本看到任何 Enter 就切，
    # 結果帳密那一下就把伺服器換掉，OTP 這關永遠看不到變化。
    state = {"otp_sent": False}

    def _watch(hwnd, actions):
        _fake_send(hwnd, actions)
        for action in actions:
            value = action.get("text") or action.get("text_fg") or ""
            if len(value) == 6 and value.isdigit():
                state["otp_sent"] = True

    monkeypatch.setattr(auto_login.input_helper, "send", _watch)
    monkeypatch.setattr(
        auto_login,
        "find_server",
        lambda pid: ("2.2.2.2", 6121) if state["otp_sent"] else ("1.2.3.4", 6900),
    )

    return sent


class _Capture:
    """假的擷取器：用背景執行緒把封包**陸續**餵進去。

    不能在 start() 就全部推完 —— `_wait_packet` 是「只等之後新來的」，
    那是刻意的（先前那一輪的回應不能算數）。全部先塞好的話等於沒送。
    """

    def __init__(self, script):
        self.script = list(script)
        self.sink = None
        self._thread = None

    def factory(self, pid, sink, **_kw):
        self.sink = sink
        return self

    def start(self):
        import threading

        def feed():
            for opcode in self.script:
                time.sleep(0.05)
                self.sink(FakePacket(opcode, _login_payload() if opcode == 0x0064 else b''))

        self._thread = threading.Thread(target=feed, daemon=True)
        self._thread.start()
        return True

    def stop(self, *_a, **_k):
        pass


def _run(monkeypatch, stages, packets, sent_ref=None):
    # 自動登入絕對不准自己截圖：真的呼叫到就當場炸掉，不要安靜地過。
    def _forbidden(*_a, **_k):
        raise AssertionError("AutoLogin 不准自己截圖（[INP-009]）")

    monkeypatch.setattr(game_screen, "capture", _forbidden)
    cap = _Capture(packets)
    monkeypatch.setattr(auto_login, "PacketCapture", cap.factory)
    return AutoLogin(_account(), 4242).run()


def test_happy_path(monkeypatch, wired):
    """合約書 → 登入畫面 → 帳密 → OTP，全部訊號都到。

    三個 stage：合約書、點完回頭確認過了、再讓 `_wait_stage` 確認一次。
    """
    result = _run(
        monkeypatch,
        [Stage.EULA, Stage.LOGIN, Stage.LOGIN],
        [0x0064, 0x0A73, 0x0A74, 0x0B60],
    )
    assert result.ok, result.summary
    assert "click" in wired                       # 合約書按了
    assert "type:demo01" in wired                 # 帳號打了
    assert wired.count("enter") >= 2              # 帳密 + OTP


def test_retries_the_whole_batch_until_the_client_connects(monkeypatch, wired):
    """「按下去」不等於「按到了」。沒連上就整組重來（再點一次同意、再打一次）。

    實測踩過：視窗剛畫出來就點，合約書還沒鋪好，那一下落空，
    後面的字全部掉進黑洞 —— 而且完全沒有錯誤訊息。
    """
    calls = {"n": 0}
    real = auto_login.find_server

    def flaky(pid):
        calls["n"] += 1
        return None if calls["n"] <= 2 else real(pid)

    result = _run(monkeypatch, [], [])
    monkeypatch.setattr(auto_login, "find_server", flaky)
    assert result.ok, result.summary
    assert wired.count("click") >= 1, "沒有點過同意"


def test_always_clicks_agree_and_it_is_harmless(monkeypatch, wired):
    """每一輪都先點「同意」，不先判斷有沒有合約書。

    判斷畫面要截圖，而截圖跟送輸入不能在同一個行程做；沒有合約書的時候
    那一下落在背景圖上，無害。成敗一律由「客戶端有沒有連上伺服器」決定。
    """
    result = _run(monkeypatch, [], [])
    assert result.ok, result.summary
    assert "click" in wired


def test_minimised_window_is_refused(monkeypatch, wired):
    """最小化的視窗收不到輸入，要當場說清楚而不是硬送。"""
    monkeypatch.setattr(game_screen, "is_minimised", lambda hwnd: True)
    result = _run(monkeypatch, [Stage.LOGIN], [])
    assert not result.ok
    assert "最小化" in result.detail


def test_no_connection_means_the_typing_never_landed(monkeypatch, wired):
    """打了字但客戶端沒連上伺服器 → 字沒進去，不要謊稱成功。

    「有沒有連上」是**客戶端實際做了什麼**，比等封包可靠 ——
    登入這一段的連線是後來才建立的，封包擷取常常整段漏掉（實測抓到 0 個）。
    """
    monkeypatch.setattr(auto_login, "find_server", lambda pid: None)
    monkeypatch.setattr(auto_login, "_INPUT_TIMEOUT", 1.0)
    result = _run(monkeypatch, [], [])
    assert not result.ok
    assert "沒有連上" in result.detail


def test_sent_account_mismatch_is_caught(monkeypatch, wired):
    """有擷取到 0x0064 的話要複驗帳號欄 —— 送出去的必須是我們要的那個帳號。"""
    class WrongAccount:
        def factory(self, pid, sink, **_kw):
            self.sink = sink
            return self

        def start(self):
            self.sink(FakePacket(0x0064, _login_payload("olddemo01")))
            return True

        def stop(self, *_a, **_k):
            pass

    monkeypatch.setattr(auto_login, "PacketCapture", WrongAccount().factory)
    result = AutoLogin(_account(), 4242).run()
    assert not result.ok
    assert "olddemo01" in result.detail


def test_bad_otp_is_reported(monkeypatch, wired):
    """OTP 送出去了但客戶端沒換伺服器 → 驗證碼不對（多半是時鐘偏移）。"""
    monkeypatch.setattr(auto_login, "find_server", lambda pid: ("1.2.3.4", 6900))
    monkeypatch.setattr(auto_login, "_OTP_TIMEOUT", 1.0)
    result = _run(monkeypatch, [], [])
    assert not result.ok
    assert "沒有換到下一台伺服器" in result.detail
    assert "時間偏移" in result.detail


def test_progress_records_every_step(monkeypatch, wired):
    """失敗時這串就是診斷報告。"""
    result = _run(monkeypatch, [], [])
    assert any("視窗" in s for s in result.steps)
    assert any("連上" in s for s in result.steps)
    assert any("OTP" in s for s in result.steps)


def test_packets_arriving_together_are_not_skipped(monkeypatch, wired):
    """0x0064 與伺服器回的 0x0A73 **時間戳相同**（實測）。

    用「從現在往後看」的話等完前者就會錯過後者，然後誤報成「帳號或密碼不對」。
    這一條把「依序消費、不跳號」釘住。
    """

    class Burst:
        """四個封包**一次全到**，模擬同一瞬間的批次。"""

        def factory(self, pid, sink, **_kw):
            self.sink = sink
            return self

        def start(self):
            for op in (0x0064, 0x0A73, 0x0A74, 0x0B60):
                self.sink(FakePacket(op, _login_payload() if op == 0x0064 else b''))
            return True

        def stop(self, *_a, **_k):
            pass

    monkeypatch.setattr(auto_login, "PacketCapture", Burst().factory)
    result = AutoLogin(_account(), 4242).run()
    assert result.ok, result.summary


def test_typing_into_the_wrong_field_is_caught(monkeypatch, wired):
    """0x0064 的帳號欄是明文 —— 送錯帳號要當場抓出來。

    少了這一條，「打到密碼欄」「舊帳號沒清乾淨」都只會表現成
    「伺服器說登入失敗」，看不出真正原因。
    """

    class WrongAccount:
        def factory(self, pid, sink, **_kw):
            self.sink = sink
            return self

        def start(self):
            # 送出去的帳號是「舊帳號＋新帳號」黏在一起
            self.sink(FakePacket(0x0064, _login_payload("olddemo01")))
            return True

        def stop(self, *_a, **_k):
            pass

    monkeypatch.setattr(auto_login, "PacketCapture", WrongAccount().factory)
    result = AutoLogin(_account(), 4242).run()
    assert not result.ok
    assert "olddemo01" in result.detail
    assert "打到別的欄位" in result.detail


# ---- 欄位驗證：帳號與密碼都要對得上 ----------------------------------------



# ---- 選角：讓客戶端自己選（我們只負責把游標移對、確認、按 Enter）----


@dataclass
class FakeChar:
    slot: int
    name: str


class _FakeClient:
    """假的選角畫面。模型照實機：

    - 方向鍵移動游標（一格一格，讀得到現在停在第幾格）。
    - **按 Enter 才會把名字寫進記憶體**，同時客戶端自己送 0x0066。
    - 空的格子按下去什麼都不會發生（名字不會被寫）。
    """

    def __init__(self, chars, start=0, stuck=False, writes=True):
        self.chars = {c.slot: c.name for c in chars}
        self.cursor = start
        self.stuck = stuck          # 模擬「按了游標不動」
        self.writes = writes        # 模擬「按了名字沒寫進來」
        self.name = ""
        self.entered = None         # 客戶端自己送出去的格號

    def press(self, vk):
        if vk == 0x27 and not self.stuck:          # →
            self.cursor = min(self.cursor + 1, 14)
        elif vk == 0x25 and not self.stuck:        # ←
            self.cursor = max(self.cursor - 1, 0)
        elif vk == 0x0D:                           # Enter
            self.entered = self.cursor
            if self.writes and self.cursor in self.chars:
                self.name = self.chars[self.cursor]


def _at_character_select(
    monkeypatch, account, characters, server="查爾斯", reply=0x0AC5,
    start=0, stuck=False, writes=True, wrong_name=None,
):
    """做一個「已經連上、角色清單也收到了」的狀態機，只測選角這一步。

    `reply`：伺服器對客戶端選角的回應（0x0AC5 成功／0x006C 拒絕／None 不回）。
    `start`：游標一開始停在第幾格。
    `stuck`：方向鍵按了游標不動（畫面根本不在選角）。
    `writes`：按下 Enter 客戶端會不會把名字寫進記憶體。
    `wrong_name`：客戶端寫進去的是別隻的名字（模擬偏移失效）。
    """
    from ro_toolbox.services import character as character_module

    client = _FakeClient(characters, start=start, stuck=stuck, writes=writes)
    bot = AutoLogin(account, 4321, lambda _: None)
    bot.server_name = server
    bot.characters = list(characters)

    class _FakeScreen:
        def __init__(self, pid):
            self.pid = pid

        ready = True
        address = 0x1000

        def index(self):
            return client.cursor

        def read(self):
            return wrong_name if (wrong_name and client.name) else client.name

        def close(self):
            pass

    monkeypatch.setattr(character_module, "SelectScreen", _FakeScreen)

    def send(hwnd, actions):
        for action in actions:
            if "key" in action:
                client.press(action["key"])
                if action["key"] == 0x0D and reply is not None:
                    bot._packets.append(FakePacket(reply, bytes([3])))

    monkeypatch.setattr(auto_login.input_helper, "send", send)
    return bot, client


def test_first_login_uses_the_typed_slot_and_learns_the_name(fast, monkeypatch):
    """第一次登入沒有清單，使用者填格號 —— 對得上就選，並且把名字學起來。"""
    account = _account()
    account.char_slot = 3
    bot, client = _at_character_select(
        monkeypatch, account, [FakeChar(0, "夜神狐"), FakeChar(3, "雪色狐狸")]
    )

    bot._select_character(0x1234)

    assert client.entered == 3, "客戶端自己按下去的要是第 3 格"
    assert bot.learned_character == "雪色狐狸"
    assert not bot.progress.stopped_at_character


def test_first_login_with_an_empty_slot_stops_there(fast, monkeypatch):
    """使用者填的格子上沒有角色 → 停在選角畫面，**不准亂按**。"""
    account = _account()
    account.char_slot = 7
    bot, client = _at_character_select(monkeypatch, account, [FakeChar(0, "夜神狐")])

    bot._select_character(0x1234)

    assert client.entered is None
    assert bot.learned_character == ""
    assert "格號 7" in bot.progress.stopped_at_character
    assert "夜神狐" in bot.progress.stopped_at_character


def test_no_character_list_means_no_guessing(fast, monkeypatch):
    """清單沒讀到就不准照格號按 —— 那等於閉著眼睛猜位置。"""
    account = _account()
    account.char_slot = 3
    bot, client = _at_character_select(monkeypatch, account, [])

    bot._select_character(0x1234)

    assert client.entered is None
    assert "讀不到角色清單" in bot.progress.stopped_at_character


def test_known_name_wins_over_the_stale_slot(fast, monkeypatch):
    """有名字就一律拿名字現查 —— 舊格號是位置，換台之後完全對不上。"""
    from ro_toolbox.services.accounts import KnownCharacter

    account = _account()
    account.server = "查爾斯"
    account.character = "雪色狐狸"
    account.char_slot = 4          # 波利留下來的舊值，這一台沒有第 4 格
    account.remember_characters([KnownCharacter("雪色狐狸", 2)], "查爾斯")
    bot, client = _at_character_select(
        monkeypatch, account, [FakeChar(2, "雪色狐狸")]
    )

    bot._select_character(0x1234)

    assert client.entered == 2


def test_it_walks_the_cursor_to_the_right_slot(fast, monkeypatch):
    """游標從別的地方開始也要走得過去，而且**只按到目標格就停**。"""
    account = _account()
    account.character = "雪色狐狸"
    account.server = "查爾斯"
    from ro_toolbox.services.accounts import KnownCharacter

    account.remember_characters([KnownCharacter("雪色狐狸", 1)], "查爾斯")
    bot, client = _at_character_select(
        monkeypatch, account, [FakeChar(1, "雪色狐狸")], start=6
    )

    bot._select_character(0x1234)

    assert client.entered == 1, "要一路走回第 1 格才按"


def test_a_cursor_that_will_not_move_stops_the_whole_thing(fast, monkeypatch):
    """按了游標不動＝畫面根本不在選角。**絕對不能繼續按 Enter。**"""
    account = _account()
    account.char_slot = 3
    bot, client = _at_character_select(
        monkeypatch, account, [FakeChar(3, "雪色狐狸")], start=0, stuck=True
    )

    bot._select_character(0x1234)

    assert client.entered is None
    assert "移不到" in bot.progress.stopped_at_character


def test_the_client_must_confirm_by_writing_the_name(fast, monkeypatch):
    """按下去之後客戶端沒把名字寫進來 → 不能當成成功。"""
    account = _account()
    account.char_slot = 3
    bot, _client = _at_character_select(
        monkeypatch, account, [FakeChar(3, "雪色狐狸")], start=3, writes=False
    )

    bot._select_character(0x1234)

    assert "沒有寫下選到的角色名字" in bot.progress.stopped_at_character


def test_a_name_that_does_not_match_means_the_offset_died(fast, monkeypatch):
    """客戶端寫的是別隻 → 游標那個偏移失效了（改版），大聲停用。

    格號來自伺服器的角色清單、名字來自客戶端，兩份獨立的資料互相驗證。
    """
    account = _account()
    account.char_slot = 3
    bot, _client = _at_character_select(
        monkeypatch, account, [FakeChar(3, "雪色狐狸")], start=3,
        wrong_name="夜神狐",
    )

    bot._select_character(0x1234)

    assert "夜神狐" in bot.progress.stopped_at_character
    assert "失效" in bot.progress.stopped_at_character


def test_select_without_the_zone_address_is_not_success(fast, monkeypatch):
    """按下去了不等於進去了 —— 沒收到 0x0AC5 就要講出來。"""
    account = _account()
    account.char_slot = 3
    bot, client = _at_character_select(
        monkeypatch, account, [FakeChar(3, "雪色狐狸")], start=3, reply=None
    )

    bot._select_character(0x1234)

    assert client.entered == 3, "還是要按下去"
    assert "沒等到地圖台位址" in bot.progress.stopped_at_character


def test_refused_enter_is_reported(fast, monkeypatch):
    """伺服器明白拒絕（0x006C）要把原因碼報出來。"""
    account = _account()
    account.char_slot = 3
    bot, _client = _at_character_select(
        monkeypatch, account, [FakeChar(3, "雪色狐狸")], start=3, reply=0x006C
    )

    bot._select_character(0x1234)

    assert "拒絕" in bot.progress.stopped_at_character


def test_stopping_at_character_select_is_not_reported_as_done(fast, monkeypatch):
    """停在選角要在總結講出來，不能混在「登入流程送出完成」裡。"""
    account = _account()
    bot, client = _at_character_select(monkeypatch, account, [FakeChar(0, "夜神狐")])

    bot._select_character(0x1234)
    bot.progress.ok = True

    assert client.entered is None
    assert bot.progress.summary.startswith("已登入，停在選角畫面")


# ---- 二次密碼：伺服器說過了才算過 ----------------------------------------


def _pin_bot(monkeypatch, packets, pin="8291"):
    account = _account()
    account.pin = pin
    bot = AutoLogin(account, 4321, lambda _: None)
    bot._packets.extend(packets)
    monkeypatch.setattr(bot, "_char_server_socket", lambda: 99)
    monkeypatch.setattr(auto_login.game_socket, "close_socket", lambda sock: None)
    return bot


def _pin_state_packet(state: int, seed: int = 0x05760EA1, aid: int = 0x016B510B):
    return FakePacket(
        0x08B9,
        seed.to_bytes(4, "little") + aid.to_bytes(4, "little")
        + state.to_bytes(2, "little"),
    )


def _login_accepted(aid: int = 0x016B510B):
    # 0x0B60：長度(2) + login_id1(4) + AID(4) + …
    return FakePacket(0x0B60, bytes(2) + bytes(4) + aid.to_bytes(4, "little") + bytes(8))


def test_pin_needs_the_all_zero_reply(fast, monkeypatch):
    """實機對照：輸入正確時伺服器回一包全零的 0x08B9。"""
    bot = _pin_bot(monkeypatch, [_pin_state_packet(1), _login_accepted()])

    def send(sock, data):
        bot._packets.append(FakePacket(0x08B9, bytes(10)))   # 全零＝通過

    monkeypatch.setattr(auto_login.game_socket, "send_on_socket", send)
    assert bot._send_pin(1234) is True


def test_wrong_pin_stops_before_the_character_select(fast, monkeypatch):
    """被退回就不准往下選角 —— 那一步會把角色卡在登入中。"""
    bot = _pin_bot(monkeypatch, [_pin_state_packet(1), _login_accepted()])

    def send(sock, data):
        bot._packets.append(_pin_state_packet(8))            # 不是 0 就是沒過

    monkeypatch.setattr(auto_login.game_socket, "send_on_socket", send)
    assert bot._send_pin(1234) is False


def test_no_reply_at_all_is_not_a_pass(fast, monkeypatch):
    """等不到回應也不算過。逾時只能當放棄的上限，不能當成功的依據。"""
    bot = _pin_bot(monkeypatch, [_pin_state_packet(1), _login_accepted()])
    monkeypatch.setattr(auto_login.game_socket, "send_on_socket", lambda s, d: None)
    assert bot._send_pin(1234) is False


def test_the_ask_packet_alone_never_counts_as_confirmation(fast, monkeypatch):
    """送出前那包「請輸入」不能被當成確認。"""
    bot = _pin_bot(monkeypatch, [_pin_state_packet(1), _login_accepted()])

    def send(sock, data):
        bot._packets.append(_pin_state_packet(1))            # 又問一次
        bot._packets.append(_pin_state_packet(8))            # 然後說錯了

    monkeypatch.setattr(auto_login.game_socket, "send_on_socket", send)
    assert bot._send_pin(1234) is False


# ---- 合約書：點不掉就跟使用者學位置 ----------------------------------------


def test_saved_agree_ratio_is_used(monkeypatch):
    """設定裡學過位置就用它，不要用內建預設值。"""
    from ro_toolbox.config import settings as settings_module

    bot = AutoLogin(_account(), 1, lambda _: None)
    monkeypatch.setattr(
        settings_module, "_current",
        settings_module.AppSettings(agree_button=[0.4, 0.7]),
    )
    assert bot._agree_ratio() == (0.4, 0.7)


def test_a_broken_saved_ratio_falls_back_to_the_builtin(monkeypatch):
    """存壞了（超出視窗）就當沒學過 —— 不准拿它去點螢幕外面。"""
    from ro_toolbox.config import settings as settings_module

    bot = AutoLogin(_account(), 1, lambda _: None)
    for bad in ([1.4, 0.7], [0.4], [-0.1, 0.5], None):
        monkeypatch.setattr(
            settings_module, "_current",
            settings_module.AppSettings(agree_button=bad),
        )
        assert bot._agree_ratio() is None, bad


def test_learning_records_where_the_user_clicked(monkeypatch, tmp_path):
    """合約書消失的那一瞬間，游標在哪就把那裡記成按鈕（存比例不存座標）。"""
    import ctypes

    from ro_toolbox.config import settings as settings_module

    monkeypatch.setattr(settings_module, "config_file", lambda: tmp_path / "s.json")
    monkeypatch.setattr(settings_module, "_current", settings_module.AppSettings())

    bot = AutoLogin(_account(), 1, lambda _: None)
    stages = [game_screen.Stage.EULA, game_screen.Stage.EULA, game_screen.Stage.LOGIN]
    monkeypatch.setattr(game_screen, "capture", lambda hwnd: object())
    monkeypatch.setattr(game_screen, "detect", lambda img: stages.pop(0))
    monkeypatch.setattr(
        game_screen, "window_ratio_of", lambda hwnd, x, y: (x / 1000, y / 500)
    )

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def _cursor(ref):
        point = ctypes.cast(ref, ctypes.POINTER(_Point)).contents
        point.x, point.y = 400, 300
        return 1

    monkeypatch.setattr(ctypes.windll.user32, "GetCursorPos", _cursor)
    assert bot._learn_agree_button(0x1234, timeout=5.0) is True
    assert settings_module.current_settings().agree_button == [0.4, 0.6]


def test_an_account_without_a_pin_can_still_reach_character_select(fast, monkeypatch):
    """二次密碼**可以不設定**（使用者確認）。那種帳號伺服器根本不會問 ——
    硬要等一包「通過」的話，帳號好好的卻永遠停在選角畫面。"""
    account = _account()
    account.pin = ""
    bot = AutoLogin(account, 4321, lambda _: None)
    bot._packets.extend([_login_accepted()])          # 完全沒有 0x08B9
    assert bot._send_pin(1234) is True


def test_a_pin_prompt_without_a_configured_pin_stops(fast, monkeypatch):
    """伺服器在問、我們卻沒有 —— 停手並講清楚要去哪裡填。"""
    account = _account()
    account.pin = ""
    bot = AutoLogin(account, 4321, lambda _: None)
    bot._packets.extend([_pin_state_packet(1), _login_accepted()])
    assert bot._send_pin(1234) is False


def test_a_server_that_says_pin_ok_needs_no_pin(fast, monkeypatch):
    account = _account()
    account.pin = ""
    bot = AutoLogin(account, 4321, lambda _: None)
    bot._packets.extend([FakePacket(0x08B9, bytes(10))])
    assert bot._send_pin(1234) is True


def test_the_password_blob_is_grabbed_from_the_login_packet(fast):
    """從 0x0064 把密碼密文記下來 —— 有它才能不開遊戲更新角色清單。"""
    account = _account()
    bot = AutoLogin(account, 4321, lambda _: None)
    bot._packets.append(FakePacket(0x0064, _login_payload("demo01")))
    bot._grab_password_blob()
    assert bot.password_blob == _login_payload("demo01")[28:52].hex()


def test_another_accounts_login_packet_is_ignored(fast):
    """多開時別人的 0x0064 也會進到擷取裡 —— 記錯帳號的密文比沒記更糟。"""
    account = _account()
    bot = AutoLogin(account, 4321, lambda _: None)
    bot._packets.append(FakePacket(0x0064, _login_payload("someoneelse")))
    bot._grab_password_blob()
    assert bot.password_blob == ""
