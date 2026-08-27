"""AOB 特徵驗證：**唯一性檢查**與**改版模擬**。

CLAUDE.md 要求所有位址一律 AOB 定位，而 AOB 有兩種安靜的壞法：

1. **命中不只一個** —— 定位器挑了第一個，剛好不是要的那個。
2. **改版後認錯目標** —— 骨架被小幅改動，定位器沒有失敗，而是回一個
   看起來合理、其實是別處的位址。

這兩種都不會拋例外、不會有警告，只會讓功能**安靜地做錯事**。
這支工具就是專門把它們逼出來的。

用法：

    py tools/verify_sigs.py                 # 自動找所有 Ragexe.exe
    py tools/verify_sigs.py --pid 1234      # 指定行程（可重複）
    py tools/verify_sigs.py --skip-live     # 只跑改版模擬

主控台只印結論，完整明細寫到 `reports/verify_sigs-<時間>.md`。

**唯一性檢查**（對執行中的行程）
  - 角色狀態：AOB 命中幾個都可以，但**通過合理性驗證的只能有 1 個**。
    （原始 AOB 唯一是錯的判準：堆積裡會有同樣位元組樣式的垃圾，見 [MEM-041]。）
  - 角色座標：`POSITION_X/Y_SIGS` 各要定位成功，而且 **y 必須是 x+4**。
  - 導航目標全域：`NAVI_DEST_SIGS` 要定位成功。
  - 送出帳號緩衝：`SUBMITTED_ACCOUNT_SIGS` 要定位成功（自動登入的閉環驗證靠它）。
  - 背包容器骨架：`sub ecx,5` + 除以 34 的魔術乘數，只能解出一個容器。
  - 封包長度表：`mov ecx,esi; call` 的最熱門目標要**明顯**領先第二名。

**改版模擬**（對程式碼快照做離線變形，可重現）
  每一項的判準只有一個：**要嘛答對，要嘛大聲失敗**（回 None／空 dict）。
  回一個「錯的位址」一律算不合格。

  - 整段程式碼往後挪 N 位元組（函式搬家、插入指令）→ 仍要答對。
  - 模組重新配置（base 位移）→ 要跟著位移，不能回舊值。
  - 容器換到別的全域（立即值改變）→ 要回**新**值（證明沒有寫死）。
  - 骨架被改掉（魔術乘數／`sub ecx,5` 不見）→ 必須失敗。
  - 出現第二組長得一樣的骨架（誘餌）→ 不可以安靜地挑到誘餌。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ro_toolbox.services import bag, packet_table  # noqa: E402
from ro_toolbox.services.aob import locate_global, scan  # noqa: E402
from ro_toolbox.services.character import CharacterReader  # noqa: E402
from ro_toolbox.services.memory_scan import MemoryScanner  # noqa: E402
from ro_toolbox.services.signatures import (  # noqa: E402
    CHAR_STATUS,
    NAVI_DEST_SIGS,
    POSITION_X_SIGS,
    POSITION_XY_GAP,
    POSITION_Y_SIGS,
    SELECT_CURSOR_SIGS,
    SELECT_NAME_SIGS,
    SUBMITTED_ACCOUNT_SIGS,
)

GAME = "ragexe.exe"


# ---------------------------------------------------------------------------
# 結果收集
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str          # "OK" | "NG" | "--"（略過）
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == "OK"


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self._put(name, "OK" if passed else "NG", detail)

    def skip(self, name: str, why: str) -> None:
        """前提不成立而沒跑成的檢查。**不算不合格，但也不算通過** ——
        混進通過數裡會讓報告看起來比實際乾淨。"""
        self._put(name, "--", why)

    def _put(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))
        show = detail if status != "OK" and detail else ""
        print(f"  {status} {name}" + (f" -- {show}" if show else ""))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "NG"]

    @property
    def skipped(self) -> list[Check]:
        return [c for c in self.checks if c.status == "--"]


# ---------------------------------------------------------------------------
# 快照：把程式碼區段抓下來，之後所有變形都在這上面做（離線、可重現）
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """一個行程的 PE 版面 + 主程式碼區段。"""

    module_base: int
    head: bytes           # module_base 起 0x400
    pe: bytes             # module_base + e_lfanew 起 0x120
    sections: bytes       # 區段表原文
    section_table: int    # 區段表的絕對位址
    text_base: int
    text: bytes


class SnapshotScanner:
    """假的 scanner：只服務快照涵蓋的位址。

    介面刻意只做 `bag` 與 `packet_table` 真正會用到的那幾個
    （`module_base` / `list_modules` / `_read_bytes` / `close`）。多做的部分
    沒有真實資料當靠山，反而會讓模擬結果不能信。
    """

    class _Module:
        def __init__(self, name: str, base: int) -> None:
            self.name = name
            self.base = base

    def __init__(self, snap: Snapshot, *, rebase: int = 0) -> None:
        self._snap = snap
        self._delta = rebase
        self.closed = False

    @property
    def _base(self) -> int:
        return self._snap.module_base + self._delta

    def module_base(self, name: str) -> int | None:
        """跟真的 `MemoryScanner` 同名同形 —— `aob.code_section` 走這條。

        ⚠ 這裡不能做成 property。正式版是**方法**（模組列舉被 GameGuard
        擋住時它會改用掃描），假的做成屬性就會在模擬時炸掉，
        而那正是我們要用它來擋下的那種「兩邊不一致」。
        """
        return self._base if name.lower() == GAME.lower() else None

    def list_modules(self):
        return [self._Module(GAME, self._base)]

    def close(self) -> None:
        self.closed = True

    def _chunks(self):
        s, d = self._snap, self._delta
        e_lfanew = struct.unpack_from("<I", s.head, 0x3C)[0]
        return (
            (s.module_base + d, s.head),
            (s.module_base + d + e_lfanew, s.pe),
            (s.section_table + d, s.sections),
            (s.text_base + d, s.text),
        )

    def _read_bytes(self, addr: int, size: int) -> bytes:
        # 區塊會重疊（e_lfanew < 0x400 時，PE 標頭與區段表都落在 head 裡面），
        # 所以挑**最窄**的那個 —— 挑到大的雖然位址也對得上，但拿到的是
        # 另一次讀取的內容，錯得很安靜。
        fits = [(start, data) for start, data in self._chunks()
                if start <= addr and addr + size <= start + len(data)]
        if not fits:
            return b""
        start, data = min(fits, key=lambda c: len(c[1]))
        return bytes(data[addr - start : addr - start + size])


def capture(pid: int) -> Snapshot | None:
    """從執行中的行程抓一份快照。讀不到就回 None。"""
    scanner = MemoryScanner()
    scanner.open(pid)
    try:
        module = next(
            (m for m in scanner.list_modules() if m.name.lower() == GAME), None
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
        sections = scanner._read_bytes(table, count * 40)  # noqa: SLF001
        if not sections or len(sections) < count * 40:
            return None
        for i in range(count):
            vsize, vaddr = struct.unpack_from("<II", sections, i * 40 + 8)
            chars = struct.unpack_from("<I", sections, i * 40 + 36)[0]
            if chars & 0x20000000 and 0x1000 < vsize <= (24 << 20):
                text = scanner._read_bytes(module.base + vaddr, vsize)  # noqa: SLF001
                if not text:
                    return None
                return Snapshot(
                    module_base=module.base, head=head, pe=pe, sections=sections,
                    section_table=table, text_base=module.base + vaddr, text=text,
                )
        return None
    finally:
        scanner.close()


_MAGIC = b"ROSNAP01"


def save_snapshot(snap: Snapshot, path: Path) -> None:
    """把快照存成檔案。

    存快照的理由：改版模擬**不需要遊戲在跑**，但要有一份真的程式碼。
    遊戲維修／關閉時仍然要能重跑驗證，所以開著的時候先存一份下來。
    """
    import json

    header = json.dumps({
        "module_base": snap.module_base,
        "section_table": snap.section_table,
        "text_base": snap.text_base,
        "sizes": [len(snap.head), len(snap.pe), len(snap.sections), len(snap.text)],
    }).encode("utf-8")
    with path.open("wb") as fh:
        fh.write(_MAGIC)
        fh.write(struct.pack("<I", len(header)))
        fh.write(header)
        for blob in (snap.head, snap.pe, snap.sections, snap.text):
            fh.write(blob)


def load_snapshot(path: Path) -> Snapshot:
    """讀回快照。格式不對就拋例外 —— 拿半份快照跑模擬只會得到假結論。"""
    import json

    raw = path.read_bytes()
    if raw[:8] != _MAGIC:
        msg = f"{path} 不是快照檔（開頭不是 {_MAGIC!r}）"
        raise ValueError(msg)
    size = struct.unpack_from("<I", raw, 8)[0]
    header = json.loads(raw[12 : 12 + size].decode("utf-8"))
    at = 12 + size
    blobs = []
    for n in header["sizes"]:
        blobs.append(raw[at : at + n])
        at += n
    if any(len(b) != n for b, n in zip(blobs, header["sizes"], strict=True)):
        msg = f"{path} 內容不完整"
        raise ValueError(msg)
    head, pe, sections, text = blobs
    return Snapshot(
        module_base=header["module_base"], head=head, pe=pe, sections=sections,
        section_table=header["section_table"], text_base=header["text_base"], text=text,
    )


# ---------------------------------------------------------------------------
# 唯一性檢查（對執行中的行程）
# ---------------------------------------------------------------------------


def _container_candidates(scanner) -> list[tuple[int, int, int]]:
    """列出所有通過骨架濾網的候選 `(骨架位址, 解析函式, 容器)`。

    ⚠ **直接呼叫正式的 `bag.find_container_sites`**，不要在這裡重寫一份。
    它本來就回**全部**候選（不是只回第一個），所以問得出「到底有幾個」。
    重寫一份的話，正式版收緊骨架時這裡不會跟上，報告就會說謊（踩過）。
    """
    return bag.find_container_sites(scanner)


def _call_targets(base: int, blob: bytes) -> dict[int, int]:
    """每個被 `mov ecx,esi ; call` 呼叫的目標被呼叫幾次。

    ⚠ 條件要跟 `packet_table._register_function` **一模一樣**（包含
    「前面要有 push imm32」那一條），不然這裡量到的領先倍數是另一個東西的。
    """
    counts: dict[int, int] = {}
    start = 0
    while True:
        k = blob.find(packet_table._CALL_PATTERN, start)  # noqa: SLF001
        if k < 0 or k + 7 > len(blob):
            return counts
        start = k + 1
        window = blob[max(0, k - packet_table._ARGS_BACK):k]  # noqa: SLF001
        if packet_table._PUSH_IMM32 not in window:  # noqa: SLF001
            continue
        rel = struct.unpack_from("<i", blob, k + 3)[0]
        target = base + k + 7 + rel
        counts[target] = counts.get(target, 0) + 1


def check_live(pid: int, snap: Snapshot, report: Report, notes: list[str]) -> None:
    print(f"\n[唯一性] PID {pid}")
    base, blob = snap.text_base, snap.text

    # --- 1. 角色狀態：AOB 是**錨**，唯一性靠內容驗證（[MEM-041]）---
    #
    # ⚠ 這裡以前要求「原始 AOB 只能命中 1 個」，那個判準是錯的：玩久了堆積裡會
    # 出現同樣位元組樣式的垃圾（實測 6 個命中裡 5 個是 HP 15／maxHP 42 億／名字空白）。
    # 真正要守的是「**驗完之後只剩一個**」—— 那才是生產路徑的行為。
    reader = CharacterReader()
    located = reader.attach(pid)
    try:
        scanner = MemoryScanner()
        scanner.open(pid)
        try:
            hits = scan(scanner, CHAR_STATUS, writable_only=True, limit=64)
        finally:
            scanner.close()
        real = [h for h in hits if reader.probe(h) is not None] if located else []
    finally:
        reader.close()
    report.add(
        f"PID {pid} 角色狀態驗證後唯一",
        len(real) == 1,
        f"AOB 命中 {len(hits)} 個，通過合理性驗證 {len(real)} 個："
        f"{[hex(h) for h in real]}",
    )
    notes.append(
        f"- PID {pid} 角色狀態：AOB 命中 {len(hits)} 個 → 驗證後 {len(real)} 個"
        f"（其餘是堆積垃圾，見 [MEM-041]）"
    )

    # --- 1b. 座標與導航目標：程式碼特徵，一定要**唯一且交叉對得上** ---
    scanner = MemoryScanner()
    scanner.open(pid)
    try:
        x = locate_global(scanner, POSITION_X_SIGS)
        y = locate_global(scanner, POSITION_Y_SIGS)
        navi = locate_global(scanner, NAVI_DEST_SIGS)
        account = locate_global(scanner, SUBMITTED_ACCOUNT_SIGS)
    finally:
        scanner.close()
    report.add(
        f"PID {pid} 角色座標定位（x 與 y 必須相鄰）",
        x is not None and y is not None and y - x == POSITION_XY_GAP,
        f"x={x and hex(x)} y={y and hex(y)}（相差 {y - x if x and y else None}，"
        f"應為 {POSITION_XY_GAP}）",
    )
    report.add(
        f"PID {pid} 導航目標全域定位",
        navi is not None,
        f"位址 {navi and hex(navi)}",
    )
    report.add(
        f"PID {pid} 送出帳號緩衝定位",
        account is not None,
        f"位址 {account and hex(account)}",
    )

    # --- 2. 背包容器骨架 ---
    scanner = MemoryScanner()
    scanner.open(pid)
    try:
        cands = _container_candidates(scanner)
    finally:
        scanner.close()
    containers = sorted({c for _s, _p, c in cands})
    report.add(
        f"PID {pid} 背包容器骨架唯一",
        len(containers) == 1,
        f"{len(cands)} 處骨架、{len(containers)} 個相異容器："
        f"{[hex(c) for c in containers]}",
    )
    notes.append(
        f"- PID {pid} 背包骨架 {len(cands)} 處 -> 容器 {[hex(c) for c in containers]}"
    )
    for site, parser, container in cands:
        notes.append(f"    骨架 {site:#x} -> 解析函式 {parser:#x} -> 容器 {container:#x}")

    # --- 3. 封包長度表的註冊函式要明顯領先 ---
    counts = _call_targets(base, blob)
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
    lead = top[0][1] / top[1][1] if len(top) > 1 and top[1][1] else float("inf")
    report.add(
        f"PID {pid} 封包長度表註冊函式領先夠多",
        bool(top) and top[0][1] >= packet_table._MIN_CALLS and lead >= 5,  # noqa: SLF001
        f"前三名 {[(hex(a), n) for a, n in top]}，領先倍數 {lead:.1f}",
    )
    notes.append(f"- PID {pid} `mov ecx,esi; call` 前三名 {[(hex(a), n) for a, n in top]}")


# ---------------------------------------------------------------------------
# 改版模擬（離線變形）
# ---------------------------------------------------------------------------


def _mutate(snap: Snapshot, text: bytes, *, rebase: int = 0) -> SnapshotScanner:
    clone = Snapshot(
        module_base=snap.module_base, head=snap.head, pe=snap.pe,
        sections=snap.sections, section_table=snap.section_table,
        text_base=snap.text_base, text=text,
    )
    return SnapshotScanner(clone, rebase=rebase)


def _shift_code(text: bytes, n: int) -> bytes:
    """整段程式碼往後挪 n 位元組。

    所有指令一起挪，`call rel32` 的相對距離不變、模組外的絕對立即值也不變，
    所以這模擬的是「函式搬家／中間插了新程式碼」—— 定位器**必須**跟得上。
    """
    return b"\x90" * n + text[:-n]


def _free_space(text: bytes, size: int, before: int) -> int:
    """在 `before` 之前找一段夠長的零填補，拿來放誘餌。

    一定要放在真骨架**前面**：`find_container` 由低位址往高找、只回第一個命中，
    放後面的話它永遠先撞到真的，這個測試就變成永遠會過的假測試。
    """
    return text.rfind(b"\x00" * size, 0, before)


def _patch_container(snap: Snapshot, cand: tuple[int, int, int],
                     value: int) -> bytes | None:
    """把解析函式裡那個 `mov ecx, imm32` 的立即值換掉。"""
    _site, parser, container = cand
    text = bytearray(snap.text)
    body = parser - snap.text_base
    at = bytes(text).find(struct.pack("<I", container), body, body + 0x200)
    if at < 0:
        return None
    struct.pack_into("<I", text, at, value)
    return bytes(text)


def simulate(snap: Snapshot, report: Report, notes: list[str]) -> None:
    print("\n[改版模擬]")
    truth = bag.find_container(SnapshotScanner(snap))
    report.add("基準：快照上定位得到容器", truth is not None, "快照本身就定位不到")
    cands = _container_candidates(SnapshotScanner(snap))
    if truth is None or not cands:
        return
    notes.append(f"- 基準容器 = {truth:#x}")

    # 1. 整段程式碼往後挪（函式搬家／中間插了新程式碼）
    for n in (0x10, 0x400, 0x4000):
        got = bag.find_container(_mutate(snap, _shift_code(snap.text, n)))
        report.add(
            f"程式碼整體位移 +{n:#x} 仍答對",
            got == truth,
            f"得到 {got if got is None else hex(got)}，應為 {truth:#x}",
        )

    # 2. 模組被重新配置：base 位移，模組內的絕對立即值跟著改
    delta = 0x100000
    patched = _patch_container(snap, cands[0], truth + delta)
    if patched is None:
        report.add(f"模組重新配置 +{delta:#x} 跟著位移", False, "找不到可改的立即值")
    else:
        got = bag.find_container(_mutate(snap, patched, rebase=delta))
        report.add(
            f"模組重新配置 +{delta:#x} 跟著位移",
            got == truth + delta,
            f"得到 {got if got is None else hex(got)}，應為 {truth + delta:#x}",
        )

    # 3. 容器換到別的全域：定位器要回**新**值（證明沒有寫死）
    #
    # ⚠ 假值一定要落在**模組映像之外**。定位器會把「指向程式碼區段的立即值」
    # 判定為不是容器（那是正確的：容器是資料不是程式碼），所以挑一個落在
    # 映像內的假值，它會被合理地跳過，然後這個檢查就會誤報成不合格 ——
    # 原本用 0x0BADF00 就是這樣（在 base+程式碼長度 之內）。
    moved = 0x1BADF00
    patched = _patch_container(snap, cands[0], moved)
    got = None if patched is None else bag.find_container(_mutate(snap, patched))
    report.add(
        "容器換全域時回新值（沒寫死）",
        got == moved,
        f"得到 {got if got is None else hex(got)}，應為 {moved:#x}",
    )

    # 4. 骨架被改掉 → 必須大聲失敗，不能回別的位址
    broken = snap.text.replace(bag._MAGIC_DIV34, b"\xb8\x11\x22\x33\x44")  # noqa: SLF001
    got = bag.find_container(_mutate(snap, broken))
    report.add(
        "魔術乘數改掉 -> 失敗（不是回錯的位址）",
        got is None,
        f"竟然回了 {got if got is None else hex(got)}",
    )

    broken = snap.text.replace(bag._SUB_5, b"\x90\x90\x90")  # noqa: SLF001
    got = bag.find_container(_mutate(snap, broken))
    report.add(
        "`sub ecx,5` 拿掉 -> 失敗（不是回錯的位址）",
        got is None,
        f"竟然回了 {got if got is None else hex(got)}",
    )

    # 5. 誘餌：改版新增了第二組一模一樣的骨架，指向別的全域。
    #    放在**真骨架前面**（find 由低位址往高找）—— 這是最壞情況。
    decoy = 0x0DEC0A0
    site = cands[0][0]
    text = bytearray(snap.text)
    spot = _free_space(bytes(text), 0x100, site - snap.text_base)
    if spot > 0:
        fake_parser = spot + 0x80
        body = bag._SUB_5 + bag._MAGIC_DIV34 + b"\xf7\xe1\xc1\xea\x05"  # noqa: SLF001
        call_at = spot + len(body)
        text[spot : spot + len(body)] = body
        text[call_at] = bag._CALL_REL32  # noqa: SLF001
        struct.pack_into("<i", text, call_at + 1, fake_parser - (call_at + 5))
        text[fake_parser] = bag._MOV_ECX_IMM  # noqa: SLF001
        struct.pack_into("<I", text, fake_parser + 1, decoy)
        got = bag.find_containers(_mutate(snap, bytes(text)))
        # 問對的問題：不是「有沒有挑到誘餌」（第一個命中本來就可能是誘餌），
        # 而是**定位器看不看得見自己有歧義**。看得見，`read_bag` 才有機會
        # 用資料裁決；看不見就只能安靜地拿別人家的全域去讀。
        report.add(
            "出現第二組骨架時看得見兩個候選",
            decoy in got and truth in got,
            f"候選 {[hex(c) for c in got]}，應同時含真容器 {truth:#x} 與誘餌 {decoy:#x}",
        )
        notes.append(
            f"- 誘餌放在 {snap.text_base + spot:#x}（真骨架 {site:#x} 之前），"
            f"候選 {[hex(c) for c in got]}"
        )
    else:
        report.skip("出現第二組骨架時看得見兩個候選",
                    "真骨架之前找不到夠大的零填補可以放誘餌")

    # --- 封包長度表 ---
    truth_table = packet_table.extract(0, SnapshotScanner(snap))
    report.add("基準：快照上抽得到封包長度表", len(truth_table) > 1000,
               f"只抽到 {len(truth_table)} 個")
    notes.append(f"- 基準封包長度表 {len(truth_table)} 個 opcode")
    if not truth_table:
        return

    got = packet_table.extract(0, _mutate(snap, _shift_code(snap.text, 0x400)))
    same = sum(1 for k, v in got.items() if truth_table.get(k) == v)
    report.add(
        "程式碼整體位移後封包長度表不變",
        len(got) == len(truth_table) == same,
        f"位移後 {len(got)} 個，其中 {same} 個與基準相同（基準 {len(truth_table)}）",
    )

    broken = snap.text.replace(packet_table._CALL_PATTERN, b"\x90\x90\x90")  # noqa: SLF001
    got = packet_table.extract(0, _mutate(snap, broken))
    report.add("呼叫骨架被破壞 -> 回空表（安全退化）", got == {},
               f"竟然抽到 {len(got)} 個")

    # --- 選角畫面的兩個全域（游標格號、選定的角色名字）---
    #
    # 這兩個是自動選角的眼睛：移游標之前要讀得到「現在在第幾格」，
    # 按下 Enter 之後要讀得到「客戶端選了誰」。定位錯了會選到別人，
    # 所以位移不變、骨架壞掉要退成 None，兩件事都要驗。
    for label, sigs in (("選角游標", SELECT_CURSOR_SIGS), ("角色名字", SELECT_NAME_SIGS)):
        truth = locate_global(SnapshotScanner(snap), sigs)
        if truth is None:
            # 舊的快照可能是在這兩條特徵存在之前存的 —— 那不算不合格。
            # 真的壞掉會在 `check_live`（對著跑起來的遊戲）那邊變成 NG。
            report.skip(f"{label}：改版模擬", "這份快照裡沒有這組骨架")
            continue
        report.add(f"基準：快照上定位得到{label}", True)
        notes.append(f"- 基準{label} {truth:#x}")

        # 1) 程式碼整體位移：骨架自己會跟著搬，讀出來的位址不該變
        #    （位址是從立即值讀的，跟骨架在哪一行無關）
        shifted = _mutate(snap, _shift_code(snap.text, 0x400))
        report.add(
            f"{label}：程式碼位移後答案不變",
            locate_global(shifted, sigs) == truth,
            f"位移後變成 {locate_global(shifted, sigs)}",
        )

        # 2) 把指令裡的立即值改掉：答案一定要跟著改
        #    這一項是在證明「答案不是寫死在特徵裡的」——
        #    如果哪天有人把位址硬編進程式，這裡會當場抓到。
        moved = truth + 0x40
        text = bytearray(snap.text)
        for sig in sigs:
            for match in sig.compiled().finditer(bytes(text)):
                for off in sig.operands:
                    spot = match.start() + off
                    text[spot:spot + 4] = moved.to_bytes(4, "little")
        report.add(
            f"{label}：立即值改掉之後答案跟著改",
            locate_global(_mutate(snap, bytes(text)), sigs) == moved,
            "答案沒跟著改（位址可能被寫死在程式裡）",
        )

        # 3) 骨架被破壞 -> 一定要回 None，不准退回舊值或猜一個
        broken = snap.text
        for sig in sigs:
            for match in list(sig.compiled().finditer(broken)):
                broken = (broken[:match.start()]
                          + b"\x90" * (match.end() - match.start())
                          + broken[match.end():])
        report.add(
            f"{label}：骨架被破壞 -> 回 None（安全退化）",
            locate_global(_mutate(snap, broken), sigs) is None,
            "竟然還定位得到",
        )

        # 4) 兩處答案不一致 -> 拒絕作答（不准挑一個用）
        first = sigs[0].compiled().search(snap.text)
        hits = list(sigs[0].compiled().finditer(snap.text))
        enough = len(hits) > 1 or len(sigs) > 1 or len(sigs[0].operands) > 1
        if first is not None and enough:
            tampered = bytearray(snap.text)
            spot = first.start() + sigs[0].operands[0]
            tampered[spot:spot + 4] = (truth + 0x40).to_bytes(4, "little")
            report.add(
                f"{label}：兩處答案不一致 -> 回 None",
                locate_global(_mutate(snap, bytes(tampered)), sigs) is None,
                "竟然還給了答案",
            )
        else:
            report.skip(f"{label}：兩處答案不一致 -> 回 None",
                        "只有一處命中，做不出不一致的情況")


# ---------------------------------------------------------------------------


def find_game_pids() -> list[int]:
    try:
        import psutil
    except ImportError:
        return []
    return [p.pid for p in psutil.process_iter(["name"])
            if (p.info["name"] or "").lower() == GAME]


def main() -> int:
    ap = argparse.ArgumentParser(description="AOB 特徵唯一性檢查與改版模擬")
    ap.add_argument("--pid", type=int, action="append", default=[])
    ap.add_argument("--skip-live", action="store_true", help="只跑改版模擬")
    ap.add_argument("--save-snapshot", type=Path,
                    help="把程式碼快照存起來，之後不用開遊戲也能跑改版模擬")
    ap.add_argument("--snapshot", type=Path,
                    help="用存好的快照跑改版模擬（不需要遊戲在跑）")
    args = ap.parse_args()

    report = Report()
    notes: list[str] = []
    snap: Snapshot | None = None
    pids: list[int] = []

    if args.snapshot is not None:
        snap = load_snapshot(args.snapshot)
        print(f"快照：{args.snapshot}（模組 {snap.module_base:#x}、"
              f"程式碼 {snap.text_base:#x}，{len(snap.text) / 1048576:.1f} MB）")
        notes.append(f"- 快照來源 {args.snapshot}")
        simulate(snap, report, notes)
        return _finish(report, notes, pids)

    pids = args.pid or find_game_pids()
    if not pids:
        print("找不到執行中的 Ragexe.exe（可用 --pid 指定，或 --snapshot 用存好的快照）")
        return 2
    print(f"行程：{pids}")

    for pid in pids:
        got = capture(pid)
        if got is None:
            report.add(f"PID {pid} 抓得到程式碼快照", False, "讀不到 Ragexe 的程式碼區段")
            continue
        report.add(f"PID {pid} 抓得到程式碼快照", True)
        notes.append(
            f"- PID {pid} 模組 {got.module_base:#x}、程式碼 {got.text_base:#x}"
            f"（{len(got.text) / 1048576:.1f} MB）"
        )
        snap = snap or got
        if not args.skip_live:
            check_live(pid, got, report, notes)

    if snap is not None:
        if args.save_snapshot is not None:
            save_snapshot(snap, args.save_snapshot)
            print(f"快照已存到 {args.save_snapshot}")
            notes.append(f"- 快照存到 {args.save_snapshot}")
        simulate(snap, report, notes)

    return _finish(report, notes, pids)


def _finish(report: Report, notes: list[str], pids: list[int]) -> int:
    stamp = _dt.datetime.now(tz=_dt.UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "reports" / f"verify_sigs-{stamp}.md"
    out.parent.mkdir(exist_ok=True)
    lines = [f"# AOB 特徵驗證 {stamp}", "", f"行程：{pids}", "", "## 結果", ""]
    marks = {"OK": "✔", "NG": "✘", "--": "—"}
    lines += [f"- {marks[c.status]} {c.name}"
              + (f"　—— {c.detail}" if c.detail else "") for c in report.checks]
    lines += ["", "## 明細", ""] + notes + [""]
    out.write_text("\n".join(lines), encoding="utf-8")

    bad, skipped = report.failed, report.skipped
    ok = len(report.checks) - len(bad) - len(skipped)
    print(f"\n{ok}/{len(report.checks)} 通過"
          + (f"，{len(skipped)} 略過" if skipped else ""))
    if bad:
        print("不合格：")
        for c in bad:
            print(f"  NG {c.name} -- {c.detail}")
    print(f"明細：{out}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
