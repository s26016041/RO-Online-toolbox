"""複製遊戲的 TCP socket，在它那條連線上送封包。

為什麼可行：GameGuard 剝掉的是記憶體寫入權限，**DUP_HANDLE 沒被剝**
（見 GAMEDATA [PKT-011]、[PKT-012]）。所以可以：
  1. 列舉遊戲行程的所有 handle
  2. 把 handle 複製到本行程
  3. **問核心**（AFD）這個 handle 綁在本機哪個埠，對照 TCP 表認出遊戲那條連線
  4. 在這個 handle 上用 `WriteFile` 送明文封包（也是走核心）

全程只做網路操作，不寫遊戲記憶體、不注入。

## ⛔⛔ 這一支**完全不碰 ws2_32**（`getpeername`／`send`／`closesocket` 一律不准）

實機 2026-09-05 量出來的（[PKT-097]）：ws2_32 對「不是它自己建立的 handle」
會在**第一次**看到那個 handle 值時把身分快取起來，**之後永遠照快取回答**，
不管那個值後來被 `CloseHandle` 掉、又被系統回收發給別的物件：

    複本 A（連到 :57967）              getpeername → :57967   send → OK
    CloseHandle(A)                     getpeername → :57967   send → **10038**
    複製 B（連到 :57968），拿到同一個值  getpeername → **:57967**（錯的）

而 handle 值是**立刻回收**的（LIFO），所以 `find_game_socket()` 每一輪
「複製 → 問 → 關掉」拿到的都是**同一個值**，整輪掃描只有第一個 socket 的身分
是真的，其餘全部照第一個回答 —— 這就是：

- 「剛連上的那幾秒複製不到 socket」（[PKT-072]，其實是快取把答案蓋掉了）
- 「換地圖伺服器後複本變成不是 socket、send 回 10038」（[PKT-096] 的解讀
  是**錯的**：綁到的是一個被快取誤認成遊戲 socket 的**別的 handle**，
  而 `socket_alive()` 問的還是快取，所以一直說「活著」，15 秒後判定斷線）
- 「WSA 10022／10045／10057 綁錯對象」那些從來沒解釋清楚的錯誤

核心不會撒謊：`NtDeviceIoControlFile(AFD_GET_SOCK_NAME)` 回的是**這個 handle
現在指到的物件**的本機位址，被關掉就是 `STATUS_INVALID_HANDLE`；
`WriteFile` 也一樣（被關掉回 `ERROR_INVALID_HANDLE`，對方 reset 回 64）。
所以身分認定、活著沒、送封包，三件事都改走核心。

⚠ 這是在遊戲自己的連線上送封包。與遊戲同時 send 可能造成 TCP 位元組交錯，
   但 RO 送封包不頻繁、一次寫完整封包，實務上可行。
"""

from __future__ import annotations

import ctypes
import logging
import socket
import time
from ctypes import wintypes

from ro_toolbox.services.process_monitor import connections_of

log = logging.getLogger(__name__)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.windll.ntdll
# ⛔ 這裡**故意沒有 ws2_32**。它對複製來的 handle 會照第一次的快取回答
#    （見模組說明），而且 `closesocket` 會把遊戲的連線一起關掉（[PKT-094]）。

_PROCESS_DUP_HANDLE = 0x0040
_DUPLICATE_SAME_ACCESS = 0x0002
_SYSTEM_EXTENDED_HANDLE_INFORMATION = 0x40
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_STATUS_PENDING = 0x00000103
#: `NtDeviceIoControlFile` 給 AFD 的「這個 socket 綁在本機哪裡」。
#: 實測（2026-09-05，Windows 11 26200）：回 `sockaddr_in`（family 2、埠是網路序）；
#: handle 被關掉回 `0xC0000008`（STATUS_INVALID_HANDLE）、檔案 handle 回
#: `0xC000000D`（STATUS_INVALID_PARAMETER）。
_IOCTL_AFD_GET_SOCK_NAME = 0x1202F
_ERROR_IO_PENDING = 997

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
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, ctypes.c_void_p]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_k32.WriteFile.restype = wintypes.BOOL
_k32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
]
_k32.GetOverlappedResult.restype = wintypes.BOOL
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_size_t), ("Information", ctypes.c_size_t)]


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


