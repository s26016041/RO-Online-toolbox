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

import json
import logging
import pathlib
import struct
from dataclasses import dataclass

from .aob import code_section
from .memory_scan import MemoryScanner

log = logging.getLogger(__name__)

#: 主程式碼區段最多讀這麼多（實測 Ragexe 的程式碼區段約 11.5 MB）。
_MAX_CODE = 24 << 20
#: `mov ecx, esi ; call rel32`
_CALL_PATTERN = b"\x8b\xce\xe8"
#: `push imm32`。註冊點一定會推 opcode，拿它把「純粹的 mov ecx,esi; call」濾掉。
_PUSH_IMM32 = b"\x68"
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


def _register_function(base: int, blob: bytes) -> int | None:
    """被 `mov ecx,esi ; call` 呼叫最多次的目標 = 註冊函式。

    ⚠ **只數「前面真的推了參數」的呼叫點。** `mov ecx,esi ; call` 是
    「對 esi 這個物件呼叫方法」，滿地都是；光數它的話第二名跟第一名只差 4.9 倍
    （實測 1785 vs 366），離「一眼看得出是哪一個」還很遠。
    註冊點一定會先 `push <opcode>`（imm32），把這個條件加上去之後：

        真的那支 1785 → 1775（只掉 0.6%），第二名 366 → 102，領先 4.9 → 17.4 倍

    這不是為了讓數字好看：領先倍數就是「這條特徵有多不容易認錯人」。
    """
    counts: dict[int, int] = {}
    start = 0
    while True:
        k = blob.find(_CALL_PATTERN, start)
        if k < 0 or k + 7 > len(blob):
            break
        start = k + 1
        if _PUSH_IMM32 not in blob[max(0, k - _ARGS_BACK):k]:
            continue
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
        section = code_section(scanner)
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


# ---- 存檔重用 --------------------------------------------------------------
#
# ⚠ 為什麼要存：抽這張表要讀遊戲的程式碼區段，而**遊戲剛開的那一兩分鐘
# GameGuard 會擋住那個讀取**（實測：剛開時抽不到，穩定後 0.8 秒就抽到 1783 筆）。
# 偏偏擷取器最需要它的時候正是剛開機那段 —— 沒有長度表就只能「一段當一包」，
# 黏在後面的封包全部看不到（[PKT-043]），二次密碼的 seed 就是這樣不見的。
#
# 這張表跟**客戶端版本**綁在一起，不會每次不同，所以抽到就存起來重用。
# 用 exe 的大小與修改時間當鑰匙：改版換了 exe 就自動失效、重抽。


def _cache_file():
    from ro_toolbox.config.paths import user_data_dir

    return user_data_dir() / "packet_lengths.json"


def _exe_key(pid: int) -> str | None:
    """用 Ragexe.exe 的大小與修改時間當鑰匙。改版就會變，自動失效。"""
    try:
        import psutil

        path = pathlib.Path(psutil.Process(pid).exe())
        stat = path.stat()
        return f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}"
    except Exception as exc:  # noqa: BLE001
        log.debug("取不到遊戲執行檔資訊：%s", exc)
        return None


def load_cached(pid: int) -> dict[int, PacketInfo] | None:
    """讀出先前存好的長度表。沒有、或客戶端換版本了就回 None。"""
    key = _exe_key(pid)
    if key is None:
        return None
    try:
        raw = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = raw.get(key)
    if not entry:
        return None
    table = {
        int(op): PacketInfo(opcode=int(op), length=info[0], header=info[1])
        for op, info in entry.items()
    }
    log.info("封包長度表用存檔（%d 個 opcode，鑰匙 %s）", len(table), key)
    return table


def load_any_cached() -> dict[int, PacketInfo] | None:
    """不指定行程，讀存檔裡**最後存進去的**那一份長度表。

    ⚠ 這是給「遊戲沒開」的情況用的（例如不開遊戲直接跟伺服器要角色清單）——
    那時候沒有 pid，算不出鑰匙。存檔裡通常只有一份（同一個客戶端），
    有多份時取最後寫入的那一份。

    這份表是**從實際跑過的客戶端抽出來的**，不是猜的；客戶端改版時鑰匙會變，
    抽出來的新表會另外存一份，舊的就不會再被寫入 —— 所以「最後一份」等於「最新的」。
    """
    try:
        raw = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    key = list(raw)[-1]
    entry = raw[key]
    table = {
        int(op): PacketInfo(opcode=int(op), length=info[0], header=info[1])
        for op, info in entry.items()
    }
    log.info("封包長度表用存檔（沒有指定行程，取 %s，%d 個 opcode）", key, len(table))
    return table


def save_cached(pid: int, table: dict[int, PacketInfo]) -> None:
    """把抽到的長度表存起來給下次用。存不了只記一筆，不影響功能。"""
    key = _exe_key(pid)
    if key is None or not table:
        return
    path = _cache_file()
    try:
        raw = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        raw[key] = {str(op): [info.length, info.header] for op, info in table.items()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw), encoding="utf-8")
        log.info("封包長度表已存檔（%d 個 opcode）", len(table))
    except (OSError, ValueError) as exc:
        log.debug("存長度表失敗：%s", exc)
