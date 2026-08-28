r"""列出執行中 RO 角色目前擁有的技能（等級、SP、射程）。

    .venv\Scripts\python.exe tools\show_skills.py
    .venv\Scripts\python.exe tools\show_skills.py --all      # 連還沒點的也列

位址一律當場掃出來（見 CLAUDE.md），不記絕對位址、不靠封包。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ro_toolbox.services import window_list  # noqa: E402
from ro_toolbox.services.process_monitor import is_admin  # noqa: E402
from ro_toolbox.services.skills import SkillReader  # noqa: E402

PROCESS = "ragexe.exe"


def main() -> int:
    parser = argparse.ArgumentParser(description="顯示 RO 角色的技能")
    parser.add_argument("pid", nargs="?", type=int, help="指定 RO 行程 PID")
    parser.add_argument("--all", action="store_true", help="連等級 0（還沒點）的也列")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not is_admin():
        print("提醒：不是系統管理員，可能讀不到遊戲記憶體。", file=sys.stderr)

    targets = [
        w for w in window_list.enumerate_windows()
        if w.process_name.lower() == PROCESS
    ]
    if args.pid:
        targets = [w for w in targets if w.pid == args.pid]
    if not targets:
        print(f"找不到執行中的 {PROCESS}")
        return 1

    exit_code = 1
    for win in targets:
        reader = SkillReader()
        if not reader.attach(win.pid):
            print(f"PID {win.pid}：附加失敗")
            continue
        started = time.monotonic()
        skills = reader.read()
        elapsed = time.monotonic() - started
        reader.close()
        if skills is None:
            print(f"PID {win.pid}：讀不到技能表（詳見上面的訊息）")
            continue

        exit_code = 0
        shown = [s for s in skills if args.all or s.learned]
        learned = sum(1 for s in skills if s.learned)
        print(f"\nPID {win.pid}　技能 {len(skills)} 個（已學會 {learned} 個）"
              f"　掃描 {elapsed:.1f}s")
        print(f"  {'ID':>6}  {'等級':<7} {'SP':>4}  {'代號':<24}名稱")
        for skill in shown:
            level = f"{skill.level}/{skill.max_level}"
            mark = " " if skill.learned else "·"
            print(f"{mark} {skill.id:>6}  {level:<7} {skill.sp:>4}  "
                  f"{skill.key:<24}{skill.name}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
