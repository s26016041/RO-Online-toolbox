"""從記憶體讀背包（用合成的記憶體，不需要遊戲）。"""

from __future__ import annotations

import struct

import pytest

from ro_toolbox.services import bag
from ro_toolbox.services.bag import as_dict, find_container, read_bag

MODULE = 0x400000
CODE_RVA = 0x1000
CONTAINER = 0x1500000
LIST_OFF = 0x1738
HEAD = 0x6000000
NODE0 = 0x6100000
HEAD2 = 0x6001000
NODE1 = 0x6300000
NODE_SIZE = 0x80
ITEMS = [(2, 501, 7), (5, 502, 245), (50, 12345, 1)]


def _code_at(parser_rva: int, container: int) -> bytes:
    """一段自足的程式碼：`sub ecx,5` + 除以 34 的魔術乘數 + `call parser`，
    parser 裡有 `mov ecx, 容器`。

    裡面的 `call` 是 rel32（相對位移），所以整塊接在哪裡都成立 ——
    要模擬「改版多出第二段一樣的骨架」時，直接把兩塊接起來就好。
    """
    out = bytearray(0x2000)   # 要 > 0x1000，_code_section 才會採用
    body = (
        b"\x83\xe9\x05"                       # sub ecx, 5
        + b"\xb8\xf1\xf0\xf0\xf0"             # mov eax, 0xF0F0F0F1
        + b"\xf7\xe1"                         # mul ecx
        + b"\xc1\xea\x05"                     # shr edx, 5
    )
    out[0x100:0x100 + len(body)] = body
    call_at = 0x100 + len(body)
    rel = parser_rva - (call_at + 5)
    out[call_at:call_at + 5] = b"\xe8" + struct.pack("<i", rel)
    out[parser_rva:parser_rva + 5] = b"\xb9" + struct.pack("<I", container)
    return bytes(out)


def _code() -> bytes:
    return _code_at(0x400, CONTAINER)


class FakeModule:
    def __init__(self, name, base, size=0x2000):
        self.name = name
        self.base = base
        self.size = size


class FakeScanner:
    """提供最小 PE 版面、容器欄位與串列節點。"""

    def __init__(self, items=None, *, container=CONTAINER, broken_id=False):
        self.code = _code()
        self.items = ITEMS if items is None else items
        self.container = container
        self.broken_id = broken_id
        self.closed = False
        #: 第二個容器（模擬誘餌也讀得出東西）：(容器位址, 道具清單)
        self.second: tuple[int, list] | None = None

    def open(self, pid):  # noqa: ARG002
        return None

    def list_modules(self):
        return [FakeModule("ragexe.exe", MODULE)]

    def module_base(self, name):
        # 正式版走這條（模組列舉會被 GameGuard 擋，見 aob.code_section）。
        return MODULE if name.lower() == "ragexe.exe" else None

    def close(self):
        self.closed = True

    # ---- 記憶體版面 ----

    def _node(self, i):
        return NODE0 + i * NODE_SIZE

    def _lists(self):
        """[(容器, 串列頭, 節點起點, 道具清單)]。第二個是模擬誘餌也讀得出東西。"""
        out = [(self.container, HEAD, NODE0, self.items)]
        if self.second is not None:
            container, items = self.second
            out.append((container, HEAD2, NODE1, items))
        return out

    def _read_bytes(self, addr, size):  # noqa: C901
        out = bytearray(size)

        def put(off, data):
            """把 data 貼到相對位移 off；off 可以是負的（表示從資料中間開始讀）。"""
            if off >= size:
                return
            if off < 0:
                data = data[-off:]
                off = 0
                if not data:
                    return
            end = min(size, off + len(data))
            out[off:end] = data[: end - off]

        if addr <= MODULE < addr + size or MODULE <= addr < MODULE + 0x400:
            head = bytearray(0x400)
            struct.pack_into("<I", head, 0x3C, 0x80)
            put(MODULE - addr, bytes(head))
        if MODULE + 0x80 <= addr < MODULE + 0x200:
            pe = bytearray(0x120)
            struct.pack_into("<H", pe, 6, 1)
            struct.pack_into("<H", pe, 20, 0xE0)
            put(MODULE + 0x80 - addr, bytes(pe))
        table = MODULE + 0x80 + 24 + 0xE0
        if table <= addr < table + 40:
            row = bytearray(40)
            row[:8] = b".text\x00\x00\x00"
            struct.pack_into("<II", row, 8, len(self.code), CODE_RVA)
            struct.pack_into("<I", row, 36, 0x20000000)
            put(table - addr, bytes(row))
        if MODULE + CODE_RVA <= addr < MODULE + CODE_RVA + len(self.code):
            put(MODULE + CODE_RVA - addr, self.code)
        for container, head, node0, items in self._lists():
            if addr == container + LIST_OFF:
                put(0, struct.pack("<I", head))
            if addr == head:
                put(0, struct.pack("<I", node0 if items else head))
            for i, (slot, item_id, amount) in enumerate(items):
                node = node0 + i * NODE_SIZE
                if node <= addr < node + NODE_SIZE:
                    block = bytearray(NODE_SIZE)
                    nxt = node0 + (i + 1) * NODE_SIZE if i + 1 < len(items) else head
                    struct.pack_into("<I", block, 0x00, nxt)
                    struct.pack_into("<I", block, 0x0C, slot)
                    struct.pack_into("<I", block, 0x18, amount)
                    text = b"nope" if self.broken_id else str(item_id).encode()
                    block[0x34:0x34 + len(text)] = text
                    put(node - addr, bytes(block))
        return bytes(out[:size]) if any(out) else bytes(out[:size])


