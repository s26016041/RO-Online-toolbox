"""技能面板：這隻角色學會的技能，分成「打怪」與「補助」兩區。

排版照遊戲的技能欄（名稱 → 圖示 → `◀ 7 / 10 ▶` → 勾選框），滑鼠移上去顯示
遊戲自己的技能說明。

- **只列學會的**（`level > 0`）。沒學的技能放在這裡只會讓人以為可以用。
- **分不出是打怪還是補助的不列**（`kind == UNKNOWN`）——
  硬塞進其中一區就是安靜地放錯地方（見 `services/skills._classify`）。
- 勾選與等級存的是**技能編號**，不是第幾格（CLAUDE.md：存身分，不存位置）。
- 補助技能要查得到「它會上哪個狀態」才給勾，查不到的**格子鎖起來**並在
  tooltip 說明為什麼 —— 讓人勾了卻沒反應是最糟的。

補助區的標題就是「自動補助技能」開關，**跟自動打怪完全獨立**（使用者指定）：
不掛機也可以只開這個。打怪那一區的勾選目前沒有動作，但一樣會存起來。
"""

from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ro_toolbox.services import icons
from ro_toolbox.services.buffs import BuffPlan, buff_efst
from ro_toolbox.services.skill_store import SkillSaved
from ro_toolbox.services.skills import ACTIVE, BUFF, Skill

#: RO 的小圖用洋紅當透明色（跟道具圖示一樣）。
_TRANSPARENT = "#ff00ff"
#: 圖示邊長。遊戲的技能圖是 24×24。
ICON_PX = 24
#: 一列放幾個技能。
COLUMNS = 4
#: 一格多寬。**固定寬度**，不然格子會被表格拉開，`◀ 7 / 10 ▶` 會散在兩端。
TILE_PX = 92
#: 說明文字裡的顏色碼。`^000000` 是「回到預設色」——**不要真的塗成黑色**，
#: 深色主題下會變成看不見的字。
_COLOR = re.compile(r"\^([0-9a-fA-F]{6})")
_DEFAULT_COLOR = "000000"

#: 查不到「會上哪個狀態」的補助技能，勾了也補不了 —— 說清楚為什麼。
UNUSABLE_TIP = (
    "<div><b>查不到它會上哪個狀態，沒辦法自動補。</b><br>"
    "這類技能多半本來就不上狀態（瞬間移動、物品鑑定、偷竊…）。</div><hr>"
)

_ICON_CACHE: dict[str, QPixmap] = {}


def skill_pixmap(key: str) -> QPixmap:
    """技能小圖。沒有就回空的 —— 介面照樣顯示文字，不拿別的圖來頂。"""
    got = _ICON_CACHE.get(key)
    if got is not None:
        return got
    pixmap = QPixmap()
    data = icons.skill_icon_bytes(key)
    if data and pixmap.loadFromData(data) and not pixmap.isNull():
        image = pixmap.toImage()
        image.setAlphaChannel(image.createMaskFromColor(
            QColor(_TRANSPARENT).rgb(), Qt.MaskMode.MaskOutColor
        ))
        pixmap = QPixmap.fromImage(image)
    else:
        pixmap = QPixmap()
    _ICON_CACHE[key] = pixmap
    return pixmap


def description_html(lines) -> str:
    """把遊戲的技能說明轉成 tooltip 用的 HTML，顏色碼照著上色。"""
    rows = []
    for line in lines:
        parts = _COLOR.split(line)
        buf = [html.escape(parts[0])]
        for index in range(1, len(parts), 2):
            color, text = parts[index], html.escape(parts[index + 1])
            if not text:
                continue
            if color.lower() == _DEFAULT_COLOR:
                buf.append(text)          # 預設色交給主題決定
            else:
                buf.append(f'<span style="color:#{color}">{text}</span>')
        rows.append("".join(buf) or "&nbsp;")
    return "<div>" + "<br>".join(rows) + "</div>"


