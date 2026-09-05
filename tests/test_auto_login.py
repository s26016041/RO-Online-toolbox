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


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    """每一條測試都從「什麼都沒記過」的設定開始。

    ⚠ `AppSettings` 是模組層級的單例，測試之間會互相污染 ——
    前一條測試寫進去的東西會讓後面那條測到完全不同的行為（實際踩過）。
    """
    from ro_toolbox.config import settings as settings_module

    monkeypatch.setattr(settings_module, "_current", settings_module.AppSettings())
    monkeypatch.setattr(settings_module, "save_settings", lambda s: None)


@pytest.fixture
def fast(monkeypatch):
    """把等待縮短，測試才不會真的等幾十秒。"""
    for name in (
        "_PACKET_TIMEOUT",
        "_CREDENTIAL_TIMEOUT",
        "_INPUT_TIMEOUT",
        "_OTP_TIMEOUT",
        "_OTP_STEP_TIMEOUT",
        "_OTP_SENT_TIMEOUT",
        "_OTP_ACCEPT_TIMEOUT",
        "_OTP_PROMPT_TIMEOUT",
        "_PIN_SEED_TIMEOUT",
        "_PIN_SOCKET_TIMEOUT",
        "_PIN_REPLY_TIMEOUT",
        "_ZONE_TIMEOUT",
        "_CHAR_LIST_TIMEOUT",
        "_CHAR_LIST_QUIET",
        "_BLOB_TIMEOUT",
        "_PROBE_TIMEOUT",
        "_FIELD_CHECK_TIMEOUT",
    ):
        if hasattr(auto_login, name):
            monkeypatch.setattr(auto_login, name, 0.4)
    monkeypatch.setattr(auto_login, "_POLL", 0.02)
    # 選角那一段要容得下「頭幾下被吃掉」，不能縮到只按得了一下。
    monkeypatch.setattr(auto_login, "_SELECT_MOVE_TIMEOUT", 3.0)
    monkeypatch.setattr(auto_login, "_SELECT_MOVE_WAIT", 0.1)
    monkeypatch.setattr(auto_login, "_SELECT_KEY_PAUSE", 0.02)
    monkeypatch.setattr(auto_login, "_OTP_MIN_SECONDS", 0)
    # 帳密這一關的預算要**容得下好幾輪**：合約書那幾輪現在很便宜（看一眼、
    # 按一下就回頭再看），0.4 秒會讓第一輪按完就逾時，測不到後面的流程。
    monkeypatch.setattr(auto_login, "_INPUT_TIMEOUT", 3.0)


@pytest.fixture
def wired(monkeypatch, fast):
    """把外部相依全部換成假的，並記錄送出去的輸入。"""
    sent: list[str] = []
    # 假的世界裡遊戲一直活著。真的死活檢查另外有測（見檔案結尾）。
    monkeypatch.setattr(auto_login, "_process_alive", lambda pid: True)
    monkeypatch.setattr(auto_login, "_window_alive", lambda hwnd: True)
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
    # 預設「看畫面」就回登入畫面 —— 沒有這一行的話會真的去開子行程，
    # 而且新規則「認不出畫面就先別打字」會讓那些測試整個空轉。
    monkeypatch.setattr(
        auto_login.input_helper, "look_at_screen",
        lambda hwnd: game_screen.ScreenReport(Stage.LOGIN, (10, 20), "測試畫面", 165.0),
    )

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

    #: 伺服器**不可能在客戶端送出認證碼之前**回這兩包，所以假的擷取器也不准。
    #: （`_send_otp` 現在會確認「這一次打完之後」客戶端有沒有真的送 `0x0A74`。）
    AFTER_OTP = (0x0A74, 0x0B60)

    def __init__(self, script, gate=None):
        self.script = list(script)
        self.sink = None
        self._thread = None
        self._gate = gate

    def factory(self, pid, sink, **_kw):
        self.sink = sink
        return self

    def start(self):
        import threading

        def feed():
            for opcode in self.script:
                if opcode in self.AFTER_OTP and self._gate is not None:
                    self._gate.wait(60)   # 帳密那幾輪重試就要 20 秒，等短了會提前放行
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
    _looks_like(monkeypatch, stages)
    cap = _Capture(packets, gate=_otp_gate(monkeypatch))
    monkeypatch.setattr(auto_login, "PacketCapture", cap.factory)
    return AutoLogin(_account(), 4242).run()


def _otp_gate(monkeypatch):
    """回一個 Event：客戶端**真的把六位數打出去**的那一刻才會被設起來。

    假的擷取器拿它當閘門 —— 這樣 `0x0A74`／`0x0B60` 才會落在
    「打完認證碼之後」，跟真實世界一樣。以前是 start() 就全部推完，
    於是「這一次到底有沒有送出去」永遠驗不到。
    """
    import threading

    gate = threading.Event()
    inner = auto_login.input_helper.send

    def _send(hwnd, actions):
        inner(hwnd, actions)
        for action in actions:
            text = action.get("text_fg", action.get("text", ""))
            if len(text) == 6 and text.isdigit():
                gate.set()

    monkeypatch.setattr(auto_login.input_helper, "send", _send)
    return gate


def _looks_like(monkeypatch, stages, agree=(10, 20)):
    """安排子行程「看畫面」會回報什麼。

    ⚠ 畫面判定一律來自**子行程**（截圖與送輸入不能同一個行程，[INP-009]），
    所以測試要換掉的是 `input_helper.look_at_screen`，不是 `game_screen.detect`。
    腳本用完之後就停在最後一個 —— 真實世界也是這樣，畫面不會自己變回去。
    """
    script = list(stages) or [Stage.LOGIN]

    def _look(hwnd):
        stage = script.pop(0) if len(script) > 1 else script[0]
        return game_screen.ScreenReport(stage, agree, "測試畫面")

    monkeypatch.setattr(auto_login.input_helper, "look_at_screen", _look)
    return script


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

    result = _run(monkeypatch, [Stage.EULA, Stage.LOGIN], [])
    monkeypatch.setattr(auto_login, "find_server", flaky)
    assert result.ok, result.summary
    assert wired.count("click") >= 1, "沒有點過同意"


def test_it_does_not_click_when_it_can_see_the_login_screen(monkeypatch, wired):
    """★ 認得出已經在登入畫面就**不要再點同意**。

    ⚠ 這一條跟舊版相反（舊版：「每一輪都先點，點空了也無害」）。
    使用者實機踩過，那一下**不是無害的**：合約書早就過了，工具照樣拿
    「猜的位置」往畫面上點，最後還請他「手動按一次同意」——
    他看著沒有合約書的畫面，只能去按當下唯一那顆按鈕（公告框的「確定」），
    於是把「確定」學成了「同意」，設定裡也存了一個錯的比例。
    """
    result = _run(monkeypatch, [Stage.LOGIN], [])
    assert result.ok, result.summary
    assert "click" not in wired, "在登入畫面上不該點同意"
    assert "type:demo01" in wired


def test_it_does_not_type_while_the_eula_is_still_up(monkeypatch, wired):
    """★ 合約書還在就**不要打字**：那個畫面整個不吃 `PostMessage`（[INP-001]）。

    使用者原話：「不該有已經在打帳號密碼、他還在合約沒按下」。
    舊版照打不誤，六個子行程一個一個失敗，日誌上只有 debug 級的「送不進去」。
    """
    calls: list[str] = []
    monkeypatch.setattr(
        AutoLogin, "_type_credentials",
        lambda self, hwnd: calls.append("typed") or True,
    )
    monkeypatch.setattr(auto_login, "_INPUT_TIMEOUT", 0.8)
    monkeypatch.setattr(auto_login, "find_server", lambda pid: None)
    result = _run(monkeypatch, [Stage.EULA], [])
    assert not result.ok
    assert not calls, "合約書還在的時候不該打字"
    assert "合約書" in result.detail, result.detail


