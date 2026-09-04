"""一個遊戲行程的三條線：**送封包的 socket、讀狀態的 reader、收封包的擷取**。

## 為什麼非抽出來不可

自動打怪、自動尋路（之後還有自動補貨）都要這三樣，而且「怎麼取得」與
「換頻道之後怎麼重綁」的規則**完全一樣**。以前是兩份幾乎相同的程式碼 ——
memory 早就記著「要合併就抽 GameLink」，這裡把它做掉。

⚠ 這不是潔癖。[PKT-072] 實際踩過：「剛連上的那幾秒複製不到 socket，要重試」
這條知識被抄成四份，其中**兩份漏掉了**，症狀是「按下自動尋路一按就死」。
CLAUDE.md 那條「同一個位址不准寫第二次」對**知識**同樣成立：
抄第二份就等於保證有人會漏掉修正。

## 失敗訊息只講事實，不講是誰在用

`open()` / `resync()` 回**給人看的失敗原因**（None = 沒事）。
「自動打怪已停止」「自動尋路已停止」這種尾巴由呼叫端自己加 ——
同一個事實在不同功能裡要講不同的話。

## 這支不決定任何事

它不知道地圖、不知道路線、不知道要打誰。換地圖之後要做什麼（重載地形、
學傳點、走回家）是呼叫端的事 —— `resync()` **只管連線**。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ro_toolbox.services import game_socket
from ro_toolbox.services.character import CharacterReader
from ro_toolbox.services.packet_capture import PacketCapture
from ro_toolbox.services.ro_capture import find_server, find_servers

log = logging.getLogger(__name__)

#: 連續送不出去多久就判定這條連線死了。
#:
#: 換頻道的那一兩拍也會送失敗（正常，重綁就好），所以門檻不能太短；
#: 但也不能沒有 —— 沒有的話就是實測那個「一小時 5,185 行錯誤、沒有人喊停」。
DEAD_AFTER_SEC = 15.0


class GameLink:
    """接上一個遊戲行程。`open()` 成功之後 `sock` / `reader` / `capture` 才有值。"""

    def __init__(
        self,
        pid: int,
        on_packet: Callable[[object], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        note: Callable[[str], None] | None = None,
        need_position: bool = False,
    ) -> None:
        self.pid = pid
        self._on_packet = on_packet
        self._should_stop = should_stop or (lambda: False)
        self._note = note or (lambda _text: None)
        self._need_position = need_position
        self.sock: int | None = None
        self.server: tuple[str, int] | None = None
        self.reader: CharacterReader | None = None
        self.capture: PacketCapture | None = None
        #: 上一次 `resync()` 有沒有真的重綁（呼叫端要拿來記日誌）
        self.rebound = False
        #: 連續送失敗從什麼時候開始（None = 現在好好的）。見 `dead`。
        self._failing_since: float | None = None
        #: **正在失敗的是哪一條連線**。⚠ 一定要在這裡另外記一份 ——
        #: `send()` 失敗時會把 `self.server` 清成 None（那是用來逼 `resync()`
        #: 重綁的），清掉之後就再也認不出「重綁回來的是不是同一條死的」。
        self._failing_server: tuple[str, int] | None = None
        #: 「這條死了」講過了沒（不擋就是每拍一行）。
        self._said_dead = False

    # ---- 建立與收攤 -------------------------------------------------

    def open(self) -> str | None:
        """找連線 → 複製 socket → 定位角色 → 開擷取。回 None = 成功。

        每一步都先講一句話：這一整段要一秒以上（AOB 掃描 ＋ 開 pcap），
        不講的話使用者按下按鈕之後看到的是完全的沉默。
        """
        self._note("正在找遊戲連線…")
        # ⚠⚠ **候選要全部拿，不能只拿最新的那條。** 實機 2026-08-29：
        # Ragexe 除了地圖伺服器還掛著一條 `.55:3000`，而且它比較新 ——
        # 舊版就鎖上它，複製不到、等 10 秒、整個功能停掉，而真正在跑的
        # `.101:10010` 就在旁邊。可以驗證的判準只有「複製得到」，
        # 所以一條一條試（見 `ro_capture.find_servers`）。
        servers = find_servers(self.pid)
        if not servers:
            return "找不到伺服器連線（還沒登入？）"

        # ⚠ 剛連上／剛換頻道的那幾秒複製不到，**一定要重試**（[PKT-072]）。
        self._note("正在複製遊戲連線（剛上線的幾秒可能要等一下）…")
        sock, server = game_socket.open_any_game_socket(
            self.pid, servers, should_stop=self._should_stop,
        )
        if not sock:
            return "找不到遊戲 socket，無法送封包（等了也沒出現）"
        self.sock, self.server = sock, server

        self._note("正在定位角色（AOB 特徵掃描）…")
        reader = CharacterReader()
        if not reader.attach(self.pid, should_stop=self._should_stop):
            return "角色定位失敗"
        if self._need_position and not reader.position_located:
            # 沒有座標就沒有「我在哪」，走路類功能整個不成立。
            # 與其每一拍空轉，不如立刻說清楚（CLAUDE.md：定位失敗要大聲）。
            reader.close()
            return "⚠ 角色座標定位失敗（遊戲可能已改版）"
        self.reader = reader

        if self._on_packet is not None:
            # Walker 靠 0x0087 判斷每一段有沒有被接受；沒有擷取就只能瞎送（[PKT-030]）
            self._note("正在啟動封包擷取…")
            capture = PacketCapture(self.pid, self._on_packet)
            if not capture.start():
                return "封包擷取啟動失敗（需要系統管理員）"
            self.capture = capture
        return None

    def close(self) -> None:
        """收攤。**每一項各自 try**：其中一項炸掉不該讓其他項留著不關。"""
        for shut in (self._close_capture, self._close_reader, self._close_socket):
            try:
                shut()
            except Exception as exc:  # noqa: BLE001 - 收尾失敗只記錄，不往上丟
                log.debug("收尾時出錯：%s", exc)

    def _close_capture(self) -> None:
        if self.capture is not None:
            self.capture.stop()
            self.capture = None

    def _close_reader(self) -> None:
        if self.reader is not None:
            self.reader.close()
            self.reader = None

    def _close_socket(self) -> None:
        if self.sock is not None:
            game_socket.close_socket(self.sock)
            self.sock = None

    # ---- 維持連線 ---------------------------------------------------

    def resync(self, server: tuple[str, int] | None = None) -> str | None:
        """連線換了就重綁。回 None = 沒事（含「本來就沒變」）。

        `server` 是呼叫端**已經讀到**的連線。給了就用它 —— 呼叫端常常為了別的
        判斷（例如換地圖）先讀過一次，這裡再讀一次不只浪費，兩次讀到的結果
        還可能不一樣（TCP 表是快照），於是判斷與動作對不起來。

        ⚠ 換地圖時伺服器會把連線移到另一台地圖伺服器（[PKT-038]），
        舊 socket 送出去的東西**不會報錯，只是沒人收** —— 那正是
        「安靜地做錯事」，所以一定要主動偵測。

        ⚠ **這裡只管連線**。換地圖之後要重載地形、學傳點、走回家…
        那些是呼叫端的事，這支不知道也不該知道。
        """
        self.rebound = False
        if server is None:
            server = find_server(self.pid)
        if server is None:
            if self.server is not None:
                return "⚠ 遊戲連線已中斷"
            return None
        # ★ **端點沒變不代表我們手上那份還能用。** 換地圖伺服器時遊戲會
        #   `closesocket()` 舊連線，而重連到同一台的話 (ip, port) 一模一樣 ——
        #   舊版只好等 `send()` 撞出 WSA 10038 才知道（見 `game_socket.socket_alive`）。
        stale = not self.alive()
        if server == self.server and not stale:
            return None
        if self.dead and server == self._failing_server:
            # ⚠ **綁到同一條死連線不算重綁。** 伺服器 reset 之後那條連線還留在
            # TCP 表裡，`find_server()` 照樣讀得到 —— 舊版就是這樣「重綁成功」
            # 了幾千次，每次都綁回同一條死的。
            return "⚠ 遊戲連線已中斷（重綁還是同一條斷掉的連線）"
        if stale and server == self.server:
            log.info("連線 %s 沒變，但我們複製的那份已經被遊戲關掉了"
                     "（換地圖伺服器）—— 重新複製", server)
        else:
            log.info("連線 %s → %s，重新綁定", self.server, server)
        self._close_socket()
        # 一樣一條一條試：挑中的那條複製不到的話，旁邊那條才是真的（見 `open()`）。
        # 指定的那條排第一，其餘照新到舊接在後面。
        picks = [server] + [s for s in find_servers(self.pid) if s != server]
        sock, bound = game_socket.open_any_game_socket(
            self.pid, picks,
            timeout=game_socket.SOCKET_REBIND_SEC, should_stop=self._should_stop,
        )
        if not sock:
            return "⚠ 換頻道後找不到新的遊戲 socket"
        self.sock, self.server = sock, bound
        self.rebound = True
        self._revive()
        return None

    def alive(self) -> bool:
        """我們手上這份 socket 複本**現在**還接得上遊戲那條連線嗎？

        微秒級的唯讀查詢，所以呼叫端可以**每一拍都問**，不必被
        「每 N 秒才查一次 TCP 表」的節流一起擋住 —— 貴的是 `find_server()`
        （撈整張 TCP 表），不是這一句。細節見 `game_socket.socket_alive()`。
        """
        return game_socket.socket_alive(self.sock)

    @property
    def dead(self) -> bool:
        """這條連線已經**確定沒救了**嗎（連續送不出去超過 `DEAD_AFTER_SEC`）。

        ⚠ 這個判斷不能只看「送失敗」：換頻道的那一兩拍也會失敗，那是正常的、
        重綁一下就好。真正沒救的長相是**一直失敗**——實測伺服器把連線 reset
        （WSA 10054）之後，`find_server()` 還是讀得到那條連線（TCP 表裡還在），
        所以「重綁」每次都成功、每次都綁到同一條死的，一小時噴 5,185 行錯誤
        而且**沒有人喊停**。呼叫端看到 `dead` 就該停下來大聲說。
        """
        if self._failing_since is None:
            return False
        return time.monotonic() - self._failing_since >= DEAD_AFTER_SEC

    def send(self, data: bytes) -> bool:
        """送一個封包。回 False 代表 socket 可能失效了（下一拍會重綁）。

        走**複製出來的遊戲 socket**，全程不碰記憶體
        （CLAUDE.md：RO 掛 GameGuard，寫記憶體會被反制）。
        """
        if self.sock is None:
            return False
        if game_socket.send_on_socket(self.sock, data) < 0:
            now = time.monotonic()
            if self._failing_since is None:
                self._failing_since = now
                self._failing_server = self.server
                log.warning("送封包失敗，socket 可能已失效，強制重新綁定")
            elif self.dead and not self._said_dead:
                self._said_dead = True
                log.error(
                    "這條連線連續 %.0f 秒送不出去，判定已經斷了（%s）—— 停止重試",
                    now - self._failing_since, self._failing_server,
                )
            self.server = None      # 逼下一次 resync() 重綁
            return False
        self._revive()
        return True

    def _revive(self) -> None:
        """送出去了 = 這條連線好好的。把失敗的記錄全部清掉。"""
        self._failing_since = None
        self._failing_server = None
        self._said_dead = False