@pytest.fixture
def scanner(monkeypatch):
    fake = FakeScanner()
    monkeypatch.setattr(bag, "MemoryScanner", lambda: fake)
    return fake


def test_finds_the_container_by_aob(scanner):
    """容器位址是從指令的立即值讀出來的，沒有寫死。"""
    assert find_container(scanner) == CONTAINER


def test_reads_every_slot(scanner):  # noqa: ARG001
    rows = read_bag(1234)
    assert [(r.slot, r.item_id, r.amount) for r in rows] == ITEMS


def test_item_id_comes_from_a_decimal_string(scanner):  # noqa: ARG001
    """編號存的是十進位**字串**（`"502"`），不是數字 —— 這就是先前找不到的原因。"""
    assert as_dict(1234)[5] == (502, 245)


def test_rows_are_sorted_by_slot(monkeypatch):
    # 至少要 3 格才會被採信（_MIN_ROWS），所以這裡放 3 個亂序的
    fake = FakeScanner(items=[(50, 12345, 1), (2, 501, 7), (9, 512, 4)])
    monkeypatch.setattr(bag, "MemoryScanner", lambda: fake)
    assert [r.slot for r in read_bag(1234)] == [2, 9, 50]


def test_unreadable_id_rejects_the_whole_list(monkeypatch):
    """整條串列都要解得出來才採信 —— 半信半疑的不要。"""
    fake = FakeScanner(broken_id=True)
    monkeypatch.setattr(bag, "MemoryScanner", lambda: fake)
    assert read_bag(1234) == []


def test_returns_empty_when_the_container_is_not_found(monkeypatch):
    fake = FakeScanner()
    fake.code = b"\x90" * 0x800          # 沒有那段指令骨架
    monkeypatch.setattr(bag, "MemoryScanner", lambda: fake)
    assert find_container(fake) is None
    assert read_bag(1234) == []


def test_one_pids_missing_bag_is_not_reset_by_another_pids_success(monkeypatch, caplog):
    """★ [DAT-078]：多開時「AOB 定位不到背包容器」每 3 秒噴一行的成因。

    降噪的 `StateLog` 以前是**全域共用一份**：一隻角色找到容器就 `.ok()` 把
    去重狀態清成 None，另一隻找不到的下一拍又用 WARNING 重講一次 —— 兩隻互相
    洗掉彼此的去重，等於沒降噪。改成每 pid 一份之後，找不到的那隻只吼一次。
    """
    import logging as _logging

    bag._notes.clear()
    empty = FakeScanner()
    empty.code = b"\x90" * 0x800                # PID 1：找不到容器
    good = FakeScanner()                        # PID 2：正常
    holder = {}
    monkeypatch.setattr(bag, "MemoryScanner", lambda: holder["s"])

    with caplog.at_level(_logging.WARNING):
        for _ in range(5):
            holder["s"] = empty
            read_bag(1)                          # 找不到的那一隻
            holder["s"] = good
            read_bag(2)                          # 正常的那一隻（會 .ok()）
    warns = [r for r in caplog.records
             if r.levelno >= _logging.WARNING and "定位不到背包" in r.getMessage()]
    assert len(warns) == 1, f"找不到的那一隻只准吼一次，卻吼了 {len(warns)} 次"


