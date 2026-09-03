"""從記憶體讀「說明小視窗現在顯示哪個道具」。

使用者指定（2026-09-04）：「我右鍵會出現該物品說明小視窗，肯定記憶體有東西
說是哪個物品」、「**不准用圖片辨識**」、「讀記憶體」、「絕對有記憶體」。
還提示了關鍵那句：「**文字也可以找找看，不一定是 ID**」——
編號與名字都找不到，**說明文找得到**（GAMEDATA [DAT-070]）。

這裡用假的記憶體把版面搭出來，釘住三件實機踩過的事：
1. 說明文與「重量」中間夾**一行空的**，往回走不能碰到空行就停
2. 客戶端**每個道具**都預先建了一模一樣的行陣列 —— 要靠區段密度分辨
3. 分不出來就回空的，不准挑一個最像的湊數
"""

from __future__ import annotations

import struct

import pytest

from ro_toolbox.services import item_window as iw

PLANT = 905        # 植物梗
JELLOPY = 909


class FakeMemory:
    """一小塊假記憶體。`regions()` / `read_region()` 跟 MemoryScanner 同介面。"""

    def __init__(self) -> None:
        self._blocks: dict[int, bytearray] = {}

    def block(self, base: int, size: int) -> None:
        self._blocks[base] = bytearray(size)

    def put(self, addr: int, data: bytes) -> None:
        for base, buf in self._blocks.items():
            if base <= addr < base + len(buf):
                off = addr - base
                buf[off:off + len(data)] = data
                return
        raise AssertionError(f"0x{addr:X} 不在任何區塊裡")

    def put_str(self, addr: int, text: str) -> None:
        self.put(addr, text.encode("cp950") + b"\x00")

    def put_ptr(self, addr: int, value: int) -> None:
        self.put(addr, struct.pack("<I", value))

    # ---- MemoryScanner 介面 ----
    def regions(self, writable_only: bool = True):
        return [(b, len(buf)) for b, buf in self._blocks.items()]

    def read_region(self, base: int, size: int):
        for b, buf in self._blocks.items():
            if b <= base < b + len(buf):
                off = base - b
                return memoryview(bytes(buf[off:off + size]))
        return None


DESC = "植物細長的梗,可以當做藥材,可向收集商購買。"
LINE1, LINE2 = "植物細長的梗,可以當做藥材,", "可向收集商購買。"


def window_at(mem: FakeMemory, record: int, strings: int, lines, weight="1") -> None:
    """在 `record` 擺一筆視窗的行記錄（間距 0x18），文字放在 `strings`。

    版面就是實機量到的：說明文各行 → 空行 → `重量 : ^777777N^000000`。
    """
    at = strings
    for i, line in enumerate([*lines, "_", f"重量 : ^777777{weight}^000000"]):
        mem.put_str(at, line)
        mem.put_ptr(record + i * iw.LINE_STRIDE, at)
        at += 0x80


@pytest.fixture
def mem() -> FakeMemory:
    m = FakeMemory()
    m.block(0x10000000, 0x2000)      # 「道具表」那一區（錨很密）
    m.block(0x20000000, 0x2000)      # 字串
    m.block(0x30000000, 0x2000)      # 視窗的行記錄
    m.block(0x40000000, 0x2000)      # 視窗的字串（錨稀疏）
    return m


# ---- 說明文 → 編號 ----------------------------------------------------------


def test_descriptions_drops_the_weight_line():
    """比對的是說明文本體 —— 重量那行是視窗自己組的，不算。"""
    table = iw.descriptions([PLANT])
    assert any("重量" not in body for body in table)


def test_descriptions_groups_items_that_share_one_text():
    """好幾樣共用同一段說明文是常態 —— 值要是清單，不能只留一個。"""
    table = iw.descriptions([PLANT, JELLOPY])
    assert all(isinstance(v, list) for v in table.values())