def test_it_stops_when_the_client_submitted_but_cannot_reach_the_server(
    monkeypatch, wired
):
    """★ 字進去了、客戶端也送出了，但**連不上伺服器** —— 再打幾次都沒用。

    2026-08-30 實機：客戶端停在自己畫的「與伺服器斷線」訊息框上，自動登入
    一路「客戶端還沒連上伺服器，再打一次」，等於對著錯誤框打字打到逾時。

    ⚠ 分辨「字沒進去」與「連不上」的依據是**記憶體**（客戶端按下送出時自己
    寫下的那串帳號，[MEM-032]），不是畫面 —— 在那個卡住的行程上當場驗過：
    連線從頭到尾沒建立，那塊緩衝照樣是我們的帳號。
    """
    monkeypatch.setattr(auto_login, "find_server", lambda pid: None)
    monkeypatch.setattr(
        auto_login.input_helper, "submitted_account", lambda pid: "demo01"
    )
    result = _run(monkeypatch, [Stage.LOGIN], [])
    assert not result.ok
    assert "連不上伺服器" in result.detail, result.detail
    assert wired.count("enter") == 1, "確定送不出去就不該再打第二次"


def test_it_keeps_trying_when_the_client_never_recorded_a_submit(monkeypatch, wired):
    """反面：客戶端**沒有**記下我們的帳號＝字可能根本沒進去 → 不准提早收手。

    只驗「不走那條捷徑」：真的重試幾次由預算決定，不是這一條要釘的東西。
    """
    monkeypatch.setattr(auto_login, "find_server", lambda pid: None)
    monkeypatch.setattr(auto_login.input_helper, "submitted_account", lambda pid: None)
    monkeypatch.setattr(auto_login, "_INPUT_TIMEOUT", 1.0)
    result = _run(monkeypatch, [Stage.LOGIN], [])
    assert not result.ok
    assert "連不上伺服器" not in result.detail, result.detail
    assert "沒有連上" in result.detail, result.detail


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
        """`0x0064` 與 `0x0A73` **一次全到**，模擬同一瞬間的批次。

        後面兩包要等客戶端真的打出認證碼才會到（伺服器不可能提前回）。
        """

        def __init__(self, gate):
            self._gate = gate

        def factory(self, pid, sink, **_kw):
            self.sink = sink
            return self

        def start(self):
            import threading

            self.sink(FakePacket(0x0064, _login_payload()))
            self.sink(FakePacket(0x0A73, b''))

            def answer():
                self._gate.wait(60)   # 帳密那幾輪重試就要 20 秒，等短了會提前放行
                self.sink(FakePacket(0x0A74, b''))
                self.sink(FakePacket(0x0B60, b''))

            threading.Thread(target=answer, daemon=True).start()
            return True

        def stop(self, *_a, **_k):
            pass

    monkeypatch.setattr(
        auto_login, "PacketCapture", Burst(_otp_gate(monkeypatch)).factory
    )
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

    def __init__(self, chars, start=0, stuck=False, writes=True, drops=0):
        self.chars = {c.slot: c.name for c in chars}
        self.cursor = start
        self.stuck = stuck          # 模擬「按了游標不動」
        self.writes = writes        # 模擬「按了名字沒寫進來」
        self.drops = drops          # 前幾下被客戶端吃掉（換畫面之後很常見）
        self.name = ""
        self.entered = None         # 客戶端自己送出去的格號

    def press(self, vk):
        if self.drops > 0:
            self.drops -= 1
            return
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
    start=0, stuck=False, writes=True, wrong_name=None, drops=0,
):
    """做一個「已經連上、角色清單也收到了」的狀態機，只測選角這一步。

    `reply`：伺服器對客戶端選角的回應（0x0AC5 成功／0x006C 拒絕／None 不回）。
    `start`：游標一開始停在第幾格。
    `stuck`：方向鍵按了游標不動（畫面根本不在選角）。
    `writes`：按下 Enter 客戶端會不會把名字寫進記憶體。
    `wrong_name`：客戶端寫進去的是別隻的名字（模擬偏移失效）。
    """
    from ro_toolbox.services import character as character_module

    client = _FakeClient(
        characters, start=start, stuck=stuck, writes=writes, drops=drops
    )
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


def _settings_in(monkeypatch, tmp_path):
    from ro_toolbox.config import settings as settings_module

    monkeypatch.setattr(settings_module, "config_file", lambda: tmp_path / "s.json")
    monkeypatch.setattr(settings_module, "_current", settings_module.AppSettings())
    return settings_module


def _no_mouse(monkeypatch, down=False):
    """把左鍵狀態固定住（預設沒按），這樣測試不會被真的滑鼠影響。"""
    import ctypes

    monkeypatch.setattr(
        ctypes.windll.user32, "GetAsyncKeyState",
        lambda _vk: (-32768 if down else 0),
    )


def test_agree_click_reports_where_the_position_came_from(monkeypatch, tmp_path):
    """⚠ 使用者朋友的機器實際踩過：解析度跟我們不同 → 用內建比例點了 11 次空氣。

    以前這裡只有 `log.debug`，日誌上完全看不出「其實在猜」。
    現在 `_click_agree` 要把來源講出來，主迴圈才有辦法決定要不要求救。
    """
    _settings_in(monkeypatch, tmp_path)
    bot = AutoLogin(_account(), 1, lambda _: None)
    monkeypatch.setattr(auto_login.input_helper, "send", lambda *a, **k: None)
    monkeypatch.setattr(
        auto_login.game_screen, "agree_button_position", lambda hwnd, ratio: (5, 6)
    )

    # 1) 畫面上認出來 —— 最可信
    _looks_like(monkeypatch, [Stage.EULA], agree=(11, 22))
    assert bot._click_agree(0x1) == auto_login.AGREE_FOUND

    # 2) 認不出來、也沒學過 —— 只能用內建比例**猜**
    _looks_like(monkeypatch, [Stage.UNKNOWN], agree=None)
    assert bot._click_agree(0x1) == auto_login.AGREE_GUESS

    # 3) 認不出來，但使用者教過 —— 用學到的
    from ro_toolbox.config import settings as settings_module

    settings_module.current_settings().agree_button = [0.5, 0.6]
    assert bot._click_agree(0x1) == auto_login.AGREE_LEARNED

    # 猜與學到的都算「沒把握」，那才是該求救的理由（不是「認不認得出合約書」）
    assert auto_login.AGREE_GUESS in auto_login.AGREE_UNSURE
    assert auto_login.AGREE_LEARNED in auto_login.AGREE_UNSURE
    assert auto_login.AGREE_FOUND not in auto_login.AGREE_UNSURE


def test_learning_works_even_when_the_screen_is_unrecognisable(monkeypatch, tmp_path):
    """★ 這就是朋友那台的情況：解析度不同 → `detect()` 永遠認不出合約書。

    學習的主訊號因此改成「看到他按下左鍵」—— 跟畫面長什麼樣完全無關。
    """
    import ctypes

    settings_module = _settings_in(monkeypatch, tmp_path)
    bot = AutoLogin(_account(), 1, lambda _: None)
    monkeypatch.setattr(auto_login.game_screen, "capture", lambda hwnd: object())
    # 認不出來：從頭到尾都是 UNKNOWN
    monkeypatch.setattr(auto_login.game_screen, "detect", lambda img: Stage.UNKNOWN)
    monkeypatch.setattr(
        game_screen, "window_ratio_of", lambda hwnd, x, y: (x / 1000, y / 500)
    )
    monkeypatch.setattr(AutoLogin, "_save_screen", lambda self, hwnd: None)

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def _cursor(ref):
        point = ctypes.cast(ref, ctypes.POINTER(_Point)).contents
        point.x, point.y = 400, 300
        return 1

    monkeypatch.setattr(ctypes.windll.user32, "GetCursorPos", _cursor)
    # 第一拍沒按、第二拍按下去（按下緣）
    states = [0, -32768, -32768]
    monkeypatch.setattr(
        ctypes.windll.user32, "GetAsyncKeyState",
        lambda _vk: states.pop(0) if states else 0,
    )
    assert bot._learn_agree_button(0x1234, timeout=5.0) is True
    assert settings_module.current_settings().agree_button == [0.4, 0.6]


