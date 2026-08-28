"""自動掛機分頁。

每個「已登入」的 RO 視窗對應一個子分頁，會自動跟著遊戲視窗增減：
開新的就多一頁、關掉就少一頁、還沒登入的不會出現（AOB 定位不到就不建分頁，
之後定期重試，玩家登入後自然會冒出來）。

定位是 AOB 掃描（約 1 秒），放在背景執行緒做，不擋 UI；
定位完成後每秒只做幾次 read_value，成本很低。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ro_toolbox.config.paths import in_selftest
from ro_toolbox.config.settings import current_settings
from ro_toolbox.core.worker import Worker, WorkerThread
from ro_toolbox.services import bag, icons, potion_store, window_list
from ro_toolbox.services.character import CharacterReader, CharacterStatus
from ro_toolbox.services.farm_bot import FarmBot, FarmStats
from ro_toolbox.services.gamedata import (
    heals_hp,
    heals_sp,
    item_name,
    map_display_name,
    map_name_table,
)
from ro_toolbox.services.potion import PotionBot, PotionConfig, PotionStats
from ro_toolbox.services.process_monitor import local_network_up
from ro_toolbox.services.reconnect import RECONNECT, ReconnectDecider
from ro_toolbox.services.ro_capture import find_server
from ro_toolbox.services.travel_bot import TravelBot
from ro_toolbox.ui.pages.base_page import BasePage
from ro_toolbox.ui.widgets.toast import show_notice

log = logging.getLogger(__name__)

PROCESS_NAME = "ragexe.exe"

_SCAN_INTERVAL_MS = 3000  # 多久重掃一次視窗清單
_READ_INTERVAL_MS = 1000  # 多久更新一次數值
_RETRY_AFTER_SEC = 10.0  # 定位失敗後隔多久重試（多半是還沒登入）
#: 回連之後，最多等這麼久讓新遊戲的分頁長出來。
#:
#: ⚠ **這是放棄的上限，不是成功的依據**（CLAUDE.md：不准拿「等幾秒」當機制）。
#: 真正的訊號是 `_on_attached` —— 分頁建好的那一刻就接回去。
#: 要撐得夠久：新遊戲登入完還要進圖，分頁要等「有連線」＋背景 AOB 定位成功，
#: 而定位失敗的重試冷卻本身就是 10 秒。
_RESTORE_TIMEOUT_SEC = 180.0
_MAX_READ_FAILURES = 3  # 連續讀取失敗幾次就當它登出／關閉了


class AttachWorker(QThread):
    """在背景對一個行程做 AOB 定位，避免 1 秒的掃描卡住介面。"""

    done = Signal(int, object)  # pid, CharacterReader 或 None

    def __init__(self, pid: int) -> None:
        super().__init__()
        self._pid = pid

    def run(self) -> None:
        reader = CharacterReader()
        try:
            ok = reader.attach(self._pid, should_stop=self.isInterruptionRequested)
        except Exception as exc:  # noqa: BLE001 - 背景執行緒不能讓例外逸出
            log.debug("PID %s 定位時發生例外：%s", self._pid, exc)
            ok = False
        if not ok:
            reader.close()
            reader = None
        self.done.emit(self._pid, reader)


class BagWorker(QThread):
    """在背景讀背包（AOB 定位實測 22 ms），不要卡住介面。

    讀出來的是 {格號: (道具編號, 數量)} —— 名字查表、數量即時，
    完全不需要封包（見 GAMEDATA [MEM-028]）。
    """

    done = Signal(int, object)  # pid, {格號: (道具編號, 數量)}

    def __init__(self, pid: int) -> None:
        super().__init__()
        self._pid = pid

    def run(self) -> None:
        try:
            rows = bag.as_dict(self._pid)
        except Exception as exc:  # noqa: BLE001 - 背景執行緒不能讓例外逸出
            log.debug("PID %s 讀背包失敗：%s", self._pid, exc)
            rows = {}
        self.done.emit(self._pid, rows)


_ICON_CACHE: dict[int, QIcon] = {}
#: RO 的道具圖示用洋紅當透明色（實測 501 的左上角像素就是 #ff00ff）。
_TRANSPARENT = "#ff00ff"


def item_icon(item_id: int) -> QIcon:
    """道具小圖。找不到就回空 QIcon（介面照樣顯示文字，不拿別的圖來頂）。"""
    got = _ICON_CACHE.get(item_id)
    if got is not None:
        return got
    # 走 `icon_bytes` 不走 `icon_path`：使用者的電腦沒有 RODATA，
    # 圖示的唯一來源是打包資產 `assets/icons.bin`。
    data = icons.icon_bytes(item_id)
    icon = QIcon()
    if data is not None:
        pixmap = QPixmap()
        if pixmap.loadFromData(data) and not pixmap.isNull():
            image = pixmap.toImage()
            image.setAlphaChannel(image.createMaskFromColor(
                QColor(_TRANSPARENT).rgb(), Qt.MaskMode.MaskOutColor
            ))
            icon = QIcon(QPixmap.fromImage(image))
    _ICON_CACHE[item_id] = icon
    return icon


class CharacterCard(QWidget):
    """單一角色的資訊卡。刻意做緊湊，掛機設定之後再往下加。"""

    #: 卡片內容的最大寬度。資訊沒幾行，拉滿整頁只會讓進度條變得又長又空。
    CONTENT_WIDTH = 380
    #: 一列控制項的固定高度。**固定**是刻意的：之後再往下加功能時，
    #: 上面的東西不會因為空間不夠被壓扁（不夠就整張卡片捲動）。
    ROW_HEIGHT = 26
    SPIN_WIDTH = 56
    #: 自動尋路按鈕的**最小**尺寸。它在主欄之外的獨立右欄，所以卡片內容一格都不用讓。
    #:
    #: ⚠ 這裡是下限不是固定值。樣式表給 QPushButton 的內距是 `7px 18px`
    #: ＋ 1px 邊框，光上下就吃掉 16px；鎖成 ROW_HEIGHT(26) 會把字夾扁
    #: （使用者實際回報）。而且鎖死尺寸換一種字型或高 DPI 就會再夾一次。
    #: 讓它照內容自然長、只擋「不要太小」，字永遠不會被壓到。
    TRAVEL_BUTTON_MIN_W = 96
    TRAVEL_BUTTON_MIN_H = 34
    #: 道具小圖的邊長（解包出來的圖示是 24×24）。
    ICON_PX = 24

    #: 使用者勾選/取消「自動打怪」。參數：是否開啟。
    farm_toggled = Signal(bool)
    #: 背景 FarmBot 回報狀態（跨執行緒，用 signal 轉回 UI 執行緒才安全）。
    farm_stats = Signal(object)
    #: 使用者勾選/取消「自動補水」。參數：是否開啟。
    potion_toggled = Signal(bool)
    #: 補水設定（選了哪一格、百分比）有變動。
    potion_changed = Signal()
    #: 背景 PotionBot 回報狀態。
    potion_stats = Signal(object)
    #: 使用者按下／取消「自動尋路」。參數：是否開啟。
    travel_toggled = Signal(bool)
    #: 使用者按了「暫停」。**沒有參數** —— 這顆只會暫停，
    #: 要繼續是再按一次「自動尋路」（使用者指定）。
    travel_pause_pressed = Signal()
    #: 背景 TravelBot 回報狀態。
    travel_stats = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        #: 這張卡是哪一隻角色（補水設定用它當鍵，不是 PID —— PID 每次開遊戲都變）
        self.character = ""
        #: 這一趟的「到了」通知跳過沒有。bot 停下來時還會再回報一次同一份
        #: stats（arrived 仍是 True），沒有這道閘門會跳兩次。
        self._travel_notified = False
        #: 死亡通知跳過沒有。bot 停下來時還會再回報一次同一份 stats
        #: （died 仍是 True），沒有這道閘門會連跳兩個框。
        self._death_notified = False
        #: 現在是不是在趕路／是不是暫停中（決定暫停鈕能不能按）
        self._travel_busy = False
        self._travel_paused = False
        #: 想選但清單裡還沒出現的道具（背包是非同步讀的，選單填好才選得到）
        self._want_item: dict[str, int | None] = {"hp": None, "sp": None, "home": None}
        #: True = 現在是**程式自己**在改 UI，不是使用者的意思 —— 這種變動不存檔。
        #: 少了這道閘門，bot 啟動失敗時自動取消勾選會把使用者的設定覆蓋成「關閉」。
        self.quiet = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 包進捲動區：**元件一律保持自然大小，空間不夠就捲動，不是壓扁**。
        # 沒有這層的話每加一個新功能，上面的進度條就會被擠得越來越薄。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 橫向捲軸改成「需要才出現」：主欄 380 之外多了一個 96px 的右欄之後，
        # 卡片需要約 516px。視窗被縮很小的時候，AlwaysOff 會讓右邊的按鈕
        # **直接消失**（沒有捲軸可以捲過去）—— 寧可出現捲軸，不要弄丟功能。
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        outer.addWidget(scroll)

        holder = QWidget()
        scroll.setWidget(holder)
        board = QVBoxLayout(holder)
        board.setContentsMargins(14, 12, 14, 12)
        board.setSpacing(0)

        inner = QWidget()
        # 固定寬度而非上限：靠左對齊時 maximumWidth 會讓容器縮到內容寬度，
        # 進度條就變得又細又短。
        inner.setFixedWidth(self.CONTENT_WIDTH)

        # 自動尋路的按鈕放在**卡片主欄之外**的右邊欄。
        # 放進 Base 那一行的話會跟等級／經驗數字搶同一條的寬度，把它擠扁；
        # 分成獨立的一欄，主欄 380px 一格都不用讓（分頁區約有 950px 可用）。
        side = QWidget()
        side_box = QVBoxLayout(side)
        side_box.setContentsMargins(0, 0, 0, 0)
        side_box.setSpacing(0)
        # 往下推一行，讓按鈕大致對齊 Base 區塊而不是角色名字那一行
        side_box.addSpacing(self.ROW_HEIGHT)
        side_box.addWidget(self._make_travel_button())
        side_box.addWidget(self._make_pause_button())
        side_box.addWidget(self._make_destination_box())
        side_box.addStretch(1)

        top = QWidget()
        top_row = QHBoxLayout(top)
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(12)
        top_row.addWidget(inner)
        top_row.addWidget(side, 0, Qt.AlignmentFlag.AlignTop)
        top_row.addStretch(1)

        board.addWidget(top, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        board.addStretch(1)

        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        # 高度一律照元件自己要的來，不讓版面把它們拉長或壓扁
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinAndMaxSize)
        # ⚠ 上面那個約束會**蓋掉** `inner.setFixedWidth()` —— 它把 min/max 都改成
        # 版面自己算出來的值，所以卡片實際只有 354px（量出來的），
        # 那行 setFixedWidth 從頭到尾沒生效過，進度條也就一直比預期短。
        # 用一個「寬 CONTENT_WIDTH、高 0」的間隔件把版面的最小寬度撐起來：
        # 高度保護留著，寬度也真的變成我們要的。
        layout.addItem(
            QSpacerItem(
                self.CONTENT_WIDTH, 0, QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
            )
        )

        self.name_label = QLabel("—")
        self.name_label.setObjectName("cardTitle")
        layout.addWidget(self.name_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)

        # 經驗值：遊戲畫面只給百分比，這裡把實際數字讀出來。
        # 條子做細的，數字放在條子上方 —— 6px 高的條塞不下文字。
        base_block, self.base_head, self.base_value, self.base_bar = (
            self._make_exp_block("Base", "baseBar")
        )
        job_block, self.job_head, self.job_value, self.job_bar = (
            self._make_exp_block("Job", "jobBar")
        )
        layout.addWidget(base_block)
        layout.addWidget(job_block)

        self.hp_bar = self._make_bar("hpBar")
        self.sp_bar = self._make_bar("spBar")
        grid.addWidget(QLabel("HP"), 0, 0)
        grid.addWidget(self.hp_bar, 0, 1)
        grid.addWidget(QLabel("SP"), 1, 0)
        grid.addWidget(self.sp_bar, 1, 1)
        layout.addLayout(grid)

        self.exp_label = QLabel("")
        self.exp_label.setObjectName("pageSubtitle")
        layout.addWidget(self.exp_label)

        self._note_text = "定位中…"
        self._last_alert = ""
        self.status_label = QLabel(self._note_text)
        self.status_label.setObjectName("pageSubtitle")
        layout.addWidget(self.status_label)

        # ---- 自動打怪 ----
        self.auto_hunt = QCheckBox("自動打怪")
        self.auto_hunt.setFixedHeight(self.ROW_HEIGHT)
        self.auto_hunt.toggled.connect(self.farm_toggled)
        layout.addWidget(self.auto_hunt)

        # ---- 自動補水 ----
        layout.addWidget(self._build_potion_panel())

        self.farm_stats.connect(self._apply_farm_stats)
        self.potion_stats.connect(self._apply_potion_stats)
        self.travel_stats.connect(self._apply_travel_stats)

    # ---- 自動尋路 ---------------------------------------------------

    def _make_travel_button(self) -> QPushButton:
        """按下去就照**遊戲自己的尋路目標**走過去，會自己穿越多張地圖。

        目的地不在這裡選：按下遊戲內建的尋路鍵時客戶端一個封包都沒送
        （實測 `封包/按下尋路.txt`），箭頭是客戶端自己算的 —— 所以我們從記憶體
        讀它指向哪張圖，再用同一份 `navi_link` 傳點表自己算路走過去。
        """
        self.auto_travel = QPushButton("自動尋路")
        self.auto_travel.setCheckable(True)
        self.auto_travel.setMinimumSize(
            self.TRAVEL_BUTTON_MIN_W, self.TRAVEL_BUTTON_MIN_H
        )
        self.auto_travel.setToolTip(
            "先在遊戲的尋路視窗設好目的地（箭頭出現），再按這裡。\n"
            "純趕路：途中不打怪、不撿東西，抵達就停。"
        )
        self.auto_travel.toggled.connect(self.travel_toggled)
        return self.auto_travel

    def _make_pause_button(self) -> QPushButton:
        """趕路中站住不動。**只有暫停，沒有繼續** —— 要繼續是再按一次「自動尋路」。

        使用者指定的形狀：「自動尋路按鈕只需要暫停就好不需要變成繼續，
        他要繼續可以再按一次自動尋路就會繼續了」。所以這顆**不是**開關，
        按下去就是暫停，然後自己壓成不能按；「自動尋路」那顆會彈起來，
        看起來就是「可以再按一次」。

        ⚠ 為什麼不叫人「取消再按一次」：取消是**收攤** —— 關 socket、關封包
        擷取、忘掉這一趟學到的傳點黑名單，再開要重新 AOB 定位、重新複製
        socket（剛換頻道那幾秒常常複製不到，[PKT-072]）。暫停只是不送走路封包。

        沒在趕路時**壓著不能按**（不是藏起來）：藏起來會讓版面跳動，
        而且看不到就不知道有這個功能。
        """
        self.travel_pause = QPushButton("暫停")
        self.travel_pause.setEnabled(False)
        self.travel_pause.setFixedHeight(self.ROW_HEIGHT)
        self.travel_pause.setMinimumWidth(self.TRAVEL_BUTTON_MIN_W)
        self.travel_pause.setToolTip(
            "趕路中站住不動。要繼續就再按一次「自動尋路」，"
            "會從現在的位置接下去（連線與路線都留著）。"
            "⚠ 已經送出去的那一段會走完（移動是伺服器帶的，沒有「立刻站住」的封包）。"
        )
        self.travel_pause.clicked.connect(self.travel_pause_pressed)
        return self.travel_pause

    def set_travel_paused(self, paused: bool) -> None:
        """暫停中的樣子：暫停鈕壓著不能按，「自動尋路」彈起來等你再按一次。

        ⚠ 改「自動尋路」的狀態時**一定要擋住訊號**：那顆的 `toggled` 直接接到
        `travel_toggled`，不擋的話彈起來會被當成「使用者要取消」，
        整個 bot 就被收攤掉了 —— 正好是暫停要避免的事。
        """
        self._travel_paused = paused
        self.travel_pause.setEnabled(self._travel_busy and not paused)
        blocked = self.auto_travel.blockSignals(True)
        try:
            self.auto_travel.setChecked(self._travel_busy and not paused)
        finally:
            self.auto_travel.blockSignals(blocked)

    def _make_destination_box(self) -> QComboBox:
        """目的地選單：**打中文或地圖代碼都能搜**。

        沒選（留在第一項）就照舊讀**遊戲自己的尋路目標**；選了就以這裡為準。
        為什麼要它：遊戲的目標讀得到但不是每種情況都有（例如根本還沒去設），
        而我們手上就有完整的地圖表，讓人直接挑最直接。
        """
        combo = QComboBox()
        combo.setEditable(True)                    # 可以打字搜尋
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setFixedHeight(self.ROW_HEIGHT)
        combo.setMinimumWidth(self.TRAVEL_BUTTON_MIN_W)
        combo.addItem("（讀遊戲的尋路目標）", None)
        for code, name in sorted(map_name_table().items(), key=lambda kv: kv[1]):
            # 中文名跟地圖代碼都放進同一行，兩種打法都搜得到
            combo.addItem(f"{name}（{code}）", code)
        completer = QCompleter([combo.itemText(i) for i in range(combo.count())], combo)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)   # 打中間的字也搜得到
        combo.setCompleter(completer)
        combo.setCurrentIndex(0)
        combo.currentIndexChanged.connect(self.potion_changed)  # 設定變了要存
        self.destination = combo
        return combo

    def pending_items(self):
        """還原存檔時選了、但**下拉裡還沒有**的道具編號（背包還沒讀到）。

        有東西在等 = 「現在配置看起來是空的」只是暫時的，不是使用者沒設定。
        """
        return [v for v in self._want_item.values() if v]

    def chosen_destination(self) -> str | None:
        """使用者在這裡挑的地圖代碼。沒挑回 None（＝讀遊戲的尋路目標）。"""
        data = self.destination.currentData()
        return data if isinstance(data, str) else None

    def _apply_travel_stats(self, stats) -> None:  # noqa: ANN001 - TravelStats
        """⚠ 這裡**不記日誌** —— `TravelBot._note()` 已經記過了。
        兩邊都記的症狀是同一句話印兩次（實測：「前往 依斯魯得島　前往 izlude」）。
        介面上唯一的表現是：bot 停了，按鈕就彈起來。"""
        self._notify_death(stats)
        if getattr(stats, "arrived", False) and not self._travel_notified:
            # 到了要**跳到螢幕最前面**講一聲：趕路動輒幾十秒，人早就切回遊戲
            # 或去做別的事了，只寫在日誌等於沒講。
            self._travel_notified = True
            where = stats.goal_label or stats.goal or "目的地"
            who = self.character or "角色"
            body = f"{who} 已抵達 {where}"
            if stats.note:
                body = f"{body}\n{stats.note}"
            # 使用者指定：要**按確定才消失**的驚嘆號框，不是幾秒後自己收掉的
            # 那種 —— 趕路要幾十秒，人早就離開電腦，自動收掉等於沒講。
            show_notice("自動尋路：到了", body)
        if not stats.running:
            # 走完（或失敗）就把按鈕彈起來 —— 按鈕壓著卻沒在走，
            # 看起來會像「還在趕路」，那是最糟的失效方式。
            if self.auto_travel.isChecked():
                self.auto_travel.setChecked(False)

    def set_travel_busy(self, busy: bool) -> None:
        """趕路途中不讓人再去勾自動打怪 —— 兩個都在送走路封包會互相打架。"""
        self._travel_busy = busy
        self.auto_hunt.setEnabled(not busy)
        self.travel_pause.setEnabled(busy)
        if not busy:
            # 停下來就不是「暫停中」了。下一趟要從乾淨的狀態開始，
            # 不然新的一趟看起來會像「一開始就暫停」。
            self._travel_paused = False
        if busy:
            self._travel_notified = False  # 新的一趟，抵達通知重新算

    # ---- 自動補水 ---------------------------------------------------

    def _build_potion_panel(self) -> QWidget:
        """獨立執行緒跑，一拍 0.05 秒，低於門檻就連喝到過線為止。

        下拉選單直接列背包裡的補血／補魔道具：名字查表、數量從記憶體即時讀
        （[MEM-028]）。選單的值是**道具編號**不是格號 —— 格號會挪動。
        """
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        head = QHBoxLayout()
        self.auto_potion = QCheckBox("自動補水")
        self.auto_potion.setFixedHeight(self.ROW_HEIGHT)
        self.auto_potion.toggled.connect(self.potion_toggled)
        head.addWidget(self.auto_potion)
        head.addStretch(1)
        box.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(3, 1)
        self.hp_item, self.hp_threshold, self.hp_icon = self._make_potion_row(
            grid, 0, "HP 低於"
        )
        self.sp_item, self.sp_threshold, self.sp_icon = self._make_potion_row(
            grid, 1, "SP 低於"
        )
        self.go_home, self.home_item, self.home_icon = self._make_home_row(grid, 2)
        box.addLayout(grid)
        # ⚠ 設定區裡**不放任何文字**（使用者：「不要配置說明文字，很怪」）。
        # 但「大聲停用」不能一起消失（CLAUDE.md：失效只准大聲或安全退化），
        # 所以警示改走卡片最上面那行 `set_alert()`。
        return panel

    def _make_home_row(self, grid: QGridLayout, row: int):
        """「水用完回程」：HP 或 SP 的藥水**任一種**用完就用選的道具回程。

        下拉列**整個背包**，不過濾。道具表裡認不出哪個是回程道具 ——
        蝴蝶翅膀的描述寫「移動至儲存的位置」、蒼蠅翅膀寫「移動至任意的位置」，
        差別只在那句話。靠關鍵字猜就是規範說的「很有自信的錯」，所以讓人自己挑。
        """
        check = QCheckBox("水用完回程")
        check.setFixedHeight(self.ROW_HEIGHT)
        check.toggled.connect(self.potion_changed)
        combo = QComboBox()
        combo.setFixedHeight(self.ROW_HEIGHT)
        combo.setMinimumWidth(170)
        combo.setIconSize(QSize(self.ICON_PX, self.ICON_PX))
        preview = QLabel()
        preview.setFixedSize(self.ICON_PX, self.ICON_PX)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        combo.currentIndexChanged.connect(self.potion_changed)
        combo.currentIndexChanged.connect(
            lambda _i, c=combo, w=preview: self._show_icon(c, w)
        )
        grid.addWidget(check, row, 0, 1, 3)
        grid.addWidget(combo, row, 3)
        grid.addWidget(preview, row, 4)
        return check, combo, preview

    def _make_potion_row(self, grid: QGridLayout, row: int, title: str):
        label = QLabel(title)
        spin = QSpinBox()
        # 直接打數字：不要上下箭頭，% 放在框外面。
        # 範圍 0~100，超出的打不進去（QSpinBox 自己會夾住）；0 = 關閉這一項。
        spin.setRange(0, 100)
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        spin.setFixedSize(self.SPIN_WIDTH, self.ROW_HEIGHT)
        spin.valueChanged.connect(self.potion_changed)
        percent = QLabel("%")
        combo = QComboBox()
        combo.setFixedHeight(self.ROW_HEIGHT)
        combo.setMinimumWidth(170)
        combo.setIconSize(QSize(self.ICON_PX, self.ICON_PX))
        preview = QLabel()
        preview.setFixedSize(self.ICON_PX, self.ICON_PX)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        combo.currentIndexChanged.connect(self.potion_changed)
        combo.currentIndexChanged.connect(
            lambda _i, c=combo, w=preview: self._show_icon(c, w)
        )
        grid.addWidget(label, row, 0)
        grid.addWidget(spin, row, 1)
        grid.addWidget(percent, row, 2)
        grid.addWidget(combo, row, 3)
        grid.addWidget(preview, row, 4)
        return combo, spin, preview

    @staticmethod
    def _show_icon(combo: QComboBox, holder: QLabel) -> None:
        """把選到的那個道具的小圖放在選單右邊。"""
        icon = combo.itemIcon(combo.currentIndex())
        holder.setPixmap(icon.pixmap(CharacterCard.ICON_PX, CharacterCard.ICON_PX))

    def set_slots(self, rows: dict[int, tuple[int, int]]) -> None:
        """把背包裡的補血／補魔道具填進兩個下拉選單。

        道具名字查表（`assets/items.json.gz`），格號與數量從記憶體讀（[MEM-028]）。
        **選單的值是道具編號不是格號** —— 格號會挪動，存格號遲早會喝錯東西。
        同一組道具時只改文字不重建，避免每秒閃爍、也不會打斷正在挑的人。
        """
        for key, combo, wants in (
            ("hp", self.hp_item, heals_hp), ("sp", self.sp_item, heals_sp),
            # 回程那個**不過濾**：道具表認不出哪個是回程道具，讓人自己挑。
            ("home", self.home_item, lambda _item_id: True),
        ):
            wanted = [
                (item_id, amount) for _slot, (item_id, amount) in sorted(rows.items())
                if wants(item_id)
            ]
            current = [
                (combo.itemData(i), combo.itemData(i, Qt.ItemDataRole.UserRole + 1))
                for i in range(1, combo.count())
            ]
            if [c[0] for c in current] == [w[0] for w in wanted]:
                for position, (item_id, amount) in enumerate(wanted, start=1):
                    combo.setItemText(position, self._slot_text(item_id, amount))
                    combo.setItemData(position, amount, Qt.ItemDataRole.UserRole + 1)
                continue
            if combo.view().isVisible():
                continue  # 使用者正在挑，別把清單抽掉
            keep = combo.currentData()
            if keep is None:
                keep = self._want_item[key]      # 還原存檔時選的那個
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("未選擇", None)
            for item_id, amount in wanted:
                combo.addItem(item_icon(item_id), self._slot_text(item_id, amount), item_id)
                combo.setItemData(combo.count() - 1, amount, Qt.ItemDataRole.UserRole + 1)
            position = combo.findData(keep)
            combo.setCurrentIndex(position if position >= 0 else 0)
            combo.blockSignals(False)
            if position >= 0 and keep == self._want_item[key]:
                self._want_item[key] = None      # 還原成功，不必再等了
        self._show_icon(self.hp_item, self.hp_icon)
        self._show_icon(self.sp_item, self.sp_icon)
        self._show_icon(self.home_item, self.home_icon)

    @staticmethod
    def _slot_text(item_id: int, amount: int) -> str:
        """只顯示名稱與數量 —— 格號是內部資料，使用者不需要看到。"""
        return f"{item_name(item_id)} × {amount}"

    def apply_saved_potion(self, saved) -> None:  # noqa: ANN001 - PotionSaved
        """把存檔的補水設定填回畫面。

        ⚠ 全程 `quiet=True`：這是程式在還原，不是使用者剛剛改的，**不該再存一次**。
        道具可能還不在下拉選單裡（背包是非同步讀的），所以先記在 `_want_item`，
        等 `set_slots()` 把清單填好時再選起來。
        """
        self.quiet = True
        try:
            self._want_item = {
                "hp": saved.hp_item, "sp": saved.sp_item, "home": saved.home_item,
            }
            self.hp_threshold.setValue(saved.hp_percent)
            self.sp_threshold.setValue(saved.sp_percent)
            self.go_home.setChecked(bool(saved.go_home))
            position = self.destination.findData(saved.travel_dest)
            self.destination.setCurrentIndex(position if position >= 0 else 0)
            for key, combo in (
                ("hp", self.hp_item), ("sp", self.sp_item), ("home", self.home_item)
            ):
                position = combo.findData(self._want_item[key])
                if position >= 0:
                    combo.setCurrentIndex(position)
                    self._want_item[key] = None
            self.auto_potion.setChecked(bool(saved.enabled))
        finally:
            self.quiet = False

    def saved_potion(self):  # noqa: ANN201 - PotionSaved
        """目前畫面上的補水設定，要存起來的樣子。"""
        from ro_toolbox.services.potion_store import PotionSaved

        return PotionSaved(
            hp_item=self.hp_item.currentData(),
            hp_percent=self.hp_threshold.value(),
            sp_item=self.sp_item.currentData(),
            sp_percent=self.sp_threshold.value(),
            enabled=self.auto_potion.isChecked(),
            go_home=self.go_home.isChecked(),
            home_item=self.home_item.currentData(),
            travel_dest=self.chosen_destination(),
        )

    def potion_config(self) -> PotionConfig:
        return PotionConfig(
            hp_item=self.hp_item.currentData(),
            hp_percent=self.hp_threshold.value(),
            sp_item=self.sp_item.currentData(),
            sp_percent=self.sp_threshold.value(),
            # 沒勾就不帶道具進去 —— 沒勾卻回程是「安靜地做錯事」
            home_item=self.home_item.currentData() if self.go_home.isChecked() else None,
        )

    def _apply_potion_stats(self, stats: PotionStats) -> None:
        if not stats.running:
            if stats.went_home and self.auto_hunt.isChecked():
                # 已經用回程道具回城了。沒水又沒怪還勾著自動打怪，
                # 只會站在城裡空轉 —— 而且看起來像「還在掛機」。
                self.auto_hunt.setChecked(False)
            if self.auto_potion.isChecked():
                # ⚠ 這是 bot 自己停掉（沒登入、定位失敗…），**不是使用者關的**。
                # 不加這道閘門，一次啟動失敗就會把使用者存的「開啟」覆蓋成「關閉」。
                self.quiet = True
                try:
                    self.auto_potion.setChecked(False)
                finally:
                    self.quiet = False
            return
        # ⚠ 這裡不記日誌 —— `PotionBot._note()` 已經記過了，兩邊都記會印兩次。

    def _apply_farm_stats(self, stats: FarmStats) -> None:
        """⚠ 這裡**不寫任何介面文字**（使用者指定：提示字一律進執行日誌）。
        擊殺／撿取／目標與 bot 自己的 note 都由 `FarmBot._note()` 記進日誌。
        介面上唯一的表現是：bot 停了，勾勾就彈起來。

        **死亡是唯一的例外**：使用者指定死了要跳「按確定才消失」的通知窗，
        而且**只**關掉自動打怪，別的什麼都不做（不回城、不重連、不繼續打）。
        """
        self._notify_death(stats)
        if not stats.running and self.auto_hunt.isChecked():
            # 勾著卻沒在跑，看起來會像「還在掛機」，那是最糟的失效方式
            self.auto_hunt.setChecked(False)

    def _notify_death(self, stats) -> None:  # noqa: ANN001 - FarmStats／TravelStats
        """角色死了就跳一次通知窗。

        ⚠ **只跳一次**：bot 停下來時還會再回報一次同一份 stats（`died` 仍是
        True），沒有這道閘門會連跳兩個框。跟抵達通知同一個道理。
        """
        if getattr(stats, "running", False) and not getattr(stats, "died", False):
            self._death_notified = False   # 新的一輪，下次死了要再講一次
        if not getattr(stats, "died", False) or self._death_notified:
            return
        self._death_notified = True
        who = self.character or "角色"
        show_notice("角色死亡", f"{who} 已經死亡，自動打怪已關閉。")

    @staticmethod
    def _make_bar(object_name: str) -> QProgressBar:
        bar = QProgressBar()
        bar.setObjectName(object_name)
        bar.setRange(0, 100)
        bar.setTextVisible(True)
        bar.setFixedHeight(18)
        return bar

    @staticmethod
    def _make_exp_block(title: str, bar_name: str):
        """一個經驗值區塊：上面一行「等級 …… 數字」，下面一條細銀條。

        高度與顏色在 qss（baseBar / jobBar）裡設定，這裡只管版面。
        """
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        head = QLabel(title)
        head.setObjectName("expHead")
        value = QLabel("—")
        value.setObjectName("expValue")
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(head)
        row.addStretch(1)
        row.addWidget(value)
        column.addLayout(row)

        bar = QProgressBar()
        bar.setObjectName(bar_name)
        bar.setRange(0, 100)
        bar.setTextVisible(False)  # 條子太細，文字放在上面那行
        column.addWidget(bar)
        return box, head, value, bar

    def update_status(self, status: CharacterStatus) -> None:
        self.name_label.setText(status.name or "（讀不到名稱）")
        self.base_head.setText(f"Base {status.base_level}")
        self.job_head.setText(f"Job {status.job_level}")
        self._fill_exp(self.base_bar, self.base_value, status.base_exp,
                       status.base_exp_next, status.base_percent,
                       status.base_maxed, status.has_exp)
        self._fill_exp(self.job_bar, self.job_value, status.job_exp,
                       status.job_exp_next, status.job_percent,
                       status.job_maxed, status.has_exp)

        self.hp_bar.setValue(int(status.hp_percent))
        self.hp_bar.setFormat(f"{status.hp} / {status.max_hp}")
        self.sp_bar.setValue(int(status.sp_percent))
        self.sp_bar.setFormat(f"{status.sp} / {status.max_sp}")

    @staticmethod
    def _fill_exp(bar, label, exp: int, need: int, percent: float,
                  maxed: bool, ok: bool) -> None:
        """經驗條與旁邊的數字。讀不到就明說，不要顯示 0% 讓人以為真的是 0。"""
        if not ok:
            bar.setValue(0)
            label.setText("經驗值讀取失敗")
            return
        if maxed:
            bar.setValue(100)
            label.setText(f"{exp:,}（已滿級）")
            return
        bar.setValue(int(percent))
        # 遊戲畫面只給到小數一位且無條件捨去，這裡多給一位看得出有沒有在動
        label.setText(f"{exp:,} / {need:,}　{percent:.2f}%")

    def set_exp_gain(self, text: str) -> None:
        self.exp_label.setText(text)

    def set_note(self, text: str) -> None:
        """卡片上唯一的那行字：這是哪一個遊戲視窗（PID）。不放提示、不放進度。"""
        self._note_text = text
        self.status_label.setText(text)

    def set_alert(self, text: str) -> None:
        """功能出事或有進度要講 —— **寫進「執行日誌」面板，不放介面**（使用者指定）。

        ⚠ 這仍然滿足 CLAUDE.md 的「大聲停用」：日誌面板就在主視窗底下，
        而且勾勾會自己彈起來。安靜地什麼都不說才是被禁止的那種。
        同樣的話連續講不重複記，不然每拍一筆會把日誌洗掉。
        """
        if text and text != self._last_alert:
            log.warning("%s：%s", self.character or "—", text)
        self._last_alert = text


class _ReconnectWorker(Worker):
    """在背景把一個斷線的實例救回來：關掉 → 重開 → 重新登入。

    ⚠ **一定要跑在 worker 執行緒**：這三步加起來三十秒級，放 UI 執行緒
    等於整個程式凍住。跟批次登入是同一條規則（見 account_page）。

    ⚠ 接續一個斷在半途的客戶端是賭博，關掉重開是確定的。
    """

    #: (新的 pid, 角色名, 快照, 失敗原因)。pid <= 0 代表失敗。
    done = Signal(int, str, object, str)

    def __init__(self, pid: int, who: str, snap) -> None:
        super().__init__()
        self._pid = pid
        self._who = who
        self._snap = snap

    def run(self) -> None:
        from pathlib import Path

        from ro_toolbox.services import accounts as account_store
        from ro_toolbox.services import game_census, game_launcher
        from ro_toolbox.services.auto_login import AutoLogin

        fresh = 0     # 已經開起來的新遊戲；失敗時要把它關掉（見 `_give_up`）
        try:
            account = self._find_account(account_store)
            if account is None:
                self.done.emit(0, self._who, self._snap,
                               f"帳號設定裡找不到角色「{self._who}」")
                return
            game_census.close_idle()
            # ⚠⚠ 這兩行以前都是錯的，而且錯得**完全沒有徵兆**：
            #   1. `GamePaths` 要吃 `Path`，餵字串進去的話 `.parent`／`.name` 會炸。
            #   2. `problem` 是**方法不是屬性** —— `if paths.problem:` 永遠為真，
            #      於是每一次回連都在這裡提前放棄，回報的「原因」還是一個
            #      bound method 的字串。自動回連因此**從來不可能成功過**。
            # 這段沒有測試會踩到（要真的去開遊戲），只有實跑才看得出來。
            paths = game_launcher.GamePaths(Path(current_settings().game_path))
            problem = paths.problem()
            if problem:
                self.done.emit(0, self._who, self._snap, problem)
                return
            fresh = game_launcher.launch_game_directly(paths)
            progress = AutoLogin(account, fresh, lambda t: log.info("回連：%s", t)).run()
            if not getattr(progress, "ok", True):
                self._give_up(fresh, "重新登入沒有完成")
                return
            self.done.emit(fresh, self._who, self._snap, "")
        except Exception as exc:  # noqa: BLE001 - 背景失敗要回報，不能吞掉
            log.exception("自動回連失敗")
            self._give_up(fresh, str(exc))

    def _give_up(self, pid: int, why: str) -> None:
        """這次回連失敗 —— **把剛開起來的那個遊戲關掉**再回報。

        ⚠ 使用者實測回報：「回連失敗當斷線應該直接關閉再開重新連線，
        現在卻卡在那邊一直按 ENTER。」登入沒完成的客戶端是**沒救的**
        （多半是卡登：角色還掛在伺服器上，怎麼打都不會過），留著只有壞處：

        1. 畫面上就停在那個半死的登入畫面，看起來像程式當掉了；
        2. 它還佔著帳號，下一次重登更容易再卡一次；
        3. 分頁是照「有連線的遊戲行程」建的 —— 它永遠不會有分頁，
           等於一個沒人看得到、也沒人會收拾的殭屍。

        關掉它，退避時間到了再開一個乾淨的（`_reconnect_done` 會把觀察接回去）。
        """
        if pid:
            from ro_toolbox.services import game_census

            game_census.close(pid)
        self.done.emit(0, self._who, self._snap, why)

    def _find_account(self, account_store):
        """用**角色名**找帳號（身分），不是用 pid（位置）。"""
        try:
            for account in account_store.load().accounts:
                if account.character == self._who:
                    return account
        except Exception as exc:  # noqa: BLE001
            log.warning("讀不到帳號設定：%s", exc)
        return None


class FarmPage(BasePage):
    title = "自動掛機"
    subtitle = "每個已登入的遊戲視窗一個分頁，會自動跟著增減。"
    stretch_at_end = False

    def __init__(self) -> None:
        self._readers: dict[int, CharacterReader] = {}
        self._cards: dict[int, CharacterCard] = {}
        self._workers: dict[int, AttachWorker] = {}
        self._bots: dict[int, FarmBot] = {}
        self._potions: dict[int, PotionBot] = {}
        self._travelers: dict[int, TravelBot] = {}
        #: 自動回連：角色名 → 斷線前在跑什麼／目前的判斷狀態
        self._snaps: dict = {}
        self._deciders: dict = {}
        #: 已經講過「沒有快照」的角色。那條路每拍都會走到，不擋會洗版。
        self._no_snapshot_said: set = set()
        #: 角色 → 他上次連線正常時住在哪個 PID。**閃退偵測靠它** ——
        #: 分頁會跟著行程一起消失，只看分頁的話最需要救的情況沒人在看。
        self._watching: dict = {}
        #: 回連之後還沒接回去的：新 PID → (快照, 角色名, 放棄時間)。
        #: 等的是「分頁長出來」這個訊號，不是等秒數 —— 見 `_reconnect_done`。
        self._pending_restore: dict = {}
        #: 勾了自動補水、但背包還沒讀到 —— 等背包回來再啟動
        self._pending_potion: set[int] = set()
        self._reconnecting = False
        self._reconnect_thread = None
        self._reconnect_decider = None
        #: 正在回連的那個舊 PID。失敗時放回 `_watching` 讓退避重試接得下去。
        self._reconnect_pid = 0
        self._bag_workers: dict[int, BagWorker] = {}
        self._bag_loaded: set[int] = set()
        # 每個角色最近一次讀到的背包 {格號: (道具編號, 數量)}
        self._bags: dict[int, dict[int, tuple[int, int]]] = {}
        self._names: dict[int, str] = {}
        self._retry_at: dict[int, float] = {}
        self._failures: dict[int, int] = {}
        self._loot_shown: list[tuple[int, int]] = []
        # 已停掉的掛機累計的道具。停掉掛機不該把『道具總攬』清空 ——
        # 那是這隻角色撿到的東西，不是 bot 的內部狀態。
        self._loot_totals: dict[int, dict[int, int]] = {}
        # 掛機開始當下的 (時間, Base經驗, Job經驗)，用來算這次練了多少、每小時多少
        self._exp_start: dict[int, tuple[float, int, int]] = {}
        super().__init__()

        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(_SCAN_INTERVAL_MS)
        self._scan_timer.timeout.connect(self._scan)
        self._scan_timer.start()

        self._read_timer = QTimer(self)
        self._read_timer.setInterval(_READ_INTERVAL_MS)
        self._read_timer.timeout.connect(self._read_all)
        self._read_timer.timeout.connect(self._refresh_loot)
        self._read_timer.timeout.connect(self._refresh_current_bag)
        self._read_timer.timeout.connect(self._watch_connections)
        if in_selftest():
            # 自檢只驗「東西有沒有收進來」，不附加遊戲行程（[ENV-005]）。
            self._scan_timer.stop()
            self._read_timer.stop()
        else:
            self._read_timer.start()
            self._scan()

    # ---- 版面 -------------------------------------------------------

    def build(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setObjectName("farmTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._layout.addWidget(self.tabs, 1)

        self.empty_label = QLabel("找不到已登入的遊戲視窗。開好遊戲並登入後會自動出現。")
        self.empty_label.setObjectName("placeholder")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(self.empty_label, 1)

        self._build_loot_panel()
        self._update_empty_state()

    def _build_loot_panel(self) -> None:
        """底部『道具總攬』：目前分頁角色撿到的道具，ID 一律查表顯示中文名。

        撿到就自己更新（每秒），不必按按鈕 —— 掛機時人不在電腦前，
        要能一眼看到現在撿了什麼。按鈕留著給「想立刻看」用。
        """
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("道具總攬"))
        self.loot_refresh = QPushButton("重新整理")
        self.loot_refresh.clicked.connect(self._refresh_loot)
        row.addWidget(self.loot_refresh)
        self.loot_summary = QLabel("尚未撿到東西")
        self.loot_summary.setObjectName("pageSubtitle")
        row.addWidget(self.loot_summary)
        row.addStretch(1)
        self._layout.addWidget(header)

        self.loot_table = QTableWidget(0, 2)
        self.loot_table.setHorizontalHeaderLabels(["道具", "數量"])
        self.loot_table.verticalHeader().setVisible(False)
        self.loot_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.loot_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.loot_table.setMaximumHeight(160)
        h = self.loot_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._layout.addWidget(self.loot_table)

    def _current_pid(self) -> int | None:
        widget = self.tabs.currentWidget()
        for pid, card in self._cards.items():
            if card is widget:
                return pid
        return None

    def _refresh_loot(self) -> None:
        """把 {物品ID: 次數} 查表換成中文名列出來。

        編號不顯示給使用者 —— 那是內部資料。只有查不到名字時才會退化成 `#編號`，
        那種情況本來就該一眼看出「這個查不到」。
        """
        pid = self._current_pid()
        loot = dict(self._loot_totals.get(pid, {})) if pid is not None else {}
        bot = self._bots.get(pid) if pid is not None else None
        if bot is not None:  # 正在跑的再疊上去
            for item_id, count in bot.loot().items():
                loot[item_id] = loot.get(item_id, 0) + count

        rows = sorted(loot.items(), key=lambda kv: (-kv[1], kv[0]))
        if rows != self._loot_shown:
            self._loot_shown = rows
            self.loot_table.setRowCount(len(rows))
            for i, (item_id, count) in enumerate(rows):
                self.loot_table.setItem(i, 0, QTableWidgetItem(item_name(item_id)))
                self.loot_table.setItem(i, 1, QTableWidgetItem(str(count)))
        total = sum(count for _id, count in rows)
        self.loot_summary.setText(
            f"{len(rows)} 種、共 {total} 個" if rows else "尚未撿到東西"
        )

    def _keep_loot(self, pid: int, bot: FarmBot) -> None:
        """把要停掉的 bot 撿到的東西併進累計，這樣關掉掛機列表不會被清空。"""
        totals = self._loot_totals.setdefault(pid, {})
        for item_id, count in bot.loot().items():
            totals[item_id] = totals.get(item_id, 0) + count

    def _exp_gain_text(self, pid: int, status: CharacterStatus) -> str:
        """這次掛機練了多少經驗、換算成每小時多少 —— 自動練等最想看的數字。"""
        start = self._exp_start.get(pid)
        if start is None or not status.has_exp:
            return ""
        began, base0, job0 = start
        elapsed = max(time.monotonic() - began, 1.0)
        base_gain = status.base_exp - base0
        job_gain = status.job_exp - job0
        if base_gain < 0 or job_gain < 0:
            # 升級了，經驗歸零重算 —— 顯示不出正確累計就重新起算，不要報負數
            self._exp_start[pid] = (time.monotonic(), status.base_exp, status.job_exp)
            return "剛升級，重新起算"
        per_hour = base_gain * 3600 / elapsed
        minutes = elapsed / 60
        left = ""
        if per_hour > 0 and not status.base_maxed:
            need = status.base_exp_next - status.base_exp
            left = f"，約 {need / per_hour * 60:.0f} 分升級"
        return (
            f"本次 {minutes:.0f} 分：Base +{base_gain:,}　Job +{job_gain:,}"
            f"（Base 每小時 {per_hour:,.0f}{left}）"
        )

    def _update_empty_state(self) -> None:
        has_any = self.tabs.count() > 0
        self.tabs.setVisible(has_any)
        self.empty_label.setVisible(not has_any)

    # ---- 視窗掃描 ---------------------------------------------------

    def _scan(self) -> None:
        """該加的加、該移的移。

        ⚠ 移除的判斷**不能**用視窗列舉：遊戲在載入畫面／換地圖時視窗標題會
        瞬間變空，`enumerate_windows()` 會跳過無標題的視窗，於是好端端的遊戲
        看起來像「關掉了」，分頁就被無聲砍掉（實際踩過）。
        行程死活改問 GetExitCodeProcess，那是明確事實。
        視窗列舉只留著做一件事：發現還沒納入的新遊戲行程。
        """
        self._expire_pending_restores(time.monotonic())

        for pid in list(self._cards):
            reader = self._readers.get(pid)
            if reader is not None and not reader.alive():
                self._remove(pid, "遊戲行程已結束")

        now = time.monotonic()
        for window in window_list.enumerate_windows():
            if window.process_name.lower() != PROCESS_NAME:
                continue
            pid = window.pid
            if pid in self._cards or pid in self._workers:
                continue
            if now < self._retry_at.get(pid, 0.0):
                continue  # 還在重試冷卻中
            if find_server(pid) is None:
                # 還沒登入（停在登入畫面）。**先問連線再決定要不要 AOB**：
                # 記憶體不是判斷依據 —— 斷線後角色狀態還留著（[MEM-029]），
                # 登入畫面也可能掃到殘留結構，於是建出一個讀不到背包的空分頁，
                # 再對著使用者喊「AOB 定位失敗」。那不是失敗，是還沒登入。
                self._retry_at[pid] = now + _RETRY_AFTER_SEC
                continue
            self._start_attach(pid)

    def _start_attach(self, pid: int) -> None:
        worker = AttachWorker(pid)
        worker.done.connect(self._on_attached)
        worker.finished.connect(lambda p=pid: self._workers.pop(p, None))
        self._workers[pid] = worker
        worker.start()

    def _on_attached(self, pid: int, reader: object) -> None:
        if reader is None:
            # 多半是還沒登入（角色結構還沒建立），等冷卻後再試一次
            self._retry_at[pid] = time.monotonic() + _RETRY_AFTER_SEC
            log.debug("PID %s 定位失敗，%.0f 秒後重試", pid, _RETRY_AFTER_SEC)
            return

        assert isinstance(reader, CharacterReader)
        status = reader.read()
        if status is None:
            reader.close()
            self._retry_at[pid] = time.monotonic() + _RETRY_AFTER_SEC
            return

        card = CharacterCard()
        card.character = status.name
        card.update_status(status)
        card.set_note(f"PID {pid}")
        card.farm_toggled.connect(lambda on, p=pid: self._toggle_farm(p, on))
        card.potion_toggled.connect(lambda on, p=pid: self._toggle_potion(p, on))
        card.potion_changed.connect(lambda p=pid: self._apply_potion_config(p))
        card.travel_toggled.connect(lambda on, p=pid: self._toggle_travel(p, on))
        card.travel_pause_pressed.connect(lambda p=pid: self._pause_travel(p))

        self._readers[pid] = reader
        self._cards[pid] = card
        # 把這隻角色上次的補水設定帶回來（存的是道具編號，不是格號）。
        # ⚠ 要在接上 signal **之後**才套用，勾選才會真的把 bot 帶起來。
        saved = potion_store.get(status.name)
        if saved is not None:
            card.apply_saved_potion(saved)
            if saved.enabled:
                self._toggle_potion(pid, True)
        self._failures[pid] = 0
        self._names[pid] = status.name
        self.tabs.addTab(card, status.name or f"PID {pid}")
        self._update_empty_state()
        # 讀背包要全記憶體掃描（約 2 秒），三個視窗一起掃會很鈍 ——
        # 只讀正在看的那一頁，切過去再讀。
        if self._current_pid() == pid:
            self._load_bag(pid)
        log.info("自動掛機：加入 %s（PID %s）", status.name, pid)

        # ⚠⚠ **一建卡就開始看著他。** 舊版只在 `_watch_connections` 的
        # 「連線正常」那一拍才記 `_watching` 與快照 —— 但分頁剛建好的那幾秒
        # `find_server()` 常常還是 None（客戶端還在換到地圖伺服器）。
        # 使用者要是在那段時間關掉遊戲，就**完全沒有人在看**，
        # 閃退偵測從頭到尾沒被啟用（實機日誌：分頁 13:03:04 加入、
        # **同一秒**移除，中間一拍「連線正常」都沒有）。
        #
        # 這裡記得起來是因為 `_scan()` 只在 `find_server(pid) is not None`
        # 時才去 attach —— 也就是說**建得出卡就代表那一刻是連線正常的**。
        self._watching[status.name] = pid
        # 快照用 setdefault：回連之後卡是新建的，這時 `_snaps` 裡放的是
        # 斷線前那一份（要接回去的那份），不能被一份空的蓋掉。
        self._snaps.setdefault(status.name, self.snapshot_for(pid))
        # ⚠ 一定要放在最後：`restore_into` 會去勾那些開關，而勾選要能真的把
        # bot 帶起來，得先接好 signal、套過存檔的補水設定。
        self._restore_if_pending(pid)

    def _remove(self, pid: int, reason: str) -> None:
        # 角色名要在清掉 _names 之前拿 —— 日誌上沒有身分就對不上 `_watching`。
        who = self._names.get(pid)
        bot = self._bots.pop(pid, None)
        if bot is not None:
            self._keep_loot(pid, bot)
            bot.stop()

        potion = self._potions.pop(pid, None)
        if potion is not None:
            potion.stop()

        traveler = self._travelers.pop(pid, None)
        if traveler is not None:
            traveler.stop()
        self._names.pop(pid, None)
        self._bag_loaded.discard(pid)
        self._bags.pop(pid, None)
        worker = self._bag_workers.pop(pid, None)
        if worker is not None:
            worker.wait(3000)

        card = self._cards.pop(pid, None)
        if card is not None:
            index = self.tabs.indexOf(card)
            if index >= 0:
                self.tabs.removeTab(index)
            card.deleteLater()

        reader = self._readers.pop(pid, None)
        if reader is not None:
            reader.close()

        self._failures.pop(pid, None)
        self._update_empty_state()
        log.info("自動掛機：移除 %s（PID %s，%s）", who or "—", pid, reason)

    # ---- 自動打怪 ---------------------------------------------------

    # ---- 自動回連 ---------------------------------------------------

    def _watch_connections(self) -> None:
        """每拍檢查一次。真的要重連時把工作丟到背景執行緒。

        ## 兩種都要救，而且**閃退那種看不到分頁**

        1. **斷線**：遊戲還在，但沒有連線 —— 走 `self._cards`。
        2. **閃退／被關掉**：遊戲行程整個不見了。分頁是照行程建的，行程沒了
           分頁也會被 `_scan()` 收掉 —— **所以絕對不能只看 `self._cards`**，
           那樣最需要救的情況反而沒有人在看（使用者回報漏掉這一塊）。
           改成另外記一份「我在看哪個角色、他在哪個 PID」（`self._watching`），
           那個 PID 從遊戲行程清單裡消失就是閃退。

        兩種都餵給同一個 `ReconnectDecider`：**不憑一拍的讀數就重開遊戲**
        （行程清單也會有讀不到的那一拍）。

        ⚠ **重連會關掉並重開遊戲**，所以只有使用者在帳號頁勾了「自動回連」
        才會動作。⚠ 關遊戲＋重新登入要三十秒級，**絕不能放在 UI 執行緒**。
        """
        if not current_settings().auto_reconnect or self._reconnecting:
            return
        now = time.monotonic()
        for pid, card in list(self._cards.items()):
            who = card.character
            if not who:
                continue
            # 只在連線正常時更新快照 —— 斷線當下什麼都停了，那時候拍等於忘光
            if find_server(pid) is not None:
                self._snaps[who] = self.snapshot_for(pid)
                self._deciders.pop(who, None)
                self._no_snapshot_said.discard(who)
                self._watching[who] = pid      # 記住他現在住在哪個行程
                continue
            decider = self._deciders.setdefault(who, ReconnectDecider())
            state = decider.decide(False, local_network_up(), now)
            if state == RECONNECT:
                self._begin_reconnect(pid, who, decider)
                return
            log.info("「%s」%s", who, decider.note)

        if self._watch_for_crashes(now):
            return

    def _watch_for_crashes(self, now: float) -> bool:
        """遊戲行程整個不見了（閃退／被關掉）也要重開。回傳有沒有開始重連。

        ⚠ **不能用「分頁還在不在」判斷**：分頁是照行程建的，行程沒了分頁也沒了，
        於是最需要救的情況反而沒有人在看。用的是「我記下來的那個 PID 還在不在
        遊戲行程清單裡」。

        ⚠ 也**不能只憑一拍**就重開 —— 行程清單一樣有讀不到的時候。
        所以照樣走 `ReconnectDecider`（`has_server=False`），連續幾拍都不見才動手。
        """
        if not self._watching:
            return False
        try:
            from ro_toolbox.services import game_launcher

            alive = set(game_launcher.game_pids())
        except Exception as exc:  # noqa: BLE001 - 查不到就別亂關人家的遊戲
            log.debug("查不到遊戲行程清單，這一拍不判斷閃退：%s", exc)
            return False
        if not alive:
            # 一個遊戲都查不到有兩種可能：真的全掛了，或 psutil 沒裝／查不到。
            # 後者在 `game_pids()` 裡回的也是空清單 —— 分不出來，所以只在
            # 「本來就有在看的行程」不見時才算數，下面那個迴圈自然會處理。
            log.debug("目前查不到任何遊戲行程")
        for who, pid in list(self._watching.items()):
            if pid in alive:
                continue
            decider = self._deciders.setdefault(who, ReconnectDecider())
            state = decider.decide(False, local_network_up(), now)
            if state == RECONNECT:
                log.warning("「%s」的遊戲不見了（PID %s）—— 當成閃退，重開", who, pid)
                self._begin_reconnect(pid, who, decider)
                return True
            log.info("「%s」的遊戲不見了：%s", who, decider.note)
        return False

    def _begin_reconnect(self, pid: int, who: str, decider) -> None:
        """把「關遊戲→重開→登入」丟到背景，完成後回 UI 執行緒接回設定。"""
        snap = self._snaps.get(who)
        if snap is None:
            # 沒有斷線前的快照就不知道要接回什麼 —— 不如不動，交給人。
            # （只有「程式開起來時就已經斷線」或「斷線後才勾自動回連」會走到這裡；
            #  正常情況連線正常的每一拍都在更新快照。）
            #
            # ⚠ **只講一次。** 這個判斷每拍都會走到一次，照講會變成每秒一行洗版
            # —— 而且真正的訊息會被自己洗掉。
            if who not in self._no_snapshot_said:
                self._no_snapshot_said.add(who)
                log.warning("「%s」斷線了，但沒有斷線前的快照，先不自動回連", who)
            return
        # ⚠ 先忘掉舊 PID：接下來那個行程一定會消失（我們自己關的／已經掛了），
        # 留著會讓下一拍又判定一次閃退。等新的遊戲連上線再重新記。
        self._watching.pop(who, None)
        worker = _ReconnectWorker(pid, who, snap)
        thread = WorkerThread(worker)   # ⚠ 只吃一個參數，沒有 parent
        worker.done.connect(self._reconnect_done)
        worker.finished.connect(lambda: setattr(self, "_reconnecting", False))
        self._reconnecting = True
        self._reconnect_thread = thread
        self._reconnect_decider = decider
        self._reconnect_pid = pid       # 失敗時要放回 `_watching`，見 `_reconnect_done`
        thread.start()

    def _reconnect_done(self, new_pid: int, who: str, snap, why: str) -> None:
        if new_pid <= 0:
            # ⚠ 失敗要退避，不能無腦一直重開（伺服器維修時我們分不出來）
            self._reconnect_decider.note_attempt_failed(time.monotonic())
            # ⚠⚠ **失敗之後還要繼續看著他，不然這一趟就此永遠停住。**
            # `_begin_reconnect` 把 `_watching[who]` 拿掉了（那個行程一定會消失），
            # 而分頁是照「**有連線的**遊戲行程」建的 —— 登入沒完成就沒有分頁。
            # 所以不放回去的話：`_cards` 裡沒有他、`_watching` 裡也沒有他，
            # 退避時間到了也**沒有任何一拍會再試一次**。使用者看到的正是
            # 「回連失敗之後就卡在那裡」。
            # 放回去的是那個已經關掉的 PID：下一拍照樣判定「行程不見了」，
            # 走同一個 decider，退避到期就自動再重連一次。
            self._watching[who] = self._reconnect_pid
            log.warning("「%s」自動回連失敗：%s；%s", who, why,
                        self._reconnect_decider.note)
            return
        self._deciders.pop(who, None)
        # ⚠ **不要用「等 N 秒再接」**（舊版是 `singleShot(3000, ...)`）。
        # 分頁不是 `_scan()` 當場生出來的：要先有連線，再開背景 `AttachWorker`
        # 做 AOB 定位，成功了才建卡；剛登入完客戶端還在進圖，三秒鐘通常不夠。
        # 實際踩過：使用者的日誌出現「回連後找不到 PID 2788 的分頁，接不回去」——
        # 遊戲確實重開也重登成功了，就只有最後這一步落空。
        #
        # 正確的形狀是**等一個讀得到的訊號**：分頁建好的那一刻（`_on_attached`）
        # 就接回去。逾時只當放棄的上限。
        self._pending_restore[new_pid] = (
            snap, who, time.monotonic() + _RESTORE_TIMEOUT_SEC
        )
        log.info("等 PID %s 的分頁長出來再把設定接回去", new_pid)
        self._scan()

    # ---- 拍下現在在跑什麼，回來之後接回去 -----------------------------

    def snapshot_for(self, pid: int):
        """這個遊戲視窗現在在跑什麼。**存身分不存位置**（[DAT-026] 同一條規矩）。

        重新登入之後角色多半在存檔點，不會在斷線的地方 —— 所以存的是
        「目的地是哪張圖」而不是「路線走到第幾段」，回來重算就好。
        """
        from ro_toolbox.services.reconnect_bot import Snapshot

        card = self._cards.get(pid)
        if card is None:
            return Snapshot()
        labels = []
        farming = card.auto_hunt.isChecked()
        if farming:
            labels.append("自動打怪")
        potion = card.potion_config() if card.auto_potion.isChecked() else None
        if potion is not None:
            labels.append("自動補水")
        travel = self._travelers.get(pid)
        # 目的地取**已經解析出來的那個地圖**：可能是使用者自己選的，
        # 也可能是當初從遊戲導航讀來的 —— 重連之後遊戲那個值不一定還在，
        # 所以這裡把答案本身記下來，不是記「去問遊戲」。
        dest = travel.destination if travel is not None else None
        if dest:
            labels.append(f"前往 {map_display_name(dest) or dest}")
        return Snapshot(farming=farming, potion=potion,
                        destination=dest, labels=labels)

    def _restore_if_pending(self, pid: int) -> None:
        """這個分頁是回連後在等的那個嗎？是就把設定接回去。"""
        waiting = self._pending_restore.pop(pid, None)
        if waiting is None:
            return
        snap, who, _deadline = waiting
        log.warning("「%s」的分頁回來了（PID %s），接回設定", who, pid)
        self.restore_into(pid, snap)

    def _expire_pending_restores(self, now: float) -> None:
        """等太久還沒長出分頁就放棄，而且要**大聲**。

        安靜地忘掉才是最糟的：使用者會以為東西都接回去了，實際上掛機沒開。
        """
        for pid, (snap, who, deadline) in list(self._pending_restore.items()):
            if now < deadline:
                continue
            self._pending_restore.pop(pid, None)
            log.warning(
                "⚠「%s」回連後等了 %.0f 秒還沒出現分頁（PID %s），"
                "沒能接回：%s —— 請自己開一下",
                who, _RESTORE_TIMEOUT_SEC, pid, "、".join(snap.labels) or "（無）",
            )

    def restore_into(self, pid: int, snap) -> None:
        """把快照接回**新開的**那個遊戲視窗上。"""
        card = self._cards.get(pid)
        if card is None:
            log.warning("回連後找不到 PID %s 的分頁，接不回去", pid)
            return
        if snap.potion is not None and not card.auto_potion.isChecked():
            card.auto_potion.setChecked(True)
        if snap.destination:
            position = card.destination.findData(snap.destination)
            if position >= 0:
                card.destination.setCurrentIndex(position)
            card.auto_travel.setChecked(True)     # 這會觸發 _toggle_travel
        elif snap.farming and not card.auto_hunt.isChecked():
            # 趕路途中不打怪（兩個都在送走路封包會互相打架），所以只有
            # 「沒有要趕路」時才把自動打怪接回去；到站之後使用者自己開。
            card.auto_hunt.setChecked(True)
        log.warning("已接回 PID %s：%s", pid, "、".join(snap.labels) or "（無）")

    def _toggle_farm(self, pid: int, on: bool) -> None:
        card = self._cards.get(pid)
        if on:
            if pid in self._bots:
                return
            # 背景 FarmBot 的回報在它自己的執行緒，用 card 的 signal 轉回 UI 執行緒。
            # start() 只起執行緒就返回（設定在背景做，UI 不卡）；成敗看回報的 note。
            bot = FarmBot(pid, on_update=lambda s, c=card: c.farm_stats.emit(s))
            self._bots[pid] = bot
            reader = self._readers.get(pid)
            status = reader.read() if reader is not None else None
            if status is not None and status.has_exp:
                self._exp_start[pid] = (time.monotonic(), status.base_exp, status.job_exp)
            bot.start()
        else:
            bot = self._bots.pop(pid, None)
            if bot is not None:
                self._keep_loot(pid, bot)
                bot.stop()
            self._exp_start.pop(pid, None)
            if card is not None:
                card.set_exp_gain("")

    # ---- 自動尋路 ---------------------------------------------------

    def _toggle_travel(self, pid: int, on: bool) -> None:
        """按下自動尋路：讀遊戲的導航目標，走過去，到了就停。

        **會先把自動打怪關掉**：兩個都在送走路封包會互相搶目標，
        角色會在原地抽搐。純趕路是使用者選的行為，所以這裡直接讓路。
        """
        card = self._cards.get(pid)
        if not on:
            traveler = self._travelers.pop(pid, None)
            if traveler is not None:
                traveler.stop()
            if card is not None:
                card.set_travel_busy(False)
            return
        traveler = self._travelers.get(pid)
        if traveler is not None:
            # 已經有 bot 在跑。暫停中的話，**再按一次就是繼續**（使用者指定的
            # 形狀：暫停鈕只暫停，繼續走「自動尋路」這一顆）。
            if getattr(traveler, "paused", False):
                traveler.resume()
                if card is not None:
                    card.set_travel_paused(False)
            return

        if pid in self._bots and card is not None and card.auto_hunt.isChecked():
            # 先讓 UI 走正常的關閉流程（_toggle_farm 會停 bot、保留戰利品）
            card.auto_hunt.setChecked(False)
            card.set_alert("已先關掉自動打怪（趕路途中不打怪）")
        if card is None:
            return  # 沒有卡片就沒有回報去處，別讓它在背景默默走
        card.set_travel_busy(True)

        # 選單有挑就以它為準，沒挑（None）才去讀遊戲自己的尋路目標。
        traveler = TravelBot(
            pid,
            destination=card.chosen_destination(),
            on_update=lambda s, c=card: c.travel_stats.emit(s),
        )
        self._travelers[pid] = traveler
        traveler.start()

    def _pause_travel(self, pid: int) -> None:
        """按下「暫停」：站住不動，但**不收攤**（socket、擷取、路線、黑名單都留著）。

        找不到 bot 就把介面收回正常狀態 —— 壓著卻沒有東西在暫停等於騙人。
        """
        traveler = self._travelers.get(pid)
        card = self._cards.get(pid)
        if traveler is None:
            if card is not None:
                card.set_travel_paused(False)
            return
        traveler.pause()
        if card is not None:
            card.set_travel_paused(True)

    # ---- 自動補水 ---------------------------------------------------

    def _refresh_current_bag(self) -> None:
        """每秒更新目前分頁的道具數量 —— 背景自己跑，不用按按鈕。"""
        pid = self._current_pid()
        if pid is not None:
            self._refresh_bag(pid)

    def _on_tab_changed(self) -> None:
        """切到某個角色時才讀它的背包（之後由每秒的計時器自己更新）。"""
        pid = self._current_pid()
        if pid is not None and pid not in self._bag_loaded:
            self._load_bag(pid)

    def _load_bag(self, pid: int, again: bool = False) -> None:
        """在背景讀背包（實測 22 ms）。數量會自己一直更新，不需要任何按鈕。

        ⚠ **沒登入就不掃。** 分頁是登入時建的，但玩家可能回到選角畫面或斷線；
        那時候記憶體裡的背包結構已經不在了，再掃只會每秒噴一行
        「AOB 定位不到背包容器」，看起來像特徵壞了 —— 其實只是沒登入
        （[MEM-029]、[PKT-044]）。有沒有登入一律問連線，不能看記憶體。
        """
        if pid in self._bag_workers or (pid in self._bags and not again):
            return
        if find_server(pid) is None:
            card = self._cards.get(pid)
            if card is not None:
                card.set_alert("尚未登入（回到選角畫面？）—— 暫停讀背包")
            return
        self._bag_loaded.add(pid)
        worker = BagWorker(pid)
        worker.done.connect(self._bag_ready)
        worker.finished.connect(lambda p=pid: self._bag_workers.pop(p, None))
        self._bag_workers[pid] = worker
        worker.start()

    def _bag_ready(self, pid: int, rows: object) -> None:
        if pid in self._cards and rows:
            self._bags[pid] = rows
        self._apply_bag(pid)

    def _apply_bag(self, pid: int) -> None:
        """把讀到的背包填進選單。"""
        card = self._cards.get(pid)
        if card is None:
            return
        rows = self._bags.get(pid, {})
        card.set_slots(rows)
        if not rows:
            card.set_alert("⚠ 讀不到背包（AOB 定位失敗）")
            return
        if pid in self._pending_potion and card.auto_potion.isChecked():
            # 背包回來了，剛才被擱著的「自動補水」現在可以真的啟動
            self._toggle_potion(pid, True)

    def _refresh_bag(self, pid: int) -> None:
        """重讀背包。一次實測 22 ms，放在背景執行緒做。"""
        self._load_bag(pid, again=True)

    def _toggle_potion(self, pid: int, on: bool) -> None:
        card = self._cards.get(pid)
        if not on:
            bot = self._potions.pop(pid, None)
            if bot is not None:
                bot.stop()
            self._save_potion(pid)
            return
        if pid in self._potions or card is None:
            return
        config = card.potion_config()
        if not (config.wants_hp() or config.wants_sp()):
            if any(card.pending_items()):
                # ⚠ 還原存檔時**背包通常還沒讀到**（那是背景執行緒在做），
                # 下拉是空的、道具還在等著被選起來 —— 這時候取消勾選，
                # 使用者看到的就是「我上次明明有開，怎麼沒記住」。
                # 勾著等背包回來，`_apply_bag()` 會再啟動一次。
                self._pending_potion.add(pid)
                log.info("自動補水先等背包讀到再啟動（PID %s）", pid)
                return
            card.set_alert("⚠ 還沒選道具或百分比是 0，沒有東西可以補")
            card.quiet = True
            try:
                # ⚠ 這是**程式**判定開不起來，不是使用者關的 —— 不能覆蓋掉存檔。
                card.auto_potion.setChecked(False)
            finally:
                card.quiet = False
            return
        self._pending_potion.discard(pid)
        bot = PotionBot(pid, config, on_update=lambda s, c=card: c.potion_stats.emit(s))
        self._potions[pid] = bot
        bot.start()
        self._save_potion(pid)

    def _apply_potion_config(self, pid: int) -> None:
        """設定改了就即時套用，不必關掉重開；順便記到本機。"""
        card = self._cards.get(pid)
        bot = self._potions.get(pid)
        if card is not None and bot is not None:
            bot.configure(card.potion_config())
        self._save_potion(pid)

    def _save_potion(self, pid: int) -> None:
        """把畫面上的補水設定記起來（依角色名）。

        ⚠ `card.quiet` 為 True 時**不存**：那是程式自己在改 UI ——
        還原存檔、或 bot 啟動失敗自動取消勾選。把那些當成使用者的意思，
        一次啟動失敗就會把設定覆蓋成「關閉」。
        """
        card = self._cards.get(pid)
        if card is None or card.quiet or not card.character:
            return
        potion_store.save(card.character, card.saved_potion())

    # ---- 數值更新 ---------------------------------------------------

    def _read_all(self) -> None:
        for pid in list(self._readers):
            reader = self._readers.get(pid)
            card = self._cards.get(pid)
            if reader is None or card is None:
                continue

            status = reader.read()
            if status is None:
                self._failures[pid] = self._failures.get(pid, 0) + 1
                if self._failures[pid] >= _MAX_READ_FAILURES:
                    # 連續讀不到，多半是登出回到選角畫面，或遊戲關了
                    self._remove(pid, "連續讀取失敗")
                    self._retry_at[pid] = time.monotonic() + _RETRY_AFTER_SEC
                else:
                    card.set_note(f"PID {pid}　讀取失敗 {self._failures[pid]} 次")
                continue

            self._failures[pid] = 0
            card.update_status(status)
            card.set_exp_gain(self._exp_gain_text(pid, status))
            index = self.tabs.indexOf(card)
            if index >= 0 and status.name and self.tabs.tabText(index) != status.name:
                self.tabs.setTabText(index, status.name)

    # ---- 收尾 -------------------------------------------------------

    def shutdown(self) -> None:
        super().shutdown()
        self._scan_timer.stop()
        self._read_timer.stop()

        for bot in list(self._bots.values()):
            bot.stop()
        self._bots.clear()

        for worker in list(self._workers.values()):
            worker.requestInterruption()
            worker.wait(3000)
        self._workers.clear()

        for pid in list(self._cards):
            self._remove(pid, "程式關閉")
