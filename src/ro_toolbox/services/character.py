"""讀取角色狀態（HP / SP / 等級）。

用 AOB 特徵定位，不記絕對位址 —— 見專案 CLAUDE.md 的最高原則。

    from ro_toolbox.services.character import CharacterReader
    reader = CharacterReader()
    reader.attach(pid)
    status = reader.read()
"""

from __future__ import annotations

import logging

from ro_toolbox.services.aob import locate_global, scan
from ro_toolbox.services.memory_scan import VALUE_TYPES, MemoryScanner
from ro_toolbox.services.player_position import PlayerPosition
from ro_toolbox.services.signatures import (
    CHAR_STATUS,
    MAP_NAME_ENCODING,
    MAP_NAME_MAX_BYTES,
    NAME_ENCODING,
    NAME_MAX_BYTES,
    SELECT_CURSOR_SIGS,
    SELECT_NAME_SIGS,
    STATUS_OFFSETS,
)
from ro_toolbox.utils.logging import StateLog

log = logging.getLogger(__name__)

_INT32 = VALUE_TYPES["int32"]

# 合理性驗證用的上限。讀到超出範圍的值代表定位跑掉了，寧可回報失敗。
_MAX_LEVEL = 999
_MAX_HP = 10_000_000
#: 定位時最多收幾個候選。以前是 8 —— 實測堆積裡的垃圾命中就有 5 個，
#: 上限訂太低會在驗證之前就把真的角色截掉（而且完全看不出來被截了）。
_SCAN_LIMIT = 64
# 經驗值是 int64，但單一等級的門檻不會離譜。超過就是讀到垃圾，寧可不顯示。
_MAX_EXP = 10_000_000_000
# 滿級時伺服器塞一個哨兵值當「升級所需」（實測商狐 Job 50 讀到 999999999999999999）。
_EXP_MAXED = _MAX_EXP


class CharacterStatus:
    """一次讀取的角色狀態快照。"""

    __slots__ = (
        "name",
        "map_name",
        "hp",
        "max_hp",
        "sp",
        "max_sp",
        "base_level",
        "job_level",
        "base_exp",
        "base_exp_next",
        "job_exp",
        "job_exp_next",
        "aid",
    )

    def __init__(
        self,
        hp: int,
        max_hp: int,
        sp: int,
        max_sp: int,
        base_level: int,
        job_level: int,
        base_exp: int = 0,
        base_exp_next: int = 0,
        job_exp: int = 0,
        job_exp_next: int = 0,
        aid: int = 0,
        name: str = "",
        map_name: str = "",
    ) -> None:
        self.name = name
        self.map_name = map_name
        self.hp = hp
        self.max_hp = max_hp
        self.sp = sp
        self.max_sp = max_sp
        self.base_level = base_level
        self.job_level = job_level
        self.base_exp = base_exp
        self.base_exp_next = base_exp_next
        self.job_exp = job_exp
        self.job_exp_next = job_exp_next
        self.aid = aid

    @property
    def base_maxed(self) -> bool:
        """已滿級。滿級時「升級所需」是個哨兵大數，不是真的門檻。"""
        return self.base_exp_next >= _EXP_MAXED

    @property
    def job_maxed(self) -> bool:
        return self.job_exp_next >= _EXP_MAXED

    @property
    def base_percent(self) -> float:
        """距離下一級的百分比。遊戲畫面是**無條件捨去**到小數一位
        （實測 69.01% 顯示 69.0%、32.59% 顯示 32.5%），這裡回精確值。"""
        if self.base_maxed:
            return 100.0
        return 100.0 * self.base_exp / self.base_exp_next if self.base_exp_next else 0.0

    @property
    def job_percent(self) -> float:
        if self.job_maxed:
            return 100.0
        return 100.0 * self.job_exp / self.job_exp_next if self.job_exp_next else 0.0

    @property
    def has_exp(self) -> bool:
        """經驗值讀得到且合理嗎？讀不到就別顯示，不要秀 0% 誤導人。"""
        return self.base_exp_next > 0 and self.job_exp_next > 0

    @property
    def hp_percent(self) -> float:
        return 100.0 * self.hp / self.max_hp if self.max_hp else 0.0

    @property
    def sp_percent(self) -> float:
        return 100.0 * self.sp / self.max_sp if self.max_sp else 0.0

    def __repr__(self) -> str:
        return (
            f"CharacterStatus({self.name}@{self.map_name} "
            f"Base {self.base_level} {self.base_percent:.2f}% "
            f"Job {self.job_level} {self.job_percent:.2f}% "
            f"HP {self.hp}/{self.max_hp} SP {self.sp}/{self.max_sp})"
        )


