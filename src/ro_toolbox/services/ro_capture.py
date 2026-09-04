"""擷取單一 RO 行程與伺服器往來的封包。

用 Windows raw socket（見 raw_capture.py），**完全不碰遊戲行程** ——
GameGuard 剝掉的是記憶體寫入權限，網路層擷取它管不到（見 GAMEDATA [PKT-011]）。

限制：raw socket 只收得到 **outbound**（送出）封包（見 [PKT-003]）。
反向工程「哪個動作送哪個封包」正好只需要 outbound，所以這階段夠用。
要收伺服器推送（inbound）得改用 WinDivert（services/packet_capture，[PKT-058]）。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable

from ro_toolbox.core.ro_packet import RoPacket, split_packets
from ro_toolbox.services.process_monitor import (
    Connection,
    connections_of,
    local_addresses_of,
    local_ports_of,
    remote_endpoints_of,
)

log = logging.getLogger(__name__)

_RECV_SIZE = 65535
_TIMEOUT = 1.0
_PROTO_TCP = 6
#: 還沒認出伺服器時，多久重看一次行程的連線表。
#: 要夠密才不會漏掉登入的第一個封包（連線建立到送出帳密只隔幾毫秒）。
_SEEK_SEC = 0.2

_PRIVATE_PREFIXES = ("127.", "192.168.", "10.", "0.", "169.254.")
#: HTTP／HTTPS。GameGuard、更新檢查、公告都走這裡，**遊戲協定不走**。
#: 不排掉的話擷取器會鎖上 GameGuard 那條 TLS 連線（見 find_server）。
WEB_PORTS = frozenset({80, 443})


def find_servers(pid: int) -> list[tuple[str, int]]:
    """所有**可能**是遊戲連線的位址，**由新到舊**。第一個就是 `find_server()`。

    ## 為什麼「最新的那條」不夠

    實機 2026-08-29（狐狐狸剛開程式按自動尋路）：

        這個行程有多條非網頁連線 [('219.84.200.55', 3000),
                                  ('219.84.200.101', 10010)]，
        取最新建立的 ('219.84.200.55', 3000)
        ⚠ 10 秒內複製不到 PID 32164 連到 219.84.200.55:3000 的 socket
        ⚠ 換頻道後找不到新的遊戲 socket，自動尋路已停止

    `.55:3000` 不是地圖伺服器（真正在跑的是 `.101:10010`），但它**比較新**，
    所以被挑走了 —— 然後複製不到，等 10 秒，整個自動尋路停掉。

    ⛔ **不要用「連接埠 3000 不是遊戲」這種寫死的判斷**：那是猜的，
    改版換一個埠就又壞了（CLAUDE.md：不確定一律留空，不准猜）。
    **可以驗證的判準只有一個 —— 複製得到 socket 而且送得出去。**
    所以這裡把候選全部給出來，由呼叫端一條一條試（見
    `game_socket.open_any_game_socket()`）。
    """
    fresh = [
        conn
        for conn in connections_of(pid)  # 已經由新到舊排好
        if not conn.ip.startswith(_PRIVATE_PREFIXES) and conn.port not in WEB_PORTS
    ]
    usable = [c for c in fresh if c.established] or fresh
    if usable:
        return [c.endpoint for c in usable]
    return [
        (ip, port)
        for ip, port in sorted(remote_endpoints_of(pid))
        if not ip.startswith(_PRIVATE_PREFIXES) and port not in WEB_PORTS
    ]


def find_connection(pid: int) -> Connection | None:
    """`find_server()` 的完整版：回**整條連線**（含本機埠與建立時間）。

    ★ 本機埠才是一條連線的身分（[PKT-097]）：遊戲重連到**同一台**伺服器時
    (ip, port) 一模一樣，只有本機埠會變。`GameLink.resync()` 拿它比對
    「我手上這份複本是不是還是遊戲正在用的那條」。
    """
    fresh = [
        conn
        for conn in connections_of(pid)  # 已經由新到舊排好
        if not conn.ip.startswith(_PRIVATE_PREFIXES) and conn.port not in WEB_PORTS
    ]
    if fresh:
        # 還沒完成交握／正在收尾的連線不算數，但全都不是 ESTABLISHED 時
        # 還是要給一條出去（換圖的瞬間會短暫沒有已建立的連線）。
        usable = [c for c in fresh if c.established] or fresh
        if len(fresh) > 1:
            _log_multi(pid, [c.endpoint for c in fresh], usable[0].endpoint)
        return usable[0]

    # 退路：拿不到建立時間（非 Windows、iphlpapi 失敗）就照舊排序取第一條。
    # 這條路查不到本機埠（0），呼叫端要當「分不出來」處理。
    candidates = [
        (ip, port)
        for ip, port in sorted(remote_endpoints_of(pid))
        if not ip.startswith(_PRIVATE_PREFIXES) and port not in WEB_PORTS
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        _log_multi(pid, candidates, candidates[0])
    ip, port = candidates[0]
    return Connection(ip=ip, port=port, created=0, established=True)


def find_server(pid: int) -> tuple[str, int] | None:
    """找出行程連到的遊戲伺服器（排除本機／私有位址與網頁埠）。

    ## 為什麼要排除 80／443

    Ragexe 同時掛著 **GameGuard 的 HTTPS 連線**（實測 `43.201.119.82:443`，
    停在登入畫面時那是它**唯一**的一條連線）。舊版這裡是「排序後取第一個
    非私有位址」，於是會鎖上那條 HTTPS：擷取器忠實地抓 TLS 密文，
    切包切出一堆垃圾 opcode，畫面上看起來就是「抓封包壞了什麼都沒有」。
    更糟的是它**鎖住之後不會換** —— 遊戲伺服器的 IP 只要排序在後面，
    看門狗永遠不會切過去。

    RO 的協定不走 HTTP：`clientinfo.xml` 寫著登入伺服器是 **6900**，
    換圖後的 char／map server 實測是 10022／10004（[PKT-038]）。
    所以 80／443 一律不是遊戲連線。

    沒有符合的連線就回 None —— 那是「還沒登入」的正常狀態（[PKT-044]），
    擷取器會留在「用行程的本機連接埠認人」模式繼續等。

    ## 為什麼是「最新建立的那條」而不是「第一條」

    換地圖時伺服器會把連線移到另一台 map server，而**舊連線會留著**
    （實測 `.102:10022` 在換圖後又留了 11 分鐘才收掉，[PKT-063]）。
    這段期間有兩條都符合條件，舊版就「排序後取第一條」—— 那是擲骰子。
    挑錯的後果是把走路封包送進一條沒人收的連線，**完全不會報錯**。

    Windows 的 TCP 表本來就記著每條連線的建立時間，所以直接挑最新的那條：
    新的 map server 一定是後建立的。拿不到建立時間才退回舊的排序行為。
    """
    conn = find_connection(pid)
    return conn.endpoint if conn is not None else None


#: 上次為哪個 pid 報過哪一組連線。用來讓「有多條」只在**變化時**講一次 ——
#: 這句話以前每一拍都印（實測一秒 6 行），把日誌洗掉就等於沒有日誌。
_multi_seen: dict[int, tuple] = {}


def _log_multi(pid: int, candidates: list, chosen: tuple) -> None:
    key = (tuple(sorted(candidates)), chosen)
    if _multi_seen.get(pid) == key:
        return
    _multi_seen[pid] = key
    log.info("這個行程有多條非網頁連線 %s，取最新建立的 %s", candidates, chosen)


def bind_address_for(pid: int) -> str | None:
    """raw socket 要綁的本機 IP（取行程連線用的那張網卡）。"""
    addresses = local_addresses_of(pid)
    return sorted(addresses)[0] if addresses else None


def primary_ipv4() -> str | None:
    """本機對外用的那張網卡 IP。

    行程一條連線都還沒建立的時候（**登入畫面就是這樣**），
    `bind_address_for` 只會回 None —— 但擷取還是得挑一張網卡，
    否則就永遠等不到那條要觀察的連線出現。

    用「系統要連外部位址時會挑哪張網卡」來決定。UDP connect 不會真的送出
    任何封包，只是讓路由表告訴我們答案。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError as exc:
        log.warning("找不到對外網卡：%s", exc)
        return None
    finally:
        sock.close()