def test_it_will_not_learn_a_random_spot_when_it_cannot_see_the_eula(
    monkeypatch, tmp_path
):
    """⚠ 認不出合約書的機器上，「合約書消失了」這個訊號**第一拍就成立** ——
    照舊版的寫法會把當下的游標位置學成按鈕，那是很有自信的錯值。

    所以輔助訊號要先**真的看過合約書**才算數；沒看過就只認「按下左鍵」。
    """
    import ctypes

    settings_module = _settings_in(monkeypatch, tmp_path)
    bot = AutoLogin(_account(), 1, lambda _: None)
    monkeypatch.setattr(auto_login.game_screen, "capture", lambda hwnd: object())
    monkeypatch.setattr(auto_login.game_screen, "detect", lambda img: Stage.UNKNOWN)
    monkeypatch.setattr(
        game_screen, "window_ratio_of", lambda hwnd, x, y: (x / 1000, y / 500)
    )
    monkeypatch.setattr(AutoLogin, "_save_screen", lambda self, hwnd: None)

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def _cursor(ref):
        point = ctypes.cast(ref, ctypes.POINTER(_Point)).contents
        point.x, point.y = 400, 300
        return 1

    monkeypatch.setattr(ctypes.windll.user32, "GetCursorPos", _cursor)
    _no_mouse(monkeypatch)                       # 使用者從頭到尾沒按
    monkeypatch.setattr(auto_login, "_POLL", 0.001)
    assert bot._learn_agree_button(0x1234, timeout=0.05) is False
    assert settings_module.current_settings().agree_button is None, "不准亂學"


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
    _no_mouse(monkeypatch)          # 這一條驗的是「合約書消失」那個輔助訊號
    monkeypatch.setattr(AutoLogin, "_save_screen", lambda self, hwnd: None)
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


# ---- 焦點在哪一格：用預設假設，**不做任何探測** ------------------------
#
# ⛔⛔ 這裡試過兩種「量出來」的做法，兩種都讓事情變糟，都已移除：
#   1. 讀「上次送出的帳號」靜態緩衝 —— [MEM-032] 明寫它送出後才有值，
#      兩種情況都是空的，等於沒在判斷。
#   2. 打探針進去再搜記憶體 —— 使用者實測「剛開始在密碼時還會莫名打出
#      一些字然後自己刪掉」（那就是探針，看得見），而且判斷照樣會錯
#      （「他都會相反一次」）。
#
# 教訓：[MEM-032] 那條不對稱是**一次**特定量測下成立的。拿它當「判斷焦點」
# 的唯一依據，等於把整條登入押在沒有反覆驗證過的前提上。


def _capture_sends(monkeypatch):
    """把每一次 `_type()` 送出去的動作清單收下來。"""
    sent: list[list[dict]] = []
    monkeypatch.setattr(
        AutoLogin, "_type", lambda self, hwnd, actions: sent.append(list(actions))
    )
    return sent


def _bot(monkeypatch, findable=()):
    """做一個 AutoLogin，並決定「哪些字串在堆積上找得到」。

    焦點在哪一格**每一次都現查**（快取已經被實機推翻並移除，見
    `_type_credentials` 的說明），所以這裡不必再清什麼設定。
    """
    bot = AutoLogin(_account(), 4242)
    monkeypatch.setattr(
        auto_login.input_helper, "field_addresses",
        lambda pid, text: [0x18254788] if text in set(findable) else [],
    )
    return bot


def test_nothing_extra_is_ever_typed_into_the_boxes(monkeypatch, wired):
    """⚠ 使用者實測：「還會莫名打出一些字然後自己刪掉」——那是探針。

    現在**只准打帳號與密碼**，不准打任何使用者看得見卻不屬於他的字。
    """
    # 打之前找不到帳號 → 直接拿帳號當記號，畫面上不會出現多餘的字。
    seen = {"n": 0}

    def only_after_typing(pid, text):
        seen["n"] += 1
        return [0x1825] if seen["n"] > 1 else []      # 第一次（打之前）找不到

    bot = _bot(monkeypatch)
    monkeypatch.setattr(
        auto_login.input_helper, "field_addresses", only_after_typing)
    sent = _capture_sends(monkeypatch)
    bot._type_credentials(0x1234)
    typed = [a["text_fg"] for batch in sent for a in batch if "text_fg" in a]
    assert typed == ["demo01", "pw"], typed


# ---- Tab 過去那一格一律清空 ---------------------------------------------


def test_every_box_is_entered_by_tab_so_the_old_value_is_replaced(monkeypatch, wired):
    """★ **每一格都是 Tab 進去之後才打字** —— 舊值才會被整個取代掉。

    2026-08-31 抓圖量到：登入框的 Tab 只有兩站（帳號 ↔ 密碼），而且 Tab 進去
    的那一格內容是**選取狀態** —— 打 `BBBBBB` 就把客戶端記著的 `ss26011034`
    整串換掉了。所以不需要清空，也就不需要那 48 個 `PostMessage` 按鍵
    （它會被 GameGuard 整批擋掉，實機連續失敗 23 次、耗掉 196 秒）。

    這一條釘住：**打字前面一定有 Tab**，不然打的字會接在客戶端記著的舊值後面
    （使用者實測過：「他沒刪除舊帳，ENTER 導致錯誤」）。
    """
    bot = _bot(monkeypatch, findable=("demo01",))
    sent = _capture_sends(monkeypatch)
    bot._type_credentials(0x1234)
    for batch in sent:
        keys = [k for a in batch for k in (("TAB",) if a.get("key_fg") == 0x09
                                           else ("TEXT",) if "text_fg" in a else ())]
        for i, kind in enumerate(keys):
            if kind == "TEXT":
                assert "TAB" in keys[:i], f"打字前面沒有 Tab：{batch}"


def test_no_window_messages_are_used_while_logging_in(monkeypatch, wired):
    """★ 登入全程**只用前景 `SendInput`**，一個視窗訊息都不准送。

    `PostMessage` 會被 GameGuard 整批擋掉（[INP-023]）。清空欄位是最後一個
    還在用它的動作，實機因此連續失敗 23 次、耗掉 196 秒才逾時（2026-08-31）。
    改用「Tab 出去再 Tab 回來，內容自動選取」取代之後就沒有這個關卡了。
    """
    bot = _bot(monkeypatch, findable=("demo01",))
    sent = _capture_sends(monkeypatch)
    bot._type_credentials(0x1234)
    background = [a for b in sent for a in b if "key" in a or "text" in a]
    assert not background, f"登入不准用視窗訊息：{background}"


def test_the_enter_goes_the_same_way_as_the_typing(monkeypatch, wired):
    """★ 送出的 Enter 要走**前景**，跟打字同一條佇列（[INP-027]）。

    ⚠ 舊版這裡是 `PostMessage` 的視窗訊息（[INP-010]：它很挑，要帶字元碼）。
    混著送的代價實機量到了：兩條佇列沒有先後保證，Enter 會排在字前面被處理，
    客戶端就拿**空欄位**去送 —— OTP 第 1 次因此每一場都失敗。
    """
    bot = _bot(monkeypatch)
    sent = _capture_sends(monkeypatch)
    bot._type_credentials(0x1234)
    enter = sent[-1][-1]
    assert enter["key_fg"] == 0x0D, enter
    assert "focus" in {k for a in sent[-1] for k in a}, sent[-1]


