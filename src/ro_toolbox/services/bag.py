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

from ro_toolbox.utils.logging import StateLog

from .aob import code_section
from .memory_scan import MemoryScanner

log = logging.getLogger(__name__)

#: 這支被輪詢得很兇（每一秒多一次），失敗訊息要降噪 —— 見 StateLog。
_notes = StateLog(log)
#: 成功訊息同理：只在「容器位址／格數」變動時講一次。
_reads = StateLog(log)

#: 「除以 34」的魔術乘數。34 是背包清單封包的記錄大小（[PKT-039]）。
#: 這是指令骨架的錨，不是答案 —— 容器位址是從指令的立即值讀出來的。
_MAGIC_DIV34 = b"\xb8\xf1\xf0\xf0\xf0"      # mov eax, 0xF0F0F0F1
_SUB_5 = b"\x83\xe9\x05"                    # sub ecx, 5
#: `shr edx, 5` —— 除以 34 的最後一步。
#:
#: ⚠ **這一步不能省。** 同一個魔術乘數（0xF0F0F0F1）配 `shr edx, 4` 是**除以 17**，
#: 那是另一段完全無關的程式碼。少了這個條件，定位器會多命中一個
#: 「除以 17」的站點並推出一個假容器（實測：真的 0x15D2AC8 之外
#: 還冒出 0x11F0758，那個讀出來 0 筆）。目前是靠「讀不出資料」才被裁掉的 ——
#: 但只要哪天那個假容器剛好讀得出像樣的東西，整個功能就會**大聲停用**。
#: 骨架自己分得開，就不必賭資料。
_SHR_EDX_5 = b"\xc1\xea\x05"

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


def find_container_sites(scanner: MemoryScanner) -> list[tuple[int, int, int]]:
    """AOB 定位背包容器，回傳每一處命中的 `(骨架位址, 解析函式, 容器)`。

    為什麼要回**全部**而不是第一個：只回第一個的話，定位器就**看不見自己有歧義**。
    改版新增一段長得一樣的骨架時，它會安靜地挑到別人家的全域。
    有幾個候選要由呼叫端裁決（見 `read_bag`：兩個候選都讀得出合理背包就大聲停用）。

    為什麼連骨架位址與解析函式一起回：`tools/verify_sigs.py` 要拿它們做改版模擬
    （把容器立即值改掉，看定位器有沒有跟著改）。它以前自己重寫了一份一樣的邏輯 ——
    然後這裡收緊骨架時它沒跟上，報告就開始說謊。**一份實作，兩邊共用。**
    """
    section = code_section(scanner)
    if section is None:
        return []
    base, blob = section
    found: list[tuple[int, int, int]] = []
    seen: set[int] = set()
    start = 0
    while True:
        k = blob.find(_MAGIC_DIV34, start)
        if k < 0:
            return found
        start = k + 1
        # 這個魔術乘數在別處也會出現，要求附近有 `sub ecx, 5`（扣掉封包標頭）
        if _SUB_5 not in blob[max(0, k - 0x20) : k + 0x20]:
            continue
        span = blob[k : k + 0x40]
        # 而且要真的是**除以 34**（magic + shr 5），不是除以 17（magic + shr 4）。
        if _SHR_EDX_5 not in span:
            continue
        # 往後找第一個 call rel32 —— 那是解析函式
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
                # ⚠ DEBUG：這支每秒多會被叫一次，而且一次可能命中好幾個候選。
                # 用 INFO 的話光這一行就把執行日誌洗滿（使用者實際回報）。
                # 真正該講的是下面「讀到幾格」那一行，而且只在變動時講。
                log.debug("背包容器 AOB 命中：0x%X（解析函式 0x%X）", value, parser)
                if value not in seen:
                    seen.add(value)
                    found.append((base + k, parser, value))
                break


def find_containers(scanner: MemoryScanner) -> list[int]:
    """所有通過骨架驗證的候選容器（去重、保持順序）。"""
    return [container for _site, _parser, container in find_container_sites(scanner)]


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


def _read_list(scanner: MemoryScanner, head_at: int) -> list[BagItem]:
    """讀出 `head_at` 這個 std::list 欄位指到的串列。驗不過就回空清單。

    ⚠ 只有**整條**都解得出來、格號不重複才採信 —— 半信半疑的不要。
    """
    head = _u32(scanner, head_at)
    if not head or not (0x10000 < head < 0xF0000000):
        return []
    nodes = _walk(scanner, head)
    if len(nodes) < _MIN_ROWS:
        return []
    rows = [_read_node(scanner, n) for n in nodes]
    good = [r for r in rows if r is not None]
    if len(good) < len(nodes) or len({r.slot for r in good}) != len(good):
        return []
    return sorted(good, key=lambda r: r.slot)