def _until_null(raw: bytes) -> bytes:
    """截到第一個 null 為止（C 字串）。"""
    end = raw.find(0)
    return raw if end < 0 else raw[:end]


def _plausible(status: CharacterStatus) -> bool:
    """讀取端的合理性驗證。驗不過就當定位失敗，不拿垃圾值繼續算。"""
    if not (1 <= status.base_level <= _MAX_LEVEL):
        return False
    if not (1 <= status.job_level <= _MAX_LEVEL):
        return False
    # ⚠ **不要求 hp <= max_hp。** 升等的那一拍客戶端會先更新 HP、還沒更新 maxHP，
    # 實測讀到 `HP 1274/914`（狐狐狸升到 Base 40 的瞬間）—— 那是真的角色，
    # 卻被判成「定位已失效」而噴警告。`_plausible()` 要認的是**定位跑掉**，
    # 而定位跑掉給的是離譜數字，那已經被 `_MAX_HP` 擋住了。
    if not (0 <= status.hp <= _MAX_HP and 0 < status.max_hp <= _MAX_HP):
        return False
    if not (0 <= status.sp <= _MAX_HP and 0 <= status.max_sp <= _MAX_HP):
        return False
    if status.base_exp_next and not (0 <= status.base_exp <= _MAX_EXP):
        return False
    if status.job_exp_next and not (0 <= status.job_exp <= _MAX_EXP):
        return False
    if not status.name.strip():
        # ⚠ 角色一定有名字。名字是空的代表這不是真的角色狀態 ——
        # 實測停在**選角畫面**時會讀到殘留結構：Base 54、HP 54/54、SP 0/0，
        # 數值全部落在合理範圍內，只有名字是空的。少了這一條，
        # 自動掛機頁會拿這種垃圾建出一個分頁，然後照著它算血量百分比。
        return False
    return status.max_hp > 0


#: 以 PID 為鍵的降噪器。
#:
#: ⚠ **不能放在 `CharacterReader` 實例上。** 自動掛機頁每一輪重試都**新建一個
#: reader**（見 `farm_page._start_attach`），實例層級的降噪永遠是「第一次」——
#: 於是登入畫面上每 12 秒噴一行 WARNING，使用者實際回報過洗版。
_notes: dict[int, StateLog] = {}


def _notes_for(pid: int) -> StateLog:
    return _notes.setdefault(pid, StateLog(log))


#: 這個 PID 曾經讀到過真的角色狀態嗎？
#: 沒有 → 多半只是還沒進到遊戲（登入／選角畫面沒有角色資料），不是錯誤。
_ever_valid: set[int] = set()