# ---- 送出前只**觀察**，不做決定 -----------------------------------------
#
# ⚠⚠ 這裡也試過兩版「送出前自己判斷有沒有打反」，兩版都會把**本來正確**的
# 輸入弄壞（v0.3.1「搜不到帳號就翻面」、v0.3.3「搜到密碼就翻面」）。
# 使用者實測：「他都會相反一次，雖然後來修正了，但這是不對的。」
# 唯一會觸發翻面的依據是送出**之後**那個確定的訊號（submitted_account）。


def test_the_observation_never_changes_anything(monkeypatch, wired):
    """只寫日誌，不回傳決定、不改狀態 —— 這是這一支存在的全部理由。"""
    for findable in ((), ("demo01",), ("pw",), ("demo01", "pw")):
        bot = _bot(monkeypatch, findable)
        assert bot._note_field_placement() is None


def test_the_observation_records_what_it_saw(monkeypatch, wired):
    """留下證據，下次實測才看得出那條不對稱到底成不成立。"""
    bot = _bot(monkeypatch, ("demo01",))
    bot._note_field_placement()
    assert any("帳號在堆積上找得到" in step and "密碼找不到" in step
               for step in bot.progress.steps), bot.progress.steps


def test_a_failed_observation_is_harmless(monkeypatch, wired):
    """記憶體讀不到就算了 —— 純觀察，不該影響登入。"""
    def _boom(pid, text):
        raise RuntimeError("讀不到")

    monkeypatch.setattr(auto_login.input_helper, "field_addresses", _boom)
    bot = AutoLogin(_account(), 4242)
    assert bot._note_field_placement() is None


# ---- SendInput 打的是「前景視窗」，不是我們指定的 hwnd -------------------


def _raw_batches(monkeypatch):
    """把送出去的**原始動作清單**一批一批收下來（`wired` 只收攤平後的摘要）。"""
    batches: list[list[dict]] = []
    monkeypatch.setattr(
        auto_login.input_helper, "send",
        lambda hwnd, actions: batches.append(list(actions)),
    )
    return batches


def _assert_focus_before_foreground(batch: list[dict]) -> None:
    seen_focus = False
    for action in batch:
        if "focus" in action:
            seen_focus = True
        if "text_fg" in action or "key_fg" in action:
            assert seen_focus, f"前景動作前面沒有 focus()：{action}"


def test_every_foreground_action_has_a_focus_before_it(monkeypatch, wired):
    """⚠ v0.2.5 踩過，整個自動登入爛掉。

    清空從視窗訊息改成真的按鍵之後，忘了它也需要 `focus()` ——
    視窗訊息是直接指名 hwnd 的，`SendInput` 不是，它送給**當下的前景視窗**。
    於是那 24 個 Backspace 打進使用者正在用的視窗，而遊戲那一格根本沒清到，
    字就接在舊值後面送出去。

    測試當時沒抓到，是因為假的 `send` 不會模擬「前景」這件事 ——
    所以這裡改成檢查**動作清單本身的形狀**。
    """
    bot = _bot(monkeypatch)
    sent = _capture_sends(monkeypatch)
    bot._type_credentials(0x1234)
    for batch in sent:
        _assert_focus_before_foreground(batch)


def test_the_whole_thing_goes_in_one_subprocess(monkeypatch, wired):
    """★ 整組帳密**一個子行程做完**，視窗訊息與按鍵混著送。

    ⚠⚠ 這一條跟舊版相反。舊版照 [INP-009]「送過 SendInput 之後視窗訊息會被
    封鎖」拆成六批 —— **那條 2026-08-30 實測不成立**（[INP-022]）：
    在登入畫面上照同樣順序 `PostMessage → SendInput → PostMessage` 混著送，
    一個行程整包做完，python 子行程 8/8 全過、打包版 exe 6/8
    （那兩次是整批被擋，跟混不混無關），而且抓圖驗過兩格都打對。

    拆六批的代價才是致命的：GameGuard 會**隨機整批擋掉一個子行程的輸入**
    （打包版 40~70%），六批要連過六關 —— 使用者實機打了 22 次、73 秒。
    """
    bot = _bot(monkeypatch, findable=("demo01",))
    sent = _capture_sends(monkeypatch)
    bot._type_credentials(0x1234)
    kinds = {k for batch in sent for a in batch for k in a}
    # ⚠ 前景 `SendInput`（`text_fg`／`key_fg`）打的是「當下的前景視窗」，
    #   所以**同一批的開頭一定要有 `focus()`**，而且要在打字之前 ——
    #   搶不到前景就整批失敗，寧可不做也不要打進使用者正在用的視窗。
    assert {"focus", "ime_off", "text_fg", "key_fg"} <= kinds, kinds
    for batch in sent:
        if any("text_fg" in a or "key_fg" in a for a in batch):
            order = [k for a in batch for k in a]
            assert "focus" in order, batch
            assert order.index("focus") == 0, batch
    assert sent[-1][-1]["key_fg"] == 0x0D, "最後一定是 Enter（前景，[INP-027]）"


# ---- 遊戲不在了就馬上停，不要做無意義的重試與等待 -----------------------
#
# 使用者實測回報：突然斷線之後他直接把遊戲關掉，而自動登入照樣重試了 11 次：
#   14:00:19  回連：第 11 次 OTP 送不進去：輸入沒送出去：
#             遊戲視窗已經不在了（遊戲關掉了？）。
#   14:00:25  卡在「等登入結果」：送了 11 次 OTP…
# input.py 其實**明確知道**視窗不在了，但那個判斷被包成一般的「送不進去」。
# 使用者的話：「不要做無意義等待，有問題就馬上。」


def test_a_dead_process_stops_everything_at_once(monkeypatch, wired):
    """行程沒了就是不可恢復 —— 一次都不要再試。"""
    monkeypatch.setattr(auto_login, "_process_alive", lambda pid: False)
    bot = AutoLogin(_account(), 4242)
    assert bot._game_gone(0x1234) is True
    assert bot._stop_if_gone(0x1234, "送出 OTP") is True
    assert "遊戲已經關掉了" in bot.progress.detail


def test_a_closed_window_counts_too(monkeypatch, wired):
    """視窗沒了、而且 PID 底下也**再也找不到視窗** —— 那才是真的關掉了。"""
    monkeypatch.setattr(auto_login, "_window_alive", lambda hwnd: False)
    monkeypatch.setattr(game_screen, "find_window", lambda pid: None)
    bot = AutoLogin(_account(), 4242)
    assert bot._game_gone(0x1234) is True


def test_the_client_swapping_its_window_is_not_a_dead_game(monkeypatch, wired):
    """★ 2026-08-30 實機：**客戶端會把自己的視窗換掉**（合約書按掉之後）。

    舊 hwnd 失效 → `GetWindowRect` 丟 1400（無效的視窗控制代碼）→
    舊版把它判成「遊戲已經關掉了 —— 不再重試」，於是遊戲明明停在登入畫面，
    自動登入卻整條放棄。身分是 **PID**，視窗要現查。
    """
    monkeypatch.setattr(auto_login, "_window_alive", lambda hwnd: hwnd == 0x5678)
    monkeypatch.setattr(game_screen, "find_window", lambda pid: 0x5678)
    bot = AutoLogin(_account(), 4242)
    bot._hwnd = 0x1234
    assert bot._game_gone(0x1234) is False, "換視窗不等於遊戲關掉"
    assert bot._window() == 0x5678, "要改用新的視窗"