_ntdll.NtDeviceIoControlFile.argtypes = [
    wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(_IoStatusBlock), wintypes.ULONG,
    ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
]
_ntdll.NtDeviceIoControlFile.restype = ctypes.c_uint32


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


def _local_port_of(handle: int | None) -> int | None:
    """**問核心**：這個 handle 現在是一個 AF_INET socket 嗎？綁在本機哪個埠？

    不是 socket、handle 無效（被關掉了）、還沒綁定 → 一律回 None。
    唯讀、微秒級，而且**不經過 ws2_32 的快取**（見模組說明）。
    """
    if not handle:
        return None
    event = _k32.CreateEventW(None, True, False, None)
    if not event:
        return None
    try:
        iosb = _IoStatusBlock()
        out = ctypes.create_string_buffer(64)
        status = _ntdll.NtDeviceIoControlFile(
            handle, event, None, None, ctypes.byref(iosb),
            _IOCTL_AFD_GET_SOCK_NAME, None, 0, out, len(out),
        ) & 0xFFFFFFFF
        if status == _STATUS_PENDING:
            _k32.WaitForSingleObject(event, 0xFFFFFFFF)
            status = iosb.Status & 0xFFFFFFFF
        if status != 0 or iosb.Information < 4:
            return None
    finally:
        _k32.CloseHandle(event)
    family = int.from_bytes(out.raw[0:2], "little")
    if family != socket.AF_INET:
        return None
    port = int.from_bytes(out.raw[2:4], "big")
    return port or None


def socket_alive(sock: int | None) -> bool:
    """我們複製來的這份 handle **現在還是一個活著的 socket** 嗎？

    問的是核心，不是 ws2_32 的快取（[PKT-097]）：被 `CloseHandle` 掉的、
    根本不是 socket 的，這裡都會老實說不是。

    ⚠ 這一句**不管**「連線被對方 reset」也不管「遊戲已經換了一條新的」：
    只要我們還握著複本，核心物件就還在、還連著舊的伺服器。
    前者由 `GameLink.dead` 負責，後者靠比對本機埠（`socket_local_port()`）。
    """
    return _local_port_of(sock) is not None


def socket_local_port(sock: int | None) -> int | None:
    """這份複本連線的**本機埠**。★ 這才是一條連線的身分（[PKT-097]）：

    重連到同一台伺服器時 (ip, port) 一模一樣，只有本機埠會變。
    拿它跟 TCP 表裡**最新那條**比，就知道遊戲是不是已經換了一條連線。
    """
    return _local_port_of(sock)


def _newest_local_port(pid: int, server_ip: str, server_port: int) -> int | None:
    """TCP 表裡這個行程連到該伺服器的**最新一條**連線，本機埠是多少。

    ⚠ 只取最新的一條：舊連線在我們握著複本的期間不會消失（[PKT-063] 那個
    「留 11 分鐘」就是這樣來的），兩條都 ESTABLISHED、端點一模一樣，
    只有建立時間與本機埠分得出來。
    """
    rows = [
        c for c in connections_of(pid)          # 已經由新到舊排好
        if c.endpoint == (server_ip, server_port) and c.local_port
    ]
    if not rows:
        return None
    usable = [c for c in rows if c.established] or rows
    return usable[0].local_port


def _dup_from(source: int, value: int) -> int | None:
    """把別的行程的 handle 複製一份到本行程。失敗（已關掉、沒權限）回 None。"""
    dup = wintypes.HANDLE()
    ok = _k32.DuplicateHandle(
        source, wintypes.HANDLE(value), _k32.GetCurrentProcess(), ctypes.byref(dup),
        0, False, _DUPLICATE_SAME_ACCESS,
    )
    return int(dup.value) if ok and dup.value else None


