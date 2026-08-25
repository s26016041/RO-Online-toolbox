"""以行程為目標的封包擷取。

後端用 Windows raw socket（見 `raw_capture.py`），**不需要 Npcap**，
只需要系統管理員權限。

設計重點：

1. **用行程的連線推導要綁哪張網卡。** raw socket 必須綁在單一介面 IP 上，
   直接從目標行程的 TCP 連線取得本機 IP，比讓使用者自己猜可靠。
2. **連接埠集合定期刷新。** 目標行程重連、換伺服器都會換連接埠，
   每 2 秒重查一次，新連線自動納入追蹤。
3. **批次送進 UI。** 擷取回呼跑在背景執行緒，逐封包 emit 會塞爆事件迴圈，
   因此先進佇列，再由計時器每 100ms 整批送出。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Iterable

from PySide6.QtCore import QObject, QTimer, Signal

from ro_toolbox.core.packet import CapturedPacket
from ro_toolbox.services.process_monitor import (
    all_local_ipv4,
    local_addresses_of,
    local_ports_of,
    psutil_available,
)
from ro_toolbox.services.raw_capture import ParsedPacket, RawSocketSniffer, supported

log = logging.getLogger(__name__)

_FLUSH_INTERVAL_MS = 100
_PORT_REFRESH_MS = 2000

_INSTALL_HINT = r"    .\.venv\Scripts\python.exe -m pip install -e .[packet]"

AUTO_BIND = ""
"""空字串代表「依目標程式的連線自動選擇網卡」。"""


def missing_dependencies() -> list[str]:
    """列出缺少的選用套件。"""
    return [] if psutil_available() else ["psutil"]


def dependency_hint(missing: list[str]) -> str:
    return "缺少套件：" + "、".join(missing) + "。請執行：\n" + _INSTALL_HINT


def backend_status() -> tuple[bool, str]:
    """檢查擷取後端是否可用，回傳 (可用, 錯誤說明)。"""
    missing = missing_dependencies()
    if missing:
        return False, dependency_hint(missing)

    if not supported():
        return False, (
            "這個平台沒有 SIO_RCVALL，raw socket 擷取只支援 Windows。"
        )

    return True, ""


def list_bind_addresses() -> list[tuple[str, str]]:
    """回傳 [(顯示名稱, 綁定用 IP)]，第一筆為自動選擇。"""
    result: list[tuple[str, str]] = [("自動（依目標程式的連線）", AUTO_BIND)]
    result.extend((address, address) for address in all_local_ipv4())
    return result


class PacketCapture(QObject):
    """擷取指定行程的 TCP／UDP 流量。"""

    packets_ready = Signal(list)
    error = Signal(str)
    ports_changed = Signal(set)

    def __init__(self) -> None:
        super().__init__()
        self._sniffer: RawSocketSniffer | None = None
        self._queue: deque[CapturedPacket] = deque()
        self._local_ports: set[int] = set()
        self._pid: int | None = None
        self._counter = 0
        self._include_empty = False

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)

        self._port_timer = QTimer(self)
        self._port_timer.setInterval(_PORT_REFRESH_MS)
        self._port_timer.timeout.connect(self._refresh_ports)

    @property
    def is_running(self) -> bool:
        return self._sniffer is not None and self._sniffer.is_running

    @property
    def tracked_ports(self) -> set[int]:
        return set(self._local_ports)

    # ---- 控制 -------------------------------------------------------

    def start(
        self, pid: int, bind_ip: str = AUTO_BIND, include_empty: bool = False
    ) -> bool:
        if self.is_running:
            return True

        ok, message = backend_status()
        if not ok:
            self.error.emit(message)
            return False

        self._pid = pid
        self._include_empty = include_empty
        self._counter = 0
        self._queue.clear()
        self._refresh_ports()

        if not self._local_ports:
            self.error.emit(
                "這個行程目前沒有作用中的 TCP 連線。\n"
                "請確認遊戲已連上伺服器，或改以系統管理員身分執行本程式。"
            )
            return False

        address = bind_ip or self._auto_bind_address()
        if not address:
            self.error.emit(
                "找不到這個行程使用的本機 IP，無法決定要綁哪張網卡。\n"
                "請在「網路介面」手動指定。"
            )
            return False

        sniffer = RawSocketSniffer(
            bind_ip=address,
            on_packet=self._on_packet,
            on_error=self.error.emit,
        )
        if not sniffer.start():
            return False

        self._sniffer = sniffer
        self._flush_timer.start()
        self._port_timer.start()
        log.info(
            "開始擷取 PID %s（綁定 %s），追蹤連接埠 %s",
            pid,
            address,
            sorted(self._local_ports),
        )
        return True

    def stop(self) -> None:
        self._flush_timer.stop()
        self._port_timer.stop()

        if self._sniffer is not None:
            self._sniffer.stop()
            self._sniffer = None

        self._flush()

    # ---- 內部 -------------------------------------------------------

    def _auto_bind_address(self) -> str:
        if self._pid is None:
            return ""
        addresses = local_addresses_of(self._pid)
        if not addresses:
            return ""
        # 有多個時取任一個都對，因為同一行程通常只走一張網卡
        return sorted(addresses)[0]

    def _refresh_ports(self) -> None:
        if self._pid is None:
            return
        ports = local_ports_of(self._pid)
        if ports and ports != self._local_ports:
            added = ports - self._local_ports
            if added:
                log.info("偵測到新連線，新增追蹤連接埠 %s", sorted(added))
            self._local_ports = ports
            self.ports_changed.emit(set(ports))

    def _on_packet(self, parsed: ParsedPacket) -> None:
        """在擷取執行緒執行，只做最小處理然後丟進佇列。"""
        _protocol, src_ip, dst_ip, src_port, dst_port, payload = parsed

        outbound = src_port in self._local_ports
        if not outbound and dst_port not in self._local_ports:
            return
        if not payload and not self._include_empty:
            return

        self._counter += 1
        self._queue.append(
            CapturedPacket(
                index=self._counter,
                timestamp=time.time(),
                src_ip=src_ip,
                src_port=src_port,
                dst_ip=dst_ip,
                dst_port=dst_port,
                outbound=outbound,
                payload=payload,
            )
        )

    def _flush(self) -> None:
        if not self._queue:
            return
        batch: list[CapturedPacket] = []
        while self._queue:
            batch.append(self._queue.popleft())
        self.packets_ready.emit(batch)


def summarise(packets: Iterable[CapturedPacket]) -> str:
    items = list(packets)
    if not items:
        return "尚未擷取到封包"
    out = sum(1 for p in items if p.outbound)
    total = sum(p.length for p in items)
    return f"{len(items)} 筆（↑{out} / ↓{len(items) - out}），共 {total:,} bytes"
