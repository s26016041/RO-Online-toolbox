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


# ---- 連線建立時間 ---------------------------------------------------
#
# 為什麼需要它：換地圖時伺服器會把連線移到另一台 map server，而**舊連線會留著**
# （實測留了 11 分鐘才收掉）。這段期間 `remote_endpoints_of()` 會回兩條，
# 舊版就「取第一條」—— 那是擲骰子。挑錯就是把走路封包送進一條沒人收的連線，
# 而且完全不會報錯（[PKT-063]）。
#
# Windows 的 TCP 表本來就有每條連線的建立時間（`Get-NetTCPConnection` 的
# CreationTime 就是它），psutil 沒有轉出來，所以這裡直接叫 iphlpapi：
# `GetExtendedTcpTable(TCP_TABLE_OWNER_MODULE_ALL)` 的
# `MIB_TCPROW_OWNER_MODULE.liCreateTimestamp`。有了它就能挑**最新建立**的那條。

_AF_INET = 2
_TCP_TABLE_OWNER_MODULE_ALL = 8
_ERROR_INSUFFICIENT_BUFFER = 122
_TCPIP_OWNING_MODULE_SIZE = 16
_MIB_STATE_ESTABLISHED = 5


class _MIB_TCPROW_OWNER_MODULE(ctypes.Structure):  # noqa: N801 - 跟 Win32 SDK 同名
    _fields_ = [
        ("dwState", ctypes.c_uint32),
        ("dwLocalAddr", ctypes.c_uint32),
        ("dwLocalPort", ctypes.c_uint32),
        ("dwRemoteAddr", ctypes.c_uint32),
        ("dwRemotePort", ctypes.c_uint32),
        ("dwOwningPid", ctypes.c_uint32),
        ("liCreateTimestamp", ctypes.c_int64),
        ("OwningModuleInfo", ctypes.c_uint64 * _TCPIP_OWNING_MODULE_SIZE),
    ]


@dataclass(frozen=True, slots=True)
class Connection:
    """一條 TCP 連線。`created` 是 FILETIME（100ns 起算），只用來比新舊。"""

    ip: str
    port: int
    created: int
    established: bool

    @property
    def endpoint(self) -> tuple[str, int]:
        return self.ip, self.port


def connections_of(pid: int) -> list[Connection]:
    """該行程的對外 TCP 連線，**依建立時間由新到舊**排序。

    拿不到（非 Windows、iphlpapi 失敗、權限不足）就回空清單，
    呼叫端要能安全退化 —— 絕不要因為讀不到時間就亂猜一條。
    """
    if os.name != "nt":
        return []
    try:
        iphlpapi = ctypes.windll.iphlpapi
    except Exception:  # noqa: BLE001
        return []

    size = ctypes.c_uint32(0)
    ret = iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_MODULE_ALL, 0
    )
    if ret != _ERROR_INSUFFICIENT_BUFFER or size.value == 0:
        log.debug("GetExtendedTcpTable 量長度失敗：%s", ret)
        return []
    buffer = ctypes.create_string_buffer(size.value)
    ret = iphlpapi.GetExtendedTcpTable(
        buffer, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_MODULE_ALL, 0
    )
    if ret != 0:
        log.debug("GetExtendedTcpTable 讀取失敗：%s", ret)
        return []

    count = ctypes.c_uint32.from_buffer(buffer, 0).value
    # 陣列元素含 8-byte 欄位，所以要對齊到 8 —— 前面的 dwNumEntries 只有 4。
    offset = 8
    stride = ctypes.sizeof(_MIB_TCPROW_OWNER_MODULE)
    found: list[Connection] = []
    for i in range(count):
        start = offset + i * stride
        if start + stride > len(buffer):
            break
        row = _MIB_TCPROW_OWNER_MODULE.from_buffer(buffer, start)
        if row.dwOwningPid != pid or not row.dwRemoteAddr:
            continue
        ip = ".".join(str(b) for b in row.dwRemoteAddr.to_bytes(4, "little"))
        port = int.from_bytes(row.dwRemotePort.to_bytes(4, "little")[:2], "big")
        if not port:
            continue
        found.append(
            Connection(
                ip=ip,
                port=port,
                created=int(row.liCreateTimestamp),
                established=row.dwState == _MIB_STATE_ESTABLISHED,
            )
        )
    found.sort(key=lambda c: c.created, reverse=True)
    return found


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


def local_network_up() -> bool:
    """本機還有沒有可用的網路？**不發任何封包**，只看網卡狀態。

    為什麼需要它：遊戲沒有連線時有兩種情況，處理方式完全相反 ——
    **你自己的網路斷了**（關遊戲重開是幫倒忙：重開照樣連不上，
    而且原本還在線上的角色被登出了），或**遊戲自己斷線**（那才該重連）。

    判準：有一張**非回送**的網卡是 up 的，而且身上有 IPv4 位址。
    拔網路線／Wi-Fi 掉線都會讓這條變成 False。

    ⚠ 這條看不出「網卡正常但路由器死了」。那種情況會被當成遊戲斷線 ——
    所以重連端一定要有退避（見 `services/reconnect.py`），不能無腦一直重開。
    psutil 沒裝時一律回 True（寧可照舊行為，也不要因為查不到就停掉功能）。
    """
    if psutil is None:
        return True
    try:
        stats = psutil.net_if_stats()
        addresses = psutil.net_if_addrs()
    except Exception as exc:  # noqa: BLE001 - 查不到就別擋住功能
        log.debug("查網卡狀態失敗：%s", exc)
        return True
    for name, stat in stats.items():
        if not stat.isup:
            continue
        for addr in addresses.get(name, ()):
            if addr.family.name == "AF_INET" and not addr.address.startswith("127."):
                return True
    return False


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
