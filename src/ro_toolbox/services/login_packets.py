"""登入之後那幾步：用封包送，不用打字。

## 為什麼這一段可以走封包

帳號密碼那一包 `0x0064` 的密碼欄是**密文**，我們造不出來，所以非打字不可。
但過了登入之後就不一樣了 —— 二次密碼、選伺服器、選角**全是明文**
（[PKT-046]），而 RO 的 DUP_HANDLE 沒有被剝，可以複製遊戲自己的 socket 送出去
（[PKT-012]、[PKT-014]），全程不碰記憶體、不搶前景、不受輸入法影響。

## 封包版面（[PKT-046]，實機擷取）

    0x08B8  二次密碼 10 bytes：opcode(2) + AID(4, little-endian) + 四位數字 ASCII(4)
    0x0066  選角      3 bytes：opcode(2) + 角色格號(1)

## AID 從哪來

登入成功時伺服器回的 `0x0B60`，payload 第 6 個位元組起的 4 bytes（little-endian）。
**現場從封包解，不寫死** —— 換帳號就是另一組。
解出來的值跟 [MEM-017] 那筆實測使用道具封包裡的 AID 對得起來（交叉驗證通過）。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: 登入成功回應。payload：長度(2) + login_id1(4) + AID(4) + …
OP_LOGIN_ACCEPTED = 0x0B60

#: AID 在 `0x0B60` payload 裡的位置。
_AID_OFFSET = 6

OP_PIN = 0x08B8
OP_PIN_STATE = 0x08B9
OP_SELECT_CHARACTER = 0x0066

#: 選角之後伺服器給的地圖伺服器位址（角色編號 + 地圖名 + IP + port）。
#: 收到它＝選角成功，客戶端接著會自己去連地圖台並送 `0x0436`。
OP_ZONE_SERVER = 0x0AC5
#: 選角被拒絕（payload 第一個 byte 是原因碼）。
OP_REFUSE_ENTER = 0x006C

#: `0x08B9` 的狀態碼。**只有這兩個是實機對照過的**（2026-08-26 真人登入擷取）：
#:
#:     要求輸入： seed(4) + AID(4) + 01 00   ← state 1
#:     輸入正確： 全部 0                      ← state 0
#:
#: 其他數字（rAthena 原始碼裡還有 NOTSET/EXPIRED/NEW/WRONG 等）**沒實測過**，
#: 一律當成「不是 OK」處理，並把原始數字報出來 —— 不要猜它是什麼意思。
PIN_STATE_OK = 0
PIN_STATE_ASK = 1
#: 「這個帳號沒在用二次密碼，選角畫面直接給你按」。
#:
#: 2026-08-31 實機遇到（帳號 87103030，使用者確認**沒有設二次密碼**）：
#: 伺服器回 state=7，而工具只認得 0 與 1，於是停在選角畫面不敢動 ——
#: 帳號其實好好的。
#: 對照 rAthena 的 `0x08B9` 狀態表：7 ＝「選角畫面顯示按鈕，客戶端送 0x08C5」，
#: 也就是**不需要二次密碼**。跟實機看到的情況一致（那個帳號沒設、角色清單也到了）。
#: ⚠ 判斷錯的代價很小：照樣送選角，而選角本來就有兩道確認
#: （客戶端有沒有寫下角色名字、伺服器有沒有給地圖台位址）。
PIN_STATE_NOT_USED = 7

#: 二次密碼亂序表的兩個常數（Hercules／rAthena 的預設值，台服實測就是這組）。
#:
#: **怎麼驗出來的**：抓兩組 (seed, 送出值) 配對 —— 使用者每次都按 `8291`：
#:
#:     seed 0x05760EA1 → 送出 "5367"
#:     seed 0x05796F02 → 送出 "8623"
#:
#: 拿這兩組去反推，只有這組常數＋「送位置」的方向能同時符合（在 baseSeed
#: 附近 ±0x800 全搜也只有這一組解）。
_PIN_MULTIPLIER = 0x3498
_PIN_BASE_SEED = 0x881234


class LoginPacketError(ValueError):
    """組不出封包。訊息要直接給使用者看。"""


def parse_aid(payload: bytes) -> int | None:
    """從 `0x0B60` 的 payload 解出 AID。解不出來回 `None`（**不要猜**）。"""
    if len(payload) < _AID_OFFSET + 4:
        return None
    aid = int.from_bytes(payload[_AID_OFFSET : _AID_OFFSET + 4], "little")
    # 合理性：AID 是帳號編號，0 或大到離譜都代表解錯位置了。
    if not 0 < aid < 0x7FFF_FFFF:
        log.warning("從 0x0B60 解出來的 AID 不合理（%s），當作解不出來", aid)
        return None
    return aid


def pin_seed(payload: bytes) -> int | None:
    """從 `0x08B9` 解出這一輪的亂序 seed。解不出來回 `None`。

    版面：seed(4, little-endian) + AID(4) + state(2)。
    ⚠ **送出之後**伺服器也會回一包 `0x08B9`，但那包是結果（payload 全零），
    不是 seed —— 別拿它去算（踩過）。
    """
    if len(payload) < 4:
        return None
    seed = int.from_bytes(payload[0:4], "little")
    return seed or None


def pin_state(payload: bytes) -> int | None:
    """從 `0x08B9` 解出狀態碼。解不出來回 `None`。

    版面：seed(4) + AID(4) + state(2, little-endian)。
    """
    if len(payload) < 10:
        return None
    return int.from_bytes(payload[8:10], "little")


def shuffled_keypad(seed: int) -> list[int]:
    """伺服器這一輪要客戶端用的 0–9 亂序表。

    這就是螢幕上那個虛擬鍵盤的排列 —— 每次登入都不一樣，所以送出去的
    四位數是「按鍵的位置」，不是密碼本身。伺服器拿同一個 seed 算同一張表，
    就能驗證，所以它一定是從 seed 決定性推出來的（不是客戶端隨便亂排）。
    """
    table = list(range(10))
    value = seed
    for i in range(1, 10):
        value = (_PIN_BASE_SEED + value * _PIN_MULTIPLIER) & 0xFFFF_FFFF
        pos = value % (i + 1)
        if i != pos:
            table[i], table[pos] = table[pos], table[i]
    return table


def encode_pin(seed: int, pin: str) -> str:
    """把使用者的二次密碼換成「在這一輪亂序鍵盤上的位置」。

    實測驗證：seed `0x05760EA1` 時 `8291` → `5367`；
    seed `0x05796F02` 時 `8291` → `8623`。兩組都對得上。
    """
    if not (isinstance(pin, str) and len(pin) == 4 and pin.isdigit()):
        raise LoginPacketError("二次密碼必須是四位數字。")
    table = shuffled_keypad(seed)
    position = {digit: index for index, digit in enumerate(table)}
    return "".join(str(position[int(c)]) for c in pin)


def pin_packet(aid: int, encoded_pin: str) -> bytes:
    """二次密碼那一包。

    ⚠ `encoded_pin` 是 `encode_pin()` 的輸出（按鍵位置），
    **不是使用者輸入的那四位數**。直接送明文會被伺服器拒絕（實測）。
    """
    if not (isinstance(encoded_pin, str) and len(encoded_pin) == 4
            and encoded_pin.isdigit()):
        raise LoginPacketError("編碼後的二次密碼必須是四位數字。")
    if not 0 < aid < 0x7FFF_FFFF:
        raise LoginPacketError("AID 不合理，無法組二次密碼封包。")
    return (
        OP_PIN.to_bytes(2, "little")
        + aid.to_bytes(4, "little")
        + encoded_pin.encode("ascii")
    )


def select_character_packet(slot: int) -> bytes:
    """選角那一包。`slot` 是角色格號（實測 0x04 這種）。"""
    if not 0 <= slot <= 14:
        raise LoginPacketError(f"角色格號 {slot} 不在合理範圍（0–14）。")
    return OP_SELECT_CHARACTER.to_bytes(2, "little") + bytes([slot])
