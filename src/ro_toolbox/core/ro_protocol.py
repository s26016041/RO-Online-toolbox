"""RO 封包的組裝／解析。

座標打包（3 bytes）：RO 標準格式，x/y 各 10 bits + 方向 4 bits。
    p0 = x >> 2
    p1 = (x << 6) | (y >> 4)
    p2 = (y << 4) | dir
已用實際擷取的走路封包驗證可逆（見 GAMEDATA [PKT-014]）。

opcode 是小端 uint16，放在封包最前面。
"""

from __future__ import annotations

# 已確認的 opcode（客戶端 → 伺服器）
CZ_REQUEST_MOVE = 0x035F  # 點地板移動，payload = 3 bytes 壓縮座標
CZ_REQUEST_TIME = 0x0360  # 心跳，payload = 4 bytes client tick
CZ_REQUEST_ACT = 0x0437   # 對實體動作（攻擊/坐站），payload = 目標ID(4) + 動作(1)
CZ_REQNAME = 0x0368       # 查詢實體資訊，payload = 目標ID(4)；點怪時會送
CZ_ITEM_PICKUP = 0x0362   # 撿地上道具，payload = 道具實體ID(4)
CZ_CHANGE_DIR = 0x0361    # 轉向，payload = headDir(2) + bodyDir(1)
CZ_ITEM_THROW = 0x0363    # 丟掉道具，payload = 背包索引(2) + 數量(2)
CZ_USE_ITEM = 0x00A7      # 使用道具，payload = 背包索引(2) + 角色AID(4)

# CZ_REQUEST_ACT 的動作代碼（RO 社群通用，本服 0x07 已實測為連續攻擊）
ACT_ATTACK_ONCE = 0x00
ACT_SIT = 0x02
ACT_STAND = 0x03
ACT_ATTACK_CONT = 0x07   # 連續普攻（左鍵點怪的效果）


def pack_position(x: int, y: int, direction: int = 0) -> bytes:
    """把格座標打包成 3 bytes。"""
    return bytes(
        [
            (x >> 2) & 0xFF,
            ((x << 6) | (y >> 4)) & 0xFF,
            ((y << 4) | (direction & 0x0F)) & 0xFF,
        ]
    )


def unpack_position(data: bytes) -> tuple[int, int, int]:
    """把 3 bytes 解回 (x, y, direction)。"""
    p0, p1, p2 = data[0], data[1], data[2]
    x = (p0 << 2) | (p1 >> 6)
    y = ((p1 & 0x3F) << 4) | (p2 >> 4)
    direction = p2 & 0x0F
    return x, y, direction


def unpack_move(data: bytes) -> tuple[tuple[int, int], tuple[int, int]]:
    """解 6 bytes 的「移動」座標打包：回傳 ((起點x, 起點y), (終點x, 終點y))。

    伺服器用這個版面告訴客戶端「某個實體從哪走到哪」（0x0087 是自己、
    0x09FD 是別的實體）。前 3 bytes 的 x/y 打包方式與 3-byte 版相同，
    終點的位元則跨在第 3~5 個 byte 上。實測可對上（見 GAMEDATA [PKT-030]）。
    """
    p0, p1, p2, p3, p4 = data[0], data[1], data[2], data[3], data[4]
    x0 = (p0 << 2) | (p1 >> 6)
    y0 = ((p1 & 0x3F) << 4) | (p2 >> 4)
    x1 = ((p2 & 0x0F) << 6) | (p3 >> 2)
    y1 = ((p3 & 0x03) << 8) | p4
    return (x0, y0), (x1, y1)


def build_move(x: int, y: int) -> bytes:
    """組一個「走到 (x, y)」的完整封包（含 opcode）。"""
    return CZ_REQUEST_MOVE.to_bytes(2, "little") + pack_position(x, y)


def build_attack(target_id: int, action: int = ACT_ATTACK_CONT) -> bytes:
    """組攻擊封包：對 target_id 做 action（預設連續普攻）。"""
    return (
        CZ_REQUEST_ACT.to_bytes(2, "little")
        + target_id.to_bytes(4, "little")
        + bytes([action & 0xFF])
    )


def build_sit(sit: bool = True) -> bytes:
    """坐下(True)或站起(False)。CZ_REQUEST_ACT 目標填自己不需要，ID 用 0。"""
    action = ACT_SIT if sit else ACT_STAND
    return CZ_REQUEST_ACT.to_bytes(2, "little") + (0).to_bytes(4, "little") + bytes([action])


def build_use_item(index: int, aid: int) -> bytes:
    """使用背包第 index 格的道具（見 GAMEDATA [PKT-036]）。

    要帶角色自己的 AID —— 從記憶體 `base_level-0x4C` 讀得到（[MEM-017]）。
    伺服器會回 `0x01C8`，裡面直接給「索引 / 道具編號 / 剩餘數量」。
    """
    return (
        CZ_USE_ITEM.to_bytes(2, "little")
        + index.to_bytes(2, "little")
        + aid.to_bytes(4, "little")
    )


def build_throw_item(index: int, amount: int) -> bytes:
    """丟掉背包第 index 格的 amount 個（使用者實測封包，見 GAMEDATA [PKT-036]）。"""
    return (
        CZ_ITEM_THROW.to_bytes(2, "little")
        + index.to_bytes(2, "little")
        + amount.to_bytes(2, "little")
    )


def build_query(target_id: int) -> bytes:
    """查詢實體資訊（CZ_REQNAME）。

    玩家左鍵點怪時，客戶端的順序是 **0x0368 查詢 → 0x035F 走近 → 0x0437 攻擊**
    （見 GAMEDATA [PKT-015] 的實測封包）。自動打怪照同樣順序送，
    行為才跟真人一致。
    """
    return CZ_REQNAME.to_bytes(2, "little") + target_id.to_bytes(4, "little")


def build_pickup(item_id: int) -> bytes:
    """組撿物封包：撿起地上實體 ID 為 item_id 的道具。"""
    return CZ_ITEM_PICKUP.to_bytes(2, "little") + item_id.to_bytes(4, "little")


def parse_target_id(payload: bytes) -> int | None:
    """從 0x0368 / 0x0437 的 payload 讀目標 ID（前 4 bytes 小端）。"""
    if len(payload) < 4:
        return None
    return int.from_bytes(payload[:4], "little")


def opcode_of(packet: bytes) -> int | None:
    """讀封包最前面的 opcode。"""
    if len(packet) < 2:
        return None
    return int.from_bytes(packet[:2], "little")
