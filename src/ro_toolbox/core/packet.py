"""封包資料模型。

刻意不依賴 scapy 或 Qt：擷取層負責把 scapy 封包轉成這個型別，
之後要換擷取後端或寫解析器，都只認這個結構。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CapturedPacket:
    index: int
    timestamp: float
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    outbound: bool
    payload: bytes

    @property
    def length(self) -> int:
        return len(self.payload)

    @property
    def direction(self) -> str:
        return "送出" if self.outbound else "接收"

    @property
    def arrow(self) -> str:
        return "↑" if self.outbound else "↓"

    @property
    def source(self) -> str:
        return f"{self.src_ip}:{self.src_port}"

    @property
    def destination(self) -> str:
        return f"{self.dst_ip}:{self.dst_port}"

    def time_text(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S.%f")[:-3]

    def entropy(self) -> float:
        """Shannon 亂度（0～8）。接近 8 且幾乎沒有零位元組通常代表加密或壓縮。"""
        if not self.payload:
            return 0.0
        counts = Counter(self.payload)
        total = len(self.payload)
        return -sum((n / total) * math.log2(n / total) for n in counts.values())

    def looks_encrypted(self) -> bool:
        """粗判：高亂度又沒有零位元組，多半是加密或壓縮過的。"""
        if len(self.payload) < 16:
            return False
        zero_ratio = self.payload.count(0) / len(self.payload)
        return self.entropy() > 7.5 and zero_ratio < 0.02

    def preview(self, byte_count: int = 12) -> str:
        head = self.payload[:byte_count]
        text = " ".join(f"{b:02X}" for b in head)
        return text + " …" if len(self.payload) > byte_count else text
