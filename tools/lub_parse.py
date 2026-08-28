"""Lua 5.1 bytecode (.lub) 解析器 —— 只做「讀常數與指令」，不做完整反編譯。

用途：從 RODATA 的 luafiles514/*.lub 抽出常數表（例如 npcidentity 的
JT_xxx = id、jobname 的 id -> 資源檔名）。

格式出處：Lua 5.1.5 原始碼 lundump.c / ldump.c（header 12 bytes + function block）。
不是猜的：每個欄位大小都由 header 自己宣告，解析時會驗證。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

# Lua 5.1 opcode 表，順序出自 lopcodes.h 的 OpCode enum
OPNAMES = [
    "MOVE", "LOADK", "LOADBOOL", "LOADNIL", "GETUPVAL", "GETGLOBAL", "GETTABLE",
    "SETGLOBAL", "SETUPVAL", "SETTABLE", "NEWTABLE", "SELF", "ADD", "SUB", "MUL",
    "DIV", "MOD", "POW", "UNM", "NOT", "LEN", "CONCAT", "JMP", "EQ", "LT", "LE",
    "TEST", "TESTSET", "CALL", "TAILCALL", "RETURN", "FORLOOP", "FORPREP",
    "TFORLOOP", "SETLIST", "CLOSE", "CLOSURE", "VARARG",
]
BITRK = 1 << 8  # lopcodes.h: RK 值 >= 256 代表常數索引


@dataclass
class Instr:
    op: str
    a: int
    b: int
    c: int
    bx: int
    sbx: int


@dataclass
class Proto:
    source: str = ""
    nups: int = 0
    numparams: int = 0
    is_vararg: int = 0
    maxstack: int = 0
    code: list[Instr] = field(default_factory=list)
    consts: list[Any] = field(default_factory=list)
    protos: list[Proto] = field(default_factory=list)


class Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def take(self, n: int) -> bytes:
        b = self.d[self.p:self.p + n]
        if len(b) != n:
            raise ValueError(f"truncated at {self.p}, want {n}")
        self.p += n
        return b

    def byte(self) -> int:
        return self.take(1)[0]


class LubFile:
    def __init__(self, data: bytes):
        r = Reader(data)
        sig = r.take(4)
        if sig != b"\x1bLua":
            raise ValueError(f"not lua bytecode: {sig!r}")
        ver = r.byte()
        if ver != 0x51:
            raise ValueError(f"unsupported lua version 0x{ver:02X} (only 5.1)")
        r.byte()                       # format
        endian = r.byte()
        self.size_int = r.byte()
        self.size_sizet = r.byte()
        size_instr = r.byte()
        self.size_num = r.byte()
        integral = r.byte()
        if endian != 1 or size_instr != 4:
            raise ValueError("only little-endian, 4-byte instruction supported")
        if integral:
            raise ValueError("integral lua_Number not supported")
        self.r = r
        self.int_fmt = {4: "<i", 8: "<q"}[self.size_int]
        self.sizet_fmt = {4: "<I", 8: "<Q"}[self.size_sizet]
        self.num_fmt = {4: "<f", 8: "<d"}[self.size_num]
        self.main = self._proto()

    def _int(self) -> int:
        return struct.unpack(self.int_fmt, self.r.take(self.size_int))[0]

    def _sizet(self) -> int:
        return struct.unpack(self.sizet_fmt, self.r.take(self.size_sizet))[0]

    def _string(self) -> str | None:
        n = self._sizet()
        if n == 0:
            return None
        raw = self.r.take(n)[:-1]      # 去掉結尾的 \0
        return raw.decode("latin-1")   # 位元組原樣保留，之後才決定用哪種編碼

    def _proto(self) -> Proto:
        p = Proto()
        p.source = self._string() or ""
        self._int()                    # linedefined
        self._int()                    # lastlinedefined
        p.nups = self.r.byte()
        p.numparams = self.r.byte()
        p.is_vararg = self.r.byte()
        p.maxstack = self.r.byte()

        for _ in range(self._int()):   # code
            w = struct.unpack("<I", self.r.take(4))[0]
            op = w & 0x3F
            a = (w >> 6) & 0xFF
            c = (w >> 14) & 0x1FF
            b = (w >> 23) & 0x1FF
            bx = (w >> 14) & 0x3FFFF
            p.code.append(Instr(
                OPNAMES[op] if op < len(OPNAMES) else f"OP{op}",
                a, b, c, bx, bx - 131071,
            ))

        for _ in range(self._int()):   # constants
            t = self.r.byte()
            if t == 0:
                p.consts.append(None)
            elif t == 1:
                p.consts.append(bool(self.r.byte()))
            elif t == 3:
                p.consts.append(struct.unpack(self.num_fmt, self.r.take(self.size_num))[0])
            elif t == 4:
                p.consts.append(self._string())
            else:
                raise ValueError(f"unknown constant type {t}")

        for _ in range(self._int()):   # nested protos
            p.protos.append(self._proto())

        for _ in range(self._int()):   # debug: lineinfo
            self._int()
        for _ in range(self._int()):   # debug: locvars
            self._string()
            self._int()
            self._int()
        for _ in range(self._int()):   # debug: upvalues
            self._string()
        return p


def walk(p: Proto):
    yield p
    for sub in p.protos:
        yield from walk(sub)


def rk(p: Proto, v: int):
    """RK 值 -> 常數；是暫存器就回 None（代表這條不是純常數賦值）。"""
    return p.consts[v - BITRK] if v >= BITRK else None


def load(path: str) -> LubFile:
    with open(path, "rb") as f:
        return LubFile(f.read())


# ---------------------------------------------------------------------------
# 直線碼小型 VM
#
# luafiles514 裡的資料表都是「一長串常數賦值」，沒有分支、沒有呼叫。
# 只要支援 LOADK / NEWTABLE / SETTABLE / SETLIST / GETGLOBAL / GETTABLE
# 就能把整張表原樣重建。遇到沒支援的 opcode 會丟例外——**不准安靜略過**，
# 否則會少抽資料還不知道（CLAUDE.md：失效要大聲）。
# ---------------------------------------------------------------------------

LFIELDS_PER_FLUSH = 50  # lopcodes.h


class Global:
    """GETGLOBAL 產生的佔位物件（例如 jobname.lub 裡的 `jobtbl`）。"""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class Index:
    """GETTABLE 產生的佔位物件（例如 `jobtbl.JT_PORING`）。"""

    __slots__ = ("table", "key")

    def __init__(self, table, key):
        self.table = table
        self.key = key


_SUPPORTED = {
    "LOADK", "LOADBOOL", "LOADNIL", "NEWTABLE", "SETTABLE", "SETLIST",
    "GETGLOBAL", "SETGLOBAL", "GETTABLE", "MOVE", "RETURN",
}


def simulate(p: Proto):
    """跑完整支 proto，回傳 (globals, settable_pairs)。

    - globals: {全域名: 值}
    - settable_pairs: [(table物件, key, value)]，順序即原始碼順序。
      key/value 可能是 Global/Index 佔位物件（代表原碼是 `t[jobtbl.X] = y`）。
    """
    reg: dict[int, Any] = {}
    globs: dict[str, Any] = {}
    pairs: list[tuple[Any, Any, Any]] = []

    def val(v):
        return p.consts[v - BITRK] if v >= BITRK else reg.get(v)

    i = 0
    code = p.code
    while i < len(code):
        ins = code[i]
        if ins.op not in _SUPPORTED:
            raise ValueError(f"unsupported opcode {ins.op} at {i} in {p.source}")
        if ins.op == "LOADK":
            reg[ins.a] = p.consts[ins.bx]
        elif ins.op == "LOADBOOL":
            # lvm.c：R(A) := (Bool)B；C 非 0 時再跳過下一條指令。
            # skillinfolist.lub 用它寫 `SpAmount = false` 之類的欄位。
            reg[ins.a] = bool(ins.b)
            if ins.c:
                i += 1
        elif ins.op == "LOADNIL":
            for r in range(ins.a, ins.b + 1):
                reg[r] = None
        elif ins.op == "MOVE":
            reg[ins.a] = reg.get(ins.b)
        elif ins.op == "NEWTABLE":
            reg[ins.a] = []
        elif ins.op == "GETGLOBAL":
            reg[ins.a] = Global(p.consts[ins.bx])
        elif ins.op == "SETGLOBAL":
            globs[p.consts[ins.bx]] = reg.get(ins.a)
        elif ins.op == "GETTABLE":
            reg[ins.a] = Index(reg.get(ins.b), val(ins.c))
        elif ins.op == "SETTABLE":
            pairs.append((reg.get(ins.a), val(ins.b), val(ins.c)))
        elif ins.op == "SETLIST":
            table = reg[ins.a]
            b, c = ins.b, ins.c
            if c == 0:  # 真正的 C 放在下一個 word（不是指令）
                i += 1
                c = code[i].bx
            start = (c - 1) * LFIELDS_PER_FLUSH
            while len(table) < start:
                table.append(None)
            for j in range(1, b + 1):
                table.append(reg.get(ins.a + j))
        i += 1
    return globs, pairs


def kr(s: str | None) -> str:
    """把 latin-1 保存的位元組轉回韓文（資源檔名用 euc-kr）。"""
    return s.encode("latin-1").decode("euc-kr", "replace") if s else ""


def tw(s: str | None) -> str:
    """把 latin-1 保存的位元組轉回繁中（台版顯示名用 cp950）。"""
    return s.encode("latin-1").decode("cp950", "replace") if s else ""
