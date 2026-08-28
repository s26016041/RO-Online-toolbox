"""技能面板：只列學會的、分對兩區、勾選與等級照技能編號記住。"""

from __future__ import annotations

import pytest

from ro_toolbox.services.skill_store import SkillSaved
from ro_toolbox.services.skills import ACTIVE, BUFF, PASSIVE, UNKNOWN, Skill

pytest.importorskip("PySide6.QtWidgets")

from ro_toolbox.ui.widgets.skill_panel import (  # noqa: E402
    SkillPanel,
    description_html,
    skill_pixmap,
)

BASH = Skill(5, "SM_BASH", "狂擊", 10, 10, 15, ACTIVE)
QUICKEN = Skill(60, "KN_TWOHANDQUICKEN", "雙手劍攻擊速度增加", 7, 10, 38, BUFF)
ENDURE = Skill(8, "SM_ENDURE", "霸體", 8, 10, 10, BUFF)
SWORD = Skill(2, "SM_SWORD", "單手劍使用熟練度", 1, 10, 0, PASSIVE)
UNLEARNED = Skill(56, "KN_PIERCE", "連刺攻擊", 0, 10, 7, ACTIVE)
MYSTERY = Skill(144, "SM_MOVINGRECOVERY", "移動時恢復HP", 3, 1, 0, UNKNOWN)


@pytest.fixture
def panel(qapp):  # noqa: ARG001 - 需要 QApplication 才能建 widget
    return SkillPanel()


def test_lists_only_learned_castable_skills(panel):
    """沒學的、被動的、分不出類的都不列 —— 列出來只會讓人以為可以用。"""
    panel.set_skills([BASH, QUICKEN, SWORD, UNLEARNED, MYSTERY])
    assert set(panel._tiles) == {BASH.id, QUICKEN.id}


def test_splits_into_two_sections(panel):
    panel.set_skills([BASH, QUICKEN])
    assert panel._tiles[BASH.id].skill.kind == ACTIVE
    assert panel._tiles[QUICKEN.id].skill.kind == BUFF
    assert [p.skill_id for p in panel.buff_plans()] == []   # 還沒勾


def test_level_cannot_go_above_what_was_learned(panel):
    """上限是**學到的等級**不是技能最大等級 —— 送沒學到的等級只會被丟掉。"""
    panel.set_skills([QUICKEN])
    tile = panel._tiles[QUICKEN.id]
    assert tile.level == 7
    for _ in range(5):
        tile._step(1)
    assert tile.level == 7
    for _ in range(20):
        tile._step(-1)
    assert tile.level == 1


def test_checked_buffs_become_plans(panel):
    panel.set_skills([BASH, QUICKEN, ENDURE])
    panel._tiles[QUICKEN.id].check.setChecked(True)
    panel._tiles[QUICKEN.id]._step(-1)
    panel._tiles[BASH.id].check.setChecked(True)     # 打怪的不該進 buff 計畫

    plans = panel.buff_plans()
    assert [(p.skill_id, p.level) for p in plans] == [(QUICKEN.id, 6)]


def test_saved_settings_come_back_by_skill_id(panel):
    """技能列表會因為學了新技能而重排 —— 存編號才接得回去。"""
    panel.apply_saved(SkillSaved(buffs={QUICKEN.id: 5}, levels={BASH.id: 3}))
    panel.set_skills([BASH, QUICKEN])
    assert panel._tiles[QUICKEN.id].checked
    assert panel._tiles[QUICKEN.id].level == 5
    assert panel._tiles[BASH.id].checked
    assert panel._tiles[BASH.id].level == 3

    # 學了新技能，順序整個變了，設定照樣接得回去
    panel.set_skills([ENDURE, BASH, QUICKEN])
    assert panel._tiles[QUICKEN.id].level == 5
    assert not panel._tiles[ENDURE.id].checked


def test_snapshot_only_keeps_what_is_checked(panel):
    panel.set_skills([BASH, QUICKEN, ENDURE])
    panel._tiles[QUICKEN.id].check.setChecked(True)

    saved = panel.snapshot()
    assert saved.buffs == {QUICKEN.id: 7}
    assert saved.levels == {}


def test_learned_map_survives_a_snapshot(panel):
    """學到的「技能 → 狀態」對應是知識，存檔要帶著。"""
    panel.set_skills([QUICKEN])
    panel.remember_learned({QUICKEN.id: 2})
    assert panel.snapshot().learned == {QUICKEN.id: 2}


def test_level_cap_follows_a_level_up(panel):
    panel.set_skills([QUICKEN])
    panel._tiles[QUICKEN.id].check.setChecked(True)
    stronger = Skill(60, "KN_TWOHANDQUICKEN", "雙手劍攻擊速度增加", 9, 10, 46, BUFF)
    panel.apply_saved(panel.snapshot())
    panel.set_skills([stronger])

    tile = panel._tiles[QUICKEN.id]
    tile._step(1)
    tile._step(1)
    assert tile.level == 9


def test_description_keeps_colours_but_not_the_default_one():
    """`^000000` 是「回到預設色」，真的塗黑的話深色主題會看不見。"""
    out = description_html(["^993300主動^000000 一般", "^ffffff_^000000"])
    assert 'color:#993300' in out
    assert 'color:#000000' not in out
    assert "&lt;" not in out            # 沒有需要跳脫的內容時不該冒出實體


def test_description_escapes_html():
    assert "&lt;b&gt;" in description_html(["<b>不是標籤"])


def test_missing_icon_is_not_a_crash():
    assert skill_pixmap("NOT_A_REAL_SKILL").isNull()


def test_real_skill_icon_loads():
    """圖示從打包資產來 —— 使用者的電腦沒有 RODATA。"""
    assert not skill_pixmap("SM_BASH").isNull()
