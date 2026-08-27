r"""實機跑一輪自動打怪，用數字驗收（不靠「看起來有在動」）。

    .venv\Scripts\python.exe tools\farm_test.py 27992 --seconds 70

會印出：走了多少格、停頓多久、被伺服器忽略的移動幾次、看到幾隻怪、
擊殺／撿取幾個。詳細軌跡寫到 reports\farm_test_<pid>.json，主控台只留結論。

需要系統管理員（Npcap 擷取）。會實際操作該角色，請確認那隻角色可以動。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ro_toolbox.services.character import CharacterReader  # noqa: E402
from ro_toolbox.services.farm_bot import FarmBot  # noqa: E402
from ro_toolbox.services.gamedata import item_name, mob_name  # noqa: E402
from ro_toolbox.services.process_monitor import is_admin  # noqa: E402

_SAMPLE = 0.25  # 位置取樣間隔（走路速度約 1 格/0.15 秒）
_REPORTS = Path(__file__).resolve().parents[1] / "reports"


def _phase(bot: FarmBot) -> str:
    """這一拍 bot 在做什麼 —— 交戰時站著不動是正常的，要跟「卡住」分開算。"""
    if bot._aim is not None:  # noqa: SLF001 - 驗收腳本，要看內部狀態
        return "打怪"
    if bot._world.ground_items():  # noqa: SLF001
        return "撿東西"
    return "走路"


def main() -> int:
    parser = argparse.ArgumentParser(description="實機跑一輪自動打怪並統計")
    parser.add_argument("pid", type=int, help="RO 行程 PID")
    parser.add_argument("--seconds", type=float, default=70.0)
    parser.add_argument("--memory", action="store_true",
                        help="把記憶體掃到的怪也算進來（預設關閉，見 [MEM-014]）")
    args = parser.parse_args()

    if not is_admin():
        print("⚠ 需要系統管理員（Npcap 擷取）", file=sys.stderr)

    reader = CharacterReader()
    if not reader.attach(args.pid):
        print("角色定位失敗", file=sys.stderr)
        return 1
    status = reader.read()
    print(f"角色 {status.name}　地圖 {status.map_name}　起點 {reader.read_position()}")
    print(f"  Base {status.base_level} {status.base_exp:,}/{status.base_exp_next:,}"
          f"（{status.base_percent:.2f}%）　"
          f"Job {status.job_level} {status.job_exp:,}/{status.job_exp_next:,}"
          f"（{status.job_percent:.2f}%）")
    exp0 = (status.base_exp, status.job_exp)
    level0 = (status.base_level, status.job_level)

    notes: list[str] = []
    bot = FarmBot(args.pid, on_update=lambda s: notes.append(s.note),
                  use_memory=args.memory)
    bot.start()

    # 診斷用的第二個來源：唯讀的記憶體掃描。**不影響 bot 的決策**，
    # 只是拿來回答「明明周圍有怪，程式卻說沒有」到底是不是真的漏看。
    # 它不碰封包，跟 bot 的擷取不衝突。
    probe = None
    try:
        from ro_toolbox.services.entities import EntityScanner
        from ro_toolbox.services.mapdata import load_terrain

        probe = EntityScanner(load_terrain(status.map_name), status.map_name, view=30)
        if not probe.open(args.pid):
            probe = None
    except Exception:  # noqa: BLE001 - 診斷失敗不該擋住驗收
        probe = None

    track: list[tuple[float, int, int]] = []
    phases: list[str] = []  # 每個取樣點當下在做什麼：打怪／撿東西／走路
    near: list[int] = []  # 每個取樣點看得到幾隻怪
    blind: list[int] = []  # 記憶體看得到、但 bot（封包）沒看到的怪有幾隻
    ghost: list[int] = []  # bot 以為在、但記憶體找不到的怪有幾隻
    probe_at = 0.0
    costs: list[float] = []  # 記憶體掃描每次花多久
    start = time.monotonic()
    while time.monotonic() - start < args.seconds:
        pos = reader.read_position()
        if pos:
            track.append((round(time.monotonic() - start, 2), pos[0], pos[1]))
            phases.append(_phase(bot))
            near.append(bot.stats.monsters_near)
            scanner = bot._entities  # noqa: SLF001 - 驗收腳本，要看內部計數
            if scanner is not None:
                costs.append(scanner.last_cost)
            # 每秒比對一次就夠（記憶體掃描不便宜）
            if probe is not None and time.monotonic() - probe_at > 1.0:
                probe_at = time.monotonic()
                seen = {m.gid for m in bot._world.monsters()}  # noqa: SLF001
                real = {e.gid for e in probe.scan(pos)}
                blind.append(len(real - seen))
                ghost.append(len(seen - real))
        time.sleep(_SAMPLE)
    stats = bot.stats
    world = bot._world  # noqa: SLF001 - 驗收腳本，要看內部計數
    walker_sent, walker_rejected = bot._walker.sent, bot._walker.rejected  # noqa: SLF001
    bot.stop()
    if probe is not None:
        probe.close()
    after = reader.read()
    reader.close()

    walked = sum(
        max(abs(b[1] - a[1]), abs(b[2] - a[2])) for a, b in zip(track, track[1:], strict=False)
    )
    still = sum(1 for a, b in zip(track, track[1:], strict=False) if (a[1], a[2]) == (b[1], b[2]))
    longest, run = 0, 0
    idle_fight = idle_other = 0
    for i, (a, b) in enumerate(zip(track, track[1:], strict=False)):
        if (a[1], a[2]) == (b[1], b[2]):
            run += 1
            if phases[i] == "打怪":
                idle_fight += 1
            else:
                idle_other += 1
        else:
            run = 0
        longest = max(longest, run)

    loot = bot.loot()
    _REPORTS.mkdir(exist_ok=True)
    out = _REPORTS / f"farm_test_{args.pid}.json"
    out.write_text(
        json.dumps(
            {
                "seconds": args.seconds,
                "track": track,
                "phases": phases,
                "notes": notes[-200:],
                "loot": {str(k): v for k, v in loot.items()},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(f"\n=== {args.seconds:.0f} 秒結果 ===")
    print(f"  走了 {walked} 格（起 {track[0][1:]} → 迄 {track[-1][1:]}）")
    print(f"  取樣 {len(track)} 次，其中 {still} 次原地不動；最長連續不動 "
          f"{longest * _SAMPLE:.1f} 秒")
    print(f"    └ 不動的原因：交戰中 {idle_fight * _SAMPLE:.1f} 秒（正常）、"
          f"非交戰 {idle_other * _SAMPLE:.1f} 秒（這才是卡住）")
    print(f"  移動封包送出 {walker_sent} 次，被伺服器忽略 {walker_rejected} 次")
    print(f"  擊殺 {stats.kills}　撿取 {stats.picked}　最後看到附近怪 {stats.monsters_near}")
    print(f"  打到空氣（座標過時）{stats.missed} 次")
    print(f"  補送攻擊 {stats.resent} 次（接近 0 就代表補送機制幾乎沒在用）")
    if blind:
        print(f"  ⚠ 記憶體看得到但 bot 沒看到的怪：平均 {sum(blind)/len(blind):.2f} 隻、"
              f"最多 {max(blind)} 隻（{sum(1 for b in blind if b)}/{len(blind)} 次取樣有漏）")
        print(f"  ⚠ bot 以為在但記憶體找不到的怪：平均 {sum(ghost)/len(ghost):.2f} 隻、"
              f"最多 {max(ghost)} 隻（幽靈怪：打過去會是空氣）")
    print(f"  怪物封包驗證失敗 {world.rejected} 次（一直增加代表封包版面變了）")
    if near:
        print(f"  視野內怪物：平均 {sum(near) / len(near):.1f} 隻、最多 {max(near)} 隻")
    if costs:
        print(f"  記憶體掃描：平均 {sum(costs) / len(costs) * 1000:.0f} ms、"
              f"最久 {max(costs) * 1000:.0f} ms（每 {_SAMPLE}s 一次）")
    if after is not None and after.has_exp:
        hours = args.seconds / 3600
        # 升級後經驗會歸零重算，直接相減會變負數 —— 那是升級，不是倒扣
        parts = []
        for label, before, now, lv_before, lv_now in (
            ("Base", exp0[0], after.base_exp, level0[0], after.base_level),
            ("Job", exp0[1], after.job_exp, level0[1], after.job_level),
        ):
            if lv_now > lv_before:
                parts.append(f"{label} 升了 {lv_now - lv_before} 級")
            else:
                parts.append(f"{label} +{now - before:,}"
                             f"（每小時 {(now - before) / hours:,.0f}）")
        print("  經驗：" + "　".join(parts))
        print(f"  現在 Base {after.base_level} {after.base_percent:.2f}%　"
              f"Job {after.job_level} {after.job_percent:.2f}%")
    seen = {mob_name(m.class_id) for m in world.monsters()}
    if seen:
        print(f"  結束時視野內：{'、'.join(sorted(seen))}")
    if loot:
        print("  撿到：" + "、".join(f"{item_name(i)}×{n}" for i, n in loot.items()))
    print(f"  詳細軌跡：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