def find_game_socket(pid: int, server_ip: str, server_port: int) -> int | None:
    """找出並複製遊戲連到伺服器的 socket，回傳本行程可用的 handle。

    做法：先從 TCP 表查「這個行程連到那台伺服器**最新那條**連線的本機埠」，
    再把行程的 handle 一個一個複製過來**問核心**綁在哪個埠，對上就是它。
    回傳的 handle 用完要 `close_socket()`。找不到回 None。
    """
    wanted = _newest_local_port(pid, server_ip, server_port)
    if wanted is None:
        log.debug("TCP 表裡 PID %s 沒有連到 %s:%s 的連線", pid, server_ip, server_port)
        return None

    source = _k32.OpenProcess(_PROCESS_DUP_HANDLE, False, pid)
    if not source:
        log.error("OpenProcess(DUP_HANDLE) 失敗，PID %s", pid)
        return None

    try:
        for value in _enum_handles(pid):
            dup = _dup_from(source, value)
            if dup is None:
                continue
            if _local_port_of(dup) == wanted:
                log.debug("找到遊戲 socket：handle %#x 本機埠 %d → %s:%s",
                          dup, wanted, server_ip, server_port)
                return dup
            _k32.CloseHandle(wintypes.HANDLE(dup))
        # ⚠ 這裡**只記 DEBUG**。呼叫端幾乎都是「重試到成功或逾時」的迴圈
        # （剛換到角色伺服器的那幾秒複製不到，過一下就好），
        # 每次沒找到就 WARNING 的話，短短兩秒就是上百行洗版 ——
        # 使用者實際回報過。真的放棄時由呼叫端說一次就好。
        log.debug("在 PID %s 裡找不到本機埠 %d（→ %s:%s）的 socket",
                  pid, wanted, server_ip, server_port)
        return None
    finally:
        _k32.CloseHandle(source)


#: 複製不到 socket 時要重試多久（開機／換頻道那幾秒）。
#:
#: 剛連上伺服器的那幾秒 TCP 表裡可能還沒有 ESTABLISHED 的那條，或遊戲的
#: handle 還沒建好；過一會兒再找就 0.1 秒找到。
#: ⚠ 以前的「列舉得到 773 個 handle、複製成功 552 個，但裡面只有 GameGuard
#:   那條」其實是 ws2_32 快取把答案蓋掉了（[PKT-097]），現在改問核心，
#:   那種「明明在卻找不到」不會再發生；留著重試是為了真正的過渡期。
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


#: 同一個錯誤碼只吼一次，之後每這麼多次補一行摘要。
#:
#: ⚠ 不節流的後果是實測出來的：連線被伺服器 reset 之後，bot 每一拍
#: 都會再送一次，一小時噴了 **5,185 行** —— 日誌整個被沖掉，真正的原因反而
#: 找不到（使用者實測回報「兩隻都停了，很怪」）。
_SEND_ERROR_EVERY = 500
#: **(socket, 錯誤碼) → 連續失敗次數。**
#:
#: ⚠⚠ 一定要含 socket。舊版只用錯誤碼當鍵，而且「任何一次成功就整個清空」——
#: 多開的時候另外兩隻一直在成功送封包，於是計數每一拍都被歸零，
#: 壞掉的那一隻**每次都印「第 1 次」那句**。節流寫了等於沒寫：
#: 實機 15 秒噴了 40 行一模一樣的錯誤（使用者實測回報「看起來好怪」）。
_send_errors: dict[tuple[int, int], int] = {}


#: Win32 錯誤碼 → 給人看的一句話。**不猜，只寫量過的那幾個**（2026-09-05）。
_ERROR_MEANING = {
    6: "這個 handle 在我們這個行程裡已經不是有效的 socket"
       "（被關掉了、或複製到的根本不是 socket）—— 這是程式自己的問題，不是連線",
    64: "連線已經被對方關掉／reset（ERROR_NETNAME_DELETED）",
    5: "拒絕存取 —— 複製到的不是可寫的 socket",
}