def test_a_live_game_is_never_mistaken_for_a_dead_one(monkeypatch, wired):
    bot = AutoLogin(_account(), 4242)
    assert bot._game_gone(0x1234) is False
    assert bot._stop_if_gone(0x1234, "送出 OTP") is False


def test_an_unanswerable_check_never_declares_it_dead(monkeypatch):
    """⚠ 查不到一律當作**還活著** —— 誤判成死掉會把好端端的登入砍掉。"""
    def _boom(*_a):
        raise OSError("查不到")

    monkeypatch.setattr(auto_login.log, "debug", lambda *a, **k: None)
    monkeypatch.setattr("psutil.pid_exists", _boom)
    assert auto_login._process_alive(4242) is True


def test_the_otp_loop_does_not_grind_on_a_closed_game(monkeypatch, wired):
    """⚠ 這就是那 11 次。遊戲關掉之後 OTP 一次都不該再送。"""
    sends = []
    monkeypatch.setattr(
        auto_login.input_helper, "send",
        lambda hwnd, actions: sends.append(actions),
    )
    monkeypatch.setattr(auto_login, "_process_alive", lambda pid: False)
    bot = AutoLogin(_account(), 4242)
    assert bot._send_otp(0x1234) is False
    assert sends == [], f"遊戲都不在了還送了 {len(sends)} 批"
    assert "遊戲已經關掉了" in bot.progress.detail


def test_the_credentials_loop_does_not_grind_either(monkeypatch, wired):
    sends = []
    monkeypatch.setattr(
        auto_login.input_helper, "send",
        lambda hwnd, actions: sends.append(actions),
    )
    monkeypatch.setattr(auto_login, "_process_alive", lambda pid: False)
    bot = AutoLogin(_account(), 4242)
    assert bot._send_credentials(0x1234) is False
    assert sends == []


def test_waiting_stops_early_when_the_game_dies(monkeypatch, wired):
    """⚠ 等一個**確定不會發生**的東西等到逾時，就是使用者說的無意義等待。"""
    monkeypatch.setattr(auto_login, "find_server", lambda pid: None)
    monkeypatch.setattr(auto_login, "_process_alive", lambda pid: False)
    bot = AutoLogin(_account(), 4242)
    began = time.monotonic()
    assert bot._wait_connection(30.0) is None
    assert time.monotonic() - began < 2.0, "不該等滿 30 秒"


# ---- 登入已經死了就交棒，不要把預算送完 ---------------------------------
#
# 使用者實測：尋路進出房子造成斷線 → 回連重開 → 帳密打對了，但**卡登**
# （角色還掛在線上，伺服器不讓進）→ 登入失敗 →「卡死了一直按 ENTER 不輸入密碼」。
# 他的要求：「正常要一次輸入成功；如果失敗那應該要關閉重登。」
# 「關閉重登」是回連那一層的事 —— 這裡要做的是**當場承認失敗並交棒**。


def test_a_dropped_connection_ends_the_otp_phase_at_once(monkeypatch, wired):
    """遊戲還開著，但客戶端已經沒有連線 —— 再送 OTP 不會有結果。"""
    monkeypatch.setattr(auto_login, "find_server", lambda pid: None)
    sends = []
    monkeypatch.setattr(
        auto_login.input_helper, "send",
        lambda hwnd, actions: sends.append(actions),
    )
    bot = AutoLogin(_account(), 4242)
    bot._lost_since = time.monotonic() - auto_login._LOGIN_LOST_SEC - 1
    assert bot._send_otp(0x1234) is False
    assert sends == [], f"連線都沒了還送了 {len(sends)} 批"
    assert "沒有連線" in bot.progress.detail


def test_one_missing_tick_is_not_a_dead_login(monkeypatch, wired):
    """⚠ **換伺服器的那一瞬間本來就會短暫沒有連線**（登入台 → 角色台）。

    看到一次就放棄會把正常流程砍掉。
    """
    monkeypatch.setattr(auto_login, "find_server", lambda pid: None)
    bot = AutoLogin(_account(), 4242)
    assert bot._connection_lost() is False, "第一拍只能開始計時"
    assert bot._connection_lost() is False, "還沒滿寬限就不准判死"


def test_the_connection_coming_back_resets_the_clock(monkeypatch, wired):
    """連線回來就要把計時歸零，不然它會一路累積到誤判。"""
    seen = {"server": None}
    monkeypatch.setattr(auto_login, "find_server", lambda pid: seen["server"])
    bot = AutoLogin(_account(), 4242)
    bot._connection_lost()                       # 開始計時
    seen["server"] = ("1.2.3.4", 6900)
    assert bot._connection_lost() is False
    assert bot._lost_since == 0.0, "回來了就要歸零"


def test_a_live_connection_never_looks_dead(monkeypatch, wired):
    bot = AutoLogin(_account(), 4242)
    for _ in range(20):
        assert bot._connection_lost() is False


# ---- 等人動手的時間不算「程式卡住」（使用者實測：登入死在這裡）------------


def test_waiting_for_the_user_does_not_burn_the_watchdog_budget():
    """求救等人按合約的那 60 秒，不該吃掉登入鎖的 120 秒預算。

    實機踩過：等 61 秒 → 回來重試 17 次 → 看門狗 120 秒到了把前景放掉 →
    接下來每一次輸入都 `PostMessage` 失敗，整個自動登入死在
    「送不進視窗訊息」（使用者實測回報）。
    """
    from ro_toolbox.services.login_lock import LoginLock

    lock = LoginLock(hwnd=0)
    lock._deadline = 1000.0
    lock.wait_for_user(60.0)
    assert lock._deadline == 1060.0

    lock.wait_for_user(-5.0)          # 負數不該把期限往回拉
    assert lock._deadline == 1060.0


# ---- 「送出去的帳號」該信誰（實機炸過：信錯來源 → 無限重打）----------------


def _login_packet(username: str):
    """一包 0x0064（明文帳號，見 [PKT-046]）。"""
    from ro_toolbox.core.ro_packet import RoPacket

    payload = b"\x00" * 4 + username.encode("ascii").ljust(24, b"\x00")
    return RoPacket(seq=1, timestamp=0.0, outbound=True, opcode=0x0064, payload=payload)


def test_the_packet_beats_the_memory_guess(monkeypatch, caplog):
    """封包是**真的送出去的那串位元組**；記憶體那份會有上一次登入的殘留。

    實機炸過（2026-08-29）：畫面已經登入成功、跳到 Google OTP 了，
    記憶體卻回一個不相干的舊帳號 `s9318888` —— 於是被判成「打到別的欄位」，
    翻面重打，把帳號打進 OTP 的認證號碼欄，一路重試到逾時。
    """
    import logging

    from ro_toolbox.services import auto_login as mod

    login = mod.AutoLogin.__new__(mod.AutoLogin)
    login._packets = [_login_packet("87103030")]
    monkeypatch.setattr(mod.input_helper, "submitted_account",
                        lambda _pid: "s9318888")
    login._pid = 1234

    assert login._sent_account() == "87103030"
    with caplog.at_level(logging.WARNING):
        chosen = login._sent_account() or mod.input_helper.submitted_account(1234)
    assert chosen == "87103030", "要以封包為準"


def test_memory_is_still_the_fallback(monkeypatch):
    """沒擷取到 0x0064 的時候，記憶體那份還是聊勝於無。"""
    from ro_toolbox.services import auto_login as mod

    login = mod.AutoLogin.__new__(mod.AutoLogin)
    login._packets = []
    login._pid = 1234
    monkeypatch.setattr(mod.input_helper, "submitted_account",
                        lambda _pid: "87103030")

    chosen = login._sent_account() or mod.input_helper.submitted_account(1234)
    assert chosen == "87103030"


