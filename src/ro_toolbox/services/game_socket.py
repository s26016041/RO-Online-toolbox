"""複製遊戲的 TCP socket，在它那條連線上送封包。

為什麼可行：GameGuard 剝掉的是記憶體寫入權限，**DUP_HANDLE 沒被剝**
（見 GAMEDATA [PKT-011]、[PKT-012]）。所以可以：
  1. 列舉遊戲行程的所有 handle
  2. 把 handle 複製到本行程
  3. 對複製來的 handle 呼叫 getpeername，找出連到遊戲伺服器的那個 = 遊戲 socket
  4. 在這個 socket 上 send() 送明文封包

全程只做網路操作，不寫遊戲記憶體、不注入。

⚠ 這是在遊戲自己的連線上送封包。與遊戲同時 send 可能造成 TCP 位元組交錯，
   但 RO 送封包不頻繁、一次 send() 送完整封包，實務上可行。
"""

from __future__ import annotations

import ctypes
import logging
import socket
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

_k32 = ctypes.windll.kernel32
_ntdll = ctypes.windll.ntdll
_ws2 = ctypes.windll.ws2_32

_PROCESS_DUP_HANDLE = 0x0040
_DUPLICATE_SAME_ACCESS = 0x0002
_SYSTEM_EXTENDED_HANDLE_INFORMATION = 0x40
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

# ⚠ 一定要宣告 argtypes/restype：64 位元 Python 下不宣告的話，ctypes 把 HANDLE
#   當 32 位元 int 傳，指標被截半，DuplicateHandle 會拿到錯的值而靜默失敗。
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.DuplicateHandle.argtypes = [
    wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
    ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
]
_k32.DuplicateHandle.restype = wintypes.BOOL

_ws2.getpeername.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
]
_ws2.getpeername.restype = ctypes.c_int
_ws2.send.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
_ws2.send.restype = ctypes.c_int
_ws2.closesocket.argtypes = [ctypes.c_void_p]


class _HandleEntry(ctypes.Structure):
    _fields_ = [
        ("Object", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_void_p),
        ("HandleValue", ctypes.c_void_p),
        ("GrantedAccess", wintypes.ULONG),
        ("CreatorBackTraceIndex", wintypes.USHORT),
        ("ObjectTypeIndex", wintypes.USHORT),
        ("HandleAttributes", wintypes.ULONG),
        ("Reserved", wintypes.ULONG),
    ]


def _enum_handles(pid: int) -> list[int]:
    """列出某行程的所有 handle 值。"""
    size = 0x10000
    while True:
        buf = ctypes.create_string_buffer(size)
        need = wintypes.ULONG(0)
        status = _ntdll.NtQuerySystemInformation(
            _SYSTEM_EXTENDED_HANDLE_INFORMATION, buf, size, ctypes.byref(need)
        ) & 0xFFFFFFFF  # NTSTATUS 是有號回傳，遮成無號才好比對
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            size = max(need.value, size * 2)
            continue
        if status != 0:
            log.error("NtQuerySystemInformation 失敗：%#x", status)
            return []
        break

    count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_size_t))[0]
    # 陣列從結構開頭偏移一個指標大小的兩個欄位之後（NumberOfHandles + Reserved）
    offset = ctypes.sizeof(ctypes.c_size_t) * 2
    entries = ctypes.cast(
        ctypes.byref(buf, offset), ctypes.POINTER(_HandleEntry * count)
    )[0]

    result = []
    for entry in entries:
        if entry.UniqueProcessId == pid:
            result.append(int(entry.HandleValue))
    return result


class _SockAddrIn(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_short),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_char * 8),
    ]


def _peer_of(handle: int) -> tuple[str, int] | None:
    """對 handle 呼叫 getpeername；非 socket 或未連線回 None。"""
    addr = _SockAddrIn()
    length = ctypes.c_int(ctypes.sizeof(addr))
    if _ws2.getpeername(handle, ctypes.byref(addr), ctypes.byref(length)) != 0:
        return None
    if addr.sin_family != socket.AF_INET:
        return None
    ip = ".".join(str(b) for b in addr.sin_addr)
    port = socket.ntohs(addr.sin_port)
    return ip, port


