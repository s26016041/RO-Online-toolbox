"""讀取角色狀態（HP / SP / 等級）。

用 AOB 特徵定位，不記絕對位址 —— 見專案 CLAUDE.md 的最高原則。

    from ro_toolbox.services.character import CharacterReader
    reader = CharacterReader()
    reader.attach(pid)
    status = reader.read()
"""

from __future__ import annotations

import logging
import struct

from ro_toolbox.services.aob import scan
from ro_toolbox.services.memory_scan import VALUE_TYPES, MemoryScanner
from ro_toolbox.services.signatures import (
    CHAR_STATUS,
    MAP_NAME_ENCODING,
    MAP_NAME_MAX_BYTES,
    NAME_ENCODING,
    NAME_MAX_BYTES,
    STATUS_OFFSETS,
)

log = logging.getLogger(__name__)

_INT32 = VALUE_TYPES["int32"]

# 合理性驗證用的上限。讀到超出範圍的值代表定位跑掉了，寧可回報失敗。
_MAX_LEVEL = 999
_MAX_HP = 10_000_000
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
    if not (0 <= status.hp <= status.max_hp <= _MAX_HP):
        return False
    if not (0 <= status.sp <= status.max_sp <= _MAX_HP):
        return False
    if status.base_exp_next and not (0 <= status.base_exp <= _MAX_EXP):
        return False
    if status.job_exp_next and not (0 <= status.job_exp <= _MAX_EXP):
        return False
    return status.max_hp > 0


class CharacterReader:
    """以 AOB 特徵定位角色狀態結構，之後每次讀取都用同一個基址。

    基址在 attach() 時定位一次。遊戲重開或改版後必須重新 attach ——
    絕對不要把定位結果存到設定檔或原始碼裡。
    """

    def __init__(self) -> None:
        self._scanner = MemoryScanner()
        self._base: int | None = None

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
            limit=8,
            should_stop=should_stop,
        )
        if not hits:
            log.error("AOB 特徵定位失敗：找不到角色狀態結構（遊戲可能已改版）")
            self.close()
            return False
        if len(hits) > 1:
            # 特徵應該唯一。命中多個代表特徵已經不夠精確，不要賭哪個是對的。
            log.error("AOB 特徵命中 %d 個位址，預期只有 1 個，判定為定位失敗", len(hits))
            self.close()
            return False

        self._base = hits[0]
        log.info("角色狀態結構定位於 %#x", self._base)
        return True

    def read_position(self) -> tuple[int, int] | None:
        """讀角色的格座標 (x, y)。

        與 read() 分開：座標是每秒都在變的東西，呼叫端可能只要它而不需要
        整包狀態；而且它的驗證條件不同（要靠地形檢查，不是數值範圍）。
        """
        if self._base is None:
            return None
        raw = self._scanner._read_bytes(self._base + STATUS_OFFSETS.position, 8)
        if not raw or len(raw) < 8:
            return None
        x, y = struct.unpack("<II", raw)
        # 合理性：RO 沒有超過 512x512 的地圖
        if x >= 512 or y >= 512:
            log.debug("座標超出合理範圍：(%s, %s)", x, y)
            return None
        return x, y

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
        status = CharacterStatus(**values)
        if not _plausible(status):
            log.warning("角色狀態數值不合理，判定定位已失效：%r", status)
            return None
        return status

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
        try:
            self._scanner.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("關閉掃描器時的例外：%s", exc)