def test_an_account_without_an_otp_secret_fails_immediately():
    """帳號要 OTP、設定裡卻沒密鑰 —— 當場講清楚，不要重試到逾時。

    實機踩過（2026-08-29）：伺服器跳出 Google OTP 視窗，程式一路重試，
    還把帳號打進了認證號碼欄。
    """
    from ro_toolbox.services import auto_login as mod

    login = mod.AutoLogin.__new__(mod.AutoLogin)
    login._account = type("A", (), {"secret": ""})()
    login.progress = mod.LoginProgress()
    login._on_step = None
    login._pid = 1234

    assert login._send_otp(hwnd=0) is False
    assert "沒有密鑰" in login.progress.detail
    assert login.progress.failed_at == "送出 OTP"


def test_every_batch_grabs_the_foreground_back():
    """⚠ 每一批輸入之前都要把遊戲搶回最前面。

    `login_lock.reassert()` 以前是寫好了**沒人叫** —— 使用者只要在登入那幾秒
    點了別的視窗（或另一個角色的登入搶走前景），字就餵到別人的視窗去了，
    而且完全沒有徵兆。使用者實測：兩個角色前後斷線，後面那個的登入把前面
    那個卡死，**兩個都登不進去**。
    """
    from ro_toolbox.services import auto_login as mod

    class FakeLock:
        def __init__(self) -> None:
            self.calls = 0

        def reassert(self) -> bool:
            self.calls += 1
            return True

    sent = []
    login = mod.AutoLogin.__new__(mod.AutoLogin)
    login._lock = FakeLock()
    original = mod.input_helper.send
    mod.input_helper.send = lambda hwnd, actions: sent.append(actions)
    try:
        login._type(1234, [{"kind": "key"}])
        login._type(1234, [{"kind": "text"}])
    finally:
        mod.input_helper.send = original

    assert login._lock.calls == 2, "每一批都要 reassert，不是只有第一批"
    assert len(sent) == 2


def test_typing_still_works_without_a_lock():
    """沒有鎖的路徑（測試、或鎖還沒建）不該炸。"""
    from ro_toolbox.services import auto_login as mod

    sent = []
    login = mod.AutoLogin.__new__(mod.AutoLogin)
    login._lock = None
    original = mod.input_helper.send
    mod.input_helper.send = lambda hwnd, actions: sent.append(actions)
    try:
        login._type(1234, [{"kind": "key"}])
    finally:
        mod.input_helper.send = original
    assert len(sent) == 1


def test_the_otp_is_typed_the_same_way_as_the_credentials(monkeypatch, wired):
    """★ 認證碼那一格也是**一開始沒有焦點** —— 要先按 Tab（[INP-024]）。

    實機抓到客戶端自己跳出「不是6位認證碼。請您再次確認」，也就是那六碼
    一個都沒進去；而日誌只寫「還沒換伺服器，再送一次」，連送 10 次。
    第 2 次以後還要先按 Enter 把那個框關掉，不然字都餵給它。
    """
    bot = _bot(monkeypatch)
    bot._account.secret = _account().secret
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(auto_login, "_OTP_TIMEOUT", 0.5)
    monkeypatch.setattr(auto_login, "_OTP_STEP_TIMEOUT", 0.05)
    monkeypatch.setattr(auto_login, "find_server", lambda pid: ("1.2.3.4", 6900))
    monkeypatch.setattr(AutoLogin, "_pick_server_actions", lambda self: [])
    bot._login_server = ("1.2.3.4", 6900)
    bot._send_otp(0x1234)
    first = next(b for b in sent if any("text_fg" in a for a in b))
    order = [k for a in first for k in a]
    assert order.index("focus") < order.index("text_fg"), f"要先搶前景：{first}"
    assert any("ime_off" in a for b in sent for a in b), "要先關輸入法"
    assert not any(a.get("key") == 0x2E for b in sent for a in b), "不該清空（會灌爆客戶端）"


def test_the_account_is_verified_and_retyped_when_it_did_not_land(monkeypatch, wired):
    """★ 打完帳號**當場讀記憶體確認整串進去了**，沒有就清掉重打同一格。

    使用者 2026-08-31 把輸入法切成英數之後回報：「英文不是被輸入法吃掉，
    而是你壓根沒打出來」—— 也就是**訊息掉了**，而且常常掉第一個。
    帳號長 `s26016041` 這樣（一個英文字母＋八個數字）時，掉第一個字
    看起來完全像「英文被吃掉」。所以不要再想「怎麼送才不會掉」，送完就確認。
    """
    bot = AutoLogin(_account(), 4242)
    sent = _capture_sends(monkeypatch)

    # ⚠ 用「補打過幾次」當條件，不要用「問過幾次」—— `_field_has` 是**輪詢**的
    #   （打完到寫進堆積之間有落差），同一次確認會問很多遍。
    def flaky(pid, text):
        if text != "demo01":
            return []
        return [0x1825] if len(sent) >= 2 else []      # 前兩次沒進去

    monkeypatch.setattr(auto_login.input_helper, "field_addresses", flaky)
    bot._verify_account(0x1234)
    retypes = [b for b in sent
               if any(a.get("text_fg") == "demo01" for a in b)
               and len([a for a in b if a.get("key_fg") == 0x09]) == 2]  # Tab 出去再回來
    assert len(retypes) == 2, f"該補打兩次：{sent}"
    assert any("帳號沒完整進到欄位裡" in s for s in bot.progress.steps), bot.progress.steps


def test_it_gives_up_repairing_instead_of_looping_forever(monkeypatch, wired):
    """確認不到就重打幾次，還是不行就**照樣送出去**，讓封包驗證收尾。

    ⚠ 不准在這裡自作主張改別的東西（[INP-015]：拿模稜兩可的訊號去觸發
    修正動作，會把本來對的弄壞）。
    """
    monkeypatch.setattr(auto_login.input_helper, "field_addresses", lambda pid, t: [])
    bot = AutoLogin(_account(), 4242)
    sent = _capture_sends(monkeypatch)
    bot._type_credentials(0x1234)
    assert sent[-1][-1]["key_fg"] == 0x0D, "最後還是要送出去"
    assert any("照樣送出去" in s for s in bot.progress.steps), bot.progress.steps


def test_the_character_list_just_received_is_used_for_the_slot(monkeypatch, wired):
    """★ 這一輪剛從伺服器收到的角色清單就是最新的 —— 要拿它查格號。

    ⚠ 實機踩過（2026-08-31）：日誌前一行才印
    「記住角色清單：1 雪色狐狸、2 雪狐u、3 光狐、**4 狐狐狸**」，
    下一行卻說「這一台上沒有角色『狐狐狸』」—— 因為那份清單這一輪才收到、
    還沒寫回帳號設定（存檔是登入結束才做），而選角只查了設定裡的舊清單。
    """
    from dataclasses import dataclass

    @dataclass
    class _Entry:
        slot: int
        name: str

    account = _account()
    account.server = "波利"
    account.character = "狐狐狸"
    bot = AutoLogin(account, 4242, lambda _t: None)
    bot.server_name = "波利"
    bot.characters = [_Entry(1, "雪色狐狸"), _Entry(4, "狐狐狸")]
    picked = {}
    monkeypatch.setattr(
        AutoLogin, "_pick_in_client",
        lambda self, slot, wanted, hwnd: picked.update(slot=slot, wanted=wanted) or True,
    )
    assert bot._select_character(0x1234) is True
    assert picked == {"slot": 4, "wanted": "狐狐狸"}, picked
    assert not bot.progress.stopped_at_character, bot.progress.stopped_at_character


