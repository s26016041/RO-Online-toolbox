"""抓指定 RO 行程的雙向封包 —— 完全不碰遊戲行程，GameGuard 看不到。

## 為什麼是 WinDivert 而不是 Npcap

以前這裡走 Npcap。它抓得到，但有兩個沒辦法接受的代價：

1. **使用者要自己去下載安裝 Npcap**（還要記得勾 WinPcap 相容模式）。
   換一台電腦、給朋友用就多一道關卡；沒裝的話二次密碼那一關直接停住。
2. **Npcap 的授權不准我們把它一起打包**（要另外買 OEM 授權）。

WinDivert 兩件事都解決：驅動檔（`WinDivert64.dll` / `WinDivert64.sys`）
**隨 `pydivert` 一起帶**，第一次開啟時自己註冊成服務（要系統管理員，
本工具本來就需要），授權是 LGPL/GPL，可以跟著我們的 exe 發布。

2026-08-26 實測：整條自動登入（二次密碼 seed `0x08B9`、角色清單 `0x0B72`、
選角回應 `0x0AC5` 全是伺服器推過來的 inbound）用 WinDivert 一次通過。

## 為什麼不會影響遊戲的網路

用 `SNIFF | RECV_ONLY` 開 —— **被動複製一份**，封包照樣走它原本的路。
WinDivert 預設那個模式會把封包從協定堆疊裡攔下來、要程式自己再送回去；
那種模式只要我們當掉，遊戲的網路就斷了。**絕對不用那個模式。**

## 影格格式

WinDivert 給的是 **IP 封包**（沒有乙太網路表頭），所以 `_eth_offset = 0`。
Npcap 時代給的是乙太網路影格，前面有 14 bytes 要跳過 —— 換過來時忘了改
就會每個封包都讀偏 14 bytes。
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Callable

from ro_toolbox.core.ro_packet import RoPacket, split_stream
from ro_toolbox.services import packet_table
from ro_toolbox.services.process_monitor import local_ports_of
from ro_toolbox.services.ro_capture import (
    WEB_PORTS,
    find_server,
)

log = logging.getLogger(__name__)

#: 看門狗多久檢查一次遊戲連線有沒有換位址（換地圖／換頻道／重登）。
_RESYNC_SEC = 1.0
#: 還沒認出伺服器時的檢查間隔。要夠密才不會漏掉登入的第一個封包
#: （連線建立到送出帳密只隔幾毫秒）。
_SEEK_SEC = 0.2
#: 等長度表抽完那段期間，暫存幾個影格。857ms × 高流量也不會逼近這個數，
#: 抽不到長度表時最多撐多久才退化成「整段當一包」。
#: 實測遊戲剛開時抽不到（GameGuard 擋讀程式碼區段），穩定後 0.8 秒就抽到。
_LENGTH_GIVE_UP_SEC = 90.0

#: 滿了會**大聲**記 log（見 _drain_buffer），不會安靜地丟。
_MAX_BUFFERED_FRAMES = 20000
#: 長度表抽失敗之後多久再試一次。抽不到多半是還沒登入（客戶端加殼，
#: 那時候讀不到程式碼區段），登入後就會成功並進快取。
_LENGTH_RETRY_SEC = 5.0
#: 認不出主人的影格先留這麼久。新連線的本機連接埠是每 _SEEK_SEC 才更新一次，
#: 而 TCP 交握到送出第一個封包只隔幾毫秒 —— 中間那段空窗會漏掉**登入那一包**。
#: 留著，等連接埠集合一更新就回頭把屬於新連線的補送出來。
_RECENT_WINDOW_SEC = 2.0
_MAX_RECENT_FRAMES = 4000
#: 重組緩衝的上限。超過代表已經失去同步，丟掉重來比無限累積好。
_MAX_STREAM_LEFTOVER = 1 << 20
#: 封包長度表快取（PID → 表）。同一個遊戲行程的表不會變，
#: 抽一次要 857ms，重開擷取時不該再抽一次。
_length_cache: dict[int, dict[int, tuple[int, int]]] = {}

_PROTO_TCP = 6


#: 只收 TCP。位址與連接埠的比對交給 `_process_frame` ——
#: 它本來就要做（登入→角色→地圖會換連線），在這裡再過濾一次只會多一個地方要維護。
_FILTER = "tcp"

MISSING_MESSAGE = (
    "找不到 pydivert（WinDivert）。這是抓封包用的，正常安裝會一起帶著；"
    "從原始碼跑的話請執行 `pip install pydivert`。"
)


def available() -> tuple[bool, str]:
    """能不能抓封包。**這裡只檢查匯入，不去開驅動** ——
    開驅動要系統管理員，失敗訊息由 `start()` 報，那裡才講得出真正的原因。
    """
    try:
        import pydivert  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"{MISSING_MESSAGE}（{exc}）"
    return True, ""


class PacketCapture:
    """抓指定 RO 行程的雙向封包（送出 + 伺服器推送）。

    callback 在背景執行緒執行，呼叫端不可在裡面直接碰 UI。
    """

    def __init__(
        self,
        pid: int,
        on_packet: Callable[[RoPacket], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._pid = pid
        self._on_packet = on_packet
        self._on_error = on_error
        self._handle = None
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        # 重開期間 recv() 會丟例外，那是我們自己關掉控制代碼造成的，
        # 不能報成「擷取中斷」，也不能讓看門狗誤判成掉線。
        self._rebinding = threading.Event()
        self._lock = threading.RLock()
        self._server_ip = ""
        self._server_port = 0
        # WinDivert 給的是 IP 封包，沒有乙太網路表頭。
        self._eth_offset = 0
        self._divert = None
        self._counter = 0
        self._rebinds = 0
        # 還沒認出伺服器時，用這個行程佔用的本機連接埠認封包（見 _handle_frame）。
        self._pid_ports: frozenset[int] = frozenset()
        # 長度表抽好之前先把影格存著，抽好再補處理 —— 這樣讀取迴圈可以
        # 立刻開始（按下開始就在收），又不會因為沒有長度表而切錯包。
        self._lengths_ready = threading.Event()
        # 抽不到長度表時，撐到這個時刻才退化（見 _load_lengths）。
        self._lengths_deadline = 0.0
        self._frame_buffer: list[bytes] = []
        self._dropped_while_waiting = 0
        # 認不出主人的影格（可能屬於還沒被登記的新連線），見 _remember/_replay。
        self._recent: collections.deque = collections.deque()
        # TCP 重組用的殘段，兩個方向各一條。一個 RO 封包可能跨分段：
        # 實測 0x0B60 宣告 392 bytes 卻分成 64 + 328 送達，不接起來的話
        # 內容會截斷、下一段開頭會被誤判成新的 opcode（[PKT-055]）。
        self._streams: dict[bool, bytes] = {True: b'', False: b''}
        # opcode → (長度, 標頭)。用 AOB 從客戶端程式碼抽出來（[MEM-024]）。
        # 沒抽到就是空的 —— split_packets 會退回「整段當一包」的舊行為。
        self._lengths: dict[int, tuple[int, int]] = {}

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def server(self) -> str:
        return self._server_ip

    def _interrupt(self) -> None:
        """把阻塞中的 `recv()` 打斷 —— WinDivert 沒有 breakloop，只能關掉它。"""
        self._close_all()

    def start(self) -> bool:
        ok, message = available()
        if not ok:
            self._report(message)
            return False

        # ⚠ **不要求「已經連上伺服器」才准開始。**
        # 登入交握正是「連線從無到有」的那一刻；硬要先有連線才准擷取的話，
        # 登入封包永遠抓不到 —— 要開始抓就得先有連線，而要看的正是它怎麼建立的。
        # 沒連線就先開著等，`_handle_frame` 會改用行程佔用的本機連接埠認人，
        # 看門狗一認出伺服器就自動切回位址過濾。
        server = find_server(self._pid) or ("", 0)
        if not self._open(server):
            return False
        self._pid_ports = frozenset(local_ports_of(self._pid))
        self._streams = {True: b'', False: b''}
        # ⚠ **長度表不在這裡抽。** `extract_lengths` 要 AOB 掃遊戲的 11.5MB
        # 程式碼區段，實測 857ms，兩個工具同時掃同一個行程還會更久。
        # `start()` 是在 UI 執行緒上被呼叫的，擺在這裡＝按下「開始擷取」畫面就凍住，
        # Windows 會判定程式沒回應（實際踩過：AppHangTransient / python.exe）。
        # 改由看門狗執行緒開場時抽。
        #
        # 讀取迴圈**立刻開**，不等長度表：等它的話按下「開始擷取」之後有 0.8 秒
        # 什麼都收不到，使用者按停止再開始就一直看到空畫面（實際回報過）。
        # 沒有長度表時切包會退回「整段當一包」、黏在後面的封包會不見（[PKT-043]），
        # 所以那段期間的影格**先暫存**，長度表一到就補處理（見 _drain_buffer）。
        self._stop.clear()
        self._lengths_ready.clear()
        self._lengths_deadline = time.monotonic() + _LENGTH_GIVE_UP_SEC
        self._frame_buffer = []
        self._dropped_while_waiting = 0
        self._start_loop()
        self._watchdog = threading.Thread(
            target=self._watch, name="capture-watch", daemon=True
        )
        self._watchdog.start()
        return True

    def _open(self, server: tuple[str, int]) -> bool:
        """開一個 WinDivert 控制代碼。

        `server` 只是記下來給 `_process_frame` 比對用 —— 過濾器本身不綁位址，
        所以換伺服器（登入→角色→地圖）時**不必重開**，
        一個封包都不會漏在切換的空窗裡。Npcap 時代要重綁 BPF，
        而角色清單就曾經整包漏在那個空窗裡（[PKT-051]）。
        """
        import pydivert

        self._server_ip, self._server_port = server
        with self._lock:
            if self._divert is not None:
                return True
            try:
                handle = pydivert.WinDivert(
                    _FILTER,
                    layer=pydivert.Layer.NETWORK,
                    flags=pydivert.Flag.SNIFF | pydivert.Flag.RECV_ONLY,
                )
                handle.open()
            except Exception as exc:  # noqa: BLE001
                self._report(
                    f"WinDivert 開不起來：{exc}\n"
                    "請以系統管理員身分執行（它要載入自己的驅動）。"
                )
                return False
            self._divert = handle
            # 其餘程式用 `_handle` 判斷「開著沒」，給它一個非 None 的值。
            self._handle = True
        log.info("WinDivert 擷取已開啟（%s）", _FILTER)
        return True

    def _load_lengths(self) -> None:
        """抽封包長度表，讓切包精確。抽不到就照舊（整段當一包）。

        ⚠ **抽到了才 set `_lengths_ready`。** 抽不到時要讓暫存繼續 ——
        看門狗會定期重試，真的等不到才由它在期限到時放行（見 `_watch`）。
        沒有長度表的話，黏在同一段 TCP 裡的第二包之後全部看不見（[PKT-043]）。
        """
        cached = _length_cache.get(self._pid)
        if cached is not None:
            self._lengths = cached
            log.debug("封包長度表用快取（PID %s，%d 個）", self._pid, len(cached))
            self._lengths_ready.set()
            self._drain_buffer()
            return

        table = None
        try:
            table = packet_table.load_cached(self._pid)
            if not table:
                table = packet_table.extract(self._pid)
                if table:
                    packet_table.save_cached(self._pid, table)
        except Exception as exc:  # noqa: BLE001 - 抽不到不該讓整場擷取死掉
            log.info("這次抽不到長度表（%s）—— 影格先暫存", exc)

        if table:
            self._lengths = {
                op: (info.length, info.header) for op, info in table.items()
            }
            _length_cache[self._pid] = self._lengths
            log.info("封包長度表：%d 個 opcode，切包改為精確模式", len(self._lengths))
            self._lengths_ready.set()
            self._drain_buffer()
            return

        log.info(
            "長度表還沒抽到（遊戲剛開時 GameGuard 會擋）—— 影格先暫存，抽到再一次補切"
        )
        # 抽這一趟本身要花快一秒，期限從**抽完**開始算才公平。
        self._lengths_deadline = time.monotonic() + _LENGTH_GIVE_UP_SEC

    def _retry_lengths(self) -> None:
        """長度表沒抽到就過一陣子再試（失敗只記 debug，不洗版）。"""
        try:
            table = packet_table.load_cached(self._pid)
            if not table:
                table = packet_table.extract(self._pid)
                if table:
                    packet_table.save_cached(self._pid, table)
        except Exception as exc:  # noqa: BLE001
            log.debug("再抽一次封包長度表還是失敗：%s", exc)
            return
        if not table:
            return
        self._lengths = {op: (info.length, info.header) for op, info in table.items()}
        _length_cache[self._pid] = self._lengths
        log.info("封包長度表補抽成功：%d 個 opcode，切包改為精確模式",
                 len(self._lengths))
        if not self._lengths_ready.is_set():
            self._lengths_ready.set()
            self._drain_buffer()

    def _remember(self, frame: bytes, sport: int, dport: int) -> None:
        """把認不出主人的影格留一下下，等它的連接埠被登記出來再認領。"""
        now = self._now()
        with self._lock:
            self._recent.append((now, frame, sport, dport))
            cutoff = now - _RECENT_WINDOW_SEC
            while self._recent and (
                self._recent[0][0] < cutoff or len(self._recent) > _MAX_RECENT_FRAMES
            ):
                self._recent.popleft()

    def _replay(self, new_ports: frozenset[int]) -> None:
        """連接埠集合多了幾個 → 回頭把屬於它們的影格補送出來。

        **這是收得到「按下登入送出的第一個封包」的關鍵。** 新連線建立到送出
        `0x0064` 只隔幾毫秒，而連接埠集合 0.2 秒才更新一次；沒有這一步的話
        那一包永遠落在空窗裡（實測：使用者的擷取都是從 OTP `0x0A74` 才開始）。

        補送出來的順序是原本的到達順序，但會排在這 0.2 秒內其他封包**之後** ——
        時間戳仍然是對的，看時間戳就不會誤判先後。
        """
        with self._lock:
            keep = collections.deque()
            claim = []
            for item in self._recent:
                _t, frame, _sp, _dp = item
                if _sp in new_ports or _dp in new_ports:
                    claim.append(frame)
                else:
                    keep.append(item)
            self._recent = keep
        if not claim:
            return
        log.info("新連線出現，補回 %d 個先前認不出主人的影格", len(claim))
        for frame in claim:
            self._process_frame(frame, remember=False)

    def _drain_buffer(self) -> None:
        """把等長度表那段時間暫存的影格補處理掉，一個都不漏。"""
        with self._lock:
            buffered, self._frame_buffer = self._frame_buffer, []
            dropped, self._dropped_while_waiting = self._dropped_while_waiting, 0
        if dropped:
            log.warning("等長度表期間暫存區滿了，丟掉 %d 個影格（上限 %d）",
                        dropped, _MAX_BUFFERED_FRAMES)
        for frame in buffered:
            self._process_frame(frame)

    def _start_loop(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="capture", daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        """讓擷取自己跟上遊戲連線 —— 換地圖、換頻道、斷線重登都要能接回來。

        這不是理論問題：實測換圖後連線從 …102:10022 變成 …100:10004
        （見 GAMEDATA [PKT-038]）。BPF 綁的是舊位址，舊的擷取器整個瞎掉 ——
        自動打怪會看不到任何怪，背包清單封包也會漏掉，而且**完全不會報錯**，
        那是最糟的失效方式。所以這裡兩件事都顧：

        - 連線位址變了 → 重綁。
        - 擷取執行緒自己死了（網卡切換、睡眠喚醒）→ 重開。

        `find_server` 回 None 是換圖／登入畫面的正常過渡，繼續等就好，不拆東西。

        開場先把封包長度表抽出來 —— 這件事要掃遊戲記憶體、要花快一秒，
        放在這條執行緒上做才不會卡住 UI（見 `start()` 的註解）。
        """
        if not self._stop.is_set():
            self._load_lengths()
        # 抽失敗多半是**還沒登入**（客戶端有加殼，程式碼區段那時讀不到）。
        # 不能就此放棄整場擷取 —— 沒有長度表切包會退回「整段當一包」，
        # 黏在後面的封包全部看不到（[PKT-043]）。所以之後定期再試，
        # 成功一次就會進快取，不會一直掃。
        next_retry = time.monotonic() + _LENGTH_RETRY_SEC
        while not self._stop.wait(_SEEK_SEC):
            # ⚠ 連接埠集合**一直更新**，不是只在還沒認出伺服器時。
            # 換伺服器（登入→角色→地圖）會開新連線，那條連線的第一批封包
            # 就是靠這個集合才收得到（見 _process_frame）。
            previous = self._pid_ports
            self._pid_ports = frozenset(local_ports_of(self._pid))
            fresh = self._pid_ports - previous
            if fresh:
                # 新連線的第一批封包在集合更新前就已經飛過去了，回頭認領。
                self._replay(fresh)

            if not self._lengths and time.monotonic() >= next_retry:
                next_retry = time.monotonic() + _LENGTH_RETRY_SEC
                self._retry_lengths()
            if (not self._lengths_ready.is_set()
                    and time.monotonic() >= self._lengths_deadline):
                log.warning(
                    "等了 %.0f 秒還是抽不到封包長度表，只好退回「整段當一包」——"
                    "黏在後面的封包會看不到", _LENGTH_GIVE_UP_SEC,
                )
                self._lengths_ready.set()
                self._drain_buffer()
            server = find_server(self._pid)
            if server is None:
                continue  # 讀取中／在登入畫面，等它接回來
            thread = self._thread
            dead = thread is None or not thread.is_alive()
            if server != (self._server_ip, self._server_port):
                # 位址變了不必動控制代碼 —— BPF 收的是全部 TCP，
                # 換個過濾用的位址就接上新連線了，一個封包都不會漏。
                log.info("遊戲連線變了（%s:%s → %s:%s），切換過濾位址",
                         self._server_ip, self._server_port, server[0], server[1])
                self._server_ip, self._server_port = server
                # 換了連線，舊的殘段跟新連線無關，留著只會污染切包。
                self._streams = {True: b'', False: b''}
                self._rebinds += 1
                if not dead:
                    continue
            elif not dead:
                continue
            log.warning("擷取執行緒不在了，重開")
            self._rebind(server)

    def _rebind(self, server: tuple[str, int]) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._close_handle()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(3.0)
        with self._lock:
            if self._stop.is_set():
                return
            self._thread = None
            if self._open(server):
                self._rebinds += 1
                self._rebinding.clear()
                self._start_loop()
            else:
                # 重綁失敗不放棄：下一輪看門狗還會再試（可能只是網卡剛切換）。
                self._rebinding.clear()
                log.warning("重新綁定擷取失敗，%.0f 秒後再試", _RESYNC_SEC)

    def _close_handle(self) -> None:
        if self._handle:
            self._rebinding.set()
            self._close_all()

    def stop(self, timeout: float = 0.3) -> None:
        """停止擷取。**這是在 UI 執行緒上被呼叫的，所以不准久等。**

        原本 join 給 3 秒，而看門狗開場要跑 `extract_lengths`（857ms 掃記憶體）——
        按下「開始擷取」之後馬上按「停止」，整個介面就凍住快一秒（實測 795ms，
        使用者回報「卡卡的」）。

        兩條執行緒都是 daemon，而且每一圈都會看 `_stop`，所以**不必等它們**：
        設好旗標、把讀取迴圈打斷、短暫等一下就回去。真的還沒收完的那一兩圈
        會自己結束；期間可能還會送出封包，由呼叫端自己擋（封包頁在
        `_on_packet` 看 `self._capture is None`）。
        """
        self._stop.set()
        # 讓還卡在 _load_lengths 裡的看門狗一回來就知道要收工，
        # 也讓暫存的影格不會留在記憶體裡。
        self._lengths_ready.set()
        with self._lock:
            self._interrupt()
            self._frame_buffer = []
        for thread in (self._thread, self._watchdog):
            if thread is not None and thread.is_alive():
                thread.join(timeout)
        self._thread = None
        self._watchdog = None
        with self._lock:
            self._close_all()

    def _close_all(self) -> None:
        """真正把控制代碼還掉。"""
        handle, self._divert = self._divert, None
        self._handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("關 WinDivert 時出錯（不影響）：%s", exc)

    # ---- 內部 -------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            handle = self._divert
            if handle is None:
                return
            try:
                packet = handle.recv()
            except Exception as exc:  # noqa: BLE001
                # 停止／重開時是我們自己關掉控制代碼的，不是真的斷線 ——
                # 別嚇使用者，也別讓看門狗誤判。
                if not self._stop.is_set() and not self._rebinding.is_set():
                    log.warning("WinDivert 擷取中斷（%s），看門狗會重開", exc)
                return
            if packet is None:
                continue
            self._handle_frame(bytes(packet.raw))

    def _handle_frame(self, frame: bytes) -> None:
        """長度表還沒好就先存著。存滿了會記數，之後大聲報出來（見 _drain_buffer）。"""
        if not self._lengths_ready.is_set():
            with self._lock:
                if len(self._frame_buffer) < _MAX_BUFFERED_FRAMES:
                    self._frame_buffer.append(frame)
                else:
                    self._dropped_while_waiting += 1
            return
        self._process_frame(frame)

    def _process_frame(self, frame: bytes, remember: bool = True) -> None:
        ip = frame[self._eth_offset:]
        if len(ip) < 20 or (ip[0] >> 4) != 4 or ip[9] != _PROTO_TCP:
            return
        ihl = (ip[0] & 0x0F) * 4
        tcp = ip[ihl:]
        if len(tcp) < 20:
            return
        thl = (tcp[12] >> 4) * 4
        payload = tcp[thl:]
        if not payload:
            return

        sport = int.from_bytes(tcp[0:2], "big")
        dport = int.from_bytes(tcp[2:4], "big")
        ports = self._pid_ports

        # ⚠⚠ **歸屬只准看「本機連接埠」，不准看伺服器位址。**
        #
        # 本機連接埠是作業系統保證唯一的，一個埠只屬於一個行程 ——
        # 那是這裡唯一分得出「這包是誰的」的欄位。
        #
        # 舊版還有一條「來源或目的等於伺服器位址就算我的」的捷徑。
        # **多開的時候那條是錯的**：兩個客戶端會連到同一台地圖伺服器，
        # 而且**連接埠也一樣**（實測 2026-08-29：PID 32164 與 34020 兩邊都是
        # `219.84.200.101:10009`），於是那個條件對兩條連線同時成立。
        # 後果不是漏收，是**兩條連線的位元組被串進同一個重組緩衝**：
        # 切出來的封包一半是別人的，長度一錯後面整段跟著歪。
        #
        # 症狀完全不像封包問題，全都長得像「記憶體讀錯」：
        # 負重 101%（自己的上限配別人的重量）、「打到空氣」、換圖之後
        # 尋路從別人的座標開始算然後安靜卡住。實機踩了整個下午（[PKT-085]）。
        #
        # 那條捷徑本來是為了補「換伺服器的 0.2 秒空窗」（角色清單整包不見）——
        # 但那個洞後來已經由 `_remember()` ＋ `_replay()` 補起來了：
        # 認不出主人的影格會先留著，連接埠一登記出來就回頭認領。
        # 捷徑早就是多餘的，只剩下把別人的封包收進來這個副作用。
        if sport in ports and dport not in WEB_PORTS:
            outbound = True
        elif dport in ports and sport not in WEB_PORTS:
            outbound = False
        else:
            # 認不出主人。可能真的是別的程式的流量（多開的時候就是隔壁那隻），
            # **也可能是遊戲剛開的新連線但連接埠還沒被登記到**（集合每 0.2 秒
            # 才更新，而 TCP 交握到送出帳密只隔幾毫秒 —— 登入那一包就是這樣
            # 漏掉的）。先留著，等連接埠集合更新時回頭認領（見 `_replay`）。
            if remember and dport not in WEB_PORTS and sport not in WEB_PORTS:
                self._remember(frame, sport, dport)
            return

        # 接上這個方向先前沒切完的尾巴，再切。
        stream = self._streams[outbound] + payload
        packets, leftover = split_stream(stream, self._lengths)
        if len(leftover) > _MAX_STREAM_LEFTOVER:
            # 這麼長還沒切完代表已經失去同步（換連線、漏收），不要無限累積。
            log.warning("重組緩衝過長（%d bytes），丟棄重來", len(leftover))
            leftover = b""
        self._streams[outbound] = leftover

        timestamp = self._now()
        for opcode, packet_bytes in packets:
            self._counter += 1
            try:
                self._on_packet(
                    RoPacket(
                        seq=self._counter,
                        timestamp=timestamp,
                        outbound=outbound,
                        opcode=opcode,
                        payload=packet_bytes[2:],
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 回呼不能害死擷取迴圈
                log.debug("封包回呼發生例外：%s", exc)

    @staticmethod
    def _now() -> float:
        import time

        return time.time()

    def _report(self, message: str) -> None:
        log.error(message)
        if self._on_error is not None:
            self._on_error(message)