class RoPacketCapture:
    """抓指定 RO 行程送出的封包，逐一交給 callback。

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
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._counter = 0
        self._server_ip = ""
        self._local_ports: set[int] = set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def server(self) -> str:
        return self._server_ip

    def start(self) -> bool:
        if self.is_running:
            return True

        # ⚠ **不要求「已經連上伺服器」才准開始。**
        # 登入交握正是「連線從無到有」的那一刻；硬要先有連線才准擷取的話，
        # 登入封包永遠抓不到（要開始抓就得先有連線，而要看的正是它怎麼建立的）。
        # 沒連線就先開著等，`_handle` 會用行程佔用的本機連接埠認人。
        server = find_server(self._pid)
        self._server_ip = server[0] if server else ""

        bind_ip = bind_address_for(self._pid) or primary_ipv4()
        if not bind_ip:
            self._report("找不到可以綁定的本機 IP。")
            return False

        self._local_ports = local_ports_of(self._pid)

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
            sock.bind((bind_ip, 0))
            sock.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)
            sock.settimeout(_TIMEOUT)
        except (PermissionError, OSError) as exc:
            self._report(f"建立 raw socket 失敗：{exc}\n請以系統管理員身分執行。")
            return False

        self._socket = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ro-capture", daemon=True)
        self._thread.start()
        log.info(
            "開始擷取 PID %s 送往 %s 的封包（綁定 %s）",
            self._pid,
            self._server_ip,
            bind_ip,
        )
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        self._close_socket()

    # ---- 內部 -------------------------------------------------------

    def _loop(self) -> None:
        sock = self._socket
        if sock is None:
            return
        next_resync = 0.0
        while not self._stop.is_set():
            # 還沒認出伺服器（登入畫面）時要一直盯著行程的連線表：
            # 連線一出現就要立刻知道，晚一拍就漏掉登入的第一個封包。
            now = time.monotonic()
            if not self._server_ip and now >= next_resync:
                next_resync = now + _SEEK_SEC
                self._local_ports = local_ports_of(self._pid)
                found = find_server(self._pid)
                if found:
                    self._server_ip = found[0]
                    log.info("認出遊戲伺服器 %s（PID %s）", self._server_ip, self._pid)
            try:
                data = sock.recv(_RECV_SIZE)
            except TimeoutError:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self._report(f"擷取中斷：{exc}")
                return
            self._handle(data)

    def _handle(self, raw: bytes) -> None:
        if len(raw) < 20 or raw[9] != _PROTO_TCP:
            return
        dst = ".".join(str(b) for b in raw[16:20])

        ihl = (raw[0] & 0x0F) * 4
        tcp = raw[ihl:]
        if len(tcp) < 20:
            return
        if self._server_ip:
            if dst != self._server_ip:  # 只要送往遊戲伺服器的
                return
        elif (
            int.from_bytes(tcp[0:2], "big") not in self._local_ports
            or int.from_bytes(tcp[2:4], "big") in WEB_PORTS
        ):
            # 還不知道伺服器是誰 —— 改用「這個行程佔用的本機連接埠」認人，
            # 這樣按下登入送出的第一個封包也收得到。
            # 對端是 80／443 的排掉：那是 GameGuard 的 HTTPS，不是遊戲協定。
            return
        thl = (tcp[12] >> 4) * 4
        payload = tcp[thl:]
        if not payload:
            return

        now = time.time()
        for opcode, packet_bytes in split_packets(payload):
            self._counter += 1
            try:
                self._on_packet(
                    RoPacket(
                        seq=self._counter,
                        timestamp=now,
                        outbound=True,
                        opcode=opcode,
                        payload=packet_bytes[2:],
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 回呼不能害死擷取迴圈
                log.debug("封包回呼發生例外：%s", exc)

    def _close_socket(self) -> None:
        sock, self._socket = self._socket, None
        if sock is None:
            return
        try:
            sock.ioctl(socket.SIO_RCVALL, socket.RCVALL_OFF)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _report(self, message: str) -> None:
        log.error(message)
        if self._on_error is not None:
            self._on_error(message)
