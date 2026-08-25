"""注入攔截到的封包模型。

與 `core/packet.py` 的 `CapturedPacket`（網路層擷取）不同：這裡是在遊戲行程內
攔到的 `send` 呼叫，所以拿得到**加密前的內容**，還多了呼叫鏈與各層參數。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CodeRange:
    """目標執行檔的程式碼（.text）範圍，用來從堆疊挑出遊戲自己的返回位址。"""

    low: int
    high: int

    def contains(self, address: int) -> bool:
        return self.low <= address < self.high


@dataclass
class InterceptedPacket:
    seq: int
    timestamp: float
    caller: int
    """直接呼叫 send 的返回位址，多半是遊戲固定的 send wrapper。"""
    length: int
    """實際送出長度（可能大於 data，因為記錄有上限）。"""
    data: bytes
    frames: list[int]
    """沿 EBP 框架鏈走出來的各層返回位址，0 代表走不下去了。"""
    args: list[tuple[int, ...]] = field(default_factory=list)
    """與 frames 同索引，該層函式的前五個參數。"""
    code_range: CodeRange | None = None

    @property
    def truncated(self) -> bool:
        return self.length > len(self.data)

    @property
    def call_chain(self) -> list[int]:
        """落在遊戲程式碼範圍內的返回位址，由內而外去重。

        前一兩層通常固定（送出佇列、封包送出層），**第三層開始才是建構這種
        封包的函式**，不同動作各不相同——那才是要找的東西。
        """
        if self.code_range is None:
            return []
        chain: list[int] = []
        for address in self.frames:
            if self.code_range.contains(address) and address not in chain:
                chain.append(address)
        return chain

    def entropy(self) -> float:
        """Shannon 亂度（0～8）。越接近 8 越像加密或壓縮。"""
        if not self.data:
            return 0.0
        counts = Counter(self.data)
        total = len(self.data)
        return -sum((n / total) * math.log2(n / total) for n in counts.values())

    def preview(self, byte_count: int = 12) -> str:
        head = self.data[:byte_count]
        text = " ".join(f"{b:02X}" for b in head)
        return text + " …" if len(self.data) > byte_count else text

    def chain_text(self, depth: int = 4) -> str:
        chain = self.call_chain[:depth]
        return " ← ".join(f"{a:X}" for a in chain) if chain else "-"
