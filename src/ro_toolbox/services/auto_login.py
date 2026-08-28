"""自動登入的狀態機：一步一個**真的訊號**，不靠固定的 sleep。

## 訊號從哪來

兩種，各用在最合適的地方：

- **畫面**（`game_screen`）：只用在合約書那一關。那個畫面不吃 `PostMessage`，
  而且要靠畫面才知道它出現了沒。
- **封包**（`packet_capture`）：登入之後的每一步都靠它。伺服器的回應是**事實**，
  比截圖判斷可靠得多，也不會被畫面特效、公告視窗干擾。

    送出 0x0064（帳密）  → 伺服器回 0x0A73  ＝「要 OTP 了」
    送出 0x0A74（OTP）   → 伺服器回 0x0B60  ＝「登入成功，這是伺服器清單」

  這兩條是 2026-08-25 實機擷取到的順序（[PKT-046]、[PKT-052]）。

## 佔用使用者的只有一步

合約書要 `SendInput` 點一下（約一秒，會搶前景）—— 那是客戶端唯一不吃背景
訊息的畫面（[INP-001]）。**其餘全部背景完成，不占鍵盤滑鼠。**

## 失敗一律說得出卡在哪

每一步都有明確的等待訊號與逾時。逾時就回報「卡在哪一步、當時畫面判定是什麼」，
不會只丟一句「登入失敗」。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ro_toolbox.services import game_screen, game_socket, input_helper, login_packets
from ro_toolbox.services import input as game_input
from ro_toolbox.services.accounts import Account
from ro_toolbox.services.login_lock import LoginLock
from ro_toolbox.services.packet_capture import PacketCapture
from ro_toolbox.services.ro_capture import find_server
from ro_toolbox.services.totp import generate as generate_otp

log = logging.getLogger(__name__)

#: 客戶端送出的帳號密碼（[PKT-046]）。
#: 選角畫面用得到的虛擬鍵碼。
_VK_LEFT = 0x25
_VK_RIGHT = 0x27

OP_LOGIN = 0x0064
#: 伺服器對帳密的回應。看到它就代表「帳密收下了，接下來要 OTP」。
OP_OTP_REQUIRED = 0x0A73
#: 客戶端送出的 OTP。
OP_OTP = 0x0A74
#: 角色清單（每筆 175 bytes，見 services/char_list）。
OP_CHAR_LIST = 0x0B72

#: 伺服器的登入成功回應（帶 AID／loginID／伺服器清單）。
OP_LOGIN_ACCEPTED = 0x0B60

#: 送出二次密碼之後等伺服器回應的上限。實機上是**同一毫秒**就回，
#: 給到 10 秒純粹是留餘裕。
_PIN_REPLY_TIMEOUT = 10.0
#: 選角之後等地圖台位址的上限。實機上也是同一毫秒回。
_ZONE_TIMEOUT = 10.0
#: 等擷取把我們剛送出去的 `0x0064` 交上來的上限。
_BLOB_TIMEOUT = 8.0

#: 等伺服器把角色清單（0x0B72）送過來的上限。實機上二次密碼過了之後
#: 一兩秒內就到；沒設二次密碼的帳號會更早，但**還是要等**（見 _remember_characters）。
_CHAR_LIST_TIMEOUT = 15.0
#: 收到第一包角色清單之後再等這麼久，讓後面那幾包也到齊。
_CHAR_LIST_QUIET = 1.5

#: 等選角畫面出現（讀得到游標停在第幾格）的上限。
_SELECT_SCREEN_TIMEOUT = 30.0
#: 移動游標最多按幾下方向鍵。RO 最多 15 格，來回一趟綽綽有餘；
#: 超過就是移不動（畫面沒在選角、或那一格到不了），要停手而不是一直按。
_SELECT_MOVE_LIMIT = 20
#: 按下 Enter 之後等客戶端把名字寫進來的上限。實機上是**同一秒**就寫好。
_SELECT_NAME_TIMEOUT = 8.0
#: 自動點幾次同意還過不去，就改成請使用者按一次（順便學位置）。
#: 給 3 次是因為實測偶爾會因為 UI 還沒進入可按狀態而漏掉一兩下。
_AGREE_TRIES = 3
#: 等使用者手動按同意的上限。
_AGREE_LEARN_SEC = 60.0

#: 兩下方向鍵之間的間隔。這不是「等它穩定」——按完一定會**再讀一次**確認
#: 游標真的動了，這個間隔只是給客戶端一個處理訊息的機會。
_SELECT_KEY_PAUSE = 0.15

#: 每一步的等待上限。取得夠寬鬆 —— 讀取慢的時候寧可多等，也不要誤判成失敗。
#: 等遊戲畫出視窗。客戶端有 Themida 加殼要先解殼，而且 GameGuard 也要初始化 ——
#: 實測**最久超過 200 秒**（剛關掉前一個實例再開時特別慢）。
#: 這裡放寬到五分鐘，而且等待期間會定期回報，不要讓人以為當掉了。
_WINDOW_TIMEOUT = 300.0
_EULA_TIMEOUT = 40.0
_LOGIN_SCREEN_TIMEOUT = 60.0
_PACKET_TIMEOUT = 25.0
#: 送出帳密後等登入封包。短一點，因為失敗要能快點重試。
_CREDENTIAL_TIMEOUT = 8.0
#: 反覆「打字→讀記憶體確認」的放棄上限。**逾時只是放棄的上限，
#: 不是成功的依據** —— 成功一律以「記憶體裡讀到那串字」為準。
#: 從開始按同意到帳密送出去的上限。
#:
#: ⚠ 要撐得夠久。實測：**合約書的「同意」要等客戶端載完才真的按得動**，
#: 遊戲開了 30~80 秒之間才會生效；太早點游標會停在按鈕上但什麼也不會發生
#: （使用者親眼看到）。開了 82 秒之後測，第 1 次點就過。
#: 這裡不是「等 N 秒」——每一輪都是「點了就去看客戶端有沒有連上伺服器」，
#: 這只是放棄的上限。
_INPUT_TIMEOUT = 120.0
#: OTP 剩不到這麼多秒就等下一組 —— 送快過期的碼等於浪費一輪重試。
_OTP_MIN_SECONDS = 8
#: 每送一次 OTP，等客戶端換伺服器的上限。
_OTP_STEP_TIMEOUT = 6.0
#: 整個 OTP 階段的上限。要含得下「等一組新碼」（最多 30 秒）加幾輪重試。
_OTP_TIMEOUT = 90.0
#: 找角色伺服器 socket 的上限。
#:
#: ⚠ 剛換到角色伺服器的那幾秒**複製不到那個 socket handle**（實測：
#: 列舉得到 773 個 handle、複製成功 552 個，但裡面只有 GameGuard 那條 443，
#: 遊戲連線那條不在其中）；過一會兒再找就 0.1 秒找到。所以要給它時間重試。
_PIN_SOCKET_TIMEOUT = 20.0
#: 等伺服器送來二次密碼 seed（`0x08B9`）的上限。
#:
#: ⚠ 它不是連上角色伺服器就馬上到 —— 實測要等客戶端把角色清單收完
#: （`0x0B72`）之後才出現，從 OTP 過關算起可能超過 20 秒。等太短就會
#: 一直「缺料」（踩過：15 秒不夠）。
_PIN_SEED_TIMEOUT = 40.0
_POLL = 0.4
#: 送出**之前**確認「字打到哪一格」的等待上限。整條登入只跑這一次。
#: 一次記憶體搜尋約 0.4 秒（[MEM-032]），而每一圈搜兩次（帳號、密碼），
#: 所以這裡大約是三圈。
#: ⚠ 逾時**不是判決**：兩個都搜不到代表「讀不到」，照舊送出去
#: —— 絕不能拿它當「打反了」用（見 `_fields_look_right` 的第一版錯誤）。
_FIELD_CHECK_TIMEOUT = 3.0
#: 探針打進去之後，最多等這麼久去記憶體裡找它。
#:
#: 找得到＝焦點在帳號欄（[MEM-032]：帳號欄的字在堆積上找得到，密碼欄的**整個
#: 記憶體都搜不到**）。一次掃描約 0.4 秒（只掃 private 可寫區段），所以這裡
#: 大約是掃 5 次。⚠ **逾時不代表「在密碼欄」是確定的** —— 讀不到也會逾時，
#: 那時就退回舊的預設假設，並且照舊有送出後的閉環驗證兜底。
_PROBE_TIMEOUT = 2.5

_VK_HOME, _VK_DELETE = 0x24, 0x2E
_VK_END, _VK_BACKSPACE = 0x23, 0x08
_VK_TAB, _VK_RETURN = 0x09, 0x0D
_VK_UP, _VK_DOWN = 0x26, 0x28
#: 選伺服器前先按幾次「上」保證回到第一項（清單只有兩台，按 5 次綽綽有餘）。
_SERVER_LIST_TOP = 5
#: 清空欄位要按幾次。RO 的帳號欄最多 23 字，按 32 次一定夠。
_CLEAR_KEYS = 24


@dataclass
class LoginProgress:
    """一次自動登入的過程記錄。失敗時它就是診斷報告。"""

    steps: list[str] = field(default_factory=list)
    #: 這次登入是什麼時候開始的（`time.monotonic()`）。每一步都會標上經過秒數 ——
    #: 使用者回報「卡卡的、每個流程有點慢」時，沒有時間戳就只能猜是哪一步。
    started: float = field(default_factory=time.monotonic)
    ok: bool = False
    failed_at: str = ""
    detail: str = ""
    #: 登入成功但**停在選角畫面**的原因（沒設定角色、或那台沒有這隻）。
    #: 空字串代表一路進到遊戲裡。
    stopped_at_character: str = ""

    def note(self, text: str) -> None:
        stamped = f"[{time.monotonic() - self.started:5.1f}s] {text}"
        log.info("[自動登入] %s", stamped)
        self.steps.append(stamped)

    def fail(self, where: str, detail: str) -> LoginProgress:
        self.failed_at = where
        self.detail = detail
        log.warning("[自動登入] 卡在「%s」：%s", where, detail)
        return self

    @property
    def summary(self) -> str:
        if self.ok and self.stopped_at_character:
            # ⚠ 登入是成功了，但**沒進到遊戲裡** —— 要講清楚，
            # 不然使用者以為已經在玩了，實際上還停在選角畫面。
            return f"已登入，停在選角畫面：{self.stopped_at_character}"
        if self.ok:
            return "登入流程送出完成"
        return f"卡在「{self.failed_at}」：{self.detail}"


class AutoLogin:
    """把一個帳號登進遊戲。

    `on_step` 會在每一步被呼叫（給 UI 顯示進度用），在**呼叫端的執行緒**執行。
    """

    def __init__(
        self,
        account: Account,
        pid: int,
        on_step: Callable[[str], None] | None = None,
    ) -> None:
        self._account = account
        self._pid = pid
        self._on_step = on_step
        self._packets: list = []
        # 已經看過的封包位置。橫跨整場，讓每一步依序消費、不會跳過
        # 同一瞬間到達的下一個訊號（見 _wait_packet）。
        self._cursor = 0
        self._capture: PacketCapture | None = None
        # 登入前半段要鎖前景（見 services/login_lock）。走封包的部分不鎖。
        self._lock: LoginLock | None = None
        # 登入伺服器的位址。OTP 過了之後客戶端會換到下一台，用它分辨。
        self._login_server: tuple | None = None
        # 這一次登入讀到的角色清單（給呼叫端存回帳號設定）。
        self.characters: list = []
        # 從 `0x0064` 抓到的密碼密文（十六進位）。存起來之後就能**不開遊戲**
        # 直接跟伺服器要角色清單（見 services/login_client 的檔頭）。
        self.password_blob: str = ""
        # 這一次實際進到哪一台伺服器（用連到的 IP 認出來的）。
        self.server_name: str | None = None
        # 第一次登入時使用者只填得出格號；這裡回報那一格**實際上是誰**，
        # 讓呼叫端把名字存回設定 —— 下一次就用名字（身分）而不是格號（位置）。
        self.learned_character: str = ""
        # 這一輪要不要先按一次 Tab（＝焦點在密碼欄）。
        # 一開始用記憶體判斷，判斷不出來就先假設不用；**送出去發現打反就翻面**。
        self._tab_first: bool | None = None
        #: 已經打過一輪了嗎。重試時欄位裡有上一輪的字，一定要清空。
        self._typed_once = False
        #: 送出前的檢查已經翻過一次面了嗎。只准翻一次 —— 見 `_fields_look_right`。
        self._presubmit_flipped = False
        self.progress = LoginProgress()

    # ---- 對外 -------------------------------------------------------

    def run(self) -> LoginProgress:
        try:
            return self._run()
        finally:
            if self._capture is not None:
                self._capture.stop()
                self._capture = None
            # ⚠ 一定要解鎖。使用者的鍵盤滑鼠被 BlockInput 擋著，
            # 這裡漏掉就等於把人鎖在外面（login_lock 另有看門狗兜底）。
            if self._lock is not None:
                self._lock.release()
                self._lock = None

    # ---- 流程 -------------------------------------------------------

    def _run(self) -> LoginProgress:
        game_input.ensure_dpi_aware()

        # ⚠ 剛開的遊戲**要幾十秒才會畫出視窗**（客戶端有加殼，要先解殼）。
        # 立刻檢查會當場誤判成失敗（實測踩過）。等它。
        hwnd = self._wait_window(_WINDOW_TIMEOUT)
        if hwnd is None:
            return self.progress.fail(
                "等遊戲視窗",
                f"{_WINDOW_TIMEOUT:.0f} 秒內 PID {self._pid} 沒有畫出遊戲視窗。",
            )
        if game_screen.is_minimised(hwnd):
            return self.progress.fail(
                "檢查視窗",
                "遊戲視窗被最小化了。最小化的視窗收不到任何輸入，請先還原它。",
            )
        self._step(f"找到遊戲視窗（PID {self._pid}）")

        # 封包擷取要在送出任何東西**之前**就開著，否則登入那一包會漏掉（[PKT-054]）。
        self._capture = PacketCapture(self._pid, self._packets.append)
        if not self._capture.start():
            return self.progress.fail("開擷取", "封包擷取起不來（需要系統管理員）。")
        self._step("封包擷取已啟動")

        # 從這裡到 OTP 送出為止都要前景輸入 —— 把遊戲鎖在最前面，
        # 並擋掉使用者的實體鍵鼠。中途被搶走焦點的話，後面的按鍵會打進
        # 使用者正在用的視窗（實際發生過）。
        self._lock = LoginLock(hwnd, on_note=self._step)
        self._lock.__enter__()

        if not self._send_credentials(hwnd):
            return self.progress
        if not self._send_otp(hwnd):
            return self.progress

        # 到這裡前景輸入的部分結束了，後面走封包 —— 把鍵鼠還給使用者。
        if self._lock is not None:
            self._lock.release()
            self._lock = None

        # ⚠ **二次密碼過了才准選角。** 沒確認就送 `0x0066` 的後果是：
        # 伺服器把角色標成「進入遊戲中」，客戶端卻還停在二次密碼畫面 ——
        # 角色就這樣**卡登**（實際發生過，使用者的角色卡住進不去）。
        pin_ok = self._send_pin(hwnd)
        self._remember_characters()
        if pin_ok:
            self._select_character(hwnd)
        else:
            reason = "二次密碼沒有得到伺服器確認，不敢送選角（會把角色卡在登入中）"
            self.progress.stopped_at_character = reason
            self._step(reason)

        self.progress.ok = True
        self._step("登入完成，接下來走封包")
        return self.progress

    # ---- 各步驟 -----------------------------------------------------

    def _click_agree(self, hwnd: int) -> None:
        """按合約書的「同意」。**自己一個子行程**，理由見 `_send_credentials`。

        點法本身也有講究：游標移過去要**停一下**再按，遊戲的 UI 要先被
        「滑過」才會進入可按狀態（見 `input.click_foreground`）。

        位置優先用設定裡學到的比例，沒有才用內建的預設值 —— 預設值是在
        1280x800 的客戶端上量的，別人的解析度不同時會點空，那時候
        `_learn_agree_button` 會跟他學一次。
        """
        try:
            # ⚠ **先從畫面把按鈕找出來**（`agree_button_by_look`）。
            # 合約書是遊戲自己畫的小視窗，而且**可以拖動** —— 用視窗大小算比例
            # 在別的解析度會跑掉，被拖一下也會跑掉。找不到才退回比例法。
            # ⚠ 搜尋要在**子行程**裡做：截圖與送輸入不能在同一個行程（[INP-009]）。
            spot = input_helper.agree_button(hwnd)
            if spot is None:
                spot = game_screen.agree_button_position(hwnd, self._agree_ratio())
            input_helper.send(hwnd, [input_helper.click(*spot)])
        except (input_helper.InputHelperError, game_screen.ScreenError) as exc:
            log.debug("點合約書失敗（可能沒有合約書）：%s", exc)

    @staticmethod
    def _agree_ratio() -> tuple[float, float] | None:
        """設定裡學到的按鈕比例；沒學過回 None（用內建預設值）。"""
        from ro_toolbox.config.settings import current_settings

        saved = current_settings().agree_button
        if not saved or len(saved) != 2:
            return None
        rx, ry = float(saved[0]), float(saved[1])
        # 合理性：比例一定落在視窗內。存壞了就當沒學過，不要拿它去點螢幕外面。
        if not (0.0 < rx < 1.0 and 0.0 < ry < 1.0):
            log.warning("設定裡的同意按鈕比例不合理（%s），改用內建值", saved)
            return None
        return rx, ry

    def _learn_agree_button(self, hwnd: int, timeout: float) -> bool:
        """點不掉的時候：請使用者按一次，**把他按的位置學起來**。

        為什麼需要這一步：合約書那個畫面只吃滑鼠（鍵盤全試過都沒反應，
        [INP-001]），而按鈕位置會隨客戶端的解析度設定跑掉 ——
        內建的比例是在 1280x800 上量的。與其猜別人的版面，不如問一次。

        怎麼知道他按了哪裡：一直看兩件事 —— 游標在哪、畫面還是不是合約書。
        合約書消失的**那一瞬間**，游標所在的位置就是按鈕（存成視窗內的比例）。
        中途游標亂晃沒關係，我們只取轉變前最後一個落在視窗內的位置。
        """
        import ctypes
        from ctypes import wintypes

        from ro_toolbox.config.settings import current_settings, save_settings

        user32 = ctypes.windll.user32
        self._step(
            "自動按不掉合約書（你的解析度可能跟預設值不同）—— "
            "請你手動按一次「同意」，我會把位置記起來，之後就不用了"
        )
        deadline = time.monotonic() + timeout
        last: tuple[int, int] | None = None
        while time.monotonic() < deadline:
            point = wintypes.POINT()
            if user32.GetCursorPos(ctypes.byref(point)):
                ratio = game_screen.window_ratio_of(hwnd, point.x, point.y)
                if ratio and 0.0 < ratio[0] < 1.0 and 0.0 < ratio[1] < 1.0:
                    last = (point.x, point.y)
            try:
                gone = game_screen.detect(
                    game_screen.capture(hwnd)
                ) is not game_screen.Stage.EULA
            except game_screen.ScreenError:
                gone = False
            if gone:
                if last is None:
                    self._step("合約書過了，但沒看到你按在哪 —— 這次不學")
                    return True
                ratio = game_screen.window_ratio_of(hwnd, *last)
                if ratio is None:
                    return True
                settings = current_settings()
                settings.agree_button = [round(ratio[0], 4), round(ratio[1], 4)]
                try:
                    save_settings(settings)
                except Exception as exc:  # noqa: BLE001 - 存不了不該讓登入失敗
                    log.warning("學到的同意按鈕位置存不起來：%s", exc)
                self._step(
                    f"記起來了：同意按鈕在視窗的 "
                    f"({ratio[0]:.4f}, {ratio[1]:.4f})，下次自動按"
                )
                return True
            time.sleep(_POLL)
        return False

    def _still_on_eula(self, hwnd: int) -> bool:
        """畫面是不是還停在合約書。判斷不出來就當作不是 —— 這個判斷只用來
        決定「要不要改口問使用者」，寧可不問也不要問錯。"""
        try:
            return game_screen.detect(
                game_screen.capture(hwnd)
            ) is game_screen.Stage.EULA
        except Exception as exc:  # noqa: BLE001
            log.debug("判斷畫面失敗：%s", exc)
            return False

    #: 用來探測焦點在哪一格的標記。挑一個不會跟遊戲內容撞的組合。
    _PROBE = "ZQ7X4K"

    def _clear_actions(self) -> list[dict]:
        """把目前這一格清乾淨，**兩個方向都清**。

        客戶端會記住上次的帳號，直接打字是接在後面；而且 `Home`+`Delete` 不夠
        （那個欄位不見得吃 Home），要再補 `End`+`Backspace`。

        ⚠⚠ **走視窗訊息，不要改成真的按鍵。** v0.2.5 為了少開子行程把它改成
        `key_foreground`，結果自動登入**整個爛掉**：實跑 11 次，合約書明明過了
        （畫面判定＝登入畫面）、欄位也填對了（ID 欄看得到帳號、密碼欄 9 個星號），
        但**客戶端一次都沒有連上伺服器** —— 最後那個 Enter 送不出去，
        `submitted_account()` 全程是 None。改回來就好了。
        機制沒有查清楚，但結論很清楚：**這條路驗過會動，不要為了省時間去動它。**
        真正該省的是子行程的啟動成本（[INP-013]），不是送法。
        """
        return [
            input_helper.key(_VK_HOME),
            input_helper.key(_VK_DELETE, _CLEAR_KEYS),
            input_helper.key(_VK_END),
            input_helper.key(_VK_BACKSPACE, _CLEAR_KEYS),
        ]

    def _type_actions(self, text: str) -> list[dict]:
        """打一段字。**用真的按鍵（Unicode），不用視窗訊息。**

        使用者的輸入法停在中文時，`WM_CHAR` 送進去的**英文字母會被吃掉**
        （數字照過）—— 密碼 `s26011034` 進到欄位變成 `26011034`，
        送出去伺服器回「帳密錯誤」。`KEYEVENTF_UNICODE` 直接把字元碼塞進
        輸入串流，**完全不經過 IME**。
        """
        return [input_helper.focus(), input_helper.text_foreground(text)]

    def _tab_actions(self) -> list[dict]:
        """換行。**Tab 要送真的按鍵**（用視窗訊息送的 Tab 不生效，實測）。

        使用者手動確認：Tab 在帳號／密碼兩格之間來回，Enter 是直接送出。
        """
        return [input_helper.focus(), input_helper.key_foreground(_VK_TAB)]

    def _probe_focus(self, hwnd: int) -> bool | None:
        """量出焦點現在在哪一格。回「是不是在密碼欄」，量不出來回 None。

        ## 為什麼要量，不能猜

        使用者實測的規則本身沒錯：**帳號欄有預填 → 焦點在密碼欄；
        帳號欄是空的 → 焦點在帳號欄。** 問題是舊版拿來判斷的訊號
        **根本分不出這兩種情況**：它讀的是「上次送出去的帳號」那塊
        靜態緩衝（`input_helper.submitted_account()`），而 [MEM-032] 白紙黑字寫著
        那塊**送出之後才有值**、「不能拿來判斷現在框裡有什麼」。
        遊戲剛開還沒送出過任何東西，兩種情況讀到的都是空的 ——
        於是每次都賭同一邊，賭錯就整輪重來（使用者回報「一直打反」）。

        ## 怎麼量

        [MEM-032] 量過一個**不對稱**，這裡拿它當判準：

            打進帳號欄的字 → 輪詢幾拍就能在**堆積**上找到（ASCII）
            打進密碼欄的字 → **整個記憶體都搜不到**

        所以往現在有焦點的那一格打一個探針字串，再去記憶體裡找它：
        找得到＝在帳號欄，找不到＝在密碼欄。**不必送出去讓伺服器拒絕才知道。**

        探針用固定字串而不是帳號本身：客戶端會把記住的帳號預填進帳號欄，
        拿帳號去搜的話，就算焦點在密碼欄也會搜到那個預填值 —— 永遠是「找得到」。

        ⚠ **逾時不等於「在密碼欄」是確定的**：記憶體讀不到（GameGuard、
        行程權限）也會逾時。所以回 None 讓呼叫端沿用舊的預設假設，
        而且送出之後的閉環驗證照舊留著兜底。
        """
        try:
            # ⚠ **不先清空。** 使用者實測：剛進登入畫面時，有焦點的那一格
            # 本來就是空的（客戶端記住帳號時焦點會落在空的密碼欄，
            # 沒記住時落在空的帳號欄）。白清一次就是白開一個子行程。
            # 就算真的接在既有文字後面也不影響判斷 —— 我們搜的是探針**子字串**。
            input_helper.send(hwnd, self._type_actions(self._PROBE))
        except input_helper.InputHelperError as exc:
            log.debug("探針送不進去：%s", exc)
            return None
        deadline = time.monotonic() + _PROBE_TIMEOUT
        while True:
            if input_helper.field_addresses(self._pid, self._PROBE):
                return False              # 找得到 → 焦點在帳號欄
            if time.monotonic() >= deadline:
                return None               # 找不到，但也可能是讀不到 —— 別當定論
            time.sleep(_POLL)

    def _decide_focus(self, hwnd: int) -> None:
        """決定 `self._tab_first`（要不要先打密碼）。只在第一次做。"""
        if self._tab_first is not None:
            return
        measured = self._probe_focus(hwnd)
        if measured is None:
            # ⚠ 量不出來時**預設「焦點在密碼欄」**。客戶端只要登入過一次就會
            # 記住帳號，之後焦點都落在密碼欄（使用者實測，實務上幾乎永遠如此）。
            # 猜錯也不會卡死：送出後有閉環驗證，錯了就翻面重打。
            self._tab_first = True
            self._step("量不出焦點在哪一格（記憶體讀不到），沿用預設：焦點在密碼欄")
        else:
            self._tab_first = measured
            self._step(
                f"量到焦點在{'密碼欄' if measured else '帳號欄'}"
                f"（探針 {self._PROBE} 在堆積上{'找不到' if measured else '找得到'}）"
            )

    def _credential_batches(self, hwnd: int) -> list[list[dict]]:
        """組出整組輸入動作，**一批一個子行程**。

        順序由 `self._tab_first` 決定（`_decide_focus` 量出來的），都只需要一次 Tab：

            焦點在密碼欄：打密碼 → Tab 到帳號欄 → 清空 → 打帳號
            焦點在帳號欄：打帳號 → Tab 到密碼欄 → 清空 → 打密碼

        ## 為什麼要分成六批

        Tab 與文字要送真的按鍵（`SendInput`），清空與 Enter 走視窗訊息，
        而**同一個行程送過 `SendInput` 之後，它後續的視窗訊息會被封鎖**
        （[INP-009]）。所以每種通道各自一個乾淨的子行程。

        ⚠⚠ **不要為了少開子行程去合併。** v0.2.5 把清空也改成按鍵、六批併成
        兩批，結果自動登入**整個爛掉**（實跑 11 次，合約書過了、欄位也填對了，
        但客戶端一次都沒連上伺服器 —— 見 `_clear_actions`）。
        打包後每批要 2.7 秒是真的很痛，但**答案是讓子行程變便宜（[INP-013]），
        不是改送法**。
        """
        self._decide_focus(hwnd)
        if self._tab_first:
            first, second = self._account.password, self._account.username
        else:
            first, second = self._account.username, self._account.password
        batches = [
            # 第一格一定要清：探針剛剛就打在這裡（重試時則是上一輪打的）。
            self._clear_actions(),
            self._type_actions(first),
            self._tab_actions(),
        ]
        if self._needs_clear_after_tab():
            batches.append(self._clear_actions())
        batches.append(self._type_actions(second))
        batches.append([input_helper.key(_VK_RETURN)])  # Enter 走視窗訊息
        self._typed_once = True
        return batches

    def _fields_look_right(self) -> bool:
        """兩格都打完、**還沒按 Enter** 時檢查。**只在確定打反時才回 False。**

        ## ⚠ 這一支第一版是錯的，錯法很值得記住

        第一版是「搜不到自己的帳號就翻面」。但「搜不到」有**兩種**原因：
        真的打反了，或**記憶體那一拍讀不到**。把後者也當成打反的後果是：

            第 1 次：其實打對了 → 但搜不到 → 翻面（把對的改成錯的）
            第 2 次：真的打反了 → 已經翻過一次 → 照樣送出去

        使用者實測回報：「當初始在密碼，流程應該要直接輸入密碼→Tab→刪除帳號
        →輸入帳號，但是他現在輸入帳號是我的密碼」—— 就是這個。
        **不確定的時候去「修」一個本來是對的東西，是最糟的失效方式。**

        ## 現在的做法：兩個都是**正面**訊號，分不出來就放行

        [MEM-032] 的不對稱給了兩個方向都能確定的訊號：

            自己的帳號出現在堆積 → 它在帳號欄 → **確定打對**
            自己的密碼出現在堆積 → 密碼被打進帳號欄 → **確定打反**
            兩個都搜不到         → 讀不到，**不知道** → 照舊送出去

        （打進密碼欄的字整個記憶體都搜不到，所以密碼只要現身就一定在帳號欄。
        而帳號欄在打之前一定清過，搜到的不會是客戶端預填的舊值。）

        最壞情況因此**跟沒有這一支時完全一樣**：照舊送出，由送出後的閉環驗證
        （`submitted_account()`）兜底。它只會幫忙，不會把對的弄成錯的。

        ⚠ 送出前**只准翻面一次**，避免兩邊互相推來推去。
        """
        if self._presubmit_flipped:
            return True
        deadline = time.monotonic() + _FIELD_CHECK_TIMEOUT
        while True:
            if input_helper.field_addresses(self._pid, self._account.username):
                return True                       # 確定打對
            if input_helper.field_addresses(self._pid, self._account.password):
                # 密碼現身在堆積 = 它被打進帳號欄 = 確定打反
                self._presubmit_flipped = True
                self._tab_first = not self._tab_first
                self._step(
                    f"送出前發現密碼被打進帳號欄（打反了）—— 不送出，改成先打"
                    f"{'密碼' if self._tab_first else '帳號'}重打一次"
                )
                return False
            if time.monotonic() >= deadline:
                # 兩個都搜不到 = 讀不到，**不知道**。照舊送出去，
                # 讓送出後的閉環驗證兜底 —— 不准在這裡亂猜。
                self._step("送出前驗不出字打到哪一格（記憶體讀不到），照舊送出")
                return True
            time.sleep(_POLL)

    def _needs_clear_after_tab(self) -> bool:
        """Tab 過去那一格要不要先清空。**答案永遠是要。**

        ⚠ 這裡試過一次最佳化又收回來，理由值得記住。

        使用者實測的規則是對的：剛進登入畫面時有焦點的那一格是空的，
        而客戶端只把記住的帳號預填在**帳號欄**。照這個規則，
        「焦點在帳號欄 → Tab 過去的密碼欄是空的 → 不用清」應該成立，
        而且可以省掉一個子行程（打包後就是 2.7 秒，[INP-013]）。

        **但那個推論有兩個洞**：

        1. **我們對「Tab 過去是哪一格」的把握，來自探針**（`_probe_focus`），
           而探針**可能判錯**（找不到時分不出「在密碼欄」與「記憶體讀不到」）。
           判錯的時候「不用清」就會讓舊值留著，直接送出去。
        2. 登入畫面上有「**存檔**」選項，客戶端不見得只記帳號 ——
           「密碼欄一定是空的」本來就不是我們驗過的事實。

        使用者實測回報：「輸入完密碼移動到帳號應該要刪除帳號再打入真帳號，
        可是他沒刪除舊帳，ENTER 導致錯誤。雖然最後還是成功但不該有那個錯誤。」

        **代價完全不對稱**：省下 2.7 秒 vs 一次打錯送出（伺服器拒絕、跳錯誤框、
        關掉、翻面、整輪重打 ≈ 25 秒）。所以一律清。

        （探針**之前**那一次仍然不用清 —— 那一格是空的與否都不影響判斷，
        我們搜的是探針**子字串**，見 `_probe_focus`。）
        """
        return True

    def _dismiss_error(self, hwnd: int) -> None:
        """關掉「帳密錯誤」對話框。按一次 Enter 就回到登入畫面（使用者實測）。

        ⚠ 只在**確定送出失敗之後**呼叫。登入畫面上按 Enter 會直接送出，
        沒事亂按只會多送一次錯的帳密、再多跳一個框。
        """
        try:
            input_helper.send(hwnd, [input_helper.key(_VK_RETURN)])
        except input_helper.InputHelperError as exc:
            log.debug("關錯誤框失敗：%s", exc)

    def _restart_capture(self) -> None:
        """把封包擷取重新綁到目前的連線上。

        換伺服器＝**新的一條連線**，舊的擷取綁在舊位址上，新連線一開始那幾包
        會整段漏掉。重開一份最省事，也不會漏掉已經收到的封包
        （`self._packets` 是累積的，不清空）。
        """
        try:
            if self._capture is not None:
                self._capture.stop()
            self._capture = PacketCapture(self._pid, self._packets.append)
            self._capture.start()
        except Exception as exc:  # noqa: BLE001 - 重開失敗不該讓登入失敗
            log.debug("重開擷取失敗：%s", exc)

    def _send_pin(self, hwnd: int) -> bool:
        """送二次密碼。**用封包**，因為那一格根本沒有鍵盤可以打。

        ## 為什麼不能打字

        客戶端那一格是螢幕上的**虛擬鍵盤，而且每次排列都被伺服器打亂**
        （使用者實測）。送出去的四位數是「按鍵在那次亂序裡的位置」，
        不是密碼本身 —— 同樣按 `8291`，兩次分別送出 `5367` 與 `8623`。

        ## 亂序怎麼來的

        伺服器在要求輸入時先送一包 `0x08B9` 帶 seed，雙方用同一個
        決定性演算法算出那張 0–9 對照表（所以伺服器驗得起來）。
        常數與驗證見 `services/login_packets`。

        ## 回傳值＝「伺服器確認過了沒」

        送出去不等於過關。伺服器會回一包 `0x08B9`：**全零＝正確**（實機對照過）。
        沒等到那一包就往下選角，等於閉著眼睛賭 —— 而賭輸的下場是角色卡登。
        所以送不出去、或等不到確認，一律回 `False`，選角就不做。
        """
        pin = (self._account.pin or "").strip()
        if not pin:
            # 沒設定二次密碼：也許這個帳號本來就沒開。看伺服器最後說什麼 ——
            # 只有它明白說 OK 才往下走。
            self._step("沒有設定二次密碼")
            return self._pin_already_ok()

        # ⚠ seed 那一包（`0x08B9`）是**連上角色伺服器之後**才送過來的 ——
        # OTP 剛過就去翻擷取一定翻不到（實測 seed=None）。等它。
        # ⚠ 要用**最後一包**的 seed，不是第一包。伺服器可能重問一次（換新 seed），
        # 拿舊的去算出來的四位數就是錯的 —— 而錯的二次密碼會被退回，
        # 然後我們如果還往下選角，角色就卡登了。
        seed = None
        aid = None
        deadline = time.monotonic() + _PIN_SEED_TIMEOUT
        while time.monotonic() < deadline:
            for packet in self._packets:
                if packet.opcode == login_packets.OP_PIN_STATE:
                    fresh = login_packets.pin_seed(packet.payload)
                    if fresh is not None:
                        seed = fresh          # 後面的蓋掉前面的
                elif packet.opcode == login_packets.OP_LOGIN_ACCEPTED and aid is None:
                    aid = login_packets.parse_aid(packet.payload)
            if seed is not None and aid is not None:
                break
            time.sleep(_POLL)
        if seed is None or aid is None:
            self._step(
                f"缺料，二次密碼請手動輸入（seed={seed}, AID={aid}）"
            )
            return False

        sock = self._char_server_socket()
        if not sock:
            self._step("找不到遊戲的 socket，二次密碼請手動輸入")
            return False

        # 先記下現在收到幾包，等一下只看**送出之後**才到的回應 ——
        # 送出前那一包 `0x08B9` 是「請輸入」，拿它當確認就永遠是通過。
        mark = len(self._packets)
        try:
            encoded = login_packets.encode_pin(seed, pin)
            game_socket.send_on_socket(
                sock, login_packets.pin_packet(aid, encoded)
            )
            # ⚠ 只記位置，不記密碼本身。
            self._step(f"二次密碼已用封包送出（這一輪的鍵盤位置 {encoded}）")
        except (login_packets.LoginPacketError, OSError) as exc:
            self._step(f"送二次密碼失敗：{exc}")
            return False
        finally:
            game_socket.close_socket(sock)
        return self._wait_pin_ok(mark)

    def _pin_already_ok(self) -> bool:
        """設定裡沒有二次密碼時，判斷「可不可以直接去選角」。

        ⚠ **二次密碼本來就可以不設定**（使用者確認）。那種帳號伺服器根本
        不會問，也就不會有任何 `0x08B9` —— 這時候硬要等一包「通過」的話，
        帳號明明好好的卻永遠停在選角畫面。所以：

        - **一包 `0x08B9` 都沒有** → 伺服器沒問 → 可以直接選角。
        - 最後一包說 `OK`（全零）→ 通過 → 可以選角。
        - 最後一包在**問**（state 1）→ 這個帳號有設二次密碼但我們沒有 →
          停手，請使用者去設定裡填。
        - 其他狀態 → 沒實測過的情況，一律停手並把數字報出來。
        """
        state = None
        seen = False
        for packet in self._packets:
            if packet.opcode == login_packets.OP_PIN_STATE:
                seen = True
                state = login_packets.pin_state(packet.payload)
        if not seen:
            self._step("伺服器沒有要求二次密碼（這個帳號沒設定）")
            return True
        if state == login_packets.PIN_STATE_OK:
            self._step("伺服器說這個帳號不需要二次密碼")
            return True
        if state == login_packets.PIN_STATE_ASK:
            self._step(
                "這個帳號有設二次密碼，但工具的設定裡沒有 —— "
                "去編輯帳號填一下，或這次自己輸入"
            )
        else:
            self._step(f"二次密碼的狀態看不懂（{state}），不敢往下走")
        return False

    def _wait_pin_ok(self, mark: int) -> bool:
        """等伺服器對二次密碼的回應。**全零＝正確**（實機對照過）。

        `mark` 是送出當下已經收到的封包數 —— 只看之後才到的，
        不然會把送出前那包「請輸入」誤判成通過。
        """
        deadline = time.monotonic() + _PIN_REPLY_TIMEOUT
        while time.monotonic() < deadline:
            for packet in self._packets[mark:]:
                if packet.opcode != login_packets.OP_PIN_STATE:
                    continue
                state = login_packets.pin_state(packet.payload)
                if state == login_packets.PIN_STATE_OK:
                    self._step("二次密碼通過")
                    return True
                if state == login_packets.PIN_STATE_ASK:
                    continue        # 還在問，再等（有時候會重送一次）
                self._step(f"二次密碼被伺服器退回（狀態 {state}）")
                return False
            time.sleep(_POLL)
        self._step(f"{_PIN_REPLY_TIMEOUT:.0f} 秒內等不到二次密碼的確認")
        return False

    def _remember_characters(self) -> None:
        """把這個帳號的角色清單記到本機，下次就能直接選名字。

        角色清單是伺服器送的 `0x0B72`（每筆 175 bytes，見 `services/char_list`）。
        **第一次登入時使用者只能自己填格號**（我們還不知道有哪些角色）；
        登入過一次之後就有清單了。

        ⚠ 存的是「角色名稱 ← 身分」，格號只是快取 —— 角色刪掉重建、換順序，
        格號都會變（CLAUDE.md：存身分，不存位置）。
        """
        from ro_toolbox.services import char_list

        # ⚠ **角色清單會分成好幾包送**，要全部收齊再合併。
        # 早期版本讀到第一包就 return —— 少掉的那幾隻不會有任何錯誤訊息，
        # 使用者只會發現「我的角色不見了」（實測：三隻列出來，實際登入的
        # 那一隻在另一包裡，格號 4）。這正是「安靜地少東西」。
        # ⚠ **要等它到齊，不能讀當下有什麼就算什麼。**
        # 以前這裡沒有等：有二次密碼的帳號剛好被「送二次密碼」那三秒擋著，
        # 清單就先到了；沒設二次密碼的帳號一路衝下來，讀到的是空的，
        # 然後回報「讀不到角色清單」——帳號其實好好的（實際踩過）。
        # 那就是「拿時間當機制」的典型：碰巧會過，換個情況就壞。
        deadline = time.monotonic() + _CHAR_LIST_TIMEOUT
        quiet_until = 0.0
        while time.monotonic() < deadline:
            has = any(p.opcode == OP_CHAR_LIST for p in self._packets)
            if has:
                if not quiet_until:
                    # 清單會分成好幾包 —— 收到第一包之後再等一下下，
                    # 等後面那幾包也到齊（下面的合併本來就會處理重複）。
                    quiet_until = time.monotonic() + _CHAR_LIST_QUIET
                elif time.monotonic() >= quiet_until:
                    break
            time.sleep(_POLL)

        merged: dict[int, object] = {}
        for packet in self._packets:
            if packet.opcode != OP_CHAR_LIST:
                continue
            try:
                entries = char_list.parse(packet.payload)
            except Exception as exc:  # noqa: BLE001 - 解析失敗不該讓登入失敗
                log.debug("解析角色清單失敗：%s", exc)
                continue
            for entry in entries:
                merged[entry.slot] = entry
        if not merged:
            return
        self.characters = [merged[slot] for slot in sorted(merged)]
        self._step(
            "記住角色清單："
            + "、".join(f"{e.slot} {e.name}" for e in self.characters)
        )

    def _select_character(self, hwnd: int) -> bool:
        """選角。**這一包是明文**（實測 payload 就一個格號），走封包。

        設定裡沒有格號就跳過 —— **不准猜格號**（送錯格會登入到別的角色）。
        第一次登入時使用者要自己填；登入之後我們會把角色清單記到本機，
        之後就能直接選名字（見 `_remember_characters`）。
        """
        wanted = (self._account.server or "").strip()
        if wanted and self.server_name and wanted != self.server_name:
            # ⚠ **台別不對就停手。** 每台的角色各自獨立，同一個格號在兩台
            # 是不同的人（實測：兩份擷取都選格號 3，卻是兩隻不同的角色）。
            # 這時候照著設定的格號送出去，就是安靜地登入到別人。
            reason = (
                f"設定要「{wanted}」，實際進到「{self.server_name}」—— 台別不對，"
                "不敢選角（免得選到別的角色）"
            )
            self.progress.stopped_at_character = reason
            self._step(reason)
            return True

        # ⚠ **一律用角色名稱現查格號，不准拿舊快取。**
        # `char_slot` 是上次看到的位置，切了伺服器就完全對不上 ——
        # 實測踩過：波利留下的格號 4 被拿去查爾斯用，而查爾斯只有 0~3。
        wanted_character = (self._account.character or "").strip()
        if wanted_character:
            slot = self._account.slot_of(wanted_character, self.server_name or "")
            if slot is None:
                reason = (
                    f"「{self.server_name or '這一台'}」上沒有角色「{wanted_character}」"
                    "（清單已經更新，去設定裡重選）"
                )
                self.progress.stopped_at_character = reason
                self._step(reason)
                return True
        else:
            slot = self._first_login_slot()
            if slot is None:
                return True
            # 第一次登入是照格號進來的，名字剛剛才查出來 —— 下面要拿它跟
            # 客戶端手上那一格比對，所以這裡把它補上。
            wanted_character = self.learned_character

        return self._pick_in_client(int(slot), wanted_character, hwnd)

    def _pick_in_client(self, slot: int, wanted: str, hwnd: int) -> bool:
        """讓**客戶端自己**選角：移到那一格 → 確認 → 按 Enter。

        ## 為什麼不自己送 `0x0066`

        角色名字是客戶端**在處理自己的選角時**寫進記憶體的，伺服器之後不會再送
        （名字只出現在角色清單 `0x0B72`，見 [PKT-056]／[MEM-035]）。
        我們自己送封包的話，伺服器端一切正常、角色也對，但客戶端那一格永遠沒被寫過
        —— 遊戲裡的名字是開機殘渣（亂碼）。實際踩過。

        讓客戶端自己按下去，名字就是對的，而且 `0x0066` 也是它自己送。

        ## 每一步都有讀得到的訊號

            讀索引 → 按方向鍵 → 再讀（有沒有真的動）→ 到目標格才按 Enter
            → 讀客戶端寫的名字（是不是我們要的那隻）→ 等 0x0AC5

        中間任何一步對不上就停手。**不准盲按幾下就 Enter** —— 那是猜，會選到別人。
        """
        from ro_toolbox.services.character import SelectScreen

        screen = SelectScreen(self._pid)
        if not screen.ready:
            reason = "讀不到選角畫面的游標位置，不敢按 Enter（怕選到別人）"
            self.progress.stopped_at_character = reason
            self._step(reason)
            return True

        try:
            if not self._move_cursor_to(screen, slot, hwnd):
                return True

            mark = len(self._packets)
            input_helper.send(hwnd, [input_helper.key(_VK_RETURN)])
            self._step(f"已按下 Enter 選第 {slot} 格")

            if not self._wait_name_written(screen, wanted):
                return True
            return self._wait_zone_server(mark)
        except input_helper.InputHelperError as exc:
            reason = f"選角時按鍵送不進去：{exc}"
            self.progress.stopped_at_character = reason
            self._step(reason)
            return True
        finally:
            screen.close()

    def _move_cursor_to(self, screen, slot: int, hwnd: int) -> bool:
        """把選角游標移到指定格。移不到就回 False（呼叫端要停手）。"""
        deadline = time.monotonic() + _SELECT_SCREEN_TIMEOUT
        here = screen.index()
        while here is None and time.monotonic() < deadline:
            time.sleep(_POLL)
            here = screen.index()
        if here is None:
            reason = "讀不到選角游標停在第幾格"
            self.progress.stopped_at_character = reason
            self._step(reason)
            return False

        self._step(f"選角游標現在在第 {here} 格，要移到第 {slot} 格")
        for _ in range(_SELECT_MOVE_LIMIT):
            if here == slot:
                return True
            key = _VK_RIGHT if here < slot else _VK_LEFT
            input_helper.send(hwnd, [input_helper.key(key)])
            time.sleep(_SELECT_KEY_PAUSE)
            moved = screen.index()
            if moved is None or moved == here:
                # 按了卻沒動：畫面不在選角、或那個方向到底了。
                reason = (
                    f"選角游標卡在第 {here} 格，移不到第 {slot} 格 —— 停手"
                )
                self.progress.stopped_at_character = reason
                self._step(reason)
                return False
            here = moved
        if here == slot:
            return True
        reason = f"按了 {_SELECT_MOVE_LIMIT} 下還沒移到第 {slot} 格（停在第 {here} 格）"
        self.progress.stopped_at_character = reason
        self._step(reason)
        return False

    def _wait_name_written(self, screen, wanted: str) -> bool:
        """按下 Enter 之後，客戶端會把選到的名字寫進記憶體。核對它。

        對不上就代表游標那個偏移已經失效（改版），要大聲停用 ——
        這是「用兩份獨立的資料交叉驗證」：格號來自伺服器的清單，名字來自客戶端。
        """
        deadline = time.monotonic() + _SELECT_NAME_TIMEOUT
        seen = ""
        while time.monotonic() < deadline:
            seen = screen.read()
            if seen == wanted:
                self._step(f"客戶端確認選到「{wanted}」")
                return True
            if seen:
                reason = (
                    f"按下去之後客戶端寫的是「{seen}」，不是「{wanted}」—— "
                    "游標位置的定位可能已經失效，停手"
                )
                self.progress.stopped_at_character = reason
                self._step(reason)
                return False
            time.sleep(_POLL)
        reason = f"{_SELECT_NAME_TIMEOUT:.0f} 秒內客戶端沒有寫下選到的角色名字"
        self.progress.stopped_at_character = reason
        self._step(reason)
        return False

    def _wait_zone_server(self, mark: int) -> bool:
        """等伺服器回地圖台位址（`0x0AC5`）——**這才叫選角成功**。

        真人登入的順序（2026-08-26 擷取）：

            ↑ 0x0066 選角 → ↓ 0x0AC5 地圖台位址 → ↑ 0x0436 客戶端自己去連地圖台

        `0x0436` 是客戶端自己送的，我們不用管；但**沒收到 0x0AC5 就是沒進去**，
        要講出來，不能因為「封包送出去了」就報成功。
        """
        deadline = time.monotonic() + _ZONE_TIMEOUT
        while time.monotonic() < deadline:
            for packet in self._packets[mark:]:
                if packet.opcode == login_packets.OP_ZONE_SERVER:
                    self._step("伺服器給了地圖台位址 —— 選角成功")
                    return True
                if packet.opcode == login_packets.OP_REFUSE_ENTER:
                    why = packet.payload[0] if packet.payload else "?"
                    self._step(f"伺服器拒絕進入（原因碼 {why}）")
                    self.progress.stopped_at_character = f"選角被拒絕（原因碼 {why}）"
                    return False
            time.sleep(_POLL)
        note = f"{_ZONE_TIMEOUT:.0f} 秒內沒等到地圖台位址 —— 沒有真的進到遊戲"
        self._step(note)
        self.progress.stopped_at_character = note
        return False

    def _first_login_slot(self) -> int | None:
        """第一次登入：使用者只填得出格號 —— 拿它跟**這一次抓到的清單**對。

        還沒登入過的帳號本機沒有角色清單，使用者唯一填得出來的就是格號。
        格號是「位置」，本來不該存 —— 但這裡不是拿舊快取去猜：
        送出前**當場**跟伺服器剛送來的 `0x0B72` 對一次，對得上才送，
        而且順便把那一格的**名字**學起來（`learned_character`），
        下一次就改用名字現查，不再靠格號。

        對不上就**停在選角畫面**（使用者要求：「選的格子沒有的話就停在那邊」）——
        角色清單已經在前一步記好了，所以他回設定裡就能直接從下拉選。
        """
        manual = self._account.char_slot
        if manual is None:
            reason = "還沒指定要登入哪一隻（角色清單已經記下來了，去設定裡選）"
            self.progress.stopped_at_character = reason
            self._step(reason)
            return None

        if not self.characters:
            # 清單沒讀到就**不准送**。這時候送出去等於閉著眼睛猜位置。
            reason = f"讀不到角色清單，不敢照格號 {manual} 送（怕選到別人）"
            self.progress.stopped_at_character = reason
            self._step(reason)
            return None

        found = next((c for c in self.characters if c.slot == manual), None)
        if found is None:
            have = "、".join(f"{c.slot} {c.name}" for c in self.characters)
            reason = (
                f"格號 {manual} 上沒有角色 —— "
                f"「{self.server_name or '這一台'}」有的是 {have}"
            )
            self.progress.stopped_at_character = reason
            self._step(reason)
            return None

        self.learned_character = found.name
        self._step(f"第一次登入：格號 {manual} 是「{found.name}」，記起來")
        return int(manual)

    def _char_server_socket(self):
        """複製遊戲連到角色伺服器的 socket。找不到回 None。

        ⚠ 剛換到角色伺服器的那幾秒複製不到（實測：列舉得到 handle，
        但遊戲那條不在可複製的清單裡），過一會兒就 0.1 秒找到 —— 所以要重試。
        """
        deadline = time.monotonic() + _PIN_SOCKET_TIMEOUT
        server = None
        while time.monotonic() < deadline:
            server = find_server(self._pid) or server
            if server is not None:
                sock = game_socket.find_game_socket(self._pid, server[0], server[1])
                if sock:
                    return sock
            time.sleep(_POLL)
        # 放棄的時候說**一次**就好（迴圈裡每次都記的話兩秒就洗版一百行）。
        log.warning(
            "%.0f 秒內複製不到 PID %s 連到 %s 的 socket",
            _PIN_SOCKET_TIMEOUT, self._pid,
            f"{server[0]}:{server[1]}" if server else "遊戲伺服器",
        )
        return None

    def _wait_connection(self, timeout: float, exclude: tuple | None = None):
        """等客戶端連上伺服器。回傳 (ip, port)，逾時回 None。

        這是「帳密真的送出去了」最可靠的訊號：客戶端只有收下帳密才會去連線。
        比等封包穩 —— 登入這一段的連線是後來才建立的，封包擷取常常整段漏掉
        （實測抓到 0 個封包）。

        `exclude`：忽略這個已知的連線（用來等「換到下一台伺服器」）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            server = find_server(self._pid)
            if server is not None and server != exclude:
                return server
            time.sleep(_POLL)
        return None

    def _send_credentials(self, hwnd: int) -> bool:
        """過合約書、打帳號密碼、送出。

        ## 兩關，各有各的訊號（都不看畫面、不讀記憶體）

        1. **合約書**：點「同意」是前景滑鼠事件，必須自己一個子行程 ——
           同一個行程送過滑鼠事件之後，它後續的 `PostMessage` 會被封鎖（實測）。
           過了沒？合約書在的時候整個視窗不吃 `PostMessage`（[INP-001]），
           所以**送一個無害的 Home 進去試試**：送得進＝過了。
        2. **帳密**：打完送出，客戶端真的收下才會送出 `0x0064`。看到那一包才算數。

        逾時只是放棄的上限，不是成功的依據。
        """
        started = time.monotonic()
        deadline = started + _INPUT_TIMEOUT

        attempt = 0
        asked = False
        while True:
            attempt += 1
            if (not asked and attempt > _AGREE_TRIES
                    and self._still_on_eula(hwnd)):
                # 點了好幾次還在合約書 = 位置對不上，別再盲點下去。
                asked = True
                self._learn_agree_button(hwnd, min(_AGREE_LEARN_SEC,
                                                   deadline - time.monotonic()))
            self._step(f"按同意、輸入帳號密碼並送出（第 {attempt} 次）")
            # ⚠ 不能用「字打不打得進去」判斷合約書過了沒 —— 實測合約書還在時
            # 打進去的字**照樣會進到欄位裡**（只是看不見、Enter 也送不出去），
            # 那個判定會回報「已經好了」然後從此不再按同意（踩過）。
            #
            # ⚠ 想過用畫面判斷「合約書已經不在了就別點」來省兩個子行程，
            # **否決**：那要在主行程截圖，而 tests/test_auto_login.py 明確釘著
            # 「AutoLogin 不准自己截圖（[INP-009]）」。省下來的時間也有限 ——
            # 探針判焦點之後重試本來就少了，多點那一下落在背景圖上是無害的。
            self._click_agree(hwnd)
            try:
                # ⚠ **Enter 單獨留到最後**：送出去之前先確認字打到對的格子。
                # 打反了就翻面重打，不要送出去讓伺服器拒絕 —— 那會跳錯誤框、
                # 要關掉、再整輪重來（使用者實測：「雖然最後還是成功，
                # 但不該有那個錯誤」）。
                *typing, enter = self._credential_batches(hwnd)
                for batch in typing:
                    input_helper.send(hwnd, batch)
                if not self._fields_look_right():
                    continue
                input_helper.send(hwnd, enter)
            except input_helper.InputHelperError as exc:
                log.debug("第 %d 次送不進去：%s", attempt, exc)
                if time.monotonic() >= deadline:
                    self.progress.fail("輸入帳號密碼", f"送不進視窗訊息：{exc}")
                    return False
                continue
            server = self._wait_connection(_CREDENTIAL_TIMEOUT)
            if server is not None:
                # ⚠ 「連上了」**不等於帳密正確** —— 錯的帳密照樣會先連上，
                # 伺服器判定失敗才把連線關掉。所以回頭讀客戶端記下的
                # 「送出去的帳號」，確認我們打對格了。
                # 兩個來源都問：客戶端記下的「送出去的帳號」（靜態位址，
                # 見 input_helper.submitted_account），以及擷取到的 0x0064
                # （明文帳號，[PKT-046]）。任何一個對不上就算打錯格。
                sent_now = input_helper.submitted_account(self._pid)
                if sent_now is None:
                    sent_now = self._sent_account()
                if sent_now is not None and sent_now != self._account.username:
                    self._step(
                        f"送出去的帳號是 {sent_now!r}，不是我們的 —— 打到別的欄位了，重來"
                    )
                    # ⚠ **重試之前一定要先關掉錯誤框。**
                    # 帳密被拒絕時客戶端會跳「帳密錯誤」，那個框會**擋住輸入** ——
                    # 不關掉的話後面每一次重試都是在對著它打字，永遠不會成功
                    # （踩過：連續十幾次全部落空）。
                    # 使用者實測：按一次 Enter 就回到登入畫面。
                    self._dismiss_error(hwnd)
                    # ⚠ **把假設翻面。** 兩格事前分不出來，唯一確定的依據就是
                    # 送出之後客戶端記下的「送出去的帳號」。既然這次打反了，
                    # 下一次就換另一邊 —— 兩次之內一定對上。
                    # （早期版本每次都重新猜同一個答案，於是無限重複同樣的錯。）
                    self._tab_first = not self._tab_first
                    self._step(
                        f"下一次改成先打{'帳號' if not self._tab_first else '密碼'}"
                    )
                    # ⚠ **把假設翻面。** 兩格在記憶體裡長得一模一樣，沒有標籤
                    # 可以事先分辨；唯一確定的依據就是送出之後客戶端記下的
                    # 「送出去的帳號」。既然這次打反了，下次就換另一邊 ——
                    # 兩次之內一定對上。
                    # （早期版本每次都重新猜同一個答案，於是無限重複同樣的錯。）
                    if time.monotonic() >= deadline:
                        self.progress.fail(
                            "輸入帳號密碼",
                            f"最後一次送出去的帳號是 {sent_now!r}，"
                            f"不是 {self._account.username!r} —— 字一直打到別的欄位。",
                        )
                        return False
                    continue
                self._login_server = server
                self._step(f"客戶端連上了 {server[0]}:{server[1]} —— 帳密送出去了")
                # ⚠ 用 WARNING：預設 log_level 就是 WARNING，前面每一步的 INFO
                # 在使用者的日誌裡**一行都看不到**（實際查過，全空）。
                # 這一行是「這次到底花了多久、打了幾次」唯一留得下來的紀錄。
                log.warning(
                    "[自動登入] 帳密階段完成：%.1f 秒、打了 %d 次",
                    time.monotonic() - started, attempt,
                )
                break
            if time.monotonic() >= deadline:
                # ⚠ 這一關卡住最常見的原因是**合約書沒被按掉**：那個畫面不吃
                # 視窗訊息（[INP-001]），只能用滑鼠點，而按鈕位置是用視窗大小的
                # 比例算的（在 1942x1256 上量的）。別人的客戶端解析度不同時，
                # 那一下就可能點空 —— 要講出來，不要只說「帳密沒送出去」。
                stage = "不明"
                try:
                    stage = game_screen.detect(game_screen.capture(hwnd)).value
                except Exception as exc:  # noqa: BLE001 - 只是為了講清楚，失敗就算了
                    log.debug("收尾判定畫面失敗：%s", exc)
                extra = (
                    "（畫面還停在合約書 —— 多半是「同意」按鈕的位置對不上"
                    "你的解析度，請手動按一次同意）"
                    if stage == game_screen.Stage.EULA.value else f"（畫面：{stage}）"
                )
                self.progress.fail(
                    "輸入帳號密碼",
                    f"打了 {attempt} 次，客戶端都沒有連上伺服器 —— 帳密沒送出去。{extra}",
                )
                return False
            self._step("客戶端還沒連上伺服器，再打一次")

        # 走到這裡代表迴圈裡的閉環檢查已經確認打對格了。
        sent = input_helper.submitted_account(self._pid) or self._sent_account()
        self._step(f"帳號密碼已送出（送出去的帳號 = {sent or '讀不到'}）")
        self._grab_password_blob()

        return True

    def _grab_password_blob(self) -> None:
        """把 `0x0064` 裡那 24 bytes 密碼密文記下來。

        它每次登入都一樣（實測），存起來就能不開遊戲直接跟伺服器要角色清單。
        抓不到不算失敗 —— 那只是少一個之後的便利功能。

        ⚠ **每次登入都重抓、直接覆蓋舊的。** 密文是密碼轉出來的，
        改了遊戲密碼舊的就過期；靠這裡自動換成新的，使用者不必知道有這回事。
        """
        # ⚠ **要等它到齊。** `0x0064` 是我們剛剛才送出去的，擷取要一點時間
        # 才把它交上來 —— 讀當下有什麼的話常常是空的（實際踩過：角色清單存了、
        # 密文沒存，於是「更新角色清單」那顆按鈕一直是灰的）。
        deadline = time.monotonic() + _BLOB_TIMEOUT
        while time.monotonic() < deadline:
            for packet in self._packets:
                if packet.opcode != OP_LOGIN or len(packet.payload) < 52:
                    continue
                name = packet.payload[4:28].split(bytes(1))[0].decode(
                    "ascii", "ignore"
                )
                if name != self._account.username:
                    continue      # 別人的封包（多開）——不要記錯帳號的密文
                self.password_blob = packet.payload[28:52].hex()
                self._step("記下登入密文（之後可以不開遊戲更新角色清單）")
                return
            time.sleep(_POLL)
        # 抓不到不算失敗，但要講一聲 —— 不然使用者會納悶那顆按鈕為什麼不亮。
        self._step("這次沒抓到登入密文（擷取可能漏了那一包），下次登入再試")

    def _sent_account(self) -> str | None:
        """從擷取到的 0x0064 讀出送出去的帳號（明文，見 [PKT-046]）。"""
        for packet in self._packets:
            if packet.opcode == OP_LOGIN and len(packet.payload) >= 28:
                return packet.payload[4:28].split(bytes(1))[0].decode("ascii", "replace")
        return None

    def _pick_server_actions(self) -> list[list[dict]]:
        """選伺服器要送的按鍵。設定裡沒指定就不動（用客戶端記住的那台）。

        ## 為什麼只能用鍵盤

        **選伺服器沒有封包可以打。** 使用者實測的兩份擷取（查爾斯／波利）裡，
        送出的 `0x0065` **內容同構**，差別只有 `login_id1`（登入伺服器發的隨機值，
        與選哪台無關），而且那一包是**連上角色伺服器之後**才送的 ——
        「選哪一台」＝客戶端自己決定去連哪個 IP，不是一則訊息。

        ## 為什麼要先按到底

        不能假設反白預設停在哪（那多半是「記得上次」）。所以先連按「上」到底，
        保證停在第一項，再往下數 N 次 —— 不管起點在哪都會落到指定那台。
        送完之後**用連到的 IP 驗證**（見 `servers.name_for_ip`），
        連錯台會大聲說，不會安靜地登錯。
        """
        from ro_toolbox.services import servers

        wanted = (self._account.server or "").strip()
        if not wanted:
            return []
        index = next(
            (s.code for s in servers.KNOWN if s.name == wanted), None
        )
        if index is None:
            self._step(f"設定裡的伺服器「{wanted}」不在清單中，不動選單")
            return []

        batches = [[input_helper.key_foreground(_VK_UP)] for _ in range(_SERVER_LIST_TOP)]
        batches += [[input_helper.key_foreground(_VK_DOWN)] for _ in range(index)]
        batches.append([input_helper.key(_VK_RETURN)])
        self._step(f"選伺服器：先到頂再往下 {index} 次（{wanted}）")
        return batches

    def _send_otp(self, hwnd: int) -> bool:
        """算 OTP 並送出。**每次重算**，不能用先前算好的。

        ## 兩個實測教訓

        1. **快過期的碼不要送。** 這一段有重試，送一個剩 3 秒的碼等於浪費一輪；
           剩不到 `_OTP_MIN_SECONDS` 秒就等下一組（等的是**碼的有效期**，
           不是「等它穩定」——那是可以精確算出來的東西）。
        2. **要重試。** 帳密送出去之後，客戶端要一下子才會把 OTP 輸入框準備好；
           太早打就掉進黑洞。成敗看**客戶端有沒有換到下一台伺服器**。
        """
        # OTP 這一段的預算要**含得下等一組新碼**（最多 30 秒）加上幾輪重試。
        deadline = time.monotonic() + _OTP_TIMEOUT
        secret = self._account.secret
        attempt = 0
        while time.monotonic() < deadline:
            left = self._remaining(secret)
            if left < _OTP_MIN_SECONDS:
                # 等下一組。等的是**碼的有效期**（可以精確算出來的東西），
                # 不是「等它穩定」那種猜測。
                self._step(f"這組 OTP 只剩 {left} 秒，等下一組（約 {left} 秒）")
                # ⚠ 比較基準要**固定住**。早期版本在迴圈裡重新指派 `left`，
                # 條件就變成「現在的剩餘 <= 剛剛的剩餘」——永遠成立，
                # 於是空轉到整段逾時（實測：90 秒只送出 2 次 OTP）。
                was = left
                while self._remaining(secret) <= was and time.monotonic() < deadline:
                    time.sleep(0.3)
                continue

            attempt += 1
            code = generate_otp(secret)
            self._step(
                f"送出 OTP（第 {attempt} 次，"
                f"這組還剩 {self._remaining(secret)} 秒）"
            )
            # ⚠ **打之前先清空那一格。**
            # 不清的話重試時第二組 6 碼會接在第一組後面變成 12 碼，必然失敗 ——
            # 而且看起來就像「OTP 不對」，完全誤導。
            #
            # 文字走真的按鍵（Unicode，繞過輸入法），清空與 Enter 走視窗訊息；
            # 兩種通道要分批，同一個行程混用會被封鎖（[INP-009]）。
            batches = [
                self._clear_actions(),
                self._type_actions(code),
                [input_helper.key(_VK_RETURN)],
            ]
            try:
                for batch in batches:
                    input_helper.send(hwnd, batch)
                # OTP 過了之後客戶端會跳出伺服器選單 —— 順手把它選掉。
                # ⚠ **只在第一次送**。每次重試都補送的話，OTP 沒過時那些方向鍵
                # 和 Enter 會打進 OTP 畫面，把狀態弄亂 —— 實測會讓後面每一次
                # 都失敗（送了 9 次都沒過）。沒指定伺服器時這裡什麼都不會送。
                if attempt == 1:
                    for batch in self._pick_server_actions():
                        input_helper.send(hwnd, batch)
            except input_helper.InputHelperError as exc:
                # ⚠ 這裡以前只記 DEBUG 就 continue —— DEBUG 沒開的話**完全看不到**，
                # 外面看到的是「0.4 秒送一次、送了 145 次」的鬼打牆，
                # 而真正的原因（打不進去）一個字都沒留下。實際踩過。
                self._step(f"第 {attempt} 次 OTP 送不進去：{exc}")
                # 送不進去就等一下再試，不要把整段預算在幾秒內燒光。
                time.sleep(_OTP_STEP_TIMEOUT)
                continue

            # 過了的訊號：客戶端**換到下一台伺服器**（角色伺服器）。
            # 那是實際發生的事，不必等封包 —— 這一段的連線是後來才建立的，
            # 封包擷取常常整段漏掉（實測抓到 0 個）。
            moved = self._wait_connection(_OTP_STEP_TIMEOUT, exclude=self._login_server)
            if moved is not None:
                from ro_toolbox.services import servers

                self.server_name = servers.name_for_ip(moved[0])
                where = self.server_name or f"{moved[0]}（認不出是哪一台）"
                self._step(f"OTP 過了 —— 進到 {where}")
                return True
            self._step("還沒換伺服器，再送一次 OTP")

        self.progress.fail(
            "等登入結果",
            f"送了 {attempt} 次 OTP，客戶端都沒有換到下一台伺服器。"
            "驗證碼可能不對（本機時間偏移過大時就會這樣）。",
        )
        return False

    # ---- 小工具 -----------------------------------------------------

    @staticmethod
    def _remaining(secret) -> int:
        from ro_toolbox.services.totp import remaining_seconds

        return remaining_seconds(secret)

    def _wait_window(self, timeout: float) -> int | None:
        """等遊戲畫出視窗。

        客戶端加殼＋GameGuard 初始化，實測**最久超過 200 秒**。
        等待期間每 30 秒回報一次，不然使用者會以為程式當掉了。
        """
        started = time.monotonic()
        deadline = started + timeout
        self._step("等遊戲畫出視窗（要解殼，可能要好幾分鐘）")
        next_note = started + 30
        while time.monotonic() < deadline:
            hwnd = game_screen.find_window(self._pid)
            if hwnd is not None:
                self._step(f"視窗出現了（等了 {time.monotonic() - started:.0f} 秒）")
                return hwnd
            if time.monotonic() >= next_note:
                next_note += 30
                self._step(f"還在等遊戲視窗…（{time.monotonic() - started:.0f} 秒）")
            time.sleep(_POLL)
        return None

    def _wait_packet(self, opcode: int, timeout: float) -> bool:
        """等某個 opcode 出現，**依序消費、不跳號**。

        ⚠ 不能用「從呼叫的當下往後看」。實測 `0x0064`（我們送出）與伺服器回的
        `0x0A73` **時間戳相同** —— 等完前者時後者往往已經在清單裡了，
        用「從現在往後」會直接錯過它，然後誤報成「帳號或密碼不對」。
        所以用一個橫跨整場的游標，配對到就往前推一格。
        """
        deadline = time.monotonic() + timeout
        while True:
            while self._cursor < len(self._packets):
                packet = self._packets[self._cursor]
                self._cursor += 1
                if packet.opcode == opcode:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _step(self, text: str) -> None:
        self.progress.note(text)
        if self._on_step is not None:
            self._on_step(text)
