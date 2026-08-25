"""從記憶體讀完整背包：格號、道具編號、數量。

**為什麼之前找不到**：客戶端把道具編號存成**十進位字串**（`"502"`），不是數字。
所以掃描 uint16/uint32 的 502 永遠掃不到。這件事是從客戶端程式碼看出來的
（見 GAMEDATA [MEM-028]）：解析背包清單封包時，它把編號丟給 `_itoa`
再存進一個 `std::string`。

結構（都是從程式碼讀出來的，不是猜的）：

    背包容器（模組內的靜態全域）
      └─ +0x1738 附近某個 std::list
           節點 +0x00  next（環狀雙向串列）
           節點 +0x04  prev
           節點 +0x0C  格號        ← AddItem 用這個欄位比對是不是同一格
           節點 +0x18  數量
           節點 +0x34  道具編號的十進位字串（短字串直接內嵌）

定位方式（不寫死任何位址）：

1. AOB 找「背包清單封包的 case 區塊」—— 它有一段很好認的指令骨架：
   把封包長度減 5 之後用 `0xF0F0F0F1` 乘法除以 **34**（記錄大小）。
2. 從那裡的 `call rel32` 取得解析函式。
3. 在解析函式裡找 `mov ecx, <imm32>` —— 那個立即值就是背包容器。
4. 在容器裡逐一試 std::list 欄位，**用資料本身驗證**（節點要解得出
   合理的格號、數量、以及能轉成整數的編號字串），驗過才採用。

任何一步失敗就回空清單 —— 呼叫端要安全退化，絕不拿猜的值繼續算。
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from .memory_scan import MemoryScanner

log = logging.getLogger(__name__)

#: 「除以 34」的魔術乘數。34 是背包清單封包的記錄大小（[PKT-039]）。
#: 這是指令骨架的錨，不是答案 —— 容器位址是從指令的立即值讀出來的。
_MAGIC_DIV34 = b"\xb8\xf1\xf0\xf0\xf0"      # mov eax, 0xF0F0F0F1
_SUB_5 = b"\x83\xe9\x05"                    # sub ecx, 5
_MOV_ECX_IMM = 0xB9                         # mov ecx, imm32
_CALL_REL32 = 0xE8

# 節點欄位（結構偏移屬於 CLAUDE.md 允許寫死的類別，出處見本檔開頭）
_OFF_NEXT = 0x00
_OFF_SLOT = 0x0C
_OFF_AMOUNT = 0x18
_OFF_ID_TEXT = 0x34
_ID_TEXT_MAX = 16

_MAX_NODES = 400
_MAX_SLOT = 250
_MAX_AMOUNT = 40_000
_CONTAINER_SPAN = 0x2000     # 在容器裡往後找 std::list 欄位的範圍
_MIN_ROWS = 3                # 少於這個數量不敢當成背包


@dataclass(frozen=True)
class BagItem:
    slot: int
    item_id: int
    amount: int


def _u32(scanner: MemoryScanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)  # noqa: SLF001
    return struct.unpack("<I", raw)[0] if raw else None


def _code_section(scanner: MemoryScanner) -> tuple[int, bytes] | None:
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
        row = scanner._read_bytes(table + i * 40, 40)  # noqa: SLF001
        if not row or len(row) < 40:
            continue
        vsize, vaddr = struct.unpack_from("<II", row, 8)
        chars = struct.unpack_from("<I", row, 36)[0]
        if chars & 0x20000000 and 0x1000 < vsize <= (32 << 20):
            blob = scanner._read_bytes(module.base + vaddr, vsize)  # noqa: SLF001
            if blob:
                return module.base + vaddr, blob
    return None


def find_containers(scanner: MemoryScanner) -> list[int]:
    """AOB 定位背包容器，回傳**所有**通過骨架驗證的候選（去重、保持順序）。

    為什麼要回全部而不是第一個：只回第一個的話，定位器就**看不見自己有歧義**。
    改版新增一段長得一樣的骨架（例如另一個記錄大小 34 的封包）時，
    它會安靜地挑到別人家的全域。有幾個候選要由呼叫端用資料去裁決
    （見 `read_bag`：兩個候選都讀得出合理背包就大聲停用）。
    """
    section = _code_section(scanner)
    if section is None:
        return []
    base, blob = section
    found: list[int] = []
    start = 0
    while True:
        k = blob.find(_MAGIC_DIV34, start)
        if k < 0:
            return found
        start = k + 1
        # 這個魔術乘數在別處也會出現，要求附近有 `sub ecx, 5`（扣掉封包標頭）
        if _SUB_5 not in blob[max(0, k - 0x20) : k + 0x20]:
            continue
        # 往後找第一個 call rel32 —— 那是解析函式
        span = blob[k : k + 0x40]
        pos = span.find(bytes([_CALL_REL32]))
        if pos < 0:
            continue
        rel = struct.unpack_from("<i", span, pos + 1)[0]
        parser = base + k + pos + 5 + rel
        if not (base <= parser < base + len(blob)):
            continue
        # 在解析函式裡找 mov ecx, imm32 —— 立即值就是容器
        body = blob[parser - base : parser - base + 0x200]
        at = 0
        while True:
            at = body.find(bytes([_MOV_ECX_IMM]), at)
            if at < 0 or at + 5 > len(body):
                break
            value = struct.unpack_from("<I", body, at + 1)[0]
            at += 1
            if base <= value < base + len(blob):
                continue          # 指向程式碼的不是容器
            if 0x400000 < value < 0x10000000:
                log.info("背包容器 AOB 命中：0x%X（解析函式 0x%X）", value, parser)
                if value not in found:
                    found.append(value)
                break


def find_container(scanner: MemoryScanner) -> int | None:
    """第一個候選容器（給只需要一個位址的呼叫端用）。找不到回 None。"""
    got = find_containers(scanner)
    return got[0] if got else None


def _walk(scanner: MemoryScanner, head: int) -> list[int]:
    """走環狀雙向串列，回傳所有節點位址。"""
    nodes: list[int] = []
    seen: set[int] = set()
    node = _u32(scanner, head + _OFF_NEXT)
    while node and node != head and node not in seen and len(nodes) < _MAX_NODES:
        seen.add(node)
        nodes.append(node)
        node = _u32(scanner, node + _OFF_NEXT)
    return nodes


def _read_node(scanner: MemoryScanner, node: int) -> BagItem | None:
    slot = _u32(scanner, node + _OFF_SLOT)
    amount = _u32(scanner, node + _OFF_AMOUNT)
    if slot is None or amount is None:
        return None
    if not (0 <= slot <= _MAX_SLOT) or not (0 < amount <= _MAX_AMOUNT):
        return None
    raw = scanner._read_bytes(node + _OFF_ID_TEXT, _ID_TEXT_MAX)  # noqa: SLF001
    if not raw:
        return None
    text = raw.split(b"\x00")[0].decode("ascii", "ignore")
    if not text.isdigit():
        return None
    item_id = int(text)
    if not (0 < item_id < 2_000_000):
        return None
    return BagItem(slot=slot, item_id=item_id, amount=amount)


def _best_list(scanner: MemoryScanner, container: int) -> list[BagItem]:
    """在容器附近逐一試 std::list 欄位，回傳驗得過的最長那條。"""
    best: list[BagItem] = []
    for offset in range(0, _CONTAINER_SPAN, 4):
        head = _u32(scanner, container + offset)
        if not head or not (0x10000 < head < 0xF0000000):
            continue
        nodes = _walk(scanner, head)
        if len(nodes) < _MIN_ROWS:
            continue
        rows = [_read_node(scanner, n) for n in nodes]
        good = [r for r in rows if r is not None]
        # 整條串列都要解得出來才採信 —— 半信半疑的不要
        if len(good) < len(nodes) or len({r.slot for r in good}) != len(good):
            continue
        if len(good) > len(best):
            best = good
    return sorted(best, key=lambda r: r.slot)


def read_bag(pid: int, scanner: MemoryScanner | None = None) -> list[BagItem]:
    """讀出整個背包。定位不到或驗不過就回空清單（呼叫端要大聲停用）。

    候選容器可能不只一個（改版可能新增長得一樣的骨架），所以**每個都試**、
    用資料裁決：只有一個讀得出合理背包就用它；兩個讀出**不一樣**的背包
    代表特徵已經不夠精確 —— 不賭哪個是對的，直接大聲停用。
    """
    own = scanner is None
    if scanner is None:
        scanner = MemoryScanner()
        scanner.open(pid)
    try:
        candidates = find_containers(scanner)
        if not candidates:
            log.warning("AOB 定位不到背包容器")
            return []
        found = [(c, rows) for c in candidates if (rows := _best_list(scanner, c))]
        if not found:
            return []
        if len({tuple(rows) for _c, rows in found}) > 1:
            log.error(
                "背包容器有 %d 個候選讀出不同的資料（%s），特徵已不夠精確，判定定位失敗",
                len(found), [hex(c) for c, _r in found],
            )
            return []
        container, best = found[0]
        log.info("背包讀到 %d 格（容器 %#x）", len(best), container)
        return best
    finally:
        if own:
            scanner.close()


def as_dict(pid: int, scanner: MemoryScanner | None = None) -> dict[int, tuple[int, int]]:
    """{格號: (道具編號, 數量)}。"""
    return {r.slot: (r.item_id, r.amount) for r in read_bag(pid, scanner)}