def find_game_socket(pid: int, server_ip: str, server_port: int) -> int | None:
    """找出並複製遊戲連到伺服器的 socket，回傳本行程可用的 SOCKET handle。

    回傳的 handle 用完要 closesocket()。找不到回 None。
    """
    # 確保本行程的 Winsock 已初始化（import socket 已會做，這裡保險）
    socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()

    source = _k32.OpenProcess(_PROCESS_DUP_HANDLE, False, pid)
    if not source:
        log.error("OpenProcess(DUP_HANDLE) 失敗，PID %s", pid)
        return None

    try:
        me = _k32.GetCurrentProcess()
        for value in _enum_handles(pid):
            dup = wintypes.HANDLE()
            ok = _k32.DuplicateHandle(
                source, wintypes.HANDLE(value), me, ctypes.byref(dup),
                0, False, _DUPLICATE_SAME_ACCESS,
            )
            if not ok:
                continue
            peer = _peer_of(dup.value)
            if peer == (server_ip, server_port):
                log.debug("找到遊戲 socket：handle %#x 連到 %s:%s",
                          dup.value, server_ip, server_port)
                return dup.value
            _k32.CloseHandle(dup)
        # ⚠ 這裡**只記 DEBUG**。呼叫端幾乎都是「重試到成功或逾時」的迴圈
        # （剛換到角色伺服器的那幾秒複製不到，過一下就好），
        # 每次沒找到就 WARNING 的話，短短兩秒就是上百行洗版 ——
        # 使用者實際回報過。真的放棄時由呼叫端說一次就好。
        log.debug("在 PID %s 裡找不到連到 %s:%s 的 socket", pid, server_ip, server_port)
        return None
    finally:
        _k32.CloseHandle(source)


#: 複製不到 socket 時要重試多久（開機／換頻道那幾秒）。
#:
#: ⚠ **這不是「等一下再說」的敷衍，是實測出來的事實**：剛連上伺服器的那幾秒
#: 遊戲那條 socket **複製不到**（實測：列舉得到 773 個 handle、複製成功 552 個，
#: 但裡面只有 GameGuard 那條 443），過一會兒再找就 0.1 秒找到。
SOCKET_WAIT_SEC = 20.0
#: 換頻道／換地圖之後重綁的等待。比開機短 —— 那時整個 bot 的迴圈都卡在這裡。
SOCKET_REBIND_SEC = 10.0
_SOCKET_POLL = 0.3


def open_game_socket(
    pid: int,
    server_ip: str,
    server_port: int,
    timeout: float = SOCKET_WAIT_SEC,
    should_stop=None,
) -> int | None:
    """`find_game_socket()` 的重試版。**呼叫端一律用這支，不要自己叫一次就放棄。**

    ⚠ 這是實際踩過的坑：`find_game_socket()` 在剛連上／剛換頻道的那幾秒
    會回 None（見 `SOCKET_WAIT_SEC`）。`auto_login` 與 `potion` 各自寫了重試迴圈，
    但 `travel_bot` 與 `farm_bot` 是**叫一次就放棄** —— 使用者按下自動尋路
    只會看到「找不到遊戲 socket，無法送封包」，而且一按就死
    （實機日誌：10:51:49、10:51:58、10:52:08 連續三次，[PKT-072]）。
    同一條知識散在四個地方寫，就會有人漏掉；集中成一支。

    找不到回 None，並且**放棄時才記一次 WARNING**（迴圈裡每次都記的話
    兩秒就洗版一百行）。
    """
    deadline = time.monotonic() + timeout
    while True:
        sock = find_game_socket(pid, server_ip, server_port)
        if sock:
            return sock
        if should_stop is not None and should_stop():
            return None
        if time.monotonic() >= deadline:
            log.warning(
                "%.0f 秒內複製不到 PID %s 連到 %s:%s 的 socket",
                timeout, pid, server_ip, server_port,
            )
            return None
        time.sleep(_SOCKET_POLL)


def send_on_socket(sock: int, data: bytes) -> int:
    """在指定 socket 上送出 data，回傳送出的位元組數（-1 = 失敗）。"""
    buf = ctypes.create_string_buffer(data, len(data))
    sent = _ws2.send(sock, buf, len(data), 0)
    if sent < 0:
        err = _ws2.WSAGetLastError()
        log.error("send 失敗，WSA 錯誤 %s", err)
    return sent


def close_socket(sock: int) -> None:
    _ws2.closesocket(sock)