def test_it_will_not_type_into_a_screen_it_cannot_recognise(monkeypatch, wired):
    """★ 認不出是登入畫面就先別打字 —— 客戶端多半還在載入。

    ⚠ 實機（2026-08-31）：亮度 55、合約書差 154、登入框差 188（什麼都不像），
    工具照樣把帳密打下去，那一輪就毀了。使用者的要求是「不能有出錯機會」。
    """
    calls: list[str] = []
    monkeypatch.setattr(
        AutoLogin, "_type_credentials",
        lambda self, hwnd: calls.append("typed") or True,
    )
    monkeypatch.setattr(auto_login, "find_server", lambda pid: None)
    monkeypatch.setattr(auto_login, "_INPUT_TIMEOUT", 1.0)
    result = _run(monkeypatch, [Stage.UNKNOWN], [])
    assert not result.ok
    assert not calls, "認不出畫面的前幾秒不該打字"


def test_but_it_still_types_blind_on_a_screen_it_never_recognises(monkeypatch, wired):
    """⚠ 反面：別人的機器可能**永遠**認不出畫面（解析度不同，[INP-010]）——
    撐過 `_BLIND_AFTER` 之後還是要打，寧可試也不要卡死。"""
    calls: list[str] = []
    monkeypatch.setattr(
        AutoLogin, "_type_credentials",
        lambda self, hwnd: calls.append("typed") or True,
    )
    monkeypatch.setattr(auto_login, "find_server", lambda pid: None)
    monkeypatch.setattr(auto_login, "_BLIND_AFTER", 0.0)
    monkeypatch.setattr(auto_login, "_INPUT_TIMEOUT", 1.0)
    _run(monkeypatch, [Stage.UNKNOWN], [])
    assert calls, "認不出畫面也要有盲打的退路"


def _typed_order(sent):
    """把送出去的動作攤平成「打了什麼字、往哪個方向 Tab」的順序。

    """
    out = []
    for batch in sent:
        for a in batch:
            if "text_fg" in a:
                out.append(a["text_fg"])
            elif a.get("key_fg") == 0x09:
                out.append("TAB")
    return out


def test_it_finds_out_which_box_has_focus_instead_of_guessing(monkeypatch, wired):
    """★ 焦點在哪一格是**問出來的**，不是猜的（使用者：不能有出錯機會）。

    先把現在這一格清乾淨（這樣記憶體裡再出現我們的帳號，就一定是剛打進去的），
    打帳號進去，再問記憶體 —— 找得到就是帳號欄。

    ⚠ 猜對的時候只開**兩個**子行程（使用者：「一直在那切換焦距要很久」）。
    """
    # 打**之前**找不到、打完找得到 ＝ 剛打進去的那一格就是帳號欄。
    seen = {"n": 0}
    monkeypatch.setattr(
        auto_login.input_helper, "field_addresses",
        lambda pid, text: (seen.__setitem__("n", seen["n"] + 1)
                           or ([0x1825] if seen["n"] > 1 else [])),
    )
    bot = AutoLogin(_account(), 4242)
    sent = _capture_sends(monkeypatch)
    bot._type_credentials(0x1234)
    assert _typed_order(sent) == ["TAB", "demo01", "TAB", "pw"], _typed_order(sent)
    assert len(sent) == 2, f"猜對時只該開兩個子行程：{len(sent)} 批"
    assert not any("key" in a for b in sent for a in b), "不准用視窗訊息"
    assert any("焦點在帳號欄" in s for s in bot.progress.steps), bot.progress.steps


def test_when_the_focus_was_the_password_box_it_moves_over_and_retypes(monkeypatch, wired):
    """★ 反面：客戶端記住帳號時焦點在**密碼欄**（使用者實測的規則）——
    帳號會誤打進密碼欄，要清掉、**Shift+Tab 回帳號欄**重打。

    """
    bot = _bot(monkeypatch, findable=())                # 怎麼打都找不到＝不是帳號欄
    sent = _capture_sends(monkeypatch)
    bot._type_credentials(0x1234)
    order = _typed_order(sent)
    assert order[:2] == ["TAB", "demo01"], order        # 先試這一格
    assert "TAB" in order[2:], order                    # 換過去
    assert order[-1] == "pw", order                     # 最後才打密碼
    assert any("焦點在密碼欄" in s for s in bot.progress.steps), bot.progress.steps


def test_the_focus_is_probed_every_single_time(monkeypatch, wired):
    """★ **每一次都要現查**焦點在哪一格 —— 不准記住上次的答案。

    2026-08-31 實機推翻了「那是客戶端的固定性質」這個前提：使用者一次登入
    三個帳號，第一個查出「焦點在帳號欄」，第二個照抄就打錯了 ——
    因為第一個登入成功之後客戶端變成「有記住帳號」，焦點跟著移到密碼欄，
    結果帳號欄原封不動地把它記著的舊帳號 `s9318888` 送了出去。

    這一條釘住：不管設定裡有什麼，都要**清空兩格 ＋ 掃記憶體**。
    """
    from ro_toolbox.config import settings as settings_module

    monkeypatch.setattr(
        settings_module, "_current", settings_module.AppSettings(),
    )
    scans = []
    monkeypatch.setattr(
        auto_login.input_helper, "field_addresses",
        lambda pid, t: scans.append(t) or [0x18254788],
    )
    bot = AutoLogin(_account(), 4242)
    sent = _capture_sends(monkeypatch)
    bot._type_credentials(0x1234)

    assert scans, "每一次都要掃記憶體確認打進哪一格"
    # ⚠ 送出的 Enter 要跟打字**同一條路**（前景 SendInput）——
    #   混著背景 PostMessage 的話兩條佇列沒有先後保證，Enter 會排到字前面。
    assert sent[-1][-1]["key_fg"] == 0x0D, "最後要送出去，而且要走前景"


def test_a_wrong_submitted_account_gives_up_instead_of_retyping(monkeypatch, wired):
    """★ 送出去的帳號不是我們的 → **關掉重開**，不准原地重打。

    實機踩過（2026-08-31）：客戶端把它記著的舊帳號送了出去，舊版當場翻面
    重打 —— 但那時候畫面已經不是登入框了，接下來 60 秒判定「不確定」、
    `PostMessage` 一路失敗，最後跳出「請你手動按一次同意」而畫面上根本
    沒有合約書。使用者訂的規則就是這種情況：失敗就關掉重開。
    """
    bot = _bot(monkeypatch, findable=("demo01",))
    monkeypatch.setattr(AutoLogin, "_type", lambda self, hwnd, actions: None)
    monkeypatch.setattr(auto_login, "find_server", lambda pid: ("1.2.3.4", 6900))
    bot._packets = [FakePacket(0x0064, _login_payload("s9318888"))]

    assert bot._send_credentials(0x1234) is False
    assert bot.progress.failed_at == "輸入帳號密碼", bot.progress.failed_at
    assert "關掉重開" in bot.progress.detail, bot.progress.detail


def test_pin_state_7_means_the_account_has_no_pin(monkeypatch, wired):
    """★ 狀態 7 ＝「這個帳號沒在用二次密碼」—— 不要停在選角畫面。

    2026-08-31 實機（帳號 87103030，使用者確認沒設二次密碼）：伺服器回 7，
    而工具只認得 0／1，於是「不敢往下走」停在選角 —— 帳號其實好好的。
    對照 rAthena 的 0x08B9 狀態表：7 是「選角畫面顯示按鈕」。
    """
    from ro_toolbox.services import login_packets

    account = _account()
    account.pin = ""
    bot = AutoLogin(account, 4242, lambda _t: None)
    seed_aid = bytes(8)
    bot._packets = [FakePacket(login_packets.OP_PIN_STATE,
                               seed_aid + (7).to_bytes(2, "little"))]
    assert bot._pin_already_ok() is True
    assert any("不需要二次密碼" in s for s in bot.progress.steps), bot.progress.steps


