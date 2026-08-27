"""解析伺服器推過來的角色清單（`0x0B72`）。

## 為什麼要解這一包

自動登入最後一步是「選第幾格」，而格號是**位置**。專案鐵則說存身分不存位置 ——
所以設定裡存的是**角色名稱**，登入時拿名字來這份清單查現在的格號。
清單每次登入都會重抓，玩家在遊戲裡刪角、建角之後下一次就自己修正。

## 版面（2026-08-25 實機擷取，兩份獨立擷取交叉驗證）

    opcode(2)  宣告長度(2, little-endian)  然後是 N 筆，每筆 175 bytes

每一筆裡用得到的欄位（相對這一筆的開頭）：

    +108  名稱[24]   **cp950**
    +132  六圍[6]    STR / AGI / VIT / INT / DEX / LUK
    +138  格號       uint16 little-endian
    +142  地圖[16]   ASCII，例如 "prontera.gat"

**這一包會分頁送。** 實測一個帳號 4 個角色分成兩包：先 3 筆（527 bytes）、
再 1 筆（177 bytes），中間夾著客戶端送出的 `0x09A1`。所以呼叫端要把同一次
登入收到的每一包**累加**起來，不能只取一包。

## 交叉驗證（這是相信這份版面的理由）

同一個帳號的另一份擷取裡，選角送出 `0x0066` 格號 `0x04`，
接著伺服器回 `0x0AC5` 說地圖是 `prontera.gat`。
而這份清單解出來「格號 4 = 狐狐狸、地圖 prontera.gat」—— 兩份獨立擷取對得上。
"""

from __future__ import annotations

import logging

from ro_toolbox.services.accounts import KnownCharacter

log = logging.getLogger(__name__)

#: 角色清單的 opcode。
CHAR_LIST_OPCODE = 0x0B72

#: 每一筆的大小。
ENTRY_SIZE = 175
#: 條目陣列前面的宣告長度欄位。
_HEADER_SIZE = 2

_NAME_OFFSET = 108
_NAME_SIZE = 24
_SLOT_OFFSET = 138
_MAP_OFFSET = 142
_MAP_SIZE = 16

#: 格號的合理範圍。RO 的角色欄位最多 15 格（0..14）。
#: 超出範圍代表版面對不上，寧可整包丟掉也不要回一個會選錯角色的格號。
_MAX_SLOT = 14


def parse(payload: bytes) -> list[KnownCharacter]:
    """把 `0x0B72` 的內容解成角色清單。

    `payload` 是**扣掉 opcode 之後**的位元組（含開頭那 2 bytes 宣告長度）。

    版面對不上就回空清單並記 log —— 呼叫端會因此拒絕自動選角，
    那比回一份「看起來正常但格號錯位」的清單安全得多。
    """
    if len(payload) < _HEADER_SIZE:
        return []

    declared = int.from_bytes(payload[:_HEADER_SIZE], "little")
    body = payload[_HEADER_SIZE:]
    # 宣告長度含 opcode 的 2 bytes，所以要跟 len(payload)+2 比。
    if declared != len(payload) + 2:
        log.warning(
            "角色清單長度對不上（宣告 %d、實際 %d），可能被切包切壞了",
            declared,
            len(payload) + 2,
        )

    if len(body) % ENTRY_SIZE:
        log.warning(
            "角色清單不是 %d 的整數倍（%d bytes）—— 版面可能改了，整包不採用",
            ENTRY_SIZE,
            len(body),
        )
        return []

    characters = []
    for start in range(0, len(body), ENTRY_SIZE):
        entry = body[start : start + ENTRY_SIZE]
        name = _text(entry, _NAME_OFFSET, _NAME_SIZE)
        slot = int.from_bytes(entry[_SLOT_OFFSET : _SLOT_OFFSET + 2], "little")
        if not name:
            log.warning("角色清單第 %d 筆沒有名字，略過", start // ENTRY_SIZE)
            continue
        if slot > _MAX_SLOT:
            # 這種值一定是版面對不上，不是真的有第 300 格。
            log.warning("角色「%s」的格號 %d 超出範圍，整包不採用", name, slot)
            return []
        characters.append(KnownCharacter(name=name, slot=slot))
    return characters


def map_of(payload: bytes, index: int) -> str:
    """第 `index` 筆的所在地圖，例如 `prontera.gat`。只給診斷用。"""
    body = payload[_HEADER_SIZE:]
    entry = body[index * ENTRY_SIZE : (index + 1) * ENTRY_SIZE]
    return _text(entry, _MAP_OFFSET, _MAP_SIZE, encoding="ascii")


def _text(entry: bytes, offset: int, size: int, encoding: str = "cp950") -> str:
    raw = entry[offset : offset + size].split(b"\x00")[0]
    return raw.decode(encoding, "replace").strip()
