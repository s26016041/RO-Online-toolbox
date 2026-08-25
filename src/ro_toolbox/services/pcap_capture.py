"""用 ctypes 直接呼叫 wpcap.dll（Npcap）擷取 RO 連線的雙向封包。

scapy 2.7.0 載不到 Npcap（conf.use_pcap=False、winpcapy 匯入失敗），
所以繞過 scapy，直接用 wpcap.dll —— 更輕、更好掌控，也避開 scapy 版本問題。

網路層擷取，不碰遊戲行程，GameGuard 看不到（見 GAMEDATA [PKT-011]）。
需要 Npcap 已安裝（wpcap.dll 存在）。
"""

from __future__ import annotations

import ctypes
import logging
import socket
import threading
from collections.abc import Callable

from ro_toolbox.core.ro_packet import RoPacket, split_packets
from ro_toolbox.services.packet_table import extract as extract_lengths
from ro_toolbox.services.ro_capture import bind_address_for, find_server

log = logging.getLogger(__name__)

_WPCAP_PATH = r"C:\Windows\System32\wpcap.dll"
_SNAPLEN = 65536
_PROMISC = 0
_TIMEOUT_MS = 100
#: 看門狗多久檢查一次遊戲連線有沒有換位址（換地圖／換頻道／重登）。
_RESYNC_SEC = 1.0
_DLT_EN10MB = 1
_DLT_RAW = 12
_DLT_NULL = 0
_PROTO_TCP = 6


class _pcap_if(ctypes.Structure):
    pass


_pcap_if._fields_ = [
    ("next", ctypes.POINTER(_pcap_if)),
    ("name", ctypes.c_char_p),
    ("description", ctypes.c_char_p),
    ("addresses", ctypes.c_void_p),
    ("flags", ctypes.c_uint),
]


class _pcap_addr(ctypes.Structure):
    pass


_pcap_addr._fields_ = [
    ("next", ctypes.POINTER(_pcap_addr)),
    ("addr", ctypes.c_void_p),
    ("netmask", ctypes.c_void_p),
    ("broadaddr", ctypes.c_void_p),
    ("dstaddr", ctypes.c_void_p),
]


class _pcap_pkthdr(ctypes.Structure):
    # Windows/Npcap 的 timeval 用 32 位元 long
    _fields_ = [
        ("tv_sec", ctypes.c_long),
        ("tv_usec", ctypes.c_long),
        ("caplen", ctypes.c_uint32),
        ("len", ctypes.c_uint32),
    ]


class _bpf_program(ctypes.Structure):
    _fields_ = [("bf_len", ctypes.c_uint), ("bf_insns", ctypes.c_void_p)]


_wp = None


def _lib():
    global _wp
    if _wp is not None:
        return _wp
    lib = ctypes.WinDLL(_WPCAP_PATH)
    lib.pcap_findalldevs.argtypes = [
        ctypes.POINTER(ctypes.POINTER(_pcap_if)), ctypes.c_char_p
    ]
    lib.pcap_findalldevs.restype = ctypes.c_int
    lib.pcap_freealldevs.argtypes = [ctypes.POINTER(_pcap_if)]
    lib.pcap_open_live.argtypes = [
        ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p
    ]
    lib.pcap_open_live.restype = ctypes.c_void_p
    lib.pcap_close.argtypes = [ctypes.c_void_p]
    lib.pcap_datalink.argtypes = [ctypes.c_void_p]
    lib.pcap_datalink.restype = ctypes.c_int
    lib.pcap_compile.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_bpf_program),
        ctypes.c_char_p, ctypes.c_int, ctypes.c_uint,
    ]
    lib.pcap_compile.restype = ctypes.c_int
    lib.pcap_setfilter.argtypes = [ctypes.c_void_p, ctypes.POINTER(_bpf_program)]
    lib.pcap_setfilter.restype = ctypes.c_int
    lib.pcap_freecode.argtypes = [ctypes.POINTER(_bpf_program)]
    lib.pcap_next_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(_pcap_pkthdr)),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    lib.pcap_next_ex.restype = ctypes.c_int
    lib.pcap_breakloop.argtypes = [ctypes.c_void_p]
    _wp = lib
    return _wp


