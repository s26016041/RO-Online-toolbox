r"""重新定位「角色座標」全域（改版位移後要跑這個）。

    # 1. 角色站著別動，記下所有候選
    .\.venv\Scripts\python.exe tools\find_position.py scan
    # 2. 在遊戲裡**走幾步**（至少 5 格），再跑一次
    .\.venv\Scripts\python.exe tools\find_position.py narrow
    # 3. 再走一次、再跑一次，把剩下的雜訊清掉
    .\.venv\Scripts\python.exe tools\find_position.py narrow

為什麼不能只用「值像座標」：GAMEDATA [MEM-006] 已經踩過 ——
「三個角色值不同 + 落在可走格」這組條件**出生點、記錄點、封包記錄副本也都滿足**，
一度把 `HP-0x4290` 誤判成即時座標。唯一能分辨的是**移動驗證**：
真的座標會跟著走，其他候選不會。所以這支工具只做一件事 ——
「跟著動、而且動完還落在當下地圖的可走格上」的才留下。

只讀不寫（CLAUDE.md：RO 掛 GameGuard）。全量結果寫 reports/，主控台只印結論。
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ro_toolbox.services.aob import image_size  # noqa: E402
from ro_toolbox.services.character import CharacterReader  # noqa: E402
from ro_toolbox.services.mapdata import GatError, load_terrain  # noqa: E402
from ro_toolbox.services.memory_scan import MemoryScanner  # noqa: E402
from ro_toolbox.services.window_list import enumerate_windows  # noqa: E402

REPORTS = ROOT / "reports"
STATE = REPORTS / "pos_candidates.json"
_PROCESS = "ragexe.exe"
_MIN_MOVE = 2  # 至少要移動這麼多格才算「真的走了」


def _attach() -> tuple[MemoryScanner, CharacterReader, int]:
    pids = [w.pid for w in enumerate_windows() if w.process_name.lower() == _PROCESS]
    if not pids:
        sys.exit(f"找不到 {_PROCESS}，請先開遊戲並登入")
    pid = pids[0]
    reader = CharacterReader()
    if not reader.attach(pid):
        sys.exit("角色定位失敗（還沒進遊戲？）")
    scanner = MemoryScanner()
    scanner.open(pid)
    return scanner, reader, pid


def _current_map(reader: CharacterReader) -> str:
    status = reader.read()
    if status is None or not status.map_name:
        sys.exit("讀不到目前地圖（還沒進遊戲？）")
    return status.map_name


def _terrain(map_name: str):
    try:
        return load_terrain(map_name)
    except GatError as exc:
        sys.exit(f"讀不到 {map_name} 的地形：{exc}")


def _plausible(terrain, x: int, y: int) -> bool:
    return 1 <= x < terrain.width and 1 <= y < terrain.height and terrain.is_walkable(x, y)


def cmd_scan() -> int:
    scanner, reader, pid = _attach()
    map_name = _current_map(reader)
    terrain = _terrain(map_name)
    base = scanner.module_base(_PROCESS)
    span = image_size(scanner, _PROCESS)
    if base is None or span is None:
        sys.exit("讀不到模組範圍")
    print(f"pid={pid}  {_PROCESS}+{0:#x}..{span:#x}  "
          f"地圖 {map_name} {terrain.width}x{terrain.height}")

    found: list[list[int]] = []
    for region, size in scanner.regions(writable_only=False):
        if not (base <= region < base + span):
            continue
        raw = scanner.read_region(region, size)
        if not raw:
            continue
        raw = bytes(raw)
        for i in range(0, len(raw) - 8, 4):
            x, y = struct.unpack_from("<II", raw, i)
            if _plausible(terrain, x, y):
                found.append([region + i, x, y])

    REPORTS.mkdir(exist_ok=True)
    STATE.write_text(
        json.dumps({"pid": pid, "map": map_name, "base": base, "hits": found}),
        encoding="utf-8",
    )
    print(f"候選 (x,y) 對：{len(found):,}")
    print("→ 現在去遊戲裡**走幾步**（至少 5 格），再跑 narrow")
    return 0


def cmd_narrow() -> int:
    if not STATE.is_file():
        sys.exit(f"還沒有 {STATE}，請先跑 scan")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    scanner, reader, pid = _attach()
    if pid != state["pid"]:
        sys.exit(f"行程換了（存的是 pid {state['pid']}）—— 請重跑 scan")
    map_name = _current_map(reader)
    terrain = _terrain(map_name)
    base = scanner.module_base(_PROCESS)

    survivors: list[list[int]] = []
    for addr, old_x, old_y in state["hits"]:
        raw = scanner.read_region(addr, 8)
        if not raw or len(raw) < 8:
            continue
        x, y = struct.unpack_from("<II", bytes(raw), 0)
        if (x, y) == (old_x, old_y):
            continue  # 沒跟著動 → 不是即時座標（出生點／記錄點／封包副本）
        if max(abs(x - old_x), abs(y - old_y)) < _MIN_MOVE:
            continue  # 只抖了一格，可能是別的東西剛好變動
        if not _plausible(terrain, x, y):
            continue  # 動了但落在不可走格 → 不是角色站的地方
        survivors.append([addr, x, y])

    # 一個都不剩多半是「其實沒走」——這時**不能**把候選蓋掉，否則要整個重掃。
    if survivors:
        state["hits"] = survivors
        state["map"] = map_name
        STATE.write_text(json.dumps(state), encoding="utf-8")

    print(f"跟著移動、且新位置仍可走的候選：{len(survivors)}")
    anchor = reader._base  # noqa: SLF001 - 這支工具就是要算相對錨點的偏移
    for addr, x, y in survivors[:20]:
        print(
            f"  {addr:#x}  = {_PROCESS}+{addr - base:#x}"
            f"  = 角色結構{addr - anchor:+#x}   讀到 ({x}, {y})"
        )
    if len(survivors) == 1:
        addr = survivors[0][0]
        print()
        print(f"只剩一個 → 座標全域是 {addr:#x}（{_PROCESS}+{addr - base:#x}）")
        print("接著去程式碼區段找誰引用它，做成 CodeSignature —— ")
        print("**不要**把它寫成「相對角色結構的偏移」，那條推導 2026-08-26 就斷過")
        print("（兩個全域位移幅度不同，見 GAMEDATA [MEM-039]）。")
    elif survivors:
        print("→ 再走一次、再跑一次 narrow")
    else:
        print("一個都不剩：可能沒有真的移動，或座標不是兩個相鄰的 uint32。請重跑 scan。")
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "scan":
        return cmd_scan()
    if len(sys.argv) >= 2 and sys.argv[1] == "narrow":
        return cmd_narrow()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
