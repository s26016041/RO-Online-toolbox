"""行程與 TCP 連線查詢。

Windows 的封包擷取本身不帶 process 資訊，所以要靠系統的 TCP 連線表
把「pid」對應到「本機連接埠集合」，再拿這個集合去過濾抓到的封包。
連線會隨重連／換伺服器改變，呼叫端需定期重新查詢。
"""

from __future__ import annotations

import ctypes
import logging
import os
from dataclasses import dataclass

# psutil 屬於 [packet] extra，沒裝時本模組要能安全匯入——
# 否則整個程式會因為一個選用功能而開不起來。
try:
    import psutil
except ImportError:  # pragma: no cover - 取決於安裝方式
    psutil = None

log = logging.getLogger(__name__)


def psutil_available() -> bool:
    return psutil is not None


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    name: str
    connection_count: int

    @property
    def label(self) -> str:
        return f"{self.name}  (PID {self.pid}，{self.connection_count} 條連線)"


def is_admin() -> bool:
    """未以系統管理員執行時，抓不到封包也讀不到其他行程的連線。"""
    if os.name != "nt":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _tcp_connections() -> list:
    if psutil is None:
        return []
    try:
        return psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, PermissionError):
        log.warning("讀取連線表被拒絕，請以系統管理員身分執行")
        return []
    except Exception as exc:  # noqa: BLE001
        log.error("讀取連線表失敗：%s", exc)
        return []


def list_network_processes() -> list[ProcessInfo]:
    """列出目前有 TCP 連線的行程，連線數多的排前面。"""
    counts: dict[int, int] = {}
    for conn in _tcp_connections():
        # pid 0 是 System Idle Process，抓不到也沒意義
        if not conn.pid:
            continue
        counts[conn.pid] = counts.get(conn.pid, 0) + 1

    result: list[ProcessInfo] = []
    for pid, count in counts.items():
        try:
            name = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001 - 行程可能已結束或無權限
            name = f"pid-{pid}"
        result.append(ProcessInfo(pid=pid, name=name, connection_count=count))

    result.sort(key=lambda p: (-p.connection_count, p.name.lower()))
    return result


def local_ports_of(pid: int) -> set[int]:
    """該行程目前佔用的本機 TCP 連接埠。"""
    ports: set[int] = set()
    for conn in _tcp_connections():
        if conn.pid == pid and conn.laddr:
            ports.add(conn.laddr.port)
    return ports


def remote_endpoints_of(pid: int) -> set[tuple[str, int]]:
    """該行程連線的對端位址，用來辨識遊戲伺服器。"""
    endpoints: set[tuple[str, int]] = set()
    for conn in _tcp_connections():
        if conn.pid == pid and conn.raddr:
            endpoints.add((conn.raddr.ip, conn.raddr.port))
    return endpoints


def local_addresses_of(pid: int) -> set[str]:
    """該行程連線所使用的本機 IP。

    raw socket 必須綁定在特定介面的 IP 上，直接從目標行程的連線推導，
    比讓使用者自己猜是哪張網卡可靠。
    """
    addresses: set[str] = set()
    for conn in _tcp_connections():
        if conn.pid == pid and conn.laddr and conn.raddr:
            addresses.add(conn.laddr.ip)
    return addresses


def all_local_ipv4() -> list[str]:
    """本機所有 IPv4 位址，供使用者手動指定介面。"""
    if psutil is None:
        return []
    found: list[str] = []
    for addresses in psutil.net_if_addrs().values():
        for addr in addresses:
            if addr.family.name == "AF_INET" and addr.address != "127.0.0.1":
                found.append(addr.address)
    return sorted(set(found))
