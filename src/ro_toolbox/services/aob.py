"""AOB 特徵碼定位：用「值周圍穩定的位元組樣式」在動態記憶體中定位一個值。

跟指標路徑不同 —— 不靠固定位址、也不靠指標鏈，而是靠一段 masked 位元組樣式
（?? = 萬用字元）去掃描記憶體，命中後加一個固定偏移就是目標值。適合那些「掛在
動態結構、沒有穩定指標路徑」的值（天使之戀的技能經驗球就是這種）。

用法：
    from ro_toolbox.services.aob import AOBSignature, scan
    from ro_toolbox.services.memory_scan import MemoryScanner
    sc = MemoryScanner(); sc.open(pid)
    for addr in scan(sc, MY_SIGNATURE):
        print(sc.read_value(addr, MY_SIGNATURE.vt))

特徵怎麼來的（開發期，只做一次）：用多個分身比對某值周圍的位元組，相同→固定、
不同→萬用；再驗證它能唯一定位到目標（用多個分身／多次重開比對，再驗證唯一性）。

移植自 `s26016041/Angels-Online-toolbox` 的 `app/game/aob.py`。
"""
from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass

from ro_toolbox.services.memory_scan import VALUE_TYPES, ValueType

log = logging.getLogger(__name__)

#: PE 區段旗標：這一段可以執行（`IMAGE_SCN_MEM_EXECUTE`）。
_IMAGE_SCN_MEM_EXECUTE = 0x20000000
#: 可執行區段的合理上限。比這大就不是程式碼段，是解析錯了。
_MAX_CODE_SECTION = 32 << 20


@dataclass(frozen=True)
class AOBSignature:
    """一段 AOB 特徵：masked 位元組樣式 + 「樣式起點到目標值」的偏移。"""

    pattern: str          # 例 "01 ?? ?? 09 ... FF FF"，?? = 萬用
    value_offset: int     # 從樣式起點到目標值的位元組偏移
    vt_key: str = "int32"
    label: str = ""

    @property
    def vt(self) -> ValueType:
        return VALUE_TYPES[self.vt_key]

    def parse(self) -> tuple[bytes, bytes]:
        """把樣式字串轉成 (sig, mask)：mask[i]=1 表示該位元組固定。"""
        toks = self.pattern.split()
        sig = bytearray(len(toks))
        mask = bytearray(len(toks))
        for i, t in enumerate(toks):
            if t in ("??", "?"):
                mask[i] = 0
            else:
                sig[i] = int(t, 16)
                mask[i] = 1
        return bytes(sig), bytes(mask)


def scan(scanner, aob: AOBSignature, writable_only: bool = True, limit: int = 64,
         should_stop=None) -> list[int]:
    """在程序記憶體找 AOB 樣式，回傳 [目標值位址, ...]。

    以「最長連續固定位元組」當搜尋錨點快速定位候選，再逐一驗證整段 masked 樣式。

    should_stop: 可選的 callable，每掃一個記憶體區塊前呼叫一次；回傳 True 就中止並
    回傳目前找到的（不完整）結果。全掃一個分身要 1～2.5 秒，沒有這個中止點的話，
    要求它停止的人（例如關閉程式、觸發警報）就得整整等它掃完。
    """
    sig, mask = aob.parse()
    n = len(sig)
    # 找最長連續固定當搜尋錨點
    best_off, best_len, run = 0, 0, None
    for k in range(n + 1):
        if k < n and mask[k]:
            run = k if run is None else run
        else:
            if run is not None and k - run > best_len:
                best_off, best_len = run, k - run
            run = None
    anchor = sig[best_off:best_off + best_len]
    if not anchor:
        return []

    hits: list[int] = []
    for base, size in scanner._iter_regions(writable_only=writable_only):
        if should_stop is not None and should_stop():
            return hits
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        # memoryview 沒有 .find（見 _read_region）。複製一次的成本跟以前
        # 一模一樣 —— 以前那份複製是在 _read_region 裡面做的。
        raw = bytes(raw)
        s = 0
        while len(hits) < limit:
            i = raw.find(anchor, s)
            if i < 0:
                break
            start = i - best_off
            if start >= 0 and start + n <= len(raw):
                seg = raw[start:start + n]
                if all(m == 0 or seg[k] == sig[k] for k, m in enumerate(mask)):
                    hits.append(base + start + aob.value_offset)
            s = i + 1
        if len(hits) >= limit:
            break
    return hits


# ---------------------------------------------------------------------------
# 已找到並驗證過的 AOB 特徵登錄表
# ---------------------------------------------------------------------------
# 目前是空的。RO 的特徵都還沒生成。
#
# 新增一條特徵時必須做到（見專案 CLAUDE.md）：
#   1. 用多次重開遊戲／多個分身比對，相同的位元組才留成固定，其餘遮成 ??。
#   2. 特徵裡不准把答案寫死：模組內 4-byte 立即值與 rel32 一律遮掉，
#      靠指令骨架當錨——否則換個數值就找不到了。
#   3. 驗證唯一性：整個行程只能命中預期的那些位址。
#   4. 記進 GAMEDATA 的 MEM 條目，寫明怎麼生成、拿什麼驗的、驗證日期。
#
# 範例格式（實際特徵請照上面流程生成，不要抄別的遊戲的）：
#
#   EXAMPLE = AOBSignature(
#       pattern="01 ?? ?? ?? FF FF FF FF ?? ?? ?? ??",
#       value_offset=0x10,
#       vt_key="int32",
#       label="範例",
#   )


