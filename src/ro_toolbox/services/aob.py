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

from dataclasses import dataclass

from ro_toolbox.services.memory_scan import VALUE_TYPES, ValueType


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