def _write(sock: int, data: bytes) -> tuple[int, int]:
    """走核心把 `data` 寫進 socket。回 `(送出的位元組數, Win32 錯誤碼)`；失敗時前者是 -1。

    用 `WriteFile` 而不是 ws2_32 的 `send`：AFD 對 socket handle 本來就接受
    `IRP_MJ_WRITE`（實測 2026-09-05 對方收到的位元組一模一樣），而且回報的是
    **這個 handle 現在的真相**，不會被 ws2_32 的快取蓋掉（[PKT-097]）。
    遊戲的 socket 是 overlapped 的，所以一律帶 OVERLAPPED ＋ 事件，等它完成。
    """
    event = _k32.CreateEventW(None, True, False, None)
    if not event:
        return -1, ctypes.get_last_error()
    try:
        overlapped = _Overlapped()
        overlapped.hEvent = event
        buf = ctypes.create_string_buffer(data, len(data))
        sent = wintypes.DWORD(0)
        ok = _k32.WriteFile(sock, buf, len(data), ctypes.byref(sent), ctypes.byref(overlapped))
        err = 0 if ok else ctypes.get_last_error()
        if not ok and err == _ERROR_IO_PENDING:
            ok = _k32.GetOverlappedResult(sock, ctypes.byref(overlapped), ctypes.byref(sent), True)
            err = 0 if ok else ctypes.get_last_error()
        return (int(sent.value), 0) if ok else (-1, err)
    finally:
        _k32.CloseHandle(event)


def send_on_socket(sock: int | None, data: bytes) -> int:
    """在指定 socket 上送出 data，回傳送出的位元組數（-1 = 失敗）。

    ⚠⚠ **失敗訊息一定要帶 handle 值。** 2026-09-04 追這個問題時，
    日誌只有「send 失敗，WSA 錯誤 10038」—— 完全分不出是
    「遊戲把那條關掉了」還是「我們自己傳了一個沒有的 handle 進來」，
    最後只能靠實機重現才確定。錯誤訊息**必須夠診斷**。
    """
    if not sock:
        # ⛔ 呼叫端在 `sock` 被清成 None 之後還來送 —— 這是我們自己的 bug，
        #    跟連線無關，要分開講，不然又要靠猜的。
        log.error("要送封包但 socket 是空的（%r）—— 這是呼叫端的錯，不是連線問題",
                  sock)
        return -1
    sent, err = _write(sock, data)
    if sent < 0:
        key = (sock, err)
        count = _send_errors.get(key, 0) + 1
        _send_errors[key] = count
        why = _ERROR_MEANING.get(err, "沒見過的錯誤")
        if count == 1:
            log.error("send 失敗（socket %#x）：Win32 錯誤 %s —— %s"
                      "（同一個錯誤之後只會定期摘要）", sock, err, why)
        elif count % _SEND_ERROR_EVERY == 0:
            log.error("send 失敗（socket %#x）：Win32 錯誤 %s —— %s，已經連續 %d 次",
                      sock, err, why, count)
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

    ⚠ handle 值關掉之後**立刻**會被系統回收發給下一個 `DuplicateHandle`
    （實測同一個值連續拿到三次）。所以一個值只准關一次：關兩次等於把別人
    剛拿到的那一份關掉，症狀是對方 `send` 回 `ERROR_INVALID_HANDLE`。
    呼叫端要先把自己的欄位清成 None 再關（見 `GameLink._close_socket`）。
    """
    _k32.CloseHandle(wintypes.HANDLE(sock))
    # 順手清掉這條 socket 的送出錯誤計數：handle 值會被系統回收再發給別條連線，
    # 留著的話新連線一開始就頂著舊的計數，節流會提早把錯誤吞掉。
    for key in [k for k in _send_errors if k[0] == sock]:
        del _send_errors[key]
