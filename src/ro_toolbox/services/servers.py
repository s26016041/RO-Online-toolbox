"""伺服器對照表：名稱 ↔ 代號。

## 為什麼這張表寫死在程式裡

三個來源依序都走過了（順序見 CLAUDE.md「資料來源優先序」）：

1. **記憶體**：還沒做。客戶端在選伺服器畫面時當然握著這份清單，
   但要 AOB 定位那個控制項，成本高於這張表的價值。
2. **客戶端解包資料**：**確認不在裡面。** 用三種編碼（cp950／UTF-8／UTF-16LE）
   掃過 `RODATA/` 全部 245,178 個檔案，「波利」零命中；
   對照組（搜 `Taiwan Main`）命中 `clientinfo.xml`，證明搜尋方法有效。
   客戶端只有登入伺服器本身（`twro-acc.gnjoy.com.tw:6900`），沒有分流清單。
3. **伺服器推的封包 `0x0069`**：**這才是唯一來源。**

清單本身極少變動（新增伺服器是大改版等級的事），所以照使用者決定：
**抄成程式裡的表**，改版時重跑一次擷取更新。這跟結構偏移屬於同一類 ——
「大更新才會壞」，而且必須留出處。

## 封包版面（2026-08-25 實機擷取，登入之後伺服器推的那一包）

每筆 **164 bytes**：

    IP(4)  port(2, little-endian)  名稱[20]（**cp950**）  6 bytes  主機名字串

那 6 bytes 兩筆分別是 `AC 06 00 00 27 12` 與 `01 08 00 00 27 12` ——
前 2 bytes 疑似人數（1708 / 2049），後面 `27 12` 兩筆相同。**還沒確認，別當事實用。**

⚠ **這一包的 opcode 還沒確定。** 擷取時切包偏移了 2 bytes，opcode 被誤讀成
`0xA8C0`（實際上那 2 bytes 是第一筆的 IP 開頭 `C0 A8`）。名稱與主機名對得起來，
所以**內容**可信；**opcode 不可信**，要修好切包再確認一次。

## 更新方式

改版時重跑一次擷取，從那一包重抽。**不准手打、不准從編號規律推。**
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Server:
    """一個分流伺服器。

    `code` 是送給遊戲用的識別值，`name` 是玩家看到的名字（例如「波利」）。
    設定裡存的是 **name**（身分），`code` 每次現查 —— 不存會挪動的東西。
    """

    name: str
    code: int             # 在伺服器清單裡的位置（0 起算）
    host: str = ""        # 伺服器自己報的主機名，例如 twro-char3.gnjoy.com.tw:10029
    source: str = ""      # 哪一次擷取抄來的


#: 已知的伺服器，順序就是伺服器推過來的順序（`code` 是 0 起算的索引）。
#: 2026-08-25 實機擷取，登入之後那一包 164-byte 條目解出來的。
KNOWN: tuple[Server, ...] = (
    Server("查爾斯", 0, "twro-char2.gnjoy.com.tw:10029", "2026-08-25 擷取"),
    Server("波利", 1, "twro-char3.gnjoy.com.tw:10029", "2026-08-25 擷取"),
)


def known() -> bool:
    """有沒有可用的伺服器表。False 時 UI 要退回自由輸入並說明原因。"""
    return bool(KNOWN)


def names() -> list[str]:
    return [s.name for s in KNOWN]


def code_of(name: str) -> int | None:
    """名稱 → 代號。查不到回 None —— 呼叫端要**拒絕動作**，不准拿預設值送出去。"""
    for server in KNOWN:
        if server.name == name:
            return server.code
    return None


UNKNOWN_HINT = (
    "還沒有伺服器清單（要從伺服器推的 0x0069 封包抄出來，目前還沒擷取到）。"
    "先自己打名稱，例如「波利」。"
)


def name_for_ip(ip: str) -> str | None:
    """這個 IP 是哪一台伺服器。認不出來回 None。

    ⚠ **登入之後一定要確認自己在哪一台。** 每台的角色是各自獨立的，
    同一個格號在兩台是不同的角色（實測：兩份擷取裡都選格號 3，卻是兩隻不同的人）。
    認錯台就會安靜地選錯角色。

    位址是現查 DNS 的 —— 官方換 IP 時自動跟上，不寫死。
    """
    import socket

    for server in KNOWN:
        host = server.host.split(":")[0]
        try:
            resolved = {info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)}
        except OSError:
            continue
        if ip in resolved:
            return server.name
    return None
