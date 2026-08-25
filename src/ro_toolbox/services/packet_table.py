"""封包長度表：用 AOB 從客戶端**程式碼**裡把它抽出來。

為什麼要 AOB 而不是找資料：這張表不是靜態資料，是客戶端啟動時
**用程式碼建進 `std::map`** 的（註冊函式每次動態配置 0x20 位元組的節點）。
先前用值掃描找「(opcode, 長度) 成對」與「用 opcode 當索引的陣列」都是 0 命中，
結論寫成「表不存在」——那是錯的，見 GAMEDATA [MEM-024]。

指令骨架（主程式碼區段在記憶體裡是解開的，Themida 沒擋住讀取）：

    push <flag>
    push <len_b>
    push <len_a>
    push <opcode>
    mov  ecx, esi        ; 8B CE
    call <註冊函式>       ; E8 rel32

定位方式全部是**算出來的**，沒有寫死任何位址：

1. 掃 `8B CE E8`，把每個 rel32 換算成目標位址。
2. 取**被呼叫最多次的那個目標**當註冊函式（實測 1,783 處指向同一個）。
3. 往回反組譯，取最後四個 `push` 的立即值。

失敗就回空 dict —— 呼叫端要安全退化（退回原本的啟發式切包），不准拿猜的長度用。
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from .memory_scan import MemoryScanner

log = logging.getLogger(__name__)

#: 主程式碼區段最多讀這麼多（實測 Ragexe 的程式碼區段約 11.5 MB）。
_MAX_CODE = 24 << 20
#: `mov ecx, esi ; call rel32`
_CALL_PATTERN = b"\x8b\xce\xe8"
#: 往回看多少位元組找那四個 push
_ARGS_BACK = 0x28
#: 註冊函式至少要被呼叫這麼多次才採信（實測 1,783 次）
_MIN_CALLS = 200
_MAX_OPCODE = 0x10000


@dataclass(frozen=True)
class PacketInfo:
    """一個 opcode 的長度資訊。`length < 0` 代表可變長度。"""

    opcode: int
    length: int
    header: int

    @property
    def variable(self) -> bool:
        return self.length < 0


def _code_section(scanner: MemoryScanner) -> tuple[int, bytes] | None:
    """讀出 Ragexe.exe 的第一個可執行區段（主程式碼）。"""
    module = next(
        (m for m in scanner.list_modules() if m.name.lower() == "ragexe.exe"), None
    )
    if module is None:
        return None
    head = scanner._read_bytes(module.base, 0x400)  # noqa: SLF001
    if not head or len(head) < 0x40:
        return None
    e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
    pe = scanner._read_bytes(module.base + e_lfanew, 0x120)  # noqa: SLF001
    if not pe or len(pe) < 24:
        return None
    count = struct.unpack_from("<H", pe, 6)[0]
    opt_size = struct.unpack_from("<H", pe, 20)[0]
    table = module.base + e_lfanew + 24 + opt_size
    for i in range(count):
        raw = scanner._read_bytes(table + i * 40, 40)  # noqa: SLF001
        if not raw or len(raw) < 40:
            continue
        vsize, vaddr = struct.unpack_from("<II", raw, 8)
        chars = struct.unpack_from("<I", raw, 36)[0]
        if chars & 0x20000000 and 0x1000 < vsize <= _MAX_CODE:
            blob = scanner._read_bytes(module.base + vaddr, vsize)  # noqa: SLF001
            if blob:
                return module.base + vaddr, blob
    return None


def _register_function(base: int, blob: bytes) -> int | None:
    """被 `mov ecx,esi ; call` 呼叫最多次的目標 = 註冊函式。"""
    counts: dict[int, int] = {}
    start = 0
    while True:
        k = blob.find(_CALL_PATTERN, start)
        if k < 0 or k + 7 > len(blob):
            break
        start = k + 1
        rel = struct.unpack_from("<i", blob, k + 3)[0]
        counts[base + k + 7 + rel] = counts.get(base + k + 7 + rel, 0) + 1
    if not counts:
        return None
    target, hits = max(counts.items(), key=lambda kv: kv[1])
    if hits < _MIN_CALLS:
        log.warning("最熱門的註冊呼叫只有 %d 次，不足以認定", hits)
        return None
    return target


def _signed(value: int | None) -> int | None:
    """統一成有號 32 位。

    capstone 對 `6A FF`（push imm8）印 `-1`，對 `68 FF FF FF FF`（push imm32）
    印 `0xffffffff` —— 同一個「可變長度」的意思，兩種寫法。不統一的話
    可變長度封包會被當成長度 4,294,967,295。
    """
    if value is None:
        return None
    return value - 0x100000000 if value >= 0x80000000 else value


def _pushes(disassembler, blob: bytes, base: int, site: int) -> list[int | None]:
    """取 `site` 之前那幾個 push 的立即值。

    x86 是變長指令，從固定的 `site - N` 開始解會解錯位（解出 `add bh, bh`
    那種鬼東西）。所以逐一嘗試不同起點，只採用「有指令剛好落在 site 上」
    的那個對齊 —— 那才代表整段解對了。實測這樣做，能解出的註冊點
    從 936 個增加到接近全部 1,783 個。
    """
    best: list[int | None] = []
    for back in range(_ARGS_BACK, 7, -1):
        start = site - back
        window = blob[start - base : site - base + 3]
        if len(window) < back:
            continue
        got: list[int | None] = []
        landed = False
        for ins in disassembler.disasm(window, start):
            if ins.address == site:
                landed = True
                break
            if ins.mnemonic != "push":
                got = []
                continue
            try:
                got.append(int(ins.op_str, 0))
            except ValueError:
                got.append(None)
        if landed and len(got) > len(best):
            best = got
    return best


def extract(pid: int, scanner: MemoryScanner | None = None) -> dict[int, PacketInfo]:
    """抽出 {opcode: PacketInfo}。抽不到回空 dict（呼叫端要安全退化）。"""
    try:
        from capstone import CS_ARCH_X86, CS_MODE_32, Cs
    except ImportError:
        log.info("沒裝 capstone，跳過封包長度表")
        return {}

    own = scanner is None
    if scanner is None:
        scanner = MemoryScanner()
        scanner.open(pid)
    try:
        section = _code_section(scanner)
        if section is None:
            log.warning("讀不到 Ragexe 的程式碼區段")
            return {}
        base, blob = section
        register = _register_function(base, blob)
        if register is None:
            return {}

        sites: list[int] = []
        start = 0
        while True:
            k = blob.find(_CALL_PATTERN, start)
            if k < 0 or k + 7 > len(blob):
                break
            start = k + 1
            rel = struct.unpack_from("<i", blob, k + 3)[0]
            if base + k + 7 + rel == register:
                sites.append(base + k)

        disassembler = Cs(CS_ARCH_X86, CS_MODE_32)
        table: dict[int, PacketInfo] = {}
        for site in sites:
            args = _pushes(disassembler, blob, base, site)
            if len(args) < 4:
                continue
            _flag, header, length, opcode = (_signed(v) for v in args[-4:])
            if opcode is None or not (0 < opcode < _MAX_OPCODE):
                continue
            if length is None or header is None:
                continue
            table[opcode] = PacketInfo(opcode=opcode, length=length, header=header)
        log.info("封包長度表：%d 個 opcode（註冊函式 %#x）", len(table), register)
        return table
    finally:
        if own:
            scanner.close()
