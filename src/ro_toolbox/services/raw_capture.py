"""Windows raw socket 封包擷取。

**不需要 Npcap。** Windows 的 `SIO_RCVALL` 可以讓 raw socket 收下該介面上所有
IPv4 流量，只用標準函式庫就能做到，代價只有「必須以系統管理員執行」。

限制（選這個方案前一定要知道）：

- **收不到 inbound TCP。** Windows 的 TCP/IP 堆疊會攔下進入的 TCP，不交給
  raw socket。實測 RCVALL_ON 與 RCVALL_IPLEVEL 兩種模式下，帶 payload 的
  inbound TCP 封包數都是 0，而 outbound 正常。inbound UDP 則收得到。
  要看伺服器回應必須改用 WinDivert（見 services/packet_capture）。GAMEDATA [PKT-003]。
- 只能被動接收，不能攔截或修改封包。
- 只收 IPv4，收不到 loopback（127.0.0.1）流量。
- socket 綁在單一介面 IP 上，多網卡／VPN 環境要綁對那張才收得到。
- 收到的是 IP 層封包（含 IP 標頭），要自己解析。

參考自 Angels-Online-toolbox 的 `tools/sniff.py`。
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

_PROTO_TCP = 6
_PROTO_UDP = 17
_RECV_SIZE = 65535
_TIMEOUT_SECONDS = 1.0

ParsedPacket = tuple[str, str, str, int, int, bytes]
"""(協定, 來源IP, 目的IP, 來源埠, 目的埠, payload)"""


def parse_ip_packet(data: bytes) -> ParsedPacket | None:
    """解析 raw socket 收到的 IPv4 封包，非 TCP/UDP 一律回傳 None。"""
    if len(data) < 20:
        return None

    header_length = (data[0] & 0x0F) * 4
    if len(data) < header_length:
        return None

    protocol = data[9]
    src_ip = ".".join(str(b) for b in data[12:16])
    dst_ip = ".".join(str(b) for b in data[16:20])
    rest = data[header_length:]

    if protocol == _PROTO_TCP:
        if len(rest) < 20:
            return None
        src_port, dst_port = struct.unpack(">HH", rest[0:4])
        offset = (rest[12] >> 4) * 4
        if len(rest) < offset:
            return None
        return ("TCP", src_ip, dst_ip, src_port, dst_port, rest[offset:])

    if protocol == _PROTO_UDP:
        if len(rest) < 8:
            return None
        src_port, dst_port = struct.unpack(">HH", rest[0:4])
        return ("UDP", src_ip, dst_ip, src_port, dst_port, rest[8:])

    return None


def supported() -> bool:
    """這個後端只在 Windows 有 SIO_RCVALL。"""
    return hasattr(socket, "SIO_RCVALL")


class RawSocketSniffer:
    """在背景執行緒上收封包，逐一交給 callback。

    callback 跑在背景執行緒，呼叫端不可以在裡面直接碰 UI。
    """

    def __init__(
        self,
        bind_ip: str,
        on_packet: Callable[[ParsedPacket], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._bind_ip = bind_ip
        self._on_packet = on_packet
        self._on_error = on_error
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.is_running:
            return True

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
            sock.bind((self._bind_ip, 0))
            # 不設 IP_HDRINCL：那是給「送出」自帶 IP 標頭用的，接收端不需要。
            sock.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)
            sock.settimeout(_TIMEOUT_SECONDS)
        except (PermissionError, OSError) as exc:
            self._report(
                f"建立 raw socket 失敗（綁定 {self._bind_ip}）：{exc}\n\n"
                "最常見原因是沒有以系統管理員身分執行。"
            )
            return False

        self._socket = sock
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="raw-sniffer", daemon=True
        )
        self._thread.start()
        log.info("raw socket 擷取啟動，綁定 %s", self._bind_ip)
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("擷取執行緒未在 %s 秒內結束", timeout)
        self._thread = None
        self._close_socket()
        log.info("raw socket 擷取停止")

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

            parsed = parse_ip_packet(data)
            if parsed is not None:
                try:
                    self._on_packet(parsed)
                except Exception as exc:  # noqa: BLE001 - 回呼不可以害死擷取迴圈
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