class SelectScreen:
    """選角畫面上讀得到的兩件事：**游標停在第幾格**、**選定的是誰**。

    ## 這個東西為什麼存在

    客戶端**在選角畫面建好的時候**把那一隻的名字寫進角色結構裡；
    進遊戲之後伺服器再也不會送名字過來（實機擷取確認：名字只出現在
    角色清單 `0x0B72` 那一包）。所以只要在客戶端寫進去**之前**就送出選角封包，
    那個欄位會一直留著開機殘渣 —— 遊戲裡的名字就是亂碼，
    而伺服器那邊完全正常（2026-08-26 實際踩到）。

    所以送選角之前一定要先讀這裡，確認客戶端已經寫好、而且就是我們要的那一隻。

    ## 定位方式

    選角畫面上 HP／等級都還沒有值，`CharacterReader` 那條靠數值合理性驗證的
    AOB 過不了關。所以這兩個全域改用**程式碼骨架**定位
    （`SELECT_CURSOR_SIGS`／`SELECT_NAME_SIGS`）：指令樣式當錨、位址從立即值
    讀出來，跟寫死偏移不一樣，改版只要那幾行指令還在就跟得上。

    而且還有第二層驗證：按下 Enter 之後客戶端寫下的名字，必須是**伺服器剛送來的
    角色清單**裡我們要的那一隻。程式碼定位 + 封包資料，兩份獨立來源互相對照。
    """

    def __init__(self, pid: int) -> None:
        self._scanner = MemoryScanner()
        self._addr: int | None = None
        self._index_addr: int | None = None
        try:
            self._scanner.open(pid)
        except Exception as exc:  # noqa: BLE001
            log.warning("選角畫面：開啟行程 %s 失敗：%s", pid, exc)
            return
        # 兩個都用**程式碼骨架**定位（見 signatures 的 SELECT_* 特徵）。
        # 定位不到就維持 None，呼叫端會停手 —— 不准拿寫死的偏移頂替。
        self._addr = locate_global(self._scanner, SELECT_NAME_SIGS)
        self._index_addr = locate_global(self._scanner, SELECT_CURSOR_SIGS)

    @property
    def address(self) -> int | None:
        return self._addr

    @property
    def ready(self) -> bool:
        """兩個位址都算得出來才算能用。"""
        return self._addr is not None and self._index_addr is not None

    def index(self) -> int | None:
        """游標現在停在第幾格。讀不到回 None。

        這個數字就是**格號**（實機驗證：索引 4 按下去進到的是第 4 格那隻）。
        """
        if self._index_addr is None:
            return None
        raw = self._scanner._read_bytes(self._index_addr, 1)  # noqa: SLF001
        return raw[0] if raw else None

    def read(self) -> str:
        """讀出目前那個字串。讀不到回空字串。

        ⚠ 回傳的東西**沒有經過驗證** —— 欄位還沒被寫過的時候，
        裡面是開機殘渣（實測是一段韓文字型測試字串），照樣解得出「一個字串」。
        呼叫端一定要拿它跟角色清單比對。
        """
        if self._addr is None:
            return ""
        raw = self._scanner._read_bytes(self._addr, NAME_MAX_BYTES)  # noqa: SLF001
        if not raw:
            return ""
        return _until_null(raw).decode(NAME_ENCODING, errors="replace")

    def close(self) -> None:
        self._scanner.close()