# ---- 讀得出來 ---------------------------------------------------------------


def test_it_reads_the_item_the_window_shows(mem):
    window_at(mem, 0x30000000, 0x40000000, [LINE1, LINE2])
    got = iw.ItemWindowReader(mem).read([PLANT, JELLOPY])
    assert got.items == (PLANT,)
    assert got.at == 0x30000000 + iw.LINE_STRIDE * 3


def test_a_blank_line_does_not_stop_the_walk(mem):
    """⚠ 實機踩過：說明文與「重量」中間夾一行空的（`_`），
    往回走碰到空字串就 break 的話，視窗那筆整個被跳過 ——
    結果認成道具表裡的**另一樣**，而且完全不會有人發現。"""
    window_at(mem, 0x30000000, 0x40000000, [LINE1, LINE2])
    reader = iw.ItemWindowReader(mem)
    bodies = reader._bodies(0x30000000 + iw.LINE_STRIDE * 3)
    assert any("可向收集商購買" in b and "植物細長" in b for b in bodies)


def test_the_item_table_is_not_mistaken_for_the_window(mem):
    """客戶端**每個道具**都預先建了一模一樣的行陣列（實機 22565 個錨）。

    區別只有一個：道具表那幾個大區段錨很密，視窗那筆在活的字串堆裡很稀疏。
    這裡把「表」塞滿錨，確認不會挑到它。
    """
    # 表：同一區段裡塞很多「重量 : ^」，並擺一筆植物梗的記錄
    at = 0x10000000
    for i in range(iw.MAX_TABLE_ANCHORS + 5):
        mem.put_str(at + i * 0x40, f"重量 : ^777777{i}^000000")
    window_at(mem, 0x20000000, 0x10000000 + 0x1000, [LINE1, LINE2])
    got = iw.ItemWindowReader(mem).read([PLANT])
    assert got.items == (), "全部命中都在道具表那種密集區＝沒有視窗開著"
    assert "說明視窗" in got.why


def test_the_window_wins_over_the_table(mem):
    """表跟視窗同時存在時，要挑**視窗**那一筆。"""
    at = 0x10000000
    for i in range(iw.MAX_TABLE_ANCHORS + 5):
        mem.put_str(at + i * 0x40, f"重量 : ^777777{i}^000000")
    window_at(mem, 0x20000000, 0x10000000 + 0x1000, [LINE1, LINE2])   # 表
    window_at(mem, 0x30000000, 0x40000000, [LINE1, LINE2])            # 視窗
    got = iw.ItemWindowReader(mem).read([PLANT])
    assert got.items == (PLANT,)
    assert got.at == 0x30000000 + iw.LINE_STRIDE * 3, "挑到的要是視窗那一筆"


# ---- 認不出來就說 -----------------------------------------------------------


def test_nothing_open_recognises_nothing(mem):
    got = iw.ItemWindowReader(mem).read([PLANT, JELLOPY])
    assert got.items == ()
    assert got.why


def test_an_item_we_are_not_carrying_is_never_returned(mem):
    """候選只有背包裡真的有的 —— 身上沒有的不該被認出來。"""
    window_at(mem, 0x30000000, 0x40000000, [LINE1, LINE2])
    got = iw.ItemWindowReader(mem).read([JELLOPY])
    assert got.items == ()


def test_no_bag_says_so(mem):
    got = iw.ItemWindowReader(mem).read([])
    assert got.items == ()
    assert "背包" in got.why


# ---- 位址提示 ---------------------------------------------------------------


def test_the_second_read_reuses_the_address(mem):
    """實機：第一次整份掃 29 秒，記住位址之後 0.05 秒。"""
    window_at(mem, 0x30000000, 0x40000000, [LINE1, LINE2])
    reader = iw.ItemWindowReader(mem)
    first = reader.read([PLANT])
    assert reader._hint == first.at
    assert reader.read([PLANT]).items == (PLANT,)
