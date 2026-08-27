"""RO 封包模型。

RO 走 TCP，每個封包開頭是 2 bytes 的 opcode（小端 uint16），後面接 payload。
封包是明文（見 GAMEDATA [PKT-012]），所以能直接解析。

opcode 命名慣例（RO 社群通用）：
    CZ_* = 客戶端 → 伺服器（我們送出的，動作對照就是看這個）
    ZC_* = 伺服器 → 客戶端（伺服器推送的，怪物/其他玩家等）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RoPacket:
    seq: int
    timestamp: float
    outbound: bool
    opcode: int
    payload: bytes
    """opcode 之後的內容（不含開頭那 2 bytes）。"""

    @property
    def opcode_hex(self) -> str:
        return f"0x{self.opcode:04X}"

    @property
    def length(self) -> int:
        """整個封包長度（含 opcode 的 2 bytes）。"""
        return len(self.payload) + 2

    @property
    def arrow(self) -> str:
        return "↑" if self.outbound else "↓"

    @property
    def direction(self) -> str:
        return "送出" if self.outbound else "接收"

    def time_text(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S.%f")[:-3]

    def payload_hex(self, limit: int = 32) -> str:
        head = self.payload[:limit]
        text = " ".join(f"{b:02X}" for b in head)
        return text + " …" if len(self.payload) > limit else text


def split_stream(
    data: bytes, lengths: dict[int, tuple[int, int]] | None = None
) -> tuple[list[tuple[int, bytes]], bytes]:
    """切包，並把**沒切完的尾巴**交回給呼叫端。

    回 `(封包清單, 剩下的位元組)`。呼叫端要把剩下的接在下一段 TCP 資料前面，
    這就是 TCP 重組。

    ## 為什麼一定要重組

    TCP 是位元組流，一個 RO 封包**可能跨兩個分段**。實測 2026-08-25：
    伺服器的登入回應 `0x0B60` 宣告自己 392 bytes，卻被切成 64 + 328 兩段送達。
    沒有重組的話，第一段被當成一個 64 bytes 的 `0x0B60`（內容被截斷），
    第二段的開頭兩個 byte 被當成新的 opcode —— 於是冒出 `0xA8C0`、`0x5FF8`、
    `0x96E7` 這種不可能存在的 opcode，而**內容整個錯位**。
    伺服器清單就是這樣被誤讀的（[PKT-050]）。
    """
    if not lengths:
        # 沒有長度表時無法判斷邊界，維持舊行為：整段當一包，不留尾巴。
        return ([(int.from_bytes(data[:2], "little"), data)] if len(data) >= 2 else []), b""

    out: list[tuple[int, bytes]] = []
    pos = 0
    while pos + 2 <= len(data):
        opcode = int.from_bytes(data[pos : pos + 2], "little")
        info = lengths.get(opcode)
        if info is None:
            # 不認得的 opcode：無法安全地往下切。這通常代表我們已經失去同步，
            # 把剩下的整段交出去（至少看得到內容），不要留成尾巴無限累積。
            out.append((opcode, data[pos:]))
            return out, b""
        size, header = info
        if size < 0:
            if pos + 4 > len(data):
                break                      # 連宣告長度都還沒到齊，留給下一段
            size = int.from_bytes(data[pos + 2 : pos + 4], "little")
            if size < max(header, 4):
                # 宣告長度不合理 → 失去同步，剩下的整段交出去
                out.append((opcode, data[pos:]))
                return out, b""
        if size < 2:
            out.append((opcode, data[pos:]))
            return out, b""
        if pos + size > len(data):
            break                          # 這一包還沒到齊，留給下一段
        out.append((opcode, data[pos : pos + size]))
        pos += size
    return out, data[pos:]


def split_packets(
    data: bytes, lengths: dict[int, tuple[int, int]] | None = None
) -> list[tuple[int, bytes]]:
    """把一段 TCP 位元組流切成 [(opcode, 整個封包 bytes), ...]。

    `lengths` 是 {opcode: (長度, 標頭)}，長度 < 0 代表可變長度
    （真正的長度寫在 opcode 後面 2 bytes）。給了就**精確切包**；
    沒給就退回舊行為（整段當一包），呼叫端不必改。

    為什麼一定要精確切包：一個 TCP 分段裡常常黏著好幾個封包，
    整段當一包的話**只看得到第一個 opcode，後面的完全消失**。
    實測踩過：使用道具的回應 `0x01C8` 明明有回來（記憶體裡數量確實少了），
    但因為黏在別的封包後面，擷取端一次都沒看到。
    長度表用 AOB 從客戶端程式碼抽出來（`services/packet_table.py`，[MEM-024]）。
    """
    packets, leftover = split_stream(data, lengths)
    if len(leftover) >= 2:
        # ⚠ 尾巴沒地方去。逐段處理 TCP 流的呼叫端**必須改用 `split_stream`**
        # 並自己把尾巴接到下一段前面，否則跨段的封包會被截斷、
        # 下一段的開頭兩個 byte 會被誤判成新的 opcode。
        # 這裡至少把它當一包交出去（看得到總比消失好），但內容是截斷的。
        packets.append((int.from_bytes(leftover[:2], "little"), leftover))
    return packets
