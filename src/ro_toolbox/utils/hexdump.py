"""Hex dump 格式化。

輸出格式刻意固定，方便直接貼進對話或 diff 兩次擷取結果。
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime

from ro_toolbox.core.intercept import InterceptedPacket
from ro_toolbox.core.packet import CapturedPacket

_WIDTH = 16
_MIN_BYTES_FOR_VERDICT = 64


def hexdump(data: bytes, width: int = _WIDTH) -> str:
    """經典 offset / hex / ASCII 三欄格式。"""
    if not data:
        return "(無資料)"

    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        half = width // 2
        left = " ".join(f"{b:02X}" for b in chunk[:half])
        right = " ".join(f"{b:02X}" for b in chunk[half:])
        hex_part = f"{left:<{half * 3 - 1}}  {right:<{half * 3 - 1}}"
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:04X}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


def format_packet(packet: CapturedPacket) -> str:
    """單一封包的完整文字表示（標頭 + hex dump）。"""
    stamp = datetime.fromtimestamp(packet.timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    header = (
        f"=== #{packet.index}  {stamp}  {packet.arrow} {packet.direction}  "
        f"{packet.source} -> {packet.destination}  "
        f"len={packet.length}  亂度 {packet.entropy():.2f}/8 ==="
    )
    return f"{header}\n{hexdump(packet.payload)}"


def encryption_verdict(packets: list[CapturedPacket]) -> str:
    """把所有 payload 合起來判斷是加密還是明文。"""
    return _verdict_for(b"".join(p.payload for p in packets))


def _verdict_for(blob: bytes) -> str:
    """亂度判斷的共用實作。

    單一封包太短時亂度不可靠，合併後才有意義。判準沿用
    Angels-Online-toolbox 的 tools/sniff.py。
    """
    if len(blob) < _MIN_BYTES_FOR_VERDICT:
        return "資料量太少，無法判斷是否加密"

    counts = Counter(blob)
    total = len(blob)
    entropy = -sum((n / total) * math.log2(n / total) for n in counts.values())
    printable = sum(1 for b in blob if 32 <= b < 127) / total
    zeros = blob.count(0) / total

    if entropy > 7.5 and zeros < 0.02:
        verdict = "很可能加密或壓縮過"
    else:
        verdict = "有結構，很可能是明文或輕度混淆，可以解析"

    return (
        f"整體亂度 {entropy:.2f}/8，可列印 {printable * 100:.0f}%，"
        f"零位元組 {zeros * 100:.0f}% → {verdict}"
    )


def format_packets(packets: list[CapturedPacket], title: str = "") -> str:
    """多封包匯出。開頭附摘要，方便貼上後一眼看出擷取範圍。"""
    if not packets:
        return "(沒有封包)"

    out_count = sum(1 for p in packets if p.outbound)
    summary = [
        "# RO Toolbox 封包擷取",
        f"# 來源：{title}" if title else "",
        f"# 筆數：{len(packets)}（送出 {out_count} / 接收 {len(packets) - out_count}）",
        f"# 位元組：{sum(p.length for p in packets)}",
        f"# {encryption_verdict(packets)}",
        "",
    ]
    body = "\n\n".join(format_packet(p) for p in packets)
    return "\n".join(line for line in summary if line) + "\n" + body


# ---- 注入攔截的封包 ---------------------------------------------------


def format_intercepted(packet: InterceptedPacket, arg_depth: int = 3) -> str:
    """單一攔截封包：標頭 + 呼叫鏈 + 各層參數 + hex dump。

    呼叫鏈與參數是注入方案獨有的資訊，用來認出「建構這種封包的函式」。
    """
    stamp = datetime.fromtimestamp(packet.timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    truncated = "（截斷）" if packet.truncated else ""
    lines = [
        f"=== #{packet.seq}  {stamp}  len={packet.length}{truncated}  "
        f"亂度 {packet.entropy():.2f}/8  caller={packet.caller:X} ===",
        f"呼叫鏈: {packet.chain_text(depth=6)}",
    ]

    chain = packet.call_chain[:arg_depth]
    for address in chain:
        try:
            position = packet.frames.index(address)
        except ValueError:
            continue
        if position < len(packet.args):
            values = ", ".join(f"{v:#x}" for v in packet.args[position])
            lines.append(f"  {address:X} 參數: ({values})")

    lines.append(hexdump(packet.data))
    return "\n".join(lines)


def format_intercepted_list(
    packets: list[InterceptedPacket], title: str = ""
) -> str:
    """多筆攔截封包匯出，開頭附摘要與加密判斷。"""
    if not packets:
        return "(沒有封包)"

    blob = b"".join(p.data for p in packets)
    summary = [
        "# RO Toolbox 封包攔截（注入 hook send）",
        f"# 來源：{title}" if title else "",
        f"# 筆數：{len(packets)}",
        f"# 位元組：{sum(len(p.data) for p in packets)}",
        f"# {_verdict_for(blob)}",
        "",
    ]
    body = "\n\n".join(format_intercepted(p) for p in packets)
    return "\n".join(line for line in summary if line) + "\n" + body


# ---- RO 封包（網路層擷取）---------------------------------------------


def format_ro_packet(packet) -> str:
    """單一 RO 封包：標頭 + hex dump。"""
    stamp = datetime.fromtimestamp(packet.timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    header = (
        f"=== #{packet.seq}  {stamp}  {packet.arrow} {packet.direction}  "
        f"opcode {packet.opcode_hex}  len={packet.length} ==="
    )
    return f"{header}\n{hexdump(packet.payload)}"


def format_ro_packets(packets: list, title: str = "") -> str:
    """多筆 RO 封包匯出，開頭附 opcode 統計。"""
    if not packets:
        return "(沒有封包)"

    from collections import Counter

    counts = Counter(p.opcode for p in packets)
    out = sum(1 for p in packets if p.outbound)
    summary = [
        "# RO Toolbox 封包擷取（網路層）",
        f"# 來源：{title}" if title else "",
        f"# 筆數：{len(packets)}（送出 {out} / 接收 {len(packets) - out}）",
        "# opcode 統計：" + "  ".join(f"0x{op:04X}×{n}" for op, n in counts.most_common()),
        "",
    ]
    body = "\n\n".join(format_ro_packet(p) for p in packets)
    return "\n".join(line for line in summary if line) + "\n" + body
