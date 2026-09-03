"""「撿取黑名單」的視窗：打字搜尋全部道具，加進來的就永遠不撿。

使用者指定（2026-09-04）：

- 「只要加入黑名單就不會去撿他」
- 「黑名單可以打字搜尋**全部**的物品」（不是只有背包裡有的）
- 「跟寄信一樣點黑名單會出現一個視窗，然後可以打字搜尋要加入的」
- 「搜尋的時候會出現**名字跟物品圖案**」
- 「這個是**永遠開啟**的，所以不會有開關」→ 這裡沒有啟用勾勾
- 「同時也要記錄起來」（存檔見 `services/loot_store`）

使用者追問（同日）：「我想要我在遊戲物品**按右鍵**，程式可以識別，
然後加入黑名單」，並且明確否決抓圖：「**不准用圖片辨識**」「讀記憶體」
「絕對有記憶體」→ 「在遊戲裡按右鍵選道具」那顆按鈕。做法見
`services/item_window`：**讀說明小視窗渲染出來的說明文**，反查道具表拿編號
（GAMEDATA [DAT-070]）。攔遊戲自己的滑鼠事件是注入、GameGuard 會擋，所以
「什麼時候去讀」是我們自己看 `GetAsyncKeyState`，不碰遊戲行程。

「背包」那一頁是同一件事的鍵盤版：撿到垃圾之後最想擋的那一樣通常就在背包裡。

⚠ 道具表有兩萬多筆，**不准整份列出來**：那要抓兩萬張圖示，視窗會卡死。
搜尋框空著就什麼都不列，命中太多也只顯示前 `_MAX_ROWS` 筆並說一聲 ——
安靜地截斷會讓人以為「搜不到」而不是「講太籠統」。

⚠ 存的是**道具編號**不是名字（CLAUDE.md：存身分，不存位置）。名字只是拿來
顯示的；名單裡有編號但道具表查不到名字時照樣列出來（顯示 `#編號`），
不能因為查不到名字就把使用者設過的一條**安靜地弄丟**。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ro_toolbox.services.gamedata import find_items, item_name
from ro_toolbox.ui.widgets.item_icon import item_icon

#: 一次最多列幾筆搜尋結果。每一筆都要解一張圖示，列太多就卡。
_MAX_ROWS = 200


class BlacklistDialog(QDialog):
    """按下「儲存」之後從 `items` 拿名單（`frozenset[int]`）；按取消是 None。"""

    #: 使用者按下「在遊戲裡點一下道具」。頁面那邊接手去監看滑鼠
    #: （這裡沒有 hwnd、也不該自己開執行緒）。
    pick_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        saved=(),
        character: str = "",
        bag_counts: dict[int, int] | None = None,
    ) -> None:
        super().__init__(parent)
        who = f"（{character}）" if character else ""
        self.setWindowTitle(f"撿取黑名單{who}")
        self.setMinimumSize(600, 480)
        #: 按下儲存才有值；取消是 None。
        self.items: frozenset[int] | None = None
        #: 現在名單裡有誰。編輯過程只動這一份，按取消什麼都不會變。
        self._chosen: list[int] = sorted(saved)
        #: 背包現在有什麼 {道具編號: 幾個}。讀不到就是空的 ——
        #: 那一頁會列不出東西，但搜尋那一頁照樣能用（不能因此擋住整個視窗）。
        self._counts = dict(bag_counts or {})
        self._build()
        self._fill_bag()
        self._fill_chosen()

    # ---- 版面 -------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(
            "加進黑名單的東西，自動掛機**永遠不會去撿**（沒有開關，"
            "名單裡有就一定生效）。打字搜尋全部道具，雙擊或按「加入」就進名單。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("pageSubtitle")
        layout.addWidget(hint)

        self.found = QListWidget()
        self.bag = QListWidget()
        self.chosen = QListWidget()

        panes = QHBoxLayout()
        panes.addWidget(self._make_source_pane(), 1)
        panes.addWidget(
            self._make_pane("黑名單（不撿）", self.chosen, "← 移除", self._remove), 1
        )
        layout.addLayout(panes, 1)

        self.found.itemDoubleClicked.connect(lambda _row: self._add())
        self.bag.itemDoubleClicked.connect(lambda _row: self._add())
        self.chosen.itemDoubleClicked.connect(lambda _row: self._remove())

        self.status = QLabel()
        self.status.setObjectName("pageSubtitle")
        layout.addWidget(self.status)
        self._search("")

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

    def _make_source_pane(self) -> QWidget:
        """左欄：「搜尋」與「背包」兩頁共用一顆「加入」。

        ⚠ 兩頁刻意共用一顆按鈕：兩顆按鈕會讓人不確定按的是哪一個清單。
        加入的永遠是**現在看得到的那一頁**選中的東西（`_source()`）。
        """
        pane = QWidget()
        column = QVBoxLayout(pane)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        self.tabs = QTabWidget()

        search_page = QWidget()
        search_box = QVBoxLayout(search_page)
        search_box.setContentsMargins(6, 6, 6, 6)
        search_box.setSpacing(4)
        self.search = QLineEdit()
        self.search.setPlaceholderText("輸入道具名稱或編號搜尋（例：紅色藥水、501）")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._search)
        search_box.addWidget(self.search)
        self.found.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        search_box.addWidget(self.found, 1)
        self.tabs.addTab(search_page, "搜尋全部道具")

        self.bag.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tabs.addTab(self.bag, "背包現有")
        column.addWidget(self.tabs, 1)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        button = QPushButton("加入 →")
        button.clicked.connect(self._add)
        row.addWidget(button, 1)

        # ★ 使用者要的那顆：在遊戲裡點一下道具，程式自己認出是什麼。
        self.pick_button = QPushButton("在遊戲裡按右鍵選道具")
        self.pick_button.setToolTip(
            "按下去之後切到遊戲，對背包裡的道具**按右鍵**開出說明視窗，"
            "程式會從記憶體讀出那是什麼並加進黑名單。可以連續加好幾樣。"
            "（只認右鍵：左鍵在背包裡是拿起／拖曳，順手一點就會誤加。）"
        )
        self.pick_button.clicked.connect(self.pick_requested)
        row.addWidget(self.pick_button, 1)
        column.addLayout(row)
        return pane

    # ---- 「在遊戲裡點一下」的回報 ---------------------------------------

    def picking(self, on: bool, note: str = "") -> None:
        """切換「正在等你按右鍵」的狀態。

        ⚠ 等的期間按鈕要按不下去 —— 不然會疊出第二條背景執行緒，
        兩條搶同一個右鍵。
        """
        self.pick_button.setEnabled(not on)
        self.pick_button.setText(
            "等你在遊戲裡按右鍵…" if on else "在遊戲裡按右鍵選道具")
        if note:
            self.status.setText(note)

    def picked(self, item_ids, why: str = "") -> None:
        """認出來了就加進名單；認不出來就照實說，**不准挑一個最像的湊數**。

        好幾樣共用同一張圖示是常態（卡片、礦石）—— 那時候問人，不要自己選。
        """
        ids = [int(i) for i in item_ids or ()]
        if not ids:
            self.status.setText(
                why or "認不出說明視窗裡是什麼 —— 再按一次右鍵試試。")
            return
        if len(ids) > 1:
            menu = QMenu(self)
            actions = {menu.addAction(self._label(i)): i for i in ids}
            chosen = menu.exec(self.pick_button.mapToGlobal(
                self.pick_button.rect().bottomLeft()))
            if chosen not in actions:
                self.status.setText("好幾樣道具共用同一張圖示 —— 這次沒加。")
                return
            ids = [actions[chosen]]
        added = [i for i in ids if i not in self._chosen]
        self._chosen.extend(added)
        self._chosen.sort()
        self._fill_chosen()
        self.status.setText(
            f"認出來了：{self._label(ids[0])}"
            + ("" if added else "（本來就在名單裡了）")
        )

    def _source(self) -> QListWidget:
        """現在看得到的是哪一個清單。"""
        return self.bag if self.tabs.currentWidget() is self.bag else self.found

    @staticmethod
    def _make_pane(title: str, listing: QListWidget, label: str, on_click) -> QWidget:
        """一欄 = 標題 ＋ 清單 ＋ 一顆按鈕。左右兩欄長得一模一樣。"""
        pane = QWidget()
        column = QVBoxLayout(pane)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(QLabel(title))
        # 一次可以挑好幾個：一種東西不想撿，通常整批都不想撿。
        listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        column.addWidget(listing, 1)
        button = QPushButton(label)
        button.clicked.connect(on_click)
        column.addWidget(button)
        return pane

    # ---- 內容 -------------------------------------------------------

    @staticmethod
    def _label(item_id: int) -> str:
        return f"{item_name(item_id)}　#{item_id}"

    @classmethod
    def _row(cls, item_id: int) -> QListWidgetItem:
        """一列：圖示 ＋ 名字 ＋ 編號。編號要看得到 —— 同名的道具不只一個。"""
        row = QListWidgetItem(item_icon(item_id), cls._label(item_id))
        row.setData(Qt.ItemDataRole.UserRole, item_id)
        return row

    def _search(self, text: str) -> None:
        self.found.clear()
        hits = find_items(text)
        for item_id, _name in hits[:_MAX_ROWS]:
            self.found.addItem(self._row(item_id))
        if not text.strip():
            self.status.setText("打字開始搜尋（全部道具都找得到，不限背包裡有的）。")
        elif not hits:
            self.status.setText(f"找不到「{text.strip()}」。")
        elif len(hits) > _MAX_ROWS:
            self.status.setText(
                f"符合的有 {len(hits)} 個，只列出前 {_MAX_ROWS} 個 —— 再打幾個字縮小範圍。"
            )
        else:
            self.status.setText(f"符合的有 {len(hits)} 個。")

    def _fill_bag(self) -> None:
        """列出背包現在有的東西（含數量）。讀不到就留一行說明，不要空白。

        ⚠ 這一頁是**當下的快照**：視窗開著的時候背包還會變，但黑名單存的是
        **編號**，晚一點格號怎麼挪都不影響（CLAUDE.md：存身分，不存位置）。
        """
        self.bag.clear()
        for item_id, count in sorted(
            self._counts.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            row = self._row(item_id)
            row.setText(f"{row.text()}　×{count}")
            self.bag.addItem(row)
        if not self._counts:
            hint = QListWidgetItem("讀不到背包（等它讀完再開一次就看得到）")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)   # 不能選，它不是一樣道具
            self.bag.addItem(hint)

    def _fill_chosen(self) -> None:
        self.chosen.clear()
        for item_id in self._chosen:
            self.chosen.addItem(self._row(item_id))

    @staticmethod
    def _picked(listing: QListWidget) -> list[int]:
        out = []
        for row in listing.selectedItems():
            value = row.data(Qt.ItemDataRole.UserRole)
            if isinstance(value, int):
                out.append(value)
        return out

    def _add(self) -> None:
        added = False
        for item_id in self._picked(self._source()):
            if item_id not in self._chosen:
                self._chosen.append(item_id)
                added = True
        if added:
            self._chosen.sort()
            self._fill_chosen()

    def _remove(self) -> None:
        drop = set(self._picked(self.chosen))
        if not drop:
            return
        self._chosen = [i for i in self._chosen if i not in drop]
        self._fill_chosen()

    def _accept(self) -> None:
        self.items = frozenset(self._chosen)
        self.accept()