def available() -> tuple[bool, str]:
    import os

    if not os.path.exists(_WPCAP_PATH):
        return False, "找不到 wpcap.dll，Npcap 未安裝。"
    try:
        _lib()
        return True, ""
    except OSError as exc:
        return False, f"wpcap.dll 載入失敗：{exc}"


def _iter_ipv4(addr_ptr: int):
    """走 pcap_addr 鏈，吐出 IPv4 位址字串。"""
    p = ctypes.cast(addr_ptr, ctypes.POINTER(_pcap_addr))
    while p:
        node = p.contents
        if node.addr:
            family = ctypes.cast(node.addr, ctypes.POINTER(ctypes.c_ushort))[0]
            if family == socket.AF_INET:
                # sockaddr_in: family(2) port(2) addr(4)
                raw = ctypes.string_at(node.addr, 8)
                yield ".".join(str(b) for b in raw[4:8])
        p = node.next


def device_for_ip(target_ip: str) -> str | None:
    """找出綁著 target_ip 的擷取裝置名稱。"""
    lib = _lib()
    errbuf = ctypes.create_string_buffer(256)
    alldevs = ctypes.POINTER(_pcap_if)()
    if lib.pcap_findalldevs(ctypes.byref(alldevs), errbuf) != 0:
        log.error("pcap_findalldevs 失敗：%s", errbuf.value)
        return None
    try:
        d = alldevs
        while d:
            dev = d.contents
            if dev.addresses and target_ip in _iter_ipv4(dev.addresses):
                return dev.name.decode(errors="replace")
            d = dev.next
        return None
    finally:
        lib.pcap_freealldevs(alldevs)


