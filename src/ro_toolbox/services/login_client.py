"""不開遊戲，直接跟官方伺服器要角色清單。

## 為什麼做得到（那道鎖是怎麼開的）

擋了很久的是 `0x0064` 裡那 24 bytes 密碼欄 —— 它是**密文**，我們自己組不出來
（[PKT-046]）。2026-08-26 比對了同一個帳號相隔 4.5 小時、不同行程的兩次登入：

    14:51 那次：28 8a 6e 48 … df 56 a9 7c
    19:2x 那次：28 8a 6e 48 … df 56 a9 7c   ← **完全一樣**

沒有 session salt，它就是密碼的固定轉換。所以**抓一次就能重播**，
不必逆向那個演算法。密文由自動登入時順手從 `0x0064` 抓下來存進帳號檔
（跟密碼一樣用 DPAPI 加密）。

## 流程（每一包都是實機擷取確認過的）

    連 twro-acc.gnjoy.com.tw:6900
      ↑ 0x0064  version + 帳號[24] + 密文[24] + clienttype
      ↓ 0x0A73  要 OTP
      ↑ 0x0A74  六位數字
      ↓ 0x0B60  login_id1 / AID / login_id2 / sex / 伺服器清單
    連角色伺服器（用清單裡的**主機名**，不是 IP —— 見 _SERVER_ENTRY）
      ↑ 0x0065  AID + login_id1 + login_id2 + ? + sex
      ↑ 0x09A1  要下一頁角色（客戶端也是這樣一頁一頁要的）
      ↓ 0x0B72  角色清單

角色清單在**二次密碼之前**就送過來了（實機：`0x0B72` 14:52:13、`0x08B8` 14:52:25），
所以這條路連二次密碼都不需要。

## ⚠ 三件事要先講清楚

1. **這是一次真的登入。** 那個帳號正在玩的話會被踢下線 ——
   所以呼叫端一定要先用 `game_census.account_in_use()` 擋掉。
2. **這是一個「不是 Ragexe」的客戶端在跟官方講話。** GameGuard 看不到
   （我們完全不碰遊戲行程），但伺服器端的行為分析看得到。
   這跟「讀記憶體」「複製遊戲自己的 socket」不同級 —— 那些全程是遊戲自己在連線。
3. **密文等同密碼**，存在加密過的帳號檔裡，不要往別處複製。
"""

from __future__ import annotations

import logging
import socket
import struct
import time
from collections.abc import Callable

from ro_toolbox.core.ro_packet import split_stream
from ro_toolbox.services import packet_table
from ro_toolbox.services.accounts import Account, KnownCharacter
from ro_toolbox.services.totp import generate as generate_otp

log = logging.getLogger(__name__)

#: 登入伺服器。出處：客戶端的 `clientinfo.xml`（GAMEDATA [PKT-038]），
#: 實機擷取到的位址是 219.84.200.54:6900 —— 用主機名而不是 IP，換機房才不會壞。
LOGIN_HOST = "twro-acc.gnjoy.com.tw"
LOGIN_PORT = 6900

OP_LOGIN = 0x0064
OP_OTP_REQUIRED = 0x0A73
OP_OTP = 0x0A74
OP_LOGIN_ACCEPTED = 0x0B60
OP_ENTER_CHAR_SERVER = 0x0065
OP_CHAR_LIST_PAGE = 0x09A1
#: 伺服器先說「角色清單有幾頁」（payload = 頁數，4 bytes）。實機 #25：`06 00 00 00`。
OP_CHAR_LIST_PAGES = 0x09A0
OP_CHAR_LIST = 0x0B72
OP_REFUSE_LOGIN = 0x006A

#: `0x0064` 的固定欄位。實機擷取：version = 01 00 00 00、clienttype = 0x05。
_VERSION = 1
_CLIENT_TYPE = 5

#: `0x0B60` 的版面（實機 #19，392 bytes）：
#:
#:     長度(2) login_id1(4) AID(4) login_id2(4) …(28) sex(1)@0x2C token[17] 伺服器清單@0x3E
_ACCEPT_LOGIN_ID1 = 2
_ACCEPT_AID = 6
_ACCEPT_LOGIN_ID2 = 10
_ACCEPT_SEX = 0x2C
_ACCEPT_SERVERS = 0x3E

