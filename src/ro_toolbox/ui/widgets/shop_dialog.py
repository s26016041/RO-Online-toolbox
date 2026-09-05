"""「設定藥水商人」的視窗：使用者自己挑補水要去哪個城鎮的哪一個商人。

使用者指定（2026-09-05）：「商人我們根本不知道要找誰，所以改成使用者自己設定。
補水右邊多一個按鈕叫『設定藥水商人』，點了先讓他選哪個城鎮，然後列出有什麼
藥水商人可以選 —— 這樣就不會有找不到藥水商人的問題。」

背景：自動挑「最近的」商人一路踩坑（prt_in 互不相連的房間 [DAT-029]、
高級藥水商人沒賣紅藥 [DAT-064]、斷線被記成走不到 [DAT-065]…），每一個都是
「程式替使用者猜他要去哪」惹的。使用者自己指定就沒有猜的問題。

資料來源：`gamedata.maps_with_potion_sellers()`／`potion_sellers_on()`
（客戶端導航資料抽出來的 NPC 表，見 [PKT-093]）。

⚠ 存的是**地圖代碼 ＋ 商人站的那一格**（CLAUDE.md：存身分）。NPC 的 GID
是伺服器給的、每次都不同（[PKT-093]），不能存；名字會重複（一張圖上可以有
兩個「道具商人」），也不能單獨當身分。地圖 ＋ 格子在資料表裡是唯一的。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ro_toolbox.services.gamedata import (
    map_display_name,
    maps_with_potion_sellers,
    potion_sellers_on,
)
from ro_toolbox.services.travel import map_hop_distances

Choice = tuple[str, tuple[int, int]]


def describe_shop(map_name: str | None, cell: tuple[int, int] | None) -> str:
    """給卡片顯示用的一句話。沒設定就說「自動挑最近的」。"""
    if not map_name or cell is None:
        return "藥水商人：自動挑最近的"
    name = next(
        (s[2] for s in potion_sellers_on(map_name) if (s[0], s[1]) == tuple(cell)), ""
    )
    where = map_display_name(map_name) or map_name
    who = name or f"({cell[0]},{cell[1]})"
    return f"藥水商人：{where} 的{who}"


class ShopDialog(QDialog):
    """按「儲存」之後從 `choice` 拿結果：

    - `(地圖代碼, (x, y))` = 使用者挑了這一個商人
    - `("", None)` = 使用者按了「改回自動挑最近的」
    - `None` = 取消，什麼都不要動
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        current: Choice | None = None,
        current_map: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定藥水商人")
        self.setMinimumSize(520, 420)
        self.choice: Choice | tuple[str, None] | None = None
        self._current = current
        #: 角色現在站的地圖代碼 —— 城鎮清單照「離這裡幾張圖」由近排到遠
        #: （使用者指定 2026-09-05）。讀不到（空字串）就退回照中文名排。
        self._current_map = current_map
        self._build()
        self._fill_maps()

    # ---- 版面 -------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(
            "先選城鎮，再選那張圖上的藥水商人。補水就只會去**這一家**，"
            "不會自己換別家；走不到會直接說，不會亂猜。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("pageSubtitle")
        layout.addWidget(hint)

        layout.addWidget(QLabel("城鎮／地圖"))
        self.town = QComboBox()
        self.town.currentIndexChanged.connect(lambda _i: self._fill_sellers())
        layout.addWidget(self.town)

        layout.addWidget(QLabel("藥水商人"))
        self.sellers = QListWidget()
        self.sellers.itemDoubleClicked.connect(lambda _row: self._accept())
        layout.addWidget(self.sellers, 1)

        self.status = QLabel()
        self.status.setObjectName("pageSubtitle")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save = box.button(QDialogButtonBox.StandardButton.Save)
        save.setText("儲存")
        save.setObjectName("primaryButton")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.auto_button = QPushButton("改回自動挑最近的")
        box.addButton(self.auto_button, QDialogButtonBox.ButtonRole.ResetRole)
        self.auto_button.clicked.connect(self._use_auto)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _fill_maps(self) -> None:
        """有藥水商人的地圖，**照離當前位置的遠近由近排到遠**（使用者指定
        2026-09-05）；讀不到當前位置就退回照中文名排。距離＝換圖次數
        （跟自動尋路一致）；到不了的排最後。沒有中文名的顯示代碼、也排後面。
        """
        codes = list(maps_with_potion_sellers())
        # 一次 BFS 把每一張圖的距離都算出來（別對每張各跑一次）。
        dist = map_hop_distances(self._current_map, set(codes)) if self._current_map else {}
        far = len(codes) + 1        # 到不了／不知道距離的，一律排在最後
        rows = []
        for code in codes:
            shown = map_display_name(code) or ""
            rows.append((dist.get(code, far), 0 if shown else 1, shown or code, code))
        rows.sort()
        self.town.blockSignals(True)
        for _dist, _rank, shown, code in rows:
            label = f"{shown}（{code}）" if shown != code else code
            self.town.addItem(label, code)
        self.town.blockSignals(False)
        if not rows:
            self.status.setText("⚠ 找不到任何藥水商人的資料（NPC 表沒載到）")
            return
        wanted = self._current[0] if self._current else None
        position = self.town.findData(wanted) if wanted else -1
        self.town.setCurrentIndex(position if position >= 0 else 0)
        self._fill_sellers()

    def _fill_sellers(self) -> None:
        self.sellers.clear()
        code = self.town.currentData()
        if not code:
            return
        wanted = self._current[1] if self._current and self._current[0] == code else None
        for x, y, name, _look in potion_sellers_on(code):
            row = QListWidgetItem(f"{name}　({x},{y})")
            row.setData(Qt.ItemDataRole.UserRole, (x, y))
            self.sellers.addItem(row)
            if wanted is not None and (x, y) == tuple(wanted):
                self.sellers.setCurrentItem(row)
        if self.sellers.currentRow() < 0 and self.sellers.count():
            self.sellers.setCurrentRow(0)
        shown = map_display_name(code) or code
        self.status.setText(f"{shown} 上有 {self.sellers.count()} 個可能賣藥水的商人")

    # ---- 結果 -------------------------------------------------------

    def picked(self) -> Choice | None:
        """現在畫面上選的是哪一家。沒選回 None。"""
        code = self.town.currentData()
        row = self.sellers.currentItem()
        if not code or row is None:
            return None
        x, y = row.data(Qt.ItemDataRole.UserRole)
        return str(code), (int(x), int(y))

    def _accept(self) -> None:
        picked = self.picked()
        if picked is None:
            self.status.setText("⚠ 先選一個商人")
            return
        self.choice = picked
        self.accept()

    def _use_auto(self) -> None:
        self.choice = ("", None)
        self.accept()
