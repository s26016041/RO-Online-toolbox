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
# ⛔ 這裡**故意不宣告 `closesocket`** —— 見 `close_socket()`：對複製來的 handle
#    呼叫 closesocket 會把遊戲的連線一起關掉。不宣告就不會有人不小心用到。


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


def open_any_game_socket(
    pid: int,
    servers,
    timeout: float = SOCKET_WAIT_SEC,
    should_stop=None,
):
    """一條一條試，回 `(sock, (ip, port))`；全部失敗回 `(None, None)`。

    ## 為什麼要試不只一條

    實機 2026-08-29：Ragexe 除了地圖伺服器之外還掛著一條 `.55:3000`，
    而且它**比較新**，於是 `find_server()` 挑了它 —— 複製不到、等 10 秒、
    自動尋路整個停掉（真正在跑的 `.101:10010` 就在旁邊）。

    ⛔ 不要用「哪個埠不是遊戲」這種寫死的判斷（猜的，改版就壞）。
    **可以驗證的判準只有一個：複製得到。** 所以一條一條試。

    ⚠ 每一輪對每個候選都只快速試一次，然後整輪重來直到逾時 ——
    不能第一個候選就把 10 秒用完（剛換頻道時每一條都要幾秒才出現，
    見 `SOCKET_WAIT_SEC`），也不能只試一輪就放棄。
    """
    picks = [s for s in servers if s]
    if not picks:
        return None, None
    deadline = time.monotonic() + timeout
    while True:
        for ip, port in picks:
            sock = find_game_socket(pid, ip, port)
            if sock:
                return sock, (ip, port)
            if should_stop is not None and should_stop():
                return None, None
        if time.monotonic() >= deadline:
            log.warning(
                "%.0f 秒內複製不到 PID %s 的遊戲 socket（試過 %s）",
                timeout, pid, "、".join(f"{ip}:{port}" for ip, port in picks),
            )
            return None, None
        time.sleep(_SOCKET_POLL)


#: 同一個 WSA 錯誤碼只吼一次，之後每這麼多次補一行摘要。
#:
#: ⚠ 不節流的後果是實測出來的：連線被伺服器 reset（10054）之後，bot 每一拍
#: 都會再送一次，一小時噴了 **5,185 行** —— 日誌整個被沖掉，真正的原因反而
#: 找不到（使用者實測回報「兩隻都停了，很怪」）。
_SEND_ERROR_EVERY = 500
#: **(socket, 錯誤碼) → 連續失敗次數。**
#:
#: ⚠⚠ 一定要含 socket。舊版只用錯誤碼當鍵，而且「任何一次成功就整個清空」——
#: 多開的時候另外兩隻一直在成功送封包，於是計數每一拍都被歸零，
#: 壞掉的那一隻**每次都印「第 1 次」那句**。節流寫了等於沒寫：
#: 實機 15 秒噴了 40 行一模一樣的 10054（使用者實測回報「看起來好怪」）。
_send_errors: dict[tuple[int, int], int] = {}


def send_on_socket(sock: int, data: bytes) -> int:
    """在指定 socket 上送出 data，回傳送出的位元組數（-1 = 失敗）。"""
    buf = ctypes.create_string_buffer(data, len(data))
    sent = _ws2.send(sock, buf, len(data), 0)
    if sent < 0:
        err = _ws2.WSAGetLastError()
        key = (sock, err)
        count = _send_errors.get(key, 0) + 1
        _send_errors[key] = count
        if count == 1:
            log.error("send 失敗，WSA 錯誤 %s（同一個錯誤之後只會定期摘要）", err)
        elif count % _SEND_ERROR_EVERY == 0:
            log.error("send 失敗，WSA 錯誤 %s —— 已經連續 %d 次", err, count)
    else:
        # 只清**這一條** socket 的計數 —— 別人送得出去不代表我這條好了。
        for key in [k for k in _send_errors if k[0] == sock]:
            del _send_errors[key]
    return sent


def close_socket(sock: int) -> None:
    """放掉我們複製來的那個 handle。

    ⛔⛔ **一定要用 `CloseHandle`，不准改回 `closesocket`。**

    這個 handle 是 `DuplicateHandle` 從遊戲行程複製過來的，它跟遊戲手上那個是
    **同一條連線的兩個 handle**，不是另一條連線（[PKT-012]、[PKT-014]）。

    - `CloseHandle` = 「我這一份不要了」→ 遊戲那一份還在，連線活著。
    - `closesocket` = 「**把這個 socket 關掉**」→ 遊戲的連線跟著收掉。

    使用者實際踩過（2026-09-03 回報）：掛機中把工具關掉，RO 立刻跳
    「與伺服器斷線」。路徑是 `MainWindow.closeEvent` → `page.shutdown()` →
    `GameLink.close()` → 這裡，而那一刻關到的正是**當下活著的那條地圖連線**。

    為什麼換頻道／換地圖的重綁沒暴露這個問題：那時 `_close_socket()` 關掉的是
    **已經作廢的舊連線**（遊戲自己早就丟了），關不關都看不出差別。

    佐證：`find_game_socket()` 每次掃描都把遊戲全部 handle（實測 773 個，
    含 GameGuard 那條 443）複製一遍再 `CloseHandle` 掉，一秒好幾次，
    從來沒弄斷過任何連線 —— 動作一樣，差別只在用哪一支函式關。
    """
    _k32.CloseHandle(wintypes.HANDLE(sock))
    # 順手清掉這條 socket 的送出錯誤計數：handle 值會被系統回收再發給別條連線，
    # 留著的話新連線一開始就頂著舊的計數，節流會提早把錯誤吞掉。
    for key in [k for k in _send_errors if k[0] == sock]:
        del _send_errors[key]