class SkillTile(QWidget):
    """一個技能格：名稱、圖示、等級調整、勾選框。"""

    changed = Signal()

    def __init__(self, skill: Skill, level: int, checked: bool) -> None:
        super().__init__()
        self.skill = skill
        self._level = max(1, min(level or skill.level, skill.level))
        self.setObjectName("skillTile")
        self.setFixedWidth(TILE_PX)

        box = QVBoxLayout(self)
        box.setContentsMargins(4, 4, 4, 4)
        box.setSpacing(2)

        name = QLabel(skill.name)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setObjectName("skillName")
        box.addWidget(name)

        icon = QLabel()
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = skill_pixmap(skill.key)
        if not pixmap.isNull():
            icon.setPixmap(pixmap)
        else:
            icon.setText("—")
        icon.setFixedHeight(ICON_PX + 4)
        box.addWidget(icon)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.down = QToolButton()
        self.down.setText("◀")
        self.down.setAutoRaise(True)
        self.down.clicked.connect(lambda: self._step(-1))
        self.value = QLabel()
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.up = QToolButton()
        self.up.setText("▶")
        self.up.setAutoRaise(True)
        self.up.clicked.connect(lambda: self._step(1))
        row.addWidget(self.down)
        row.addWidget(self.value, 1)
        row.addWidget(self.up)
        box.addLayout(row)

        self.check = QCheckBox()
        self.check.setChecked(checked and self.usable)
        self.check.setEnabled(self.usable)
        self.check.stateChanged.connect(lambda _s: self.changed.emit())
        box.addWidget(self.check, 0, Qt.AlignmentFlag.AlignCenter)

        tip = description_html(skill.description()) if skill.description() else skill.name
        if not self.usable:
            tip = UNUSABLE_TIP + tip
        self.setToolTip(tip)
        for child in (name, icon, self.value, self.check):
            child.setToolTip(tip)
        self._refresh()

    @property
    def usable(self) -> bool:
        """勾了有沒有用。

        補助技能要有「它會上哪個狀態」才補得了 —— 沒有那個對應就沒辦法確認
        補上了沒，只能瞎送（見 `services/buffs.py`）。查不到的多半本來就不上狀態
        （瞬間移動、物品鑑定、偷竊…），所以**不給勾**比讓人勾了沒反應好。
        """
        return self.skill.kind != BUFF or buff_efst(self.skill.id) is not None

    # ---- 等級 -------------------------------------------------------

    @property
    def level(self) -> int:
        return self._level

    @property
    def checked(self) -> bool:
        return self.check.isChecked()

    def _step(self, delta: int) -> None:
        """調整要用第幾級。**上限是學到的等級**，不是技能的最大等級 ——
        送一個沒學到的等級出去，伺服器只會把它丟掉。"""
        wanted = max(1, min(self._level + delta, self.skill.level))
        if wanted != self._level:
            self._level = wanted
            self._refresh()
            self.changed.emit()

    def _refresh(self) -> None:
        self.value.setText(f"{self._level} / {self.skill.level}")
        self.down.setEnabled(self._level > 1)
        self.up.setEnabled(self._level < self.skill.level)