def code_section(scanner, module_name: str = "ragexe.exe") -> tuple[int, bytes] | None:
    """讀出某個模組的第一段可執行區段，回 `(起始位址, 內容)`。AOB 就掃這一段。

    ⚠ **基底不要用 `list_modules()` 拿。** GameGuard 會擋模組列舉（[MEM-031]），
    被擋的時候它回空清單，於是所有靠它的定位器一起「找不到」——
    而遊戲其實好好的（實際踩過：登入之後一直「AOB 定位不到背包容器」）。
    `module_base()` 對 `.exe` 有掃記憶體的退路，不經過會被擋的 API。

    區段是自己從記憶體裡的 PE 表頭解出來的（`IMAGE_SCN_MEM_EXECUTE`），
    不靠任何寫死的位址或大小。
    """
    base = scanner.module_base(module_name)
    if base is None:
        return None
    head = scanner._read_bytes(base, 0x400)  # noqa: SLF001
    if not head or len(head) < 0x40:
        return None
    e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
    pe = scanner._read_bytes(base + e_lfanew, 0x120)  # noqa: SLF001
    if not pe or len(pe) < 24:
        return None
    count = struct.unpack_from("<H", pe, 6)[0]
    opt_size = struct.unpack_from("<H", pe, 20)[0]
    table = base + e_lfanew + 24 + opt_size
    for i in range(count):
        row = scanner._read_bytes(table + i * 40, 40)  # noqa: SLF001
        if not row or len(row) < 40:
            continue
        vsize, vaddr = struct.unpack_from("<II", row, 8)
        chars = struct.unpack_from("<I", row, 36)[0]
        if chars & _IMAGE_SCN_MEM_EXECUTE and 0x1000 < vsize <= _MAX_CODE_SECTION:
            blob = scanner._read_bytes(base + vaddr, vsize)  # noqa: SLF001
            if blob:
                return base + vaddr, blob
    return None


@dataclass(frozen=True, slots=True)
class CodeSignature:
    """用**指令骨架**在程式碼裡找一個全域變數的位址。

    `pattern` 是十六進位樣式，`??` 代表「這個 byte 不比對」——
    **答案本身一律遮掉**（CLAUDE.md：特徵裡不准把答案寫死），
    位址是從命中的位置把立即值讀出來的，不是寫在特徵裡的。

    `operands` 是要從命中的起點算起、哪幾個位移上讀 4-byte 立即值。
    同一個骨架裡讀得到好幾個時（例如一段迴圈重複引用同一個全域），
    它們**必須全部相等** —— 那是這條特徵自帶的一致性檢查。
    """

    name: str
    pattern: str
    operands: tuple[int, ...]
    #: 這個骨架是什麼指令、為什麼挑它。改版壞掉時要靠這段話重找。
    why: str = ""

    def compiled(self) -> re.Pattern[bytes]:
        out = b""
        for token in self.pattern.split():
            out += b"." if token == "??" else re.escape(bytes([int(token, 16)]))
        return re.compile(out, re.S)


def image_size(scanner, module_name: str = "ragexe.exe") -> int | None:
    """從記憶體裡的 PE 表頭讀 `SizeOfImage`（模組佔多大）。

    用途是判斷「這個立即值是不是模組自己的位址」。用猜的範圍會兩頭錯：
    放太寬會把垃圾值當成答案，放太窄會把好的答案擋掉（踩過）。
    """
    base = scanner.module_base(module_name)
    if base is None:
        return None
    head = scanner._read_bytes(base, 0x400)  # noqa: SLF001
    if not head or len(head) < 0x40:
        return None
    e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
    pe = scanner._read_bytes(base + e_lfanew, 0x120)  # noqa: SLF001
    if not pe or len(pe) < 24 + 60:
        return None
    # 可選標頭從 PE 簽章 +24 開始，SizeOfImage 在它的 +56。
    size = struct.unpack_from("<I", pe, 24 + 56)[0]
    return size if 0x1000 <= size <= (512 << 20) else None


def locate_global(
    scanner, signatures, module_name: str = "ragexe.exe"
) -> int | None:
    """用一組特徵在程式碼裡定位同一個全域。找不到／對不上一律回 None。

    規則（照 CLAUDE.md「定位失敗要大聲」）：

    - 每條特徵可以命中很多處，但**讀出來的位址必須全部一樣**。
    - 不同特徵之間也必須一樣（互相獨立的骨架＝互相驗證）。
    - 位址必須落在模組自己的映像範圍內（不然就是解錯了）。
    - 只要有一項不成立就回 None，並記一筆 error —— 不准挑一個用。
    """
    section = code_section(scanner, module_name)
    if section is None:
        log.error("定位全域失敗：讀不到 %s 的程式碼區段", module_name)
        return None
    sec_base, blob = section
    base = scanner.module_base(module_name)
    span = image_size(scanner, module_name)
    if base is None or span is None:
        log.error("定位全域失敗：拿不到 %s 的模組基底或映像大小", module_name)
        return None

    answers: dict[str, set[int]] = {}
    for sig in signatures:
        found: set[int] = set()
        for match in sig.compiled().finditer(blob):
            for off in sig.operands:
                start = match.start() + off
                value = int.from_bytes(blob[start:start + 4], "little")
                if base <= value < base + span:
                    found.add(value)
                else:
                    log.error(
                        "特徵「%s」在 %#x 讀到的位址 %#x 不在模組範圍內",
                        sig.name, sec_base + match.start(), value,
                    )
                    return None
        if found:
            answers[sig.name] = found

    if not answers:
        log.error("定位全域失敗：%s 一條特徵都沒命中（遊戲可能已改版）",
                  "／".join(s.name for s in signatures))
        return None
    everything = set().union(*answers.values())
    if len(everything) != 1:
        log.error("定位全域失敗：各條特徵讀出來的位址不一致 %s",
                  {k: [hex(v) for v in sorted(vs)] for k, vs in answers.items()})
        return None
    return everything.pop()
