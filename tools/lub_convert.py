r"""把 32 位的 Lua 5.1 bytecode（.lub）轉成 64 位主機能載入的版本。

    .venv\Scripts\python.exe tools\lub_convert.py in.lub out.lub

RO 客戶端是 32 位程式，它的 .lub 標頭寫著 sizeof(size_t)=4；
64 位的 Lua 直譯器會直接拒收（bad header in precompiled chunk）。
指令、整數、浮點的寬度兩邊一樣，**只有字串長度前綴的寬度不同**，
所以逐段重寫一次就能載入，內容一個位元組都不會變。

格式依 Lua 5.1 lundump.c。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_SIG = b"\x1bLua"


class Reader:
    def __init__(self, data: bytes, size_t: int) -> None:
        self.d = data
        self.p = 0
        self.size_t = size_t

    def take(self, n: int) -> bytes:
        out = self.d[self.p : self.p + n]
        if len(out) != n:
            raise ValueError("bytecode 提前結束")
        self.p += n
        return out

    def byte(self) -> int:
        return self.take(1)[0]

    def int(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def size(self) -> int:
        return int.from_bytes(self.take(self.size_t), "little")

    def string(self) -> bytes:
        n = self.size()
        return self.take(n) if n else b""


class Writer:
    def __init__(self, size_t: int) -> None:
        self.out = bytearray()
        self.size_t = size_t

    def raw(self, b: bytes) -> None:
        self.out += b

    def int(self, v: int) -> None:
        self.out += struct.pack("<i", v)

    def string(self, b: bytes) -> None:
        self.out += len(b).to_bytes(self.size_t, "little")
        self.out += b


def _n(r: Reader, w: Writer) -> int:
    """讀一個計數並原樣寫出去。"""
    n = r.int()
    w.int(n)
    return n


def convert_function(r: Reader, w: Writer) -> None:
    w.string(r.string())          # source
    w.int(r.int())                # linedefined
    w.int(r.int())                # lastlinedefined
    w.raw(r.take(4))              # nups, numparams, is_vararg, maxstacksize

    n = _n(r, w)                  # code
    w.raw(r.take(4 * n))

    n = _n(r, w)                  # constants
    for _ in range(n):
        t = r.byte()
        w.raw(bytes([t]))
        if t == 0:                # nil
            pass
        elif t == 1:              # boolean
            w.raw(r.take(1))
        elif t == 3:              # number
            w.raw(r.take(8))
        elif t == 4:              # string
            w.string(r.string())
        else:
            raise ValueError(f"未知的常數型別 {t}")

    n = _n(r, w)                  # nested protos
    for _ in range(n):
        convert_function(r, w)

    n = _n(r, w)                  # debug: lineinfo
    w.raw(r.take(4 * n))
    n = _n(r, w)                  # debug: locvars
    for _ in range(n):
        w.string(r.string())
        w.int(r.int())
        w.int(r.int())
    n = _n(r, w)                  # debug: upvalues
    for _ in range(n):
        w.string(r.string())


def convert(data: bytes, target_size_t: int = 8) -> bytes:
    if data[:4] != _SIG:
        raise ValueError("不是 Lua bytecode")
    version, fmt, endian, size_int, size_t, size_ins, size_num, integral = data[4:12]
    if version != 0x51:
        raise ValueError(f"只支援 Lua 5.1，這個是 0x{version:02X}")
    if (size_int, size_ins, size_num) != (4, 4, 8):
        raise ValueError(f"寬度不支援：int={size_int} ins={size_ins} num={size_num}")
    header = bytearray(data[:12])
    header[8] = target_size_t
    r = Reader(data[12:], size_t)
    w = Writer(target_size_t)
    convert_function(r, w)
    if r.p != len(r.d):
        raise ValueError(f"尾巴沒吃完，剩 {len(r.d) - r.p} 位元組")
    return bytes(header) + bytes(w.out)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    data = src.read_bytes()
    out = convert(data, 8 if len(sys.argv) < 4 else int(sys.argv[3]))
    dst.write_bytes(out)
    print(f"{src.name} {len(data):,} → {dst.name} {len(out):,} 位元組（size_t 4→8）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
