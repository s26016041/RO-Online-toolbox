r"""查詢物品表。

    .\.venv\Scripts\python.exe tools\find_item.py 501
    .\.venv\Scripts\python.exe tools\find_item.py 藥水
    .\.venv\Scripts\python.exe tools\find_item.py 盾 --equip

資料來自 assets/items.json.gz，用 tools\build_item_table.py 產生。
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

TABLE = Path(__file__).resolve().parents[1] / "assets" / "items.json.gz"
_LIMIT = 30


def load() -> dict:
    if not TABLE.exists():
        print(f"找不到 {TABLE}，請先跑 tools/build_item_table.py", file=sys.stderr)
        sys.exit(1)
    with gzip.open(TABLE, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def show(item_id: str, entry: dict, verbose: bool) -> None:
    tags = []
    if entry.get("equip"):
        parts = entry.get("equip_at")
        tags.append("裝備" + ("（" + "／".join(parts) + "）" if parts else ""))
        if entry.get("slots"):
            tags.append(f"{entry['slots']}插槽")
    if entry.get("en"):
        tags.append(entry["en"])

    suffix = f"  [{' · '.join(tags)}]" if tags else ""
    print(f"{item_id:>6}  {entry['name']}{suffix}")

    if verbose and entry.get("desc"):
        for line in entry["desc"].splitlines():
            print(f"        {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="查 RO 物品 ID 或名稱")
    parser.add_argument("query", help="物品 ID，或名稱關鍵字")
    parser.add_argument("--equip", action="store_true", help="只列裝備")
    parser.add_argument("--all", action="store_true", help="不限制筆數")
    parser.add_argument("-v", "--verbose", action="store_true", help="顯示說明文")
    args = parser.parse_args()

    items = load()

    if args.query.isdigit() and args.query in items:
        show(args.query, items[args.query], verbose=True)
        return 0

    keyword = args.query
    hits = [
        (i, e)
        for i, e in items.items()
        if keyword in e["name"] or keyword.lower() in e.get("en", "").lower()
    ]
    if args.equip:
        hits = [(i, e) for i, e in hits if e.get("equip")]

    hits.sort(key=lambda kv: int(kv[0]))
    capped = not args.all and len(hits) > _LIMIT
    print(f"命中 {len(hits)} 筆" + (f"，顯示前 {_LIMIT} 筆" if capped else ""))
    for item_id, entry in (hits if args.all else hits[:_LIMIT]):
        show(item_id, entry, args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
