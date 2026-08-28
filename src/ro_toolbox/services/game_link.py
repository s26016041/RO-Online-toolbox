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
from collections.abc import Callable

from ro_toolbox.services import game_socket
from ro_toolbox.services.character import CharacterReader
from ro_toolbox.services.packet_capture import PacketCapture
from ro_toolbox.services.ro_capture import find_server

log = logging.getLogger(__name__)


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

    # ---- 建立與收攤 -------------------------------------------------

    def open(self) -> str | None:
        """找連線 → 複製 socket → 定位角色 → 開擷取。回 None = 成功。

        每一步都先講一句話：這一整段要一秒以上（AOB 掃描 ＋ 開 pcap），
        不講的話使用者按下按鈕之後看到的是完全的沉默。
        """
        self._note("正在找遊戲連線…")
        server = find_server(self.pid)
        if server is None:
            return "找不到伺服器連線（還沒登入？）"

        # ⚠ 剛連上／剛換頻道的那幾秒複製不到，**一定要重試**（[PKT-072]）。
        self._note("正在複製遊戲連線（剛上線的幾秒可能要等一下）…")
        sock = game_socket.open_game_socket(
            self.pid, server[0], server[1], should_stop=self._should_stop,
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
        if server == self.server:
            return None
        log.info("連線 %s → %s，重新綁定", self.server, server)
        self._close_socket()
        sock = game_socket.open_game_socket(
            self.pid, server[0], server[1],
            timeout=game_socket.SOCKET_REBIND_SEC, should_stop=self._should_stop,
        )
        if not sock:
            return "⚠ 換頻道後找不到新的遊戲 socket"
        self.sock, self.server = sock, server
        self.rebound = True
        return None

    def send(self, data: bytes) -> bool:
        """送一個封包。回 False 代表 socket 可能失效了（下一拍會重綁）。

        走**複製出來的遊戲 socket**，全程不碰記憶體
        （CLAUDE.md：RO 掛 GameGuard，寫記憶體會被反制）。
        """
        if self.sock is None:
            return False
        if game_socket.send_on_socket(self.sock, data) < 0:
            log.warning("送封包失敗，socket 可能已失效，強制重新綁定")
            self.server = None      # 逼下一次 resync() 重綁
            return False
        return True
