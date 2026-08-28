r"""實機跑一次「走去商人 → 買 HP 藥水到負重 69% → 走去船員」，用數字驗收。

    .venv\Scripts\python.exe tools\restock_test.py --item 502

這是把 `services/restock.py`（決策）與 `services/travel_bot.py`（走路）接起來
**跑一次真的**的臨時驅動 —— 正式流程還沒接進介面，先用它把每一段驗過。

會實際操作角色、實際花錢買東西。主控台只印結論，全量寫 reports\restock_test.json。

⚠ 需要系統管理員（封包擷取）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ro_toolbox.services.character import CharacterReader  # noqa: E402
from ro_toolbox.services.game_launcher import game_pids  # noqa: E402
from ro_toolbox.services.game_link import GameLink  # noqa: E402
from ro_toolbox.services.gamedata import (  # noqa: E402
    item_name,
    map_display_name,
    maps_with_potion_sellers,
    potion_sellers_on,
)
from ro_toolbox.services.mapdata import load_terrain  # noqa: E402
from ro_toolbox.services.restock import Restocker, RestockOrder  # noqa: E402
from ro_toolbox.services.travel import nearest_map_with, nearest_walkable  # noqa: E402
from ro_toolbox.services.travel_bot import TravelBot  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "restock_test.json"
#: 走一段路最多等多久（放棄上限，不是成功依據）。
WALK_GIVEUP = 180.0
#: 買東西那一段最多等多久。
SHOP_GIVEUP = 60.0


#: ⚠ 主控台在台灣是 cp950，印不出「⚠」這種字會直接 UnicodeEncodeError ——
#: 診斷工具因為印字而崩掉是最沒必要的失敗。印不出來就換成問號。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001 - 舊版／被導向時沒有這個方法，不影響功能
        pass


def note(text: str) -> None:
    print(f"  {text}", flush=True)


def walk_to(pid: int, where: str, cell: tuple[int, int] | None, log: list) -> bool:
    """走到 `where` 的 `cell`。回 False = 沒走到（原因已經印出來）。"""
    label = map_display_name(where) or where
    print(f"[走路] 前往 {label}（{where}）{cell or ''}", flush=True)
    bot = TravelBot(pid, destination=where, destination_cell=cell,
                    on_update=lambda s: log.append({"t": time.time(), "note": s.note}))
    bot.start()
    deadline = time.monotonic() + WALK_GIVEUP
    last = ""
    while bot.running and time.monotonic() < deadline:
        time.sleep(0.5)
        if bot.stats.note != last:
            last = bot.stats.note
            note(last)
    bot.stop()
    ok = bool(bot.stats.arrived)
    print(f"[走路] {'到了' if ok else '沒到'}：{bot.stats.note}", flush=True)
    return ok


def shop(pid: int, look: int, cell: tuple[int, int], order: RestockOrder,
         log: list, known: dict | None = None) -> Restocker:
    """站在商人旁邊之後的那一段：認人 → 開店 → 量單位重 → 買到 69%。

    `known` 是**走路途中**看到的實體 `{gid: (外觀, x, y, ...)}`。
    ⚠ 沒有它多半認不出商人：實體只在**進入視野**時送一次封包（[PKT-061]），
      而那一包是走過去的路上來的 —— 這裡的擷取是走到了才開，接不到。
      正式流程要由趕路那一段把 GID 帶過來（travel_bot 已經有同樣的機制）。
    """
    link = GameLink(pid, on_packet=lambda pkt: feed(pkt))
    bot = Restocker(lambda data: link.send(data), time.monotonic, order)

    def feed(pkt) -> None:
        bot.feed(pkt.opcode, pkt.payload)
        # 實體封包裡有 GID ＋ 外觀 ＋ 座標，認人靠它（[DAT-027]）
        for parsed in _entities(pkt):
            bot.note_entity(*parsed)

    problem = link.open()
    if problem:
        print(f"[買水] 接不上遊戲：{problem}", flush=True)
        return bot
    try:
        bot.start(look, cell)
        for gid, info in (known or {}).items():
            bot.note_entity(gid, info[0], info[1], info[2])
        deadline = time.monotonic() + SHOP_GIVEUP
        last = ""
        while bot.active and time.monotonic() < deadline:
            state = bot.update()
            if bot.stats.note != last:
                last = bot.stats.note
                note(last)
                log.append({"t": time.time(), "note": last})
            if state in ("done", "blocked"):
                break
            time.sleep(0.2)
    finally:
        link.close()
    return bot


def _entities(pkt):
    """從封包裡挖出 (GID, 外觀, x, y)。認不出來就什麼都不回。

    版面直接沿用 `travel_bot` 那一份（實測登入擷取確認），不要在這裡另外寫一套。
    """
    from ro_toolbox.core.ro_protocol import unpack_position
    from ro_toolbox.services import travel_bot as tb

    if pkt.opcode not in tb._OP_ENTITY or len(pkt.payload) < tb._ENT_POS + 3:
        return []
    if pkt.payload[tb._ENT_OBJTYPE] != tb._OBJTYPE_NPC:
        return []
    gid = int.from_bytes(pkt.payload[tb._ENT_GID:tb._ENT_GID + 4], "little")
    look = int.from_bytes(pkt.payload[tb._ENT_CLASS:tb._ENT_CLASS + 2], "little")
    x, y, _dir = unpack_position(pkt.payload[tb._ENT_POS:tb._ENT_POS + 3])
    return [(gid, look, x, y)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--item", type=int, default=502, help="要買的道具編號")
    ap.add_argument("--back-to", default="", help="買完之後走去哪（地圖代碼）")
    ap.add_argument("--back-cell", default="", help="買完之後走去哪一格 x,y")
    args = ap.parse_args()

    pid = args.pid or (game_pids() or [0])[0]
    if not pid:
        print("找不到遊戲行程", file=sys.stderr)
        return 1

    reader = CharacterReader()
    if not reader.attach(pid):
        print("角色定位失敗", file=sys.stderr)
        return 1
    status = reader.read()
    here = status.map_name if status else ""
    print(f"角色在 {map_display_name(here) or here}（{here}）{reader.read_position()}")

    sellers = potion_sellers_on(here)
    target_map = here
    if not sellers:
        found = nearest_map_with(here, set(maps_with_potion_sellers()))
        if found is None:
            print("附近找不到藥水商人", file=sys.stderr)
            return 1
        _route, target_map = found
        sellers = potion_sellers_on(target_map)
    x, y, name, look = sellers[0]
    terrain = load_terrain(target_map)
    cell = nearest_walkable(terrain, (x, y)) or (x, y)
    print(f"要找的商人：{name}（外觀 {look}）在 {target_map} ({x},{y})，走到 {cell}")

    log: list = []
    if not walk_to(pid, target_map, cell, log):
        return 1

    order = RestockOrder(hp_item=args.item)
    bought = shop(pid, look, (x, y), order, log)
    total = sum(bought.stats.bought.values())
    print(f"[買水] {bought.stats.note}")
    print(f"[買水] 共買了 {total} 個 {item_name(args.item)}；錢不夠={bought.stats.broke}")

    if args.back_to:
        back_cell = None
        if args.back_cell:
            bx, by = args.back_cell.split(",")
            back_cell = (int(bx), int(by))
        walk_to(pid, args.back_to, back_cell, log)

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"全量：{OUT}")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
