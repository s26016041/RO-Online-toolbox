"""用 Npcap 擷取 RO 連線的**雙向**封包（含伺服器推送的 inbound）。

raw socket 收不到 inbound TCP（見 GAMEDATA [PKT-003]），要看伺服器推送的
怪物／道具封包只能靠 Npcap。這一層走網路驅動，不碰遊戲行程，GameGuard 看不到。

需要安裝 Npcap（https://npcap.com，勾 WinPcap API-compatible mode）。
scapy 延遲匯入：沒裝 Npcap 時 available() 會回報，不會讓程式炸掉。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ro_toolbox.core.ro_packet import RoPacket, split_packets
from ro_toolbox.services.ro_capture import find_server

log = logging.getLogger(__name__)

_NPCAP_HINT = (
    "需要 Npcap 才能擷取伺服器回傳的封包。\n"
    "請到 https://npcap.com 下載安裝，安裝時勾選\n"
    "「Install Npcap in WinPcap API-compatible Mode」，裝完重跑本程式。"
)


def available() -> tuple[bool, str]:
    """檢查 Npcap 後端是否可用。"""
    try:
        from scapy.config import conf
    except ImportError:
        return False, "尚未安裝 scapy。"
    if not conf.use_pcap:
        return False, _NPCAP_HINT
    return True, ""


class NpcapCapture:
    """擷取單一 RO 行程與伺服器的雙向封包。

    callback 在 scapy 的執行緒執行，呼叫端不可在裡面直接碰 UI。
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
        self._sniffer = None
        self._server_ip = ""
        self._server_port = 0
        self._counter = 0

    @property
    def is_running(self) -> bool:
        return self._sniffer is not None

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
        self._server_ip, self._server_port = server

        try:
            from scapy.sendrecv import AsyncSniffer

            # 只抓與這台伺服器、這個埠往來的 TCP，兩個方向都收
            bpf = f"tcp and host {self._server_ip} and port {self._server_port}"
            self._sniffer = AsyncSniffer(filter=bpf, prn=self._on_scapy, store=False)
            self._sniffer.start()
        except Exception as exc:  # noqa: BLE001
            self._sniffer = None
            self._report(f"啟動 Npcap 擷取失敗：{exc}")
            return False

        log.info("Npcap 擷取啟動：PID %s ↔ %s:%s", self._pid, self._server_ip, self._server_port)
        return True

    def stop(self) -> None:
        if self._sniffer is not None:
            try:
                self._sniffer.stop()
            except Exception as exc:  # noqa: BLE001 - 未真正開始時 stop 會拋錯
                log.debug("停止 Npcap 擷取的例外（可忽略）：%s", exc)
            self._sniffer = None

    # ---- 內部 -------------------------------------------------------

    def _on_scapy(self, packet) -> None:
        try:
            from scapy.layers.inet import IP, TCP

            if IP not in packet or TCP not in packet:
                return
            payload = bytes(packet[TCP].payload)
            if not payload:
                return

            # 送往伺服器 = outbound（CZ_）；來自伺服器 = inbound（ZC_）
            outbound = packet[IP].dst == self._server_ip
            timestamp = float(packet.time)

            for opcode, packet_bytes in split_packets(payload):
                self._counter += 1
                self._on_packet(
                    RoPacket(
                        seq=self._counter,
                        timestamp=timestamp,
                        outbound=outbound,
                        opcode=opcode,
                        payload=packet_bytes[2:],
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 回呼不能害死 sniffer
            log.debug("處理封包時發生例外：%s", exc)

    def _report(self, message: str) -> None:
        log.error(message)
        if self._on_error is not None:
            self._on_error(message)