class PcapCapture:
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
        # 重綁期間 pcap_next_ex 會回錯誤，那是我們自己關掉控制代碼造成的，
        # 不能報成「擷取中斷」，也不能讓看門狗誤判成掉線。
        self._rebinding = threading.Event()
        self._lock = threading.RLock()
        self._server_ip = ""
        self._server_port = 0
        self._eth_offset = 14
        self._counter = 0
        self._rebinds = 0
        # opcode → (長度, 標頭)。用 AOB 從客戶端程式碼抽出來（[MEM-024]）。
        # 沒抽到就是空的 —— split_packets 會退回「整段當一包」的舊行為。
        self._lengths: dict[int, tuple[int, int]] = {}

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def server(self) -> str:
        return self._server_ip

    def start(self) -> bool:
        ok, message = available()
        if not ok:
            self._report(message)
            return False

        server = find_server(self._pid)
        if server is None:
            self._report("這個行程沒有連到遊戲伺服器（可能還沒登入）。")
            return False
        if not self._open(server):
            return False
        self._load_lengths()
        self._stop.clear()
        self._start_loop()
        self._watchdog = threading.Thread(
            target=self._watch, name="pcap-watch", daemon=True
        )
        self._watchdog.start()
        return True

    def _open(self, server: tuple[str, int]) -> bool:
        """開一個 pcap 控制代碼並綁定 BPF。換 map-server 後會再呼叫一次。"""
        self._server_ip, self._server_port = server

        bind_ip = bind_address_for(self._pid)
        device = device_for_ip(bind_ip) if bind_ip else None
        if not device:
            self._report(f"找不到綁著本機 IP {bind_ip} 的擷取裝置。")
            return False

        lib = _lib()
        errbuf = ctypes.create_string_buffer(256)
        handle = lib.pcap_open_live(
            device.encode(), _SNAPLEN, _PROMISC, _TIMEOUT_MS, errbuf
        )
        if not handle:
            self._report(f"pcap_open_live 失敗：{errbuf.value.decode(errors='replace')}")
            return False
        self._handle = handle

        dlt = lib.pcap_datalink(handle)
        self._eth_offset = {_DLT_EN10MB: 14, _DLT_RAW: 0, _DLT_NULL: 4}.get(dlt, 14)

        # ⚠ BPF **不綁伺服器位址**，只留 `tcp`。
        # 綁位址的話換地圖（連到新的 map-server）就得關掉重開控制代碼，
        # 而整份背包清單在換圖後立刻就送過來，正好落在重開的空窗裡收不到
        # （實測連兩趟換圖都漏掉）。改成收全部 TCP、在 `_handle_frame`
        # 用「當下的伺服器位址」過濾 —— 換位址只要換個變數，零空窗。
        bpf = "tcp"
        prog = _bpf_program()
        if lib.pcap_compile(handle, ctypes.byref(prog), bpf.encode(), 1, 0xFFFFFFFF) == 0:
            lib.pcap_setfilter(handle, ctypes.byref(prog))
            lib.pcap_freecode(ctypes.byref(prog))
        else:
            log.warning("BPF 編譯失敗，改在 Python 端過濾")

        log.info("Npcap 擷取綁定：PID %s ↔ %s:%s（%s）",
                 self._pid, self._server_ip, self._server_port, device)
        return True

    def _load_lengths(self) -> None:
        """抽封包長度表，讓切包精確。抽不到就照舊（整段當一包）。"""
        try:
            table = extract_lengths(self._pid)
        except Exception as exc:  # noqa: BLE001 - 抽不到不該讓擷取起不來
            log.warning("抽封包長度表失敗，改用整段當一包：%s", exc)
            return
        self._lengths = {op: (info.length, info.header) for op, info in table.items()}
        if self._lengths:
            log.info("封包長度表：%d 個 opcode，切包改為精確模式", len(self._lengths))
        else:
            log.warning("沒抽到封包長度表，切包退回「整段當一包」—— "
                        "黏在後面的封包會看不到")

    def _start_loop(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="pcap", daemon=True)
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
        """
        while not self._stop.wait(_RESYNC_SEC):
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
            lib = _lib()
            lib.pcap_breakloop(self._handle)
            self._rebinding.set()
            lib.pcap_close(self._handle)
            self._handle = None

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        with self._lock:
            if self._handle:
                _lib().pcap_breakloop(self._handle)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        watchdog = self._watchdog
        if watchdog is not None and watchdog.is_alive():
            watchdog.join(timeout)
        self._watchdog = None
        with self._lock:
            if self._handle:
                _lib().pcap_close(self._handle)
                self._handle = None

    # ---- 內部 -------------------------------------------------------

    def _loop(self) -> None:
        lib = _lib()
        header = ctypes.POINTER(_pcap_pkthdr)()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        while not self._stop.is_set():
            handle = self._handle
            if handle is None:
                return
            rc = lib.pcap_next_ex(handle, ctypes.byref(header), ctypes.byref(data))
            if rc == 0:
                continue  # timeout
            if rc < 0:
                # 重綁時是我們自己關掉控制代碼的，不是真的斷線 —— 別嚇使用者，
                # 也別讓它變成「大聲失敗」。看門狗會把新的接起來。
                if not self._stop.is_set() and not self._rebinding.is_set():
                    log.warning("Npcap 擷取中斷，看門狗會重開")
                return
            length = header.contents.caplen
            raw = ctypes.string_at(data, length)
            self._handle_frame(raw)

    def _handle_frame(self, frame: bytes) -> None:
        ip = frame[self._eth_offset:]
        if len(ip) < 20 or (ip[0] >> 4) != 4 or ip[9] != _PROTO_TCP:
            return
        ihl = (ip[0] & 0x0F) * 4
        src = ".".join(str(b) for b in ip[12:16])
        dst = ".".join(str(b) for b in ip[16:20])
        if self._server_ip not in (src, dst):
            return

        tcp = ip[ihl:]
        if len(tcp) < 20:
            return
        thl = (tcp[12] >> 4) * 4
        payload = tcp[thl:]
        if not payload:
            return

        outbound = dst == self._server_ip
        timestamp = self._now()
        for opcode, packet_bytes in split_packets(payload, self._lengths):
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