#: 伺服器清單每一筆 164 bytes：
#:
#:     IP(4) port(2, LE) 名稱[20 cp950] …(6) 主機名字串
#:
#: ⚠ **IP 欄位不能用**：實機讀到 192.168.204.82（伺服器的內網位址）。
#: 要連的是主機名字串裡的 `twro-char2.gnjoy.com.tw:10029`，靠 DNS 解析。
_SERVER_ENTRY = 164
_ENTRY_PORT = 4
_ENTRY_NAME = 6
_ENTRY_NAME_LEN = 20
_ENTRY_HOST = 32

#: 每一步等回應的上限。都是「放棄的上限」，不是「成功的依據」。
_CONNECT_TIMEOUT = 10.0
_REPLY_TIMEOUT = 15.0
#: 頁數解不出來時最多要幾頁。實機的客戶端送了 6 次 `0x09A1`。
_LIST_PAGES = 8
#: 要一頁之後等多久沒新東西就當作要完了。
_PAGE_QUIET = 1.2
#: 換一台伺服器要重新登入，中間停這麼久 —— 不要讓伺服器覺得我們在連發。
_RELOGIN_GAP = 3.0
#: 斷線時最多花這麼久把對方的收尾資料讀完（見 `_Stream.close`）。
_CLOSE_DRAIN = 1.0


class LoginClientError(RuntimeError):
    """跟伺服器要不到東西。訊息是要直接給使用者看的。"""


class _Stream:
    """一條連線 + 切包。切包一定要有長度表，沒有就不要猜。"""

    def __init__(self, host: str, port: int, lengths: dict) -> None:
        self._lengths = lengths
        self._buffer = b""
        self._packets: list[tuple[int, bytes]] = []
        try:
            self._sock = socket.create_connection((host, port), _CONNECT_TIMEOUT)
        except OSError as exc:
            raise LoginClientError(f"連不上 {host}:{port}：{exc}") from exc
        self._sock.settimeout(0.5)

    def send(self, opcode: int, payload: bytes = b"") -> None:
        data = struct.pack("<H", opcode) + payload
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise LoginClientError(f"送不出封包 {opcode:#06x}：{exc}") from exc

    def _refused(self, payload: bytes) -> str:
        """把伺服器的拒絕講成人話。"""
        reason = payload[0] if payload else None
        if reason == 1:
            return "帳號或密碼不對"
        return f"原因碼 {reason}"

    def wait(self, opcode: int, timeout: float) -> bytes:
        """等某個 opcode，回它的 payload。逾時丟例外（**不回空的假裝成功**）。"""
        deadline = time.monotonic() + timeout
        while True:
            for op, payload in self._drain():
                if op == opcode:
                    return payload
                if op == OP_REFUSE_LOGIN:
                    # ⚠ 這裡最常見的原因是**密碼改過了**：存下來的密文是舊密碼
                    # 轉出來的，改密碼之後它就對不上。要講出來讓人知道怎麼修，
                    # 不能只丟一句「拒絕登入」（那會讓人以為工具壞了）。
                    raise LoginClientError(
                        f"伺服器拒絕登入（{self._refused(payload)}）。\n"
                        "如果你改過遊戲密碼，存下來的登入密文就過期了 —— "
                        "用「登入」跑一次自動登入，工具會自動換成新的。"
                    )
            if time.monotonic() >= deadline:
                seen = ", ".join(f"{op:#06x}" for op, _ in self._packets[-8:])
                raise LoginClientError(
                    f"等 {opcode:#06x} 等了 {timeout:.0f} 秒沒等到"
                    f"（這段期間收到：{seen or '什麼都沒有'}）"
                )

    def collect(self, opcode: int, quiet: float) -> list[bytes]:
        """把已經收到的某個 opcode 全部拿出來（不等）。"""
        deadline = time.monotonic() + quiet
        found = []
        while time.monotonic() < deadline:
            for op, payload in self._drain():
                if op == opcode:
                    found.append(payload)
                    deadline = time.monotonic() + quiet
        return found

    def _drain(self) -> list[tuple[int, bytes]]:
        try:
            chunk = self._sock.recv(65536)
        except TimeoutError:
            return []
        except OSError as exc:
            raise LoginClientError(f"連線斷了：{exc}") from exc
        if not chunk:
            raise LoginClientError("伺服器把連線關掉了")
        self._buffer += chunk
        raw, self._buffer = split_stream(self._buffer, self._lengths)
        # ⚠ `split_stream` 回傳的是**含 opcode 的整包**；專案裡 `RoPacket.payload`
        # 的約定是**不含**那 2 bytes（`char_list.parse` 等等都照這個約定）。
        # 少剝這 2 bytes 的話所有欄位偏移都差 2 —— 伺服器名稱會變成 "-'查爾斯"（踩過）。
        packets = [(op, body[2:]) for op, body in raw]
        self._packets.extend(packets)
        return packets

    def close(self) -> None:
        """**好好地**斷線：先送 FIN，把伺服器還沒送完的東西讀完，再關。

        直接 `close()` 而緩衝區裡還有沒讀完的資料時，Windows 會送 RST ——
        伺服器看到的是「異常斷線」，那正是會讓角色卡在線上的那種收場。
        送 FIN 再讀到 EOF 是正常的收尾，伺服器知道我們是主動走的。
        """
        try:
            self._sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass                      # 對面已經先關了，那也算正常收尾
        deadline = time.monotonic() + _CLOSE_DRAIN
        try:
            self._sock.settimeout(0.2)
            while time.monotonic() < deadline:
                if not self._sock.recv(65536):
                    break             # 讀到 EOF＝雙方都關好了
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def _lengths() -> dict[int, tuple[int, int]]:
    table = packet_table.load_any_cached()
    if not table:
        raise LoginClientError(
            "沒有封包長度表，沒辦法正確切包。\n"
            "先用遊戲正常登入一次（自動登入也可以），"
            "工具會把長度表從客戶端抽出來存好，之後就不用再開遊戲了。"
        )
    return {op: (info.length, info.header) for op, info in table.items()}


