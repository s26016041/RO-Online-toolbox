r"""查詢怪物表。

    .\.venv\Scripts\python.exe tools\find_mob.py 1080        # 查 class ID
    .\.venv\Scripts\python.exe tools\find_mob.py 草          # 查名稱
    .\.venv\Scripts\python.exe tools\find_mob.py --map moc_fild01   # 查某張圖出什麼
    .\.venv\Scripts\python.exe tools\find_mob.py --plants    # 只列草

資料來自 assets/mobs.json.gz，用 tools\build_mob_table.py 產生。
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

TABLE = Path(__file__).resolve().parents[1] / "assets" / "mobs.json.gz"
_LIMIT = 40


def load() -> tuple[dict, dict]:
    if not TABLE.exists():
        print(f"找不到 {TABLE}，請先跑 tools/build_mob_table.py", file=sys.stderr)
        sys.exit(1)
    with gzip.open(TABLE, "rt", encoding="utf-8") as handle:
        table = json.load(handle)
    meta = table.pop("_meta")
    return meta, table


def describe(mid: str, entry: dict, meta: dict, verbose: bool) -> str:
    race = meta["race"].get(str(entry.get("race")), "?")
    size = meta["size"].get(str(entry.get("size")), "?")
    ele = meta["element"].get(str(entry.get("ele")), "?")
    tags = []
    if entry.get("kind") == "plant":
        tags.append("草")
    elif entry.get("kind") == "plant?":
        tags.append("疑似草(無等級可證)")
    elif entry.get("kind") is None:
        tags.append("無 navi_mob 資料")
    if entry.get("boss"):
        tags.append("MVP")
    if entry.get("level") is not None:
        tags.append(f"lv{entry['level']} {size}{race} {ele}{entry.get('ele_lv', '')}")
    if entry.get("maps"):
        tags.append(f"{len(entry['maps'])} 張圖")
    name = entry.get("name") or entry.get("en") or "?"
    line = f"{mid:>6}  {name:<10} {entry.get('res', ''):<20} [{' · '.join(tags)}]"
    if verbose and entry.get("maps"):
        top = sorted(entry["maps"].items(), key=lambda kv: -kv[1])[:12]
        line += "\n         " + "、".join(f"{m}×{n}" for m, n in top)
    return line


def main() -> None:
    ap = argparse.ArgumentParser(description="查詢怪物表")
    ap.add_argument("query", nargs="?", default="", help="class ID 或名稱片段")
    ap.add_argument("--map", help="只列這張地圖會出的怪")
    ap.add_argument("--plants", action="store_true", help="只列判定為草的")
    ap.add_argument("--boss", action="store_true", help="只列 MVP")
    ap.add_argument("--all", action="store_true", help="不截斷輸出")
    ap.add_argument("-v", "--verbose", action="store_true", help="附出沒地圖")
    args = ap.parse_args()

    meta, table = load()
    hits = list(table.items())

    if args.map:
        hits = [(m, e) for m, e in hits if args.map in e.get("maps", {})]
    if args.plants:
        hits = [(m, e) for m, e in hits if str(e.get("kind", "")).startswith("plant")]
    if args.boss:
        hits = [(m, e) for m, e in hits if e.get("boss")]
    if args.query:
        q = args.query
        if q.isdigit():
            hits = [(m, e) for m, e in hits if m == q]
        else:
            ql = q.lower()
            hits = [(m, e) for m, e in hits
                    if ql in e.get("name", "").lower()
                    or ql in e.get("res", "").lower()
                    or ql in e.get("en", "").lower()]

    if not hits:
        print("查無資料")
        return

    hits.sort(key=lambda kv: int(kv[0]))
    shown = hits if args.all else hits[:_LIMIT]
    for mid, entry in shown:
        print(describe(mid, entry, meta, args.verbose))
    if len(shown) < len(hits):
        print(f"... 共 {len(hits)} 筆，只顯示前 {len(shown)} 筆（--all 全列）")


if __name__ == "__main__":
    main()