def _best_site(scanner: MemoryScanner, container: int) -> tuple[int, list[BagItem]]:
    """在容器附近逐一試 std::list 欄位，回傳（偏移, 驗得過的最長那條）。"""
    best_off = -1
    best: list[BagItem] = []
    for offset in range(0, _CONTAINER_SPAN, 4):
        rows = _read_list(scanner, container + offset)
        if len(rows) > len(best):
            best_off, best = offset, rows
    return best_off, best


def _best_list(scanner: MemoryScanner, container: int) -> list[BagItem]:
    return _best_site(scanner, container)[1]


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
            # 這支每一秒多就會被叫一次 —— 照實記會把日誌洗成幾百行一樣的字。
            _notes.problem(
                "no-container", logging.WARNING,
                "AOB 定位不到背包容器（還沒進到遊戲裡也會這樣）",
            )
            return []
        site = _locate(scanner, candidates)
        if site is None:
            return []
        container, offset, best = site
        # ⚠ 只在**變動時**講一次。這支每秒多一次，照記就是每秒一行
        # 「背包讀到 40 格」把日誌洗掉（使用者實際回報）。
        _reads.changed(
            f"{container:#x}+{offset:#x}:{len(best)}", logging.INFO,
            "背包讀到 %d 格（容器 %#x + %#x）", len(best), container, offset,
        )
        return best
    finally:
        if own:
            scanner.close()


def _locate(
    scanner: MemoryScanner, candidates: list[int]
) -> tuple[int, int, list[BagItem]] | None:
    """在候選容器裡裁決出唯一一個真的背包，回 (容器, 偏移, 內容)。

    候選可能不只一個（改版可能新增長得一樣的骨架），所以**每個都試**、
    用資料裁決：只有一個讀得出合理背包就用它；兩個讀出**不一樣**的背包
    代表特徵已經不夠精確 —— 不賭哪個是對的，直接大聲停用。
    """
    found: list[tuple[int, int, list[BagItem]]] = []
    for container in candidates:
        offset, rows = _best_site(scanner, container)
        if rows:
            found.append((container, offset, rows))
    if not found:
        return None
    if len({tuple(rows) for _c, _o, rows in found}) > 1:
        log.error(
            "背包容器有 %d 個候選讀出不同的資料（%s），特徵已不夠精確，判定定位失敗",
            len(found), [hex(c) for c, _o, _r in found],
        )
        return None
    _notes.ok("背包容器又定位到了")
    return found[0]


class BagWatch:
    """綁定一次背包串列，之後只重走它 —— 給**高頻輪詢**用。

    為什麼需要：`as_dict()` 每次呼叫都重跑一次 AOB 掃描，再試 2048 個偏移
    去找串列（實測 **22 ms**，34 格的背包）。自動補水每喝一瓶都要現查格號
    （[MEM-028]），用 `as_dict()` 等於每瓶白花這 22 ms。
    綁定過的串列只重走幾十個節點：實測 **0.48 ms**，快約 46 倍。

    ⚠ **只記住「容器位址 + 串列頭在容器裡的偏移」**（容器本身是 AOB 定位到的
    全域），節點位址每次現走 —— 節點是動態配置的，記下來就會過期。
    每次都做跟定位當下同樣的驗證，走不通就回 None，呼叫端要重新定位。
    """

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._scanner: MemoryScanner | None = None
        self._head_at: int | None = None

    def open(self) -> bool:
        """定位一次。成功之後 `snapshot()` 才有東西可讀。"""
        self.close()
        scanner = MemoryScanner()
        scanner.open(self._pid)   # 開不起來時讀取會回 None，下面的定位就會失敗
        site = _locate(scanner, find_containers(scanner))
        if site is None:
            scanner.close()
            return False
        container, offset, _rows = site
        self._scanner = scanner
        self._head_at = container + offset
        log.info("背包串列綁定於 %#x（容器 %#x + %#x）",
                 self._head_at, container, offset)
        return True

    def snapshot(self) -> dict[int, tuple[int, int]]:
        """{格號: (道具編號, 數量)}。驗不過回空的 —— 呼叫端要重新定位。"""
        if self._scanner is None or self._head_at is None:
            return {}
        rows = _read_list(self._scanner, self._head_at)
        return {r.slot: (r.item_id, r.amount) for r in rows}

    def close(self) -> None:
        if self._scanner is not None:
            self._scanner.close()
        self._scanner = None
        self._head_at = None


def as_dict(pid: int, scanner: MemoryScanner | None = None) -> dict[int, tuple[int, int]]:
    """{格號: (道具編號, 數量)}。"""
    return {r.slot: (r.item_id, r.amount) for r in read_bag(pid, scanner)}