def _login_packet(account: Account) -> bytes:
    blob = bytes.fromhex(account.password_blob)
    if len(blob) != 24:
        raise LoginClientError(
            f"帳號「{account.name}」的登入密文長度不對（{len(blob)} bytes，應為 24）。"
        )
    name = account.username.encode("ascii", "ignore")[:23]
    return (
        struct.pack("<I", _VERSION)
        + name.ljust(24, b"\x00")
        + blob
        + bytes([_CLIENT_TYPE])
    )


def _parse_servers(payload: bytes) -> list[tuple[str, str, int]]:
    """回 [(名稱, 主機名, 埠)]。解不出來的條目跳過，不要硬湊。"""
    out = []
    offset = _ACCEPT_SERVERS
    while offset + _SERVER_ENTRY <= len(payload):
        entry = payload[offset:offset + _SERVER_ENTRY]
        offset += _SERVER_ENTRY
        port = struct.unpack_from("<H", entry, _ENTRY_PORT)[0]
        raw = entry[_ENTRY_NAME:_ENTRY_NAME + _ENTRY_NAME_LEN].split(b"\x00")[0]
        name = raw.decode("cp950", errors="replace")
        host = entry[_ENTRY_HOST:].split(b"\x00")[0].decode("ascii", errors="replace")
        if ":" in host:
            host, _, tail = host.partition(":")
            if tail.isdigit():
                port = int(tail)
        if name and host and port:
            out.append((name, host, port))
    return out


def _login(account: Account, lengths: dict, step) -> tuple[int, int, int, int, list]:
    """跟登入伺服器換一張通行證。回 (AID, login_id1, login_id2, sex, 伺服器清單)。"""
    login = _Stream(LOGIN_HOST, LOGIN_PORT, lengths)
    try:
        step(f"連上登入伺服器（{LOGIN_HOST}:{LOGIN_PORT}）")
        login.send(OP_LOGIN, _login_packet(account))
        login.wait(OP_OTP_REQUIRED, _REPLY_TIMEOUT)
        step("帳密收下了，送 OTP")
        login.send(OP_OTP, generate_otp(account.secret).encode("ascii"))
        accepted = login.wait(OP_LOGIN_ACCEPTED, _REPLY_TIMEOUT)
    finally:
        login.close()

    servers = _parse_servers(accepted)
    if not servers:
        raise LoginClientError("登入成功但解不出伺服器清單（版面可能改了）")
    step("登入成功，伺服器清單：" + "、".join(name for name, _h, _p in servers))
    return (
        struct.unpack_from("<I", accepted, _ACCEPT_AID)[0],
        struct.unpack_from("<I", accepted, _ACCEPT_LOGIN_ID1)[0],
        struct.unpack_from("<I", accepted, _ACCEPT_LOGIN_ID2)[0],
        accepted[_ACCEPT_SEX],
        servers,
    )