def test_closes_the_scanner_it_opened(scanner):
    read_bag(1234)
    assert scanner.closed is True


# ---- 候選容器不只一個時 -------------------------------------------------


def test_it_can_see_more_than_one_candidate(scanner):
    """定位器要看得見自己有歧義 —— 只回第一個的話，改版新增一段一樣的
    骨架時它會安靜地拿別人家的全域。"""
    from ro_toolbox.services.bag import find_containers

    decoy = 0x2000000
    scanner.code = _code() + _code_at(0x1000, decoy)
    got = find_containers(scanner)
    assert got == [CONTAINER, decoy]


def test_a_candidate_that_reads_nothing_is_just_ignored(scanner):
    """誘餌讀不出合理背包 → 安靜略過它，真的那個照樣讀得到。"""
    scanner.code = _code() + _code_at(0x1000, 0x2000000)
    assert [(r.slot, r.item_id, r.amount) for r in read_bag(1234)] == ITEMS


def test_two_candidates_with_different_bags_disable_loudly(scanner, caplog):
    """兩個候選都讀得出**不一樣**的背包 = 特徵不夠精確，不賭哪個對。"""
    other = 0x2000000
    scanner.code = _code() + _code_at(0x1000, other)
    scanner.second = (other, [(1, 909, 3), (4, 512, 8), (9, 501, 2)])
    with caplog.at_level("ERROR"):
        assert read_bag(1234) == []
    assert "判定定位失敗" in caplog.text


# ---- BagWatch（高頻輪詢用的快路徑）------------------------------------


def test_watch_reads_the_same_bag_as_a_full_read(scanner):  # noqa: ARG001
    """綁定之後讀到的內容要跟慢路徑一模一樣，不能為了快而讀得比較差。"""
    watch = bag.BagWatch(1234)
    assert watch.open() is True
    assert watch.snapshot() == bag.as_dict(1234)
    watch.close()


def test_watch_does_not_rescan_on_every_snapshot(scanner, monkeypatch):  # noqa: ARG001
    """這就是它存在的理由：綁定一次，之後不再重跑 AOB 掃描。

    慢路徑每次呼叫都要掃一次（約 0.1 秒），喝水每瓶都要現查格號，
    用慢路徑等於每瓶多花 0.1 秒。
    """
    watch = bag.BagWatch(1234)
    assert watch.open() is True
    scans = 0
    real = bag.find_containers

    def counted(sc):
        nonlocal scans
        scans += 1
        return real(sc)

    monkeypatch.setattr(bag, "find_containers", counted)
    for _ in range(20):
        assert watch.snapshot()
    assert scans == 0, f"綁定之後不該再掃描，實際掃了 {scans} 次"
    watch.close()


def test_watch_reports_failure_instead_of_stale_data(scanner):
    """串列走不通就回空的 —— 呼叫端才有機會重新定位，不是拿舊資料硬撐。"""
    watch = bag.BagWatch(1234)
    assert watch.open() is True
    assert watch.snapshot()
    scanner.items = []            # 串列變空 = 綁定過期
    assert watch.snapshot() == {}
    watch.close()


def test_watch_open_fails_when_the_container_is_not_found(monkeypatch):
    """定位不到就要老實回 False，不能回一個「空背包」讓人以為讀成功了。"""
    fake = FakeScanner()
    fake.code = bytes(len(fake.code))      # 特徵整個不見
    monkeypatch.setattr(bag, "MemoryScanner", lambda: fake)
    watch = bag.BagWatch(1234)
    assert watch.open() is False
    assert watch.snapshot() == {}
    assert fake.closed is True, "定位失敗要把自己開的 scanner 關掉"