class SkillPanel(QWidget):
    """打怪／補助兩區的技能格。內容跟著角色學到的技能走。"""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._tiles: dict[int, SkillTile] = {}
        self._skills: list[Skill] = []
        self._saved = SkillSaved()

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        self.note = QLabel("還沒讀到技能。")
        self.note.setObjectName("pageSubtitle")
        box.addWidget(self.note)

        self._grids: dict[str, QGridLayout] = {}
        self._sections: dict[str, QWidget] = {}
        #: 「自動補助技能」的開關。**跟自動打怪完全獨立**（使用者指定）——
        #: 不掛機也可以只開這個。
        self.auto = QCheckBox("自動補助技能（沒有或剩不到 10 秒就補）")
        self.auto.toggled.connect(lambda _on: self.changed.emit())
        for kind, title in ((ACTIVE, "打怪技能"), (BUFF, "")):
            section = QWidget()
            column = QVBoxLayout(section)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            head = self.auto if kind == BUFF else QLabel(title)
            head.setObjectName("skillSection")
            column.addWidget(head)
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(4)
            # 最後多一欄吃掉剩下的寬度，格子才會靠左排而不是被平均拉開。
            grid.setColumnStretch(COLUMNS, 1)
            column.addLayout(grid)
            box.addWidget(section)
            self._grids[kind] = grid
            self._sections[kind] = section
            section.setVisible(False)

    # ---- 內容 -------------------------------------------------------

    def set_skills(self, skills) -> None:
        """換掉顯示的技能。等級與勾選**依技能編號**接回去，不看順序。

        比對的是 `(編號, 等級)` 而不是只有編號 —— 升級之後上限要跟著放寬，
        只看編號的話會一直用舊的上限，使用者會發現「明明 9 級卻只能選到 7」。
        """
        wanted = [s for s in skills if s.learned and s.kind in (ACTIVE, BUFF)]

        def shape(items):
            return [(s.id, s.level) for s in items]

        if self._tiles and shape(wanted) == shape(self._skills):
            return
        if self._tiles:
            # 重建會把現在的格子丟掉 —— 先把使用者調到一半的設定收回來。
            self._saved = self.snapshot()
        self._skills = wanted
        self._rebuild()

    def _rebuild(self) -> None:
        for tile in self._tiles.values():
            tile.setParent(None)
            tile.deleteLater()
        self._tiles.clear()

        counts = {ACTIVE: 0, BUFF: 0}
        for skill in self._skills:
            grid = self._grids[skill.kind]
            saved_levels = self._saved.buffs if skill.kind == BUFF else self._saved.levels
            level = saved_levels.get(skill.id, skill.level)
            checked = skill.id in self._saved.buffs if skill.kind == BUFF else (
                skill.id in self._saved.levels
            )
            tile = SkillTile(skill, level, checked)
            tile.changed.connect(self.changed)
            index = counts[skill.kind]
            grid.addWidget(tile, index // COLUMNS, index % COLUMNS)
            counts[skill.kind] = index + 1
            self._tiles[skill.id] = tile

        for kind, count in counts.items():
            self._sections[kind].setVisible(count > 0)
        total = sum(counts.values())
        self.note.setText(
            "還沒讀到技能。" if not total
            else f"打怪 {counts[ACTIVE]} 個、補助 {counts[BUFF]} 個"
        )
        self.note.setVisible(not total)

    # ---- 設定進出 ---------------------------------------------------

    def apply_saved(self, saved: SkillSaved) -> None:
        self._saved = saved
        # ⚠ 用 blockSignals：套用設定不該被當成「使用者按了開關」，
        # 否則載入的當下就會去啟動 bot（而且是在還沒讀到技能之前）。
        self.auto.blockSignals(True)
        self.auto.setChecked(saved.auto)
        self.auto.blockSignals(False)
        self._rebuild()

    @property
    def auto_enabled(self) -> bool:
        return self.auto.isChecked()

    def snapshot(self) -> SkillSaved:
        """現在的勾選狀態。**只存勾起來的** —— 沒勾的存進去只是雜訊。"""
        buffs: dict[int, int] = {}
        levels: dict[int, int] = {}
        for skill_id, tile in self._tiles.items():
            if not tile.checked:
                continue
            target = buffs if tile.skill.kind == BUFF else levels
            target[skill_id] = tile.level
        return SkillSaved(buffs=buffs, levels=levels, auto=self.auto.isChecked())

    def buff_plans(self) -> list[BuffPlan]:
        """勾起來的補助技能，交給 `BuffKeeper`。"""
        return [
            BuffPlan(skill_id, tile.level)
            for skill_id, tile in sorted(self._tiles.items())
            if tile.checked and tile.skill.kind == BUFF
        ]
