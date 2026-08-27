r"""找出遊戲內建尋路（導航）把「目標地圖」存在記憶體的哪裡。

    # 1. 在遊戲裡把尋路目標設成第一張圖（例：派恩）
    .\.venv\Scripts\python.exe tools\find_navigation.py scan payon
    # 2. 在遊戲裡把尋路目標**改成**第二張圖（例：艾爾帕蘭）
    .\.venv\Scripts\python.exe tools\find_navigation.py narrow alberta
    # 3. 剩下的位址看四周有什麼（找目標座標）
    .\.venv\Scripts\python.exe tools\find_navigation.py dump

原理：`navi_link_tw.lub` 那幾份表被載進記憶體後，「payon」這串字會出現**幾百次**，
但那些是靜態表，永遠不會變。真正的導航目標只有一格，換目標時它會跟著改。
所以「第一次搜 A、把目標改成 B、再看哪些位址現在是 B」就能把它逼出來 ——
跟數值搜尋的逐步縮小同一招，只是對象是字串。

只讀不寫（CLAUDE.md：RO 掛 GameGuard，寫入一律禁止）。
全量結果寫到 reports/，主控台只印結論。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ro_toolbox.services.memory_scan import MemoryScanner  # noqa: E402

REPORTS = ROOT / "reports"
STATE = REPORTS / "navi_hits.json"
_MAPNAMES = ROOT / "RODATA" / "data" / "data" / "mapnametable.txt"
#: 導航目標可能用哪種編碼存：內部地圖名是 ASCII，介面上的中文名可能是 cp950 或 UTF-16。
ENCODINGS = ("ascii", "cp950", "utf-16-le")
_READ_LEN = 48  # 每個候選讀這麼多 bytes 出來比對／顯示
_PROCESS = "ragexe.exe"


def _display_names() -> dict[str, str]:
    """{內部地圖名: 中文名}，來自 data/mapnametable.txt。讀不到就回空的。"""
    out: dict[str, str] = {}
    try:
        raw = _MAPNAMES.read_bytes().decode("cp950", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        if line.startswith("//") or "#" not in line:
            continue
        parts = line.split("#")
        if len(parts) < 2:
            continue
        stem = parts[0].strip().rsplit(".", 1)[0].lower()
        name = parts[1].strip()
        if stem and name:
            out[stem] = name
    return out


def _forms(target: str) -> list[str]:
    """一個目標要搜的所有字串形式：中文名 + **所有**對應的內部地圖名。

    中文名對內部名是**一對多**：「妙勒尼山脈南區」就有 mjolnir_06/07/08/10/11 五張。
    只挑第一個會賭錯，所以五個都收 —— 反正差分比對會自己把不對的濾掉。
    """
    names = _display_names()
    forms = [target]
    if target.lower() in names:
        forms.append(names[target.lower()])
    else:
        # 用**包含**比對而不是相等：mapnametable 裡的名字跟介面上顯示的不一定一樣，
        # 例如 mjolnir_06 在檔案裡是「妙勒妙勒尼山脈南區」（原始資料就重複了）。
        forms.extend(
            stem for stem, chinese in names.items() if target in chinese or chinese in target
        )
    return list(dict.fromkeys(forms))


def _find_pid() -> int | None:
    from ro_toolbox.services.window_list import enumerate_windows

    for win in enumerate_windows():
        if win.process_name.lower() == _PROCESS:
            return win.pid
    return None


def _attach() -> MemoryScanner:
    pid = _find_pid()
    if pid is None:
        sys.exit(f"找不到 {_PROCESS}，請先開遊戲並登入")
    scanner = MemoryScanner()
    scanner.open(pid)
    print(f"已附加 {_PROCESS} pid={pid}")
    return scanner


def _hexdump(raw: bytes, base: int, width: int = 16) -> list[str]:
    out = []
    for i in range(0, len(raw), width):
        chunk = raw[i : i + width]
        hexes = " ".join(f"{b:02X}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{base + i:08X}  {hexes:<{width * 3}} |{text}|")
    return out


def cmd_scan(target: str) -> int:
    scanner = _attach()
    forms = _forms(target)
    print(f"搜尋 {forms}（編碼 {list(ENCODINGS)}）…")
    hits: list[dict] = []
    for form in forms:
        for addr, enc, blen in scanner.search_string(form, ENCODINGS, writable_only=True):
            hits.append({"addr": addr, "enc": enc, "len": blen, "text": form})
    REPORTS.mkdir(exist_ok=True)
    STATE.write_text(
        json.dumps({"pid": scanner.pid, "target": target, "hits": hits}, ensure_ascii=False),
        encoding="utf-8",
    )
    by_enc: dict[str, int] = {}
    for h in hits:
        by_enc[h["enc"]] = by_enc.get(h["enc"], 0) + 1
    print(f"命中 {len(hits):,} 處：{by_enc}")
    print(f"存到 {STATE}")
    print("→ 現在去遊戲裡把尋路目標改成**另一張圖**，再跑 narrow <那張圖>")
    return 0


def cmd_narrow(target: str) -> int:
    if not STATE.is_file():
        sys.exit(f"還沒有 {STATE}，請先跑 scan")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    scanner = _attach()
    if scanner.pid != state.get("pid"):
        sys.exit(f"行程換了（存的是 pid {state.get('pid')}，現在是 {scanner.pid}）—— 請重跑 scan")

    forms = _forms(target)
    print(f"在 {len(state['hits']):,} 個舊命中裡找『現在變成 {forms}』的…")
    survivors: list[dict] = []
    for hit in state["hits"]:
        enc = hit["enc"]
        text = scanner.read_string(hit["addr"], _READ_LEN, enc)
        if text is None:
            continue
        if any(text.startswith(f) for f in forms):
            survivors.append({**hit, "now": text})

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / "navi_narrow.json"
    out.write_text(
        json.dumps(
            {"pid": scanner.pid, "was": state["target"], "now": target, "hits": survivors},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"活下來 {len(survivors)} 個（{state['target']} → {target} 真的變了）")
    for hit in survivors[:20]:
        print(f"  {hit['addr']:08X}  {hit['enc']:<10} {hit['now']!r}")
    if not survivors:
        print("一個都沒有 —— 代表目標不是存成會改寫的字串緩衝區")
        print("（可能是指向 Lua 字串的指標）。下一步要改用指標掃描。")
    print(f"完整結果：{out}")
    if survivors:
        print("→ 再跑 dump 看每個位址前後的內容，找目標座標")
    return 0


def cmd_dump() -> int:
    out = REPORTS / "navi_narrow.json"
    if not out.is_file():
        sys.exit(f"還沒有 {out}，請先跑 narrow")
    state = json.loads(out.read_text(encoding="utf-8"))
    scanner = _attach()
    lines: list[str] = []
    for hit in state["hits"]:
        base = hit["addr"] - 64
        raw = scanner.read_region(base, 192)
        lines.append(f"\n=== {hit['addr']:08X}  {hit['enc']}  {hit['now']!r} ===")
        if raw is None:
            lines.append("  讀不到")
            continue
        lines.extend(_hexdump(bytes(raw), base))
    path = REPORTS / "navi_dump.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(state['hits'])} 個位址的前後 192 bytes → {path}")
    for line in lines[:40]:
        print(line)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "scan" and len(sys.argv) >= 3:
        return cmd_scan(sys.argv[2])
    if cmd == "narrow" and len(sys.argv) >= 3:
        return cmd_narrow(sys.argv[2])
    if cmd == "dump":
        return cmd_dump()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
