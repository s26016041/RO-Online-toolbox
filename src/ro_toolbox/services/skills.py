"""讀出「這個角色有哪些技能、各幾級」——全部從記憶體，不靠封包。

## 為什麼不用封包

完整技能列表封包是 `0x0B32`（ZC_SKILLINFO_LIST），**只有登入進圖那一次會送**。
實測驅動角色從 mjolnir_07 走傳點到 mjolnir_06，伺服器只補了 4 筆 `0x0B33`
（單一技能更新），完整列表一筆都沒重送（[MEM-050]）。也就是說封包路只有
「工具比遊戲先開」才拿得到 —— 中途啟動就永遠是空的，那不是安全退化，是瞎掉。
記憶體裡的技能表隨時都在，讀就有。

## 結構長什麼樣（實測 2026-08-29）

客戶端把每個技能存成一個節點（前面 0x10 bytes 是 std::map 的鏈結欄位），
資料區全部是 int32：

    +0x00  id            技能編號        ← 用英文代號字串反查驗證過
    +0x04  inf?          疑似目標型態    ← 只對已學技能對得上，未學的是垃圾值
    +0x08  level         目前等級        ← 跟封包 0x0B33 對過（0 = 還沒學）
    +0x0C  sp            這一級消耗 SP   ← 記憶體＝封包＝lub 三方一致
    +0x10  ?             疑似射程
    +0x14  ?             疑似射程
    +0x18  const char*   **英文代號字串**（"KN_TWOHANDQUICKEN"）
    +0x1C  ?             未解
    +0x20  maxlv         最大等級        ← 跟 skillinfolist.lub 的 MaxLv 全部對上

⚠ `+0x10` 與 `+0x14` **兩個都像射程，但證據不足以區分**：唯一拿得到的答案卡
（封包 `0x0B33`）只涵蓋 4 個技能，那 4 個的兩欄都是 1。所以這裡**兩個都不輸出**
—— 猜一個填進去就是「很有自信的錯」（CLAUDE.md：不確定一律留空）。

⚠ `+0x14` 一度被當成 `upgradable` 拿去粗篩（要求 <= 1），結果**安靜漏掉 4 個技能**
（實測值有 2 和 4，其中 SM_PROVOKE 是已學到 Lv5 的）。粗篩只准用驗證過的欄位。

## 怎麼確定找到的是對的東西

`+0x18` 那個字串指標就是判別的關鍵：**把它指到的英文代號拿去查
`skillid.lub` 抽出來的表，得到的 ID 必須等於 `+0x00` 的值**。這是兩份
互相獨立的欄位彼此驗證，堆積裡的垃圾湊不出來 —— 實測 16 萬個通過數值範圍
過濾的候選，交叉驗證後只剩 21 個，全部是真的技能（[MEM-050]）。

`maxlv` 再跟 `skillinfolist.lub` 的 `MaxLv` 對一次，`sp` 跟 `SpAmount[level-1]`
對一次。都是**遊戲自己的資料**，不是我猜的。

沒有任何位址被寫死：每次呼叫都當場掃出來（CLAUDE.md 最高原則）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ro_toolbox.services.character import CharacterReader
from ro_toolbox.services.gamedata import skill_codes, skill_name, skill_table
from ro_toolbox.services.memory_scan import MemoryScanner

log = logging.getLogger(__name__)

#: 結構欄位（int32 索引，不是 byte 位移）。出處見模組開頭。
#: 這是**同一個結構內部**的欄位距離，屬於「大更新才會壞」的類別，
#: 允許寫死（CLAUDE.md）。跨結構的距離一律不准這樣算。
_I_ID = 0
_I_LEVEL = 2
_I_SP = 3
_I_NAME_PTR = 6
_I_MAXLV = 8
#: 讀取一個結構要看到的欄位數（含 maxlv）。
_FIELDS = _I_MAXLV + 1

#: 英文代號最長多少（實測最長 26，取 48 有餘裕又不會讀進太多雜訊）。
_NAME_BYTES = 48

#: 數值欄位的合理範圍。這只是**粗篩**，真正的判別是字串↔ID 交叉驗證。
#: ⚠ 只准拿**確認過意義**的欄位來篩：拿沒把握的欄位篩會安靜漏掉技能（見模組開頭）。
_MAX_LEVEL = 20
_MAX_SP = 10000
#: 指標要落在使用者空間、而且 4 對齊。
_PTR_LOW = 0x00010000
_PTR_HIGH = 0x7FF00000


@dataclass(frozen=True)
class Skill:
    """一個技能。`level == 0` 代表「學得起但還沒點」，不是「沒有這個技能」。"""

    id: int
    key: str
    name: str
    level: int
    max_level: int
    #: 這一級的消耗 SP。0 通常代表被動技能（記憶體＝封包＝lub 三方一致）。
    sp: int

    @property
    def learned(self) -> bool:
        return self.level > 0

    def __repr__(self) -> str:  # pragma: no cover - 只給除錯看
        return f"<Skill {self.id} {self.key} {self.name} Lv{self.level}/{self.max_level}>"


def _table_arrays() -> tuple[np.ndarray, int]:
    """把「ID → MaxLv」攤成陣列，讓 numpy 一次比對整塊記憶體。

    值的意義：0 = 這個 ID 不是技能（直接淘汰）；-1 = 是技能但表裡沒有 MaxLv
    （放寬成範圍檢查，不能因為資料缺一欄就把真的技能丟掉）。
    """
    table = skill_table()
    if not table:
        return np.zeros(1, dtype=np.int32), 0
    top = max(table)
    arr = np.zeros(top + 1, dtype=np.int32)
    for skill_id, entry in table.items():
        maxlv = entry.get("maxlv")
        arr[skill_id] = int(maxlv) if isinstance(maxlv, int) and maxlv > 0 else -1
    return arr, top


def _candidates(u32: np.ndarray, maxlv_of: np.ndarray, top_id: int) -> np.ndarray:
    """粗篩：回傳可能是技能結構的起始索引。

    每一條都是**便宜的數值範圍檢查**，目的只是把要讀字串的候選數壓下來；
    「這到底是不是技能」由呼叫端的字串交叉驗證決定。
    """
    n = len(u32)
    if n < _FIELDS:
        return np.empty(0, dtype=np.int64)
    span = n - _FIELDS + 1
    ids = u32[:span]

    ok = (ids >= 1) & (ids <= top_id)
    if not ok.any():
        return np.empty(0, dtype=np.int64)

    # 只對還活著的候選查表，避免用超出範圍的 ID 去索引。
    safe = np.where(ok, ids, 0)
    want = maxlv_of[safe]
    ok &= want != 0

    got = u32[_I_MAXLV:_I_MAXLV + span]
    # 表裡有 MaxLv 就要求相等；沒有（-1）就退成範圍檢查。
    ok &= np.where(want > 0, got == want, (got >= 1) & (got <= _MAX_LEVEL))

    ok &= u32[_I_LEVEL:_I_LEVEL + span] <= _MAX_LEVEL
    ok &= u32[_I_SP:_I_SP + span] <= _MAX_SP

    ptr = u32[_I_NAME_PTR:_I_NAME_PTR + span]
    ok &= (ptr >= _PTR_LOW) & (ptr < _PTR_HIGH) & (ptr % 4 == 0)
    return np.nonzero(ok)[0]


class SkillReader:
    """掃出角色目前的技能表。

    ⚠ **不要每一拍呼叫。** `read()` 每次都重掃整個行程的可寫記憶體（實測約
    數秒），跟 `bag.as_dict()` 當初拖慢自動補水是同一類問題（[MEM-043]）。
    技能表是「使用者按一下才看」的東西，變動也只在加點時發生。
    """

    def __init__(self, scanner: MemoryScanner | None = None) -> None:
        self._scanner = scanner or MemoryScanner()
        self._owns = scanner is None
        self._pid: int | None = None

    @property
    def pid(self) -> int | None:
        return self._pid

    def attach(self, pid: int) -> bool:
        """附加到行程。失敗回 False，呼叫端要大聲停用功能。"""
        self.close()
        try:
            self._scanner.open(pid)
        except Exception as exc:  # noqa: BLE001
            log.error("開啟行程 %s 失敗：%s", pid, exc)
            return False
        self._pid = pid
        return True

    def _online(self) -> bool:
        """這個分身現在真的有角色在遊戲裡嗎？

        ⚠ **不能只看「有沒有連線」。** 實測 PID 4116 停在選角畫面：`find_server()`
        回得出伺服器（連著 char server 的 10022 埠），角色狀態卻定位失敗，
        而技能表照樣讀得到 **18 個技能**（AL_HEAL Lv1）—— 那是上一次登入留下的殘留。
        技能跟背包、角色狀態一樣，**登出之後不會被清掉**（同 [MEM-029]）。

        判準用「角色狀態結構定位得到」：那份結構是選角畫面之後才寫的（[MEM-035]），
        它在＝真的有角色在場上。
        """
        if self._pid is None:
            return False
        reader = CharacterReader()
        try:
            return reader.attach(self._pid) and reader.read() is not None
        finally:
            reader.close()

    def read(self, should_stop=None, *, require_online: bool = True) -> list[Skill] | None:
        """回傳技能清單（依 ID 排序）。定位失敗回 None，**不回空清單充數**。

        should_stop: 可選 callable，每個記憶體區塊掃描前問一次；回傳 True 就中止。
        require_online: 先確認真的有角色在遊戲裡（見 `_online()`）。只有在呼叫端
            已經自己確認過的時候才准關掉 —— 關掉等於接受「可能拿到上一隻角色的技能」。
        """
        if require_online and not self._online():
            log.info(
                "這個分身現在沒有角色在場上（選角或登入畫面），"
                "記憶體裡的技能表是上一次登入的殘留 —— 不採用",
            )
            return None
        codes = skill_codes()
        if not codes:
            log.warning("技能表（assets/skills.json.gz）載不到，技能功能停用")
            return None
        maxlv_of, top_id = _table_arrays()
        if not top_id:
            log.warning("技能表是空的，技能功能停用")
            return None

        found: dict[int, Skill] = {}
        conflicts: set[int] = set()
        scanned = 0
        for base, size in self._scanner._iter_regions(writable_only=True):
            if should_stop is not None and should_stop():
                log.info("技能掃描被中止")
                return None
            if size < _FIELDS * 4:
                continue
            raw = self._scanner._read_region(base, size)
            if not raw:
                continue
            blob = bytes(raw)
            u32 = np.frombuffer(blob[: len(blob) // 4 * 4], dtype=np.uint32)
            for index in _candidates(u32, maxlv_of, top_id):
                scanned += 1
                skill = self._verify(u32, int(index), codes)
                if skill is None:
                    continue
                previous = found.get(skill.id)
                if previous is None:
                    found[skill.id] = skill
                elif previous != skill:
                    # 兩份都通過了交叉驗證卻不一樣 —— 多開？上一隻角色的殘留？
                    # 這種時候不准挑一個用，賭錯就是照著別人的技能等級做決策。
                    conflicts.add(skill.id)

        if conflicts:
            log.error(
                "有 %d 個技能讀到互相矛盾的內容（例如 %s），分不出哪份是現在的角色，"
                "判定為定位失敗",
                len(conflicts), sorted(conflicts)[:5],
            )
            return None
        if not found:
            # 還沒進到遊戲裡就是讀不到，這不算錯誤（跟角色狀態一樣）。
            log.info("讀不到技能表 —— 通常是還沒進到遊戲裡，進去之後會自己接上")
            return None

        skills = sorted(found.values(), key=lambda s: s.id)
        log.info(
            "技能表定位成功：%d 個技能（已學會 %d 個），粗篩候選 %d 個",
            len(skills), sum(1 for s in skills if s.learned), scanned,
        )
        self._check_sp(skills)
        return skills

    def _verify(self, u32: np.ndarray, index: int, codes: dict[str, int]) -> Skill | None:
        """字串↔ID 交叉驗證。對不上就丟掉 —— 這是判別的主力。"""
        skill_id = int(u32[index + _I_ID])
        pointer = int(u32[index + _I_NAME_PTR])
        raw = self._scanner._read_bytes(pointer, _NAME_BYTES)
        if not raw:
            return None
        key = raw.split(b"\0", 1)[0].decode("ascii", "ignore")
        if not key or codes.get(key) != skill_id:
            return None
        return Skill(
            id=skill_id,
            key=key,
            name=skill_name(skill_id),
            level=int(u32[index + _I_LEVEL]),
            max_level=int(u32[index + _I_MAXLV]),
            sp=int(u32[index + _I_SP]),
        )

    @staticmethod
    def _check_sp(skills: list[Skill]) -> None:
        """拿 `skillinfolist.lub` 的 `SpAmount[level-1]` 再對一次消耗 SP。

        對不上**不排除**（主判別已經是字串↔ID 交叉驗證，夠強了），但要留下記錄：
        整批都對不上通常代表改版動了結構版面，那是該回頭看 GAMEDATA 的訊號。
        """
        table = skill_table()
        checked = bad = 0
        for skill in skills:
            costs = (table.get(skill.id) or {}).get("sp")
            if not costs or not 1 <= skill.level <= len(costs):
                continue
            checked += 1
            if costs[skill.level - 1] != skill.sp:
                bad += 1
        if checked and bad:
            log.warning(
                "有 %d/%d 個技能的 SP 跟 skillinfolist.lub 對不上 —— "
                "可能是改版動了結構版面，請重跑 tools/build_skill_table.py 並核對",
                bad, checked,
            )

    def close(self) -> None:
        if self._owns:
            self._scanner.close()
        self._pid = None
