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
    if len(data) < 2:
        return []
    if not lengths:
        opcode = int.from_bytes(data[:2], "little")
        return [(opcode, data)]

    out: list[tuple[int, bytes]] = []
    pos = 0
    while pos + 2 <= len(data):
        opcode = int.from_bytes(data[pos : pos + 2], "little")
        info = lengths.get(opcode)
        if info is None:
            # 不認得的 opcode：無法安全地往下切，把剩下的整段交出去
            out.append((opcode, data[pos:]))
            break
        size, header = info
        if size < 0:
            if pos + 4 > len(data):
                break
            size = int.from_bytes(data[pos + 2 : pos + 4], "little")
            if size < max(header, 4):
                break
        if size < 2 or pos + size > len(data):
            # 封包被切在分段邊界上：剩下的整段交出去，不要硬切
            out.append((opcode, data[pos:]))
            break
        out.append((opcode, data[pos : pos + size]))
        pos += size
    return out