class CharacterReader:
    """以 AOB 特徵定位角色狀態結構，之後每次讀取都用同一個基址。

    基址在 attach() 時定位一次。遊戲重開或改版後必須重新 attach ——
    絕對不要把定位結果存到設定檔或原始碼裡。
    """

    def __init__(self) -> None:
        self._scanner = MemoryScanner()
        self._base: int | None = None
        #: 座標從**角色自己的實體結構**讀（用 AID 認人），與這個 HP 結構無關。
        #: 舊版讀的是小地圖標記留下的全域，在沒有小地圖圖檔的 396 張地圖上
        #: **從頭到尾不會被寫**，卻照樣回一個看起來合理的殘留值（[MEM-047]）。
        self._position = PlayerPosition(self._scanner)
        #: 上一次讀到的地圖。換圖時要把實體位址丟掉重找 —— 舊實體會被回收，
        #: 但**回收不等於清乾淨**（[MEM-022]／[MEM-047]）。
        self._map_name = ""
        #: 這個行程裡**曾經**定位成功過嗎？
        #: 沒有 → 多半只是還沒進到遊戲（登入畫面根本沒有角色資料），不是錯誤。
        #: 有過 → 現在找不到才是真的異常（登出了，或改版讓特徵失效）。
        # ⚠ 這兩個都以 PID 為鍵放在模組層級 —— 呼叫端每一輪都新建 reader，
        # 放在實例上等於沒有記憶（見 `_notes`）。
        self._pid: int | None = None

    @property
    def located(self) -> bool:
        return self._base is not None

    @property
    def address(self) -> int | None:
        """目前定位到的 HP 位址。僅供顯示與除錯，不要拿去存檔。"""
        return self._base

    def attach(self, pid: int, should_stop=None) -> bool:
        """附加到行程並用特徵定位。失敗回傳 False，呼叫端要大聲停用功能。

        should_stop: 可選的 callable，掃描每個記憶體區塊前會問一次；
        回傳 True 就中止定位。關程式時用得到——全掃一趟要一秒多。
        """
        self.close()
        try:
            self._scanner.open(pid)
        except Exception as exc:  # noqa: BLE001
            log.error("開啟行程 %s 失敗：%s", pid, exc)
            return False

        hits = scan(
            self._scanner,
            CHAR_STATUS,
            writable_only=True,
            limit=_SCAN_LIMIT,
            should_stop=should_stop,
        )
        # ⚠ 命中多個**不代表特徵壞了**。實測（2026-08-26）玩久了之後堆積裡會出現
        # 5 個同樣位元組樣式的垃圾：HP 15、max_hp 42 億、名字與地圖都是空的。
        # 真正的角色（狐狐狸 HP 1020/1020、Base 33、在 mjolnir_06）也在裡面，
        # 但舊版看到「不只一個」就直接放棄 —— 遊戲明明開著卻讀不到，
        # 使用者實際回報過。AOB 只是**錨**，分辨「這是不是角色」要靠數值本身。
        if len(hits) > 1:
            real = [h for h in hits if self.probe(h) is not None]
            log.info("角色特徵命中 %d 個，通過合理性驗證的有 %d 個", len(hits), len(real))
            hits = real
        if not hits:
            notes = _notes_for(pid)
            if pid in _ever_valid:
                notes.problem(
                    "gone", logging.ERROR,
                    "角色狀態結構不見了 —— 角色可能已登出，或改版讓特徵失效",
                )
            else:
                # 還沒進到遊戲裡就是讀不到，這是**正常狀態不是錯誤**。
                notes.problem(
                    "not-in-game", logging.INFO,
                    "還沒讀到角色狀態 —— 通常是還沒進到遊戲裡，進去之後會自己接上",
                )
            self.close()
            return False
        if len(hits) > 1:
            # 驗證之後還是不只一個 —— 那才是真的分不出來（多開？特徵不夠精確？）。
            # 這種時候不准賭，賭錯就是照著別人的血量做決策。
            _notes_for(pid).problem(
                "ambiguous", logging.ERROR,
                "有 %d 個位址都像是真的角色狀態，分不出來，判定為定位失敗", len(hits),
            )
            self.close()
            return False

        self._base = hits[0]
        self._pid = pid
        log.info("角色狀態結構定位於 %#x", self._base)
        self._locate_position()
        return True

    def _locate_position(self) -> bool:
        """定位座標的兩個來源（進圖座標全域 ＋ 角色的移動元件）。

        AID 是從剛剛定位好的狀態結構讀出來的 —— 這就是 CLAUDE.md 的
        「存身分、當場查位置」：身分（AID）穩定，元件在堆積哪裡每次現查。
        """
        status = self._collect()
        self._map_name = status.map_name if status is not None else ""
        return self._position.locate(status.aid if status is not None else 0)

    @property
    def position_located(self) -> bool:
        """座標定位成功了嗎？沒有的話走路類功能要停用，不要空轉。"""
        return self._position.located

    def read_position(self) -> tuple[int, int] | None:
        """讀角色的格座標 (x, y)。驗不過回 None —— **絕不回殘留值**。

        與 read() 分開：座標是每秒都在變的東西，呼叫端可能只要它而不需要
        整包狀態。實作與踩過的坑見 `services/player_position.py`。

        ⚠ 這裡**自己**再讀一次地圖名（幾十微秒），不依賴呼叫端有沒有先呼叫
        `read()`。地圖名決定兩件事：換圖要丟掉舊的移動元件、以及拿哪張圖的
        地形驗「這一格站得住嗎」。少了它，換圖後那個殘留值又會安靜地過關。
        """
        map_name = self._read_text(
            STATUS_OFFSETS.map_name, MAP_NAME_MAX_BYTES, MAP_NAME_ENCODING
        )
        self._note_map(map_name)
        return self._position.read(map_name)

    def _note_map(self, map_name: str) -> None:
        """地圖換了就把記著的移動元件丟掉。客戶端會回收舊元件，而**回收不等於
        清乾淨**：GID 可能還在原地，只是不再更新（[MEM-022]／[MEM-047]）。"""
        if not map_name or map_name == self._map_name:
            return
        if self._map_name:
            log.info("地圖從 %s 換到 %s，重新定位角色移動元件",
                     self._map_name, map_name)
            self._position.invalidate()
        self._map_name = map_name

    def alive(self) -> bool:
        """目標行程還活著嗎？

        用 GetExitCodeProcess 直接問行程狀態，**不要**拿「視窗列不列得到」
        當依據——遊戲載入或換地圖時視窗標題可能瞬間為空，那時視窗列舉會漏掉它。
        查詢失敗時回 True：寧可晚一拍發現，也不要把好端端的遊戲誤判成關掉了。
        """
        return self._scanner.alive()

    def read(self) -> CharacterStatus | None:
        """讀一次狀態。定位失敗或數值不合理時回傳 None。"""
        if self._base is None or not self._scanner.attached:
            return None
        status = self._collect()
        if status is None:
            return None
        if not _plausible(status):
            notes = _notes_for(self._pid or 0)
            if self._pid in _ever_valid:
                # 之前讀得到、現在讀不到 —— 那才是真的異常（登出了，或改版）。
                notes.problem(
                    "implausible", logging.WARNING,
                    "角色狀態數值不合理，判定定位已失效：%r", status,
                )
            else:
                # 還沒進過遊戲：登入／選角畫面本來就會掃到殘留結構
                # （數值落在合理範圍、只有名字是空的，見 `_plausible`）。
                # 那是**正常狀態不是錯誤**，不准每 12 秒噴一行 WARNING。
                notes.problem(
                    "not-in-game", logging.INFO,
                    "讀到的還不是真的角色狀態（多半還沒進遊戲）：%r", status,
                )
            return None
        if self._pid is not None:
            _ever_valid.add(self._pid)
        _notes_for(self._pid or 0).ok("角色狀態恢復正常")
        self._note_map(status.map_name)
        return status

    def probe(self, base: int) -> CharacterStatus | None:
        """在**指定位址**讀一份狀態並驗合理性，不動降噪狀態也不改 `_base`。

        定位時用來把垃圾命中挑掉：AOB 只是錨，真正分辨「這是不是角色」的是
        數值本身（見 `_plausible`：名字、HP ≤ maxHP、等級範圍…）。
        """
        saved = self._base
        self._base = base
        try:
            status = self._collect()
        finally:
            self._base = saved
        return status if status is not None and _plausible(status) else None

    def _collect(self) -> CharacterStatus | None:
        """把 `_base` 那份結構的欄位讀出來組成狀態。**不做合理性驗證**。"""
        values = {}
        for field in ("hp", "max_hp", "sp", "max_sp", "base_level", "job_level"):
            offset = getattr(STATUS_OFFSETS, field)
            value = self._scanner.read_value(self._base + offset, _INT32)
            if value is None:
                log.debug("讀取 %s 失敗（位址 %#x）", field, self._base + offset)
                return None
            values[field] = value

        # 經驗值是 int64。讀不到就留 0，由 has_exp 決定不顯示 —— 經驗讀不到
        # 不該害整個角色狀態變成「定位失敗」。
        for field in ("base_exp", "base_exp_next", "job_exp", "job_exp_next"):
            values[field] = self._read_int64(getattr(STATUS_OFFSETS, field)) or 0

        # AID：使用道具封包要帶（[MEM-017]）。讀不到就留 0，由呼叫端拒絕動作。
        values["aid"] = self._scanner.read_value(
            self._base + STATUS_OFFSETS.aid, _INT32
        ) or 0

        values["name"] = self._read_name()
        values["map_name"] = self._read_text(
            STATUS_OFFSETS.map_name, MAP_NAME_MAX_BYTES, MAP_NAME_ENCODING
        )
        return CharacterStatus(**values)

    def _read_int64(self, offset: int) -> int | None:
        """讀一個 int64。經驗值欄位是 64 位元（實測高 32 位元為 0）。"""
        if self._base is None:
            return None
        raw = self._scanner._read_bytes(self._base + offset, 8)
        if raw is None or len(raw) < 8:
            return None
        return int.from_bytes(raw, "little", signed=True)

    def _read_name(self) -> str:
        """角色名是 cp950、null 結尾。讀不到就回空字串，不讓它擋掉數值。"""
        return self._read_text(STATUS_OFFSETS.name, NAME_MAX_BYTES, NAME_ENCODING)

    def _read_text(self, offset: int, size: int, encoding: str) -> str:
        """讀一段 null 結尾的字串。讀不到就回空字串，不讓它擋掉數值。"""
        if self._base is None:
            return ""
        raw = self._scanner._read_bytes(self._base + offset, size)
        if not raw:
            return ""
        return _until_null(raw).decode(encoding, errors="replace")

    def close(self) -> None:
        self._base = None
        self._map_name = ""
        self._position.forget()
        try:
            self._scanner.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("關閉掃描器時的例外：%s", exc)