def _otp_bot(monkeypatch, packets):
    """準備一個只跑 OTP 那一關的 bot。`packets` 是擷取到的東西。"""
    monkeypatch.setattr(auto_login, "_process_alive", lambda pid: True)
    monkeypatch.setattr(auto_login, "_window_alive", lambda hwnd: True)
    bot = _bot(monkeypatch)
    bot._account.secret = _account().secret
    bot._packets = list(packets)
    bot._login_server = ("1.2.3.4", 6900)
    monkeypatch.setattr(auto_login, "_OTP_TIMEOUT", 0.6)
    monkeypatch.setattr(auto_login, "_OTP_STEP_TIMEOUT", 0.05)
    monkeypatch.setattr(auto_login, "_OTP_SENT_TIMEOUT", 0.05)
    monkeypatch.setattr(auto_login, "_OTP_ACCEPT_TIMEOUT", 0.05)
    monkeypatch.setattr(auto_login, "_OTP_PROMPT_TIMEOUT", 0.05)
    monkeypatch.setattr(auto_login, "_OTP_MIN_SECONDS", 0)
    monkeypatch.setattr(AutoLogin, "_pick_server_actions", lambda self: [{"key_fg": 0x28}])
    return bot


def test_the_otp_is_retyped_at_once_when_the_client_never_sent_it(monkeypatch):
    """★ 打完要確認客戶端**真的送出去**（`0x0A74`）—— 沒送就立刻重打。

    實機踩過（2026-08-31，帳號 s26016041）：第 1 次打完等 6 秒沒反應，
    這時碼只剩 7 秒 → 判定「等下一組」→ 空等的那 7 秒裡登入台把連線收掉，
    整段登入報「客戶端已經沒有連線了」。那六碼其實**一個都沒進去**，
    是可以當場再打一次的。

    同一條也釘住：**沒送出去就不准補送「選伺服器」的按鍵** ——
    那幾下會直接打進 OTP 畫面，把狀態弄亂（舊版就是這樣，每一次都失敗）。
    """
    bot = _otp_bot(monkeypatch, [FakePacket(0x0A73, b"")])
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(auto_login, "find_server", lambda pid: ("1.2.3.4", 6900))

    assert bot._send_otp(0x1234) is False
    assert any("沒送出去" in s for s in bot.progress.steps), bot.progress.steps
    assert not any({"key_fg": 0x28} in batch for batch in sent), sent


def test_the_code_the_client_actually_sent_is_compared_with_ours(monkeypatch):
    """★ `0x0A74` 帶著客戶端送出去的六碼 —— 對不上就要**講出來**。

    實機 2026-09-05 14:33 起連續四場：第 1 次「送出了」（有 `0x0A74`）但伺服器
    沒收下，接著連線就沒了。使用者：「我看是沒打數字進去，直接就送出」。
    日誌只寫「還沒收下」，分不出是**碼不對**、**伺服器慢**、還是
    **數字根本沒進那一格**。第三種是打字的問題，修法完全不同。
    """
    from ro_toolbox.services.totp import generate

    bot = _otp_bot(monkeypatch, [FakePacket(0x0A73, b"")])
    _capture_sends(monkeypatch)
    monkeypatch.setattr(auto_login, "find_server", lambda pid: ("1.2.3.4", 6900))
    ours = generate(bot._account.secret)

    real_type = AutoLogin._type

    def typed_then_client_sends_empty(self, hwnd, actions):
        real_type(self, hwnd, actions)
        # 客戶端把「空的」認證碼送出去了（只有 Enter 到了）
        self._packets.append(FakePacket(0x0A74, b"\x00" * 6))

    monkeypatch.setattr(AutoLogin, "_type", typed_then_client_sends_empty)
    bot._send_otp(0x1234)
    said = [s for s in bot.progress.steps if "送出去的認證碼" in s]
    assert said, bot.progress.steps
    assert f"「{ours}」" in said[0], said[0]
    assert "數字沒有進到那一格" in said[0]


def test_a_dead_capture_does_not_block_the_otp(monkeypatch):
    """★ 一包都沒擷取到的機器上，**不准拿封包當關卡**。

    Npcap 沒裝／raw socket 被擋的話 `0x0A74` 永遠看不到 —— 那時候
    「沒看到」不代表沒送出去。成敗要退回舊訊號：客戶端有沒有**換台**。
    """
    bot = _otp_bot(monkeypatch, [])
    _capture_sends(monkeypatch)
    monkeypatch.setattr(auto_login, "find_server", lambda pid: ("2.2.2.2", 6121))

    assert bot._send_otp(0x1234) is True
    assert any("OTP 過了" in s for s in bot.progress.steps), bot.progress.steps


def test_the_first_arrow_key_may_be_swallowed(fast, monkeypatch):
    """★ 換到選角畫面之後的**第一下按鍵很常被吃掉** —— 不准按一下就放棄。

    實機踩過（2026-08-31，帳號 87103030）：客戶端記得上一次登入選的位置
    （第 4 格，跟哪個帳號無關），這個帳號只有 3 隻，按了一下左沒動就停手，
    停在**空格**上 —— 使用者看到的就是「選錯角色選到空的變成創建角色」。

    重按不是盲按：每一下按完都**讀游標**，讀到動了才往下算。
    """
    account = _account()
    account.character = "白狐"
    bot, client = _at_character_select(
        monkeypatch, account,
        [FakeChar(0, "白雪狐"), FakeChar(1, "白狐"), FakeChar(2, "白尾狐")],
        server="波利", start=4, drops=2,
    )
    bot._account.server = "波利"

    bot._select_character(0x1234)

    assert client.entered == 1, "要真的移到第 1 格才按 Enter"
    assert not bot.progress.stopped_at_character, bot.progress.stopped_at_character


def test_a_really_stuck_cursor_still_stops(fast, monkeypatch):
    """連按都沒動 → 還是要停手（不准按到目的地就閉眼 Enter）。"""
    account = _account()
    account.character = "白狐"
    bot, client = _at_character_select(
        monkeypatch, account,
        [FakeChar(0, "白雪狐"), FakeChar(1, "白狐")],
        server="波利", start=4, stuck=True,
    )
    bot._account.server = "波利"

    bot._select_character(0x1234)

    assert client.entered is None
    assert "移不到" in bot.progress.stopped_at_character
    assert "按了" in bot.progress.stopped_at_character


def test_a_cursor_that_never_moves_is_a_real_failure(fast, monkeypatch):
    """★ 游標按不動 ＝ **登入失敗**，不是「登入成功、停在選角」。

    實機踩過（2026-08-31）：客戶端掉進「創立角色」對話框（游標停在空格上，
    前面某一下 Enter 把它打開了）。那個框**鍵盤關不掉** —— Esc 跳的是
    「是否同意終止」，方向鍵一律無效。唯一的出路是關掉重開，
    而那是回連那一層的工作；報成 ok=True 的話就沒有人會去重開。
    """
    account = _account()
    account.character = "白狐"
    account.server = "波利"
    bot, client = _at_character_select(
        monkeypatch, account,
        [FakeChar(0, "白雪狐"), FakeChar(1, "白狐")],
        server="波利", start=4, stuck=True,
    )

    bot._select_character(0x1234)

    assert client.entered is None
    assert bot.progress.failed_at == "選角", bot.progress.failed_at
    assert "關掉重登" in bot.progress.stopped_at_character
