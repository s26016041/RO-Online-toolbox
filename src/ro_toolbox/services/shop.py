"""跟 NPC 商店買東西：封包版面 ＋ 「該買幾個」的算式。

**這支不碰 socket、不碰記憶體、不開執行緒** —— 純粹是「位元組進、位元組出」
加上一個算式，所以整段都測得起來。

## 版面是實機量的，不是猜的

來源：使用者 2026-08-28 在道具商人那裡**手動買 300 瓶藥水**的擷取
（`封包/購買藥水.txt`，48 個封包）。細節與逐位元組推導見 GAMEDATA [PKT-074]。

    ↑ 0x0090  跟 NPC 講話      GID(4) + 種類(1)=1
    ↓ 0x00C4  「這是商店」      GID(4)                 ← 收到它才代表真的是商店
    ↑ 0x00C5  選「買」          GID(4) + 種類(1)=0
    ↓ 0x00C6  商品清單          長度(2) + N×13 位元組
    ↑ 0x00C8  下單              長度(2) + N×6 位元組
    ↓ 0x00CA  買賣結果          結果(1)，0 = 成功

⚠ **道具編號是 4 位元組**（不是舊版的 2 位元組）：實機看到 `f6 01 00 00` = 502。
照舊版寫成 2 位元組會**買到別的東西**，而且看起來像成功。

## ⚠ 商店賣什麼、賣多少錢，客戶端**沒有**

那是伺服器的資料，開店那一刻才用 0x00C6 送過來。所以流程只能是
**走過去 → 開店 → 讀清單 → 才知道買不買得到**，不能事先算好。
清單裡沒有我們要的道具就**不買**（安全退化），不准挑一個「看起來像」的。

## ⚠ 負重的原始值是畫面上的 10 倍

實機對照：封包 `45304 / 48100`，畫面顯示 `4530 / 4810`。所有計算一律用
**原始值**，只有要給人看的時候才除以 10 —— 兩種單位混用會算出十倍的量。

## 買到幾成：目標 65%，硬上限 70%

使用者 2026-08-29 指定（一開始是 69%，同日改成 65%）。`fill_target()` 是
**唯一**算得出這個數字的地方，而且會把 `ratio` 夾在 `WEIGHT_CAP_RATIO`（70%）
以內 —— 買過頭會走不動。
`//` 是向下取整，所以「買下去之後」也一定還在目標以內。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

#: ↑ 跟 NPC 講話（CZ_CONTACTNPC）
OP_CONTACT_NPC = 0x0090
#: ↓ 「這個 NPC 是商店，選買還是賣」（ZC_SELECT_DEALTYPE）
OP_DEAL_TYPE = 0x00C4
#: ↑ 選買／賣（CZ_ACK_SELECT_DEALTYPE）
OP_ACK_DEAL_TYPE = 0x00C5
#: ↓ 商品清單（ZC_PC_PURCHASE_ITEMLIST）
OP_SHOP_LIST = 0x00C6
#: ↑ 下單（CZ_PC_PURCHASE_ITEMLIST）
OP_BUY = 0x00C8
#: ↓ 買賣結果（ZC_PC_PURCHASE_RESULT）
OP_BUY_RESULT = 0x00CA
#: 關閉商店視窗。**payload 是空的**（長度表說 2 = 只有 opcode）。
#:
#: ⚠⚠ **買完一定要送這一包。** 商店對話開著的時候**角色不能移動** ——
#: 買完不關就走不回練功點（使用者實測回報）。跟 [PKT-075] 的「最後那個
#: 『離開』不按掉，傳送永遠不會發生」是同一類問題。
#: 出處：使用者擷取的購買流程 `封包/購買藥水.txt` #29/#30 —— 買完
#: （`0x00C8`）之後 29 ms 內連送兩次 `0x09D4`，payload 皆為空。
OP_CLOSE_SHOP = 0x09D4
#: ↓ 道具進背包（ZC_ITEM_PICKUP_ACK）。**買東西成交最直接的證據**：
#: 它把「哪一個道具、進來幾個」講清楚，比 `0x00CA` 的一個 0 更有分辨力。
#:
#: 版面（2026-09-01 實機三次，狐狐狸 @ prt_fild05 道具商人）：
#:
#:     格號(2) + 數量(2) + 道具編號(4) + …（整包 len=70）
#:     18 00 | 01 00 | 9f 5b 00 00 → 第 0x18 格、1 個、23455（蝴蝶翅膀）
#:     1c 00 | 8b 00 | f6 01 00 00 → 第 0x1c 格、139 個、502（赤色藥水）
#:
#: 數量與送出去的 `0x00C8` 完全對得上（1／1／139），所以拿它當
#: 「這一筆到底成交了沒」的答案卡 —— 逾時的時候不必猜。
OP_ITEM_ADDED = 0x0B41
#: ↓ 數值變動（ZC_PAR_CHANGE，type u16 + value u32）
OP_PAR_CHANGE = 0x00B0
#: ↓ 數值變動的大數版（ZC_LONGPAR_CHANGE，type u16 + value i32）
OP_LONGPAR_CHANGE = 0x00B1

#: `0x00B0` / `0x00B1` 的 type 欄。實機在這份擷取裡看到 5 / 20 / 24 / 25。
SP_HP = 5
SP_ZENY = 20
SP_WEIGHT = 24
SP_MAX_WEIGHT = 25

#: 選「買」。1 是賣。
DEAL_BUY = 0
#: 跟 NPC 講話的種類欄，實機看到的值。
CONTACT_TALK = 1
#: 買賣結果 0 = 成功（實機那一次買 300 瓶收到的就是 0）。
RESULT_OK = 0

#: 商品清單一筆幾個位元組：價錢(4) + 折扣價(4) + 種類(1) + 道具編號(4)。
#: 實機驗算：宣告長度 225 − 2(opcode) − 2(長度欄) = 221 = 13 × 17 筆。
_SHOP_ENTRY = 13
#: 下單一筆幾個位元組：數量(2) + 道具編號(4)。實機 len=10 = 2+2+6，一筆。
_BUY_ENTRY = 6

#: 負重原始值 ÷ 這個 = 畫面上看到的數字。
WEIGHT_SCALE = 10
#: 買到負重的幾成為止（使用者 2026-08-29 指定：先 69%、同日改成 **65%**）。
FILL_RATIO = 0.65
#: ⚠ **硬上限**：買下去之後的負重不准越過上限的這個比例（使用者指定 70%）。
#: 傳進來的 `ratio` 再大也會被夾在這裡 —— 買過頭會走不動，是安靜地做錯事。
WEIGHT_CAP_RATIO = 0.70
#: 一次下單的數量欄是 u16。
_MAX_AMOUNT = 0xFFFF


@dataclass(frozen=True, slots=True)
class ShopItem:
    """商店賣的一項：編號、單價、種類。"""

    item_id: int
    price: int
    kind: int


@dataclass(frozen=True, slots=True)
class Purchase:
    """這一次該買幾個，以及是被什麼卡住的。

    `limited_by`：
        ``"weight"`` 負重滿了（正常結束）
        ``"zeny"``   錢不夠 —— 呼叫端要**停掉自動打怪並跳通知**（使用者指定）
        ``"none"``   兩個都還有餘裕（理論上不會出現，買的量本來就是取兩者小的）
    """

    amount: int
    limited_by: str


def contact_npc(gid: int) -> bytes:
    """↑ 0x0090：跟這隻 NPC 講話。"""
    return struct.pack("<HIB", OP_CONTACT_NPC, gid, CONTACT_TALK)


def choose_buy(gid: int) -> bytes:
    """↑ 0x00C5：在「買／賣」的選單選買。"""
    return struct.pack("<HIB", OP_ACK_DEAL_TYPE, gid, DEAL_BUY)


def close_shop() -> bytes:
    """關閉商店視窗。買完（或放棄）都要送 —— 不關的話角色動不了。"""
    return OP_CLOSE_SHOP.to_bytes(2, "little")


def open_shop(gid: int) -> bytes:
    """↑ 開店：`0x0090 接觸` ＋ `0x00C5 選買`，**併成一次送出去**。

    ⚠⚠ 併成一包不是省事，是**正確性**（[PKT-092]）：客戶端會在它覺得該關的
    時候自己送 `0x09D4`，那一包插進我們的開店流程中間，伺服器就把交易狀態
    關掉了 —— 後面的封包被**安靜地丟掉**。同一次 `send()` 寫進 socket 的
    位元組是連續的，客戶端**插不進來**。

    代價是不等 `0x00C4`（「這個 NPC 真的是商店」）就先送選買。認錯 NPC 的話
    照樣不會有 `0x00C6 商品清單`，那時候一樣大聲逾時 —— 安全退化沒有變。
    """
    return contact_npc(gid) + choose_buy(gid)


def order_packet(gid: int, orders: list[tuple[int, int]]) -> bytes:
    """↑ 下一筆單：`接觸 ＋ 選買 ＋ 下單`，**一次送出去**（見 `open_shop()`）。

    實機驗收（2026-09-01，狐狐狸 @ prt_fild05 道具商人）：客戶端當時處在
    「每收到一次商品清單就馬上送 `0x09D4`」的狀態，分開送 **0/3 成交**、
    併成一包 **3/3 成交**。
    """
    return open_shop(gid) + buy_packet(orders)


def buy_packet(orders: list[tuple[int, int]]) -> bytes:
    """↑ 0x00C8：下單。`orders` 是 [(道具編號, 數量)]。

    ⚠ 數量是 u16、道具編號是 **u32**（實機驗過，見檔頭）。
    """
    body = b"".join(
        struct.pack("<HI", min(max(int(amount), 0), _MAX_AMOUNT), int(item_id))
        for item_id, amount in orders
    )
    return struct.pack("<HH", OP_BUY, len(body) + 4) + body


def parse_shop_list(payload: bytes) -> list[ShopItem]:
    """↓ 0x00C6：商品清單。版面不合就回空的（**不猜**）。

    `payload` 是**去掉 opcode 之後**的位元組，開頭兩個仍是封包宣告的總長度
    （擷取端就是這樣給的）。
    """
    if len(payload) < 2:
        return []
    declared = struct.unpack_from("<H", payload, 0)[0]
    body = payload[2:]
    # 宣告長度含 opcode 的 2 個位元組。對不上就是版面變了 —— 寧可不買。
    if declared - 4 != len(body) or len(body) % _SHOP_ENTRY:
        return []
    out = []
    for offset in range(0, len(body), _SHOP_ENTRY):
        price, _discount, kind, item_id = struct.unpack_from("<IIBI", body, offset)
        out.append(ShopItem(item_id=item_id, price=price, kind=kind))
    return out


def parse_item_added(payload: bytes) -> tuple[int, int] | None:
    """↓ 0x0B41：(道具編號, 數量)。版面不合就回 None（**不猜**）。

    ⚠ 這是「東西真的進背包了」的答案卡。伺服器對同一筆訂單會先送它、
    再送 `0x00CA` —— 所以 `0x00CA` 沒來的時候，它是唯一分得出
    「其實成交了」與「伺服器整包丟掉了」的東西（[PKT-092]）。
    """
    if len(payload) < 8:
        return None
    amount = struct.unpack_from("<H", payload, 2)[0]
    item_id = struct.unpack_from("<I", payload, 4)[0]
    if not item_id or not amount:
        return None
    return item_id, amount


def parse_buy_result(payload: bytes) -> int | None:
    """↓ 0x00CA：買賣結果。0 = 成功。讀不到回 None。"""
    return payload[0] if payload else None


def parse_par_change(opcode: int, payload: bytes) -> tuple[int, int] | None:
    """↓ 0x00B0 / 0x00B1：(type, value)。不是這兩個或長度不對就回 None。"""
    if len(payload) < 6:
        return None
    if opcode == OP_PAR_CHANGE:
        return struct.unpack_from("<HI", payload, 0)
    if opcode == OP_LONGPAR_CHANGE:
        return struct.unpack_from("<Hi", payload, 0)
    return None


def find_item(items: list[ShopItem], item_id: int) -> ShopItem | None:
    """清單裡有沒有這個道具。**沒有就是沒有**，不准挑一個像的。"""
    for item in items:
        if item.item_id == item_id:
            return item
    return None


def fill_target(max_weight: int, ratio: float = FILL_RATIO) -> int:
    """買到哪個負重（**原始值**）為止。

    ⚠ 一律夾在 `WEIGHT_CAP_RATIO` 以內：「不可超過 70%」是使用者訂的硬規則，
    傳再大的 `ratio` 也不准越過。買到幾成只有這一個地方算得出來 ——
    同一條算式在第二個地方再寫一次，改比例的時候另一份不會跟上。
    """
    if max_weight <= 0:
        return 0
    return int(max_weight * min(ratio, WEIGHT_CAP_RATIO))


def plan_purchase(
    weight: int,
    max_weight: int,
    unit_weight: int,
    zeny: int,
    price: int,
    ratio: float = FILL_RATIO,
) -> Purchase:
    """買到「現在負重 ＋ 買下去的重量」達到 `fill_target()` 為止（使用者指定）。

    全部用**原始值**（畫面上的十倍，見檔頭）。

    ⚠ `unit_weight` 要是**量出來的**：先買 1 個、看負重跳多少。
    道具表裡的重量只寫在說明文字（「重量 : 10」）—— 靠解說明字串就是
    CLAUDE.md 禁止的那種「很有自信的錯」，而量一次就準。

    ⚠ 沒有數量上限（使用者指定），但下單欄位是 u16，所以還是夾在 65535。
    """
    if unit_weight <= 0 or price <= 0 or max_weight <= 0:
        return Purchase(0, "weight")
    room = fill_target(max_weight, ratio) - weight
    if room <= 0:
        return Purchase(0, "weight")
    by_weight = room // unit_weight
    by_zeny = zeny // price
    amount = min(by_weight, by_zeny, _MAX_AMOUNT)
    limited = "zeny" if by_zeny < by_weight else "weight"
    return Purchase(max(amount, 0), limited)


def display_weight(raw: int) -> int:
    """原始負重換成畫面上看到的數字（給人看的字才用，算式一律用原始值）。"""
    return raw // WEIGHT_SCALE