def _characters_from(
    server: tuple[str, str, int], ticket: tuple[int, int, int, int],
    lengths: dict, step,
) -> list[KnownCharacter]:
    """連上一台角色伺服器，把它的角色清單要回來。"""
    from ro_toolbox.services import char_list

    name, host, port = server
    aid, login_id1, login_id2, sex = ticket
    char = _Stream(host, port, lengths)
    try:
        step(f"連上「{name}」（{host}:{port}）")
        char.send(
            OP_ENTER_CHAR_SERVER,
            struct.pack("<IIIHB", aid, login_id1, login_id2, 0, sex),
        )
        # ⚠ **順序：先要頁，伺服器才給。** 實機是
        #   ↓ 0x09A0（有幾頁）→ ↑ 0x09A1（要一頁）→ ↓ 0x0B72（那一頁）
        # 一開始就等 0x0B72 的話會等到天荒地老（踩過：等 15 秒只收到
        # 0x510B / 0x082D / 0x09A0）。
        try:
            count = struct.unpack_from("<I", char.wait(OP_CHAR_LIST_PAGES, 5.0))[0]
        except (LoginClientError, struct.error):
            count = _LIST_PAGES
        pages = []
        for _ in range(max(1, min(int(count), _LIST_PAGES))):
            char.send(OP_CHAR_LIST_PAGE)
            more = char.collect(OP_CHAR_LIST, _PAGE_QUIET)
            if not more:
                break            # 要不到新的就是要完了
            pages.extend(more)
    finally:
        char.close()

    merged: dict[int, KnownCharacter] = {}
    for payload in pages:
        try:
            for entry in char_list.parse(payload):
                merged[entry.slot] = KnownCharacter(
                    name=entry.name, slot=entry.slot, server=name
                )
        except Exception as exc:  # noqa: BLE001 - 一頁壞掉不該讓整批失敗
            log.debug("解析角色清單失敗：%s", exc)
    characters = [merged[slot] for slot in sorted(merged)]
    step(f"「{name}」："
         + ("、".join(f"{c.slot} {c.name}" for c in characters) or "沒有角色"))
    return characters


def fetch_characters(
    account: Account,
    server_name: str = "",
    on_step: Callable[[str], None] | None = None,
) -> tuple[str, list[KnownCharacter]]:
    """跟伺服器要**某一台**的角色清單，完全不開遊戲。回 (伺服器名稱, 角色清單)。"""
    def step(text: str) -> None:
        log.info("%s", text)
        if on_step is not None:
            on_step(text)

    lengths = _lengths()
    aid, id1, id2, sex, servers = _login(account, lengths, step)
    wanted = (server_name or account.server or "").strip()
    picked = next((s for s in servers if s[0] == wanted), servers[0])
    if wanted and picked[0] != wanted:
        step(f"清單裡沒有「{wanted}」，改用「{picked[0]}」")
    return picked[0], _characters_from(picked, (aid, id1, id2, sex), lengths, step)


def fetch_all(
    account: Account,
    on_step: Callable[[str], None] | None = None,
) -> dict[str, list[KnownCharacter]]:
    """把**每一台**伺服器的角色清單都抓回來。回 {伺服器名稱: 角色清單}。

    ⚠ **每一台都要重新登入一次。** 一次登入換到的通行證只能用在一台角色伺服器 ——
    實測：第一台（查爾斯）拿得到，接著用同一張票連第二台就被伺服器直接斷線。
    真人客戶端本來也只會挑一台，所以這是預期的行為，不是我們寫錯。

    也因此**這支會登入 N 次**。平常只要更新一台的話請用 `fetch_characters`，
    那個只登入一次。

    ⚠ 呼叫端要先確認這個帳號**沒有在線上**（`game_census.account_in_use`）——
    每一次都是真的登入，會把正在玩的那個踢下線。
    """
    def step(text: str) -> None:
        log.info("%s", text)
        if on_step is not None:
            on_step(text)

    lengths = _lengths()
    aid, id1, id2, sex, servers = _login(account, lengths, step)
    out: dict[str, list[KnownCharacter]] = {}
    ticket = (aid, id1, id2, sex)
    for index, server in enumerate(servers):
        if index:
            # 換一台就要換一張票。中間停一下，別讓伺服器覺得我們在連發。
            step(f"要換到「{server[0]}」，重新登入一次")
            time.sleep(_RELOGIN_GAP)
            aid, id1, id2, sex, _again = _login(account, lengths, step)
            ticket = (aid, id1, id2, sex)
        try:
            out[server[0]] = _characters_from(server, ticket, lengths, step)
        except LoginClientError as exc:
            step(f"「{server[0]}」要不到：{exc}")
    if not out:
        raise LoginClientError("每一台都要不到角色清單")
    return out
