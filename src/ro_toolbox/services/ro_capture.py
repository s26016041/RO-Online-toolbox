"""擷取單一 RO 行程與伺服器往來的封包。

用 Windows raw socket（見 raw_capture.py），**完全不碰遊戲行程** ——
GameGuard 剝掉的是記憶體寫入權限，網路層擷取它管不到（見 GAMEDATA [PKT-011]）。

限制：raw socket 只收得到 **outbound**（送出）封包（見 [PKT-003]）。
反向工程「哪個動作送哪個封包」正好只需要 outbound，所以這階段夠用。
要收伺服器推送（inbound）得改用 Npcap。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable

from ro_toolbox.core.ro_packet import RoPacket, split_packets
from ro_toolbox.services.process_monitor import (
    local_addresses_of,
    local_ports_of,
    remote_endpoints_of,
)

log = logging.getLogger(__name__)

_RECV_SIZE = 65535
_TIMEOUT = 1.0
_PROTO_TCP = 6

_PRIVATE_PREFIXES = ("127.", "192.168.", "10.", "0.", "169.254.")


def find_server(pid: int) -> tuple[str, int] | None:
    """找出行程連到的遊戲伺服器（排除本機／私有位址）。"""
    for ip, port in sorted(remote_endpoints_of(pid)):
        if not ip.startswith(_PRIVATE_PREFIXES):
            return ip, port
    return None


def bind_address_for(pid: int) -> str | None:
    """raw socket 要綁的本機 IP（取行程連線用的那張網卡）。"""
    addresses = local_addresses_of(pid)
    return sorted(addresses)[0] if addresses else None


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

        server = find_server(self._pid)
        if server is None:
            self._report("這個行程沒有連到遊戲伺服器（可能還沒登入）。")
            return False
        self._server_ip = server[0]

        bind_ip = bind_address_for(self._pid)
        if not bind_ip:
            self._report("找不到這個行程使用的本機 IP。")
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
        while not self._stop.is_set():
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
        if dst != self._server_ip:  # 只要送往遊戲伺服器的
            return

        ihl = (raw[0] & 0x0F) * 4
        tcp = raw[ihl:]
        if len(tcp) < 20:
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
