"""「寄信設定」的對話框：挑背包裡的東西、各填一個數量、選寄給誰。

使用者指定（2026-08-30）：

- 「點下去會出現視窗可以選擇**全部背包物品可以選多個**，然後每個的數量」
- 「**只要那樣物品數量達到我選擇的就會寄信，不需要全部湊齊才寄**」
- 「還可以選擇寄信給誰」、「寄信設定要有個**啟用**」
- 「這些一切都要記錄」（存檔見 `services/mail_store`）

⚠ 表格列出的是**背包現在有的東西**，但存檔存的是**道具編號**
（CLAUDE.md：存身分，不存位置）。東西暫時用完了設定也不該消失 ——
所以已經設過的規則就算背包裡沒有也照樣列出來，數量顯示 0。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ro_toolbox.services.gamedata import item_name
from ro_toolbox.services.mail_store import MailRule, MailSaved

#: 數量欄的上限。跟 `mail_store` 那邊的檢查同一個意思。
_MAX_AMOUNT = 30000


class MailDialog(QDialog):
    """按下「確定」之後從 `config` 拿設定；按取消是 None。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        saved: MailSaved | None = None,
        bag_counts: dict[int, int] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("寄信設定")
        self.setMinimumWidth(460)
        self.config: MailSaved | None = None
        self._saved = saved or MailSaved()
        self._counts = dict(bag_counts or {})
        self._build()

    # ---- 版面 -------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        self.enabled = QCheckBox("啟用自動寄信")
        self.enabled.setChecked(self._saved.enabled)
        layout.addWidget(self.enabled)

        row = QHBoxLayout()
        row.addWidget(QLabel("寄給"))
        self.receiver = QLineEdit(self._saved.receiver)
        self.receiver.setPlaceholderText("收件人的角色名稱")
        # 封包的名字欄位是 24 bytes（cp950），中文一個字兩 bytes。
        self.receiver.setMaxLength(24)
        row.addWidget(self.receiver, 1)
        layout.addLayout(row)

        hint = QLabel(
            "勾起來的東西，**任何一樣**湊到指定數量就會自己寄一封 —— 不用等全部湊齊。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("pageSubtitle")
        layout.addWidget(hint)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["寄", "道具", "湊到幾個就寄"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        self._fill()

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save = box.button(QDialogButtonBox.StandardButton.Save)
        save.setText("儲存")
        save.setObjectName("primaryButton")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _rows(self) -> list[tuple[int, int]]:
        """要列出來的 `(道具編號, 背包裡有幾個)`。

        ⚠ **已經設過的規則一定要列出來**，就算背包裡現在沒有 ——
        東西暫時用完了設定不該跟著消失（存的是編號，不是背包狀態）。
        """
        ids = dict(self._counts)
        for rule in self._saved.rules:
            ids.setdefault(rule.item_id, 0)
        return sorted(ids.items(), key=lambda pair: item_name(pair[0]) or str(pair[0]))

    def _fill(self) -> None:
        wanted = {rule.item_id: rule.amount for rule in self._saved.rules}
        rows = self._rows()
        self.table.setRowCount(len(rows))
        self._checks: dict[int, QCheckBox] = {}
        self._amounts: dict[int, QSpinBox] = {}
        for line, (item_id, have) in enumerate(rows):
            check = QCheckBox()
            check.setChecked(item_id in wanted)
            holder = QWidget()
            box = QHBoxLayout(holder)
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(check, 0, Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(line, 0, holder)
            self._checks[item_id] = check

            name = item_name(item_id) or f"#{item_id}"
            label = QTableWidgetItem(f"{name}（現有 {have}）")
            label.setData(Qt.ItemDataRole.UserRole, item_id)
            self.table.setItem(line, 1, label)

            spin = QSpinBox()
            spin.setRange(1, _MAX_AMOUNT)
            spin.setValue(wanted.get(item_id) or max(1, have))
            self.table.setCellWidget(line, 2, spin)
            self._amounts[item_id] = spin

    # ---- 存 ---------------------------------------------------------

    def _accept(self) -> None:
        rules = tuple(
            MailRule(item_id, self._amounts[item_id].value())
            for item_id, check in self._checks.items()
            if check.isChecked()
        )
        self.config = MailSaved(
            receiver=self.receiver.text().strip(),
            rules=rules,
            enabled=self.enabled.isChecked(),
        )
        self.accept()
