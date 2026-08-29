"""隊友是誰、他們身上有什麼狀態 —— 全部從封包學，不猜。

## 隊友怎麼認出來

**`0x0107`（隊員位置，10 bytes：`AID(4) + x(2) + y(2)`）**。伺服器只把它送給
同一隊的人，所以**收到誰的 `0x0107`，誰就是隊友**——這是當場學到的事實，
不是從某張表推出來的。

實測（2026-08-29，白狐 ＋ 狐狐狸同隊，換一次圖）：

    0x0107  1c907c01 3f00 5b00   → AID 24940572（白狐）在 (63, 91)
    0x0107  0b516b01 3b00 6600   → AID 23810315（狐狐狸）在 (59, 102)

⚠ **完整的隊伍清單（`0x00FB`）抓不到**：它只在加入隊伍／進圖那一刻送，
中途開擷取就永遠等不到。所以不靠它 —— 反正放 buff 只需要「看得到的隊友」，
看不到的本來也放不了。

名字用**查詢**補：送 `0x0368`（CZ_REQNAME，`目標ID(4)`）→ 伺服器回
`0x0095`（`AID(4) + 名字[24]`，長度表 30）。查不到就顯示 AID ——
⚠ **不從實體封包裡挖名字**：那一包很長而且欄位位置沒實測過，
猜一個偏移出來顯示亂碼比顯示 AID 更糟（CLAUDE.md：不確定一律留空）。

## 狀態怎麼追

同一組狀態封包**帶著 AID**，所以隊友的變化看得到：

    0x0983 (29)  EFST(2) + AID(4) + state(1) + total(4) + remain(4) + val1~3
    0x0196 (9)   EFST(2) + AID(4) + state(1)          ← 沒有時間，只有上／下

`state == 1` 是上身、`0` 是消失。剩餘時間**自己倒數**（記下收到的時刻 ＋ total）。

⚠ 這裡跟自己的狀態不一樣：自己那份是**記憶體裡的完整清單**（隨時問隨時準，
見 `services/status_effects.py`），隊友只有「開始擷取之後看到的變化」。
所以「查不到 = 他沒有」是**不成立**的推論 —— 呼叫端要自己決定怎麼處理
（`buffs` 的做法是：不知道就放一次，放完就知道了）。
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: 隊員位置。**收到誰的就代表誰是隊友。**
OP_PARTY_MOVE = 0x0107
#: 狀態變更（帶時間）。
OP_STATE_TIMED = 0x0983
#: 狀態變更（只有上／下）。
OP_STATE_PLAIN = 0x0196
#: 查名字的回應：`AID(4) + 名字[24]`（長度表 30）。查詢是 `0x0368`。
OP_NAME_ACK = 0x0095
_NAME_BYTES = 24

#: 多久沒再看到這個隊友就當他不在了（換圖、離線、走遠）。
FORGET_AFTER = 120.0
#: 狀態總時長超過這個就當「無時限」（跟 status_effects 同一個判準）。
_NO_TIME_LIMIT = 9999


@dataclass
class Mate:
    """一個看得到的隊友。"""

    aid: int
    name: str = ""
    cell: tuple[int, int] = (0, 0)
    seen_at: float = 0.0
    #: EFST → (到期時刻, 總時長毫秒 or None)。`None` = 無時限。
    buffs: dict[int, tuple[float, int | None]] = field(default_factory=dict)

    def label(self) -> str:
        return self.name or f"#{self.aid}"

    def remaining_ms(self, efst: int, now: float) -> int | None:
        """這個狀態還剩多久。**沒看過就回 None**（不是 0 —— 那是兩件事）。"""
        row = self.buffs.get(efst)
        if row is None:
            return None
        expire, total = row
        if total is None:
            return None                 # 無時限
        return max(0, int((expire - now) * 1000))

    def has(self, efst: int, now: float) -> bool:
        """看過而且還沒過期。"""
        row = self.buffs.get(efst)
        if row is None:
            return False
        expire, total = row
        return total is None or expire > now


class PartyWatch:
    """餵封包進來，問得出「隊友是誰、身上有什麼」。

    不開執行緒、不碰記憶體 —— 呼叫端把封包 `feed()` 進來就好。
    """

    def __init__(self, me: int, now=time.monotonic) -> None:
        #: 自己的 AID。自己不算隊友（自己的狀態走記憶體那條，比較準）。
        self._me = me
        self._now = now
        self._mates: dict[int, Mate] = {}

    # ---- 進來 -------------------------------------------------------

    def feed(self, opcode: int, payload: bytes) -> None:
        """收一個封包。**認不懂的一律忽略**。"""
        if opcode == OP_PARTY_MOVE and len(payload) >= 8:
            aid, x, y = struct.unpack_from("<IHH", payload, 0)
            if aid and aid != self._me:
                mate = self._mates.setdefault(aid, Mate(aid=aid))
                mate.cell = (x, y)
                mate.seen_at = self._now()
            return
        if opcode == OP_STATE_TIMED and len(payload) >= 15:
            efst, aid, state, total, remain = struct.unpack_from("<HIBII", payload, 0)
            self._note_state(aid, efst, state, total, remain)
            return
        if opcode == OP_STATE_PLAIN and len(payload) >= 7:
            efst, aid, state = struct.unpack_from("<HIB", payload, 0)
            # 沒有時間 —— 上身就記成「無時限」（總比不知道好），下身就刪掉。
            self._note_state(aid, efst, state, _NO_TIME_LIMIT, _NO_TIME_LIMIT)
            return
        if opcode == OP_NAME_ACK and len(payload) >= 4 + _NAME_BYTES:
            aid = int.from_bytes(payload[0:4], "little")
            mate = self._mates.get(aid)
            if mate is not None and not mate.name:
                raw = payload[4:4 + _NAME_BYTES].split(b"\0", 1)[0]
                mate.name = raw.decode("cp950", "ignore").strip()

    def _note_state(self, aid: int, efst: int, state: int,
                    total: int, remain: int) -> None:
        """只記**隊友**的狀態。自己的走記憶體那條（完整且隨時準）。"""
        if aid == self._me:
            return
        mate = self._mates.get(aid)
        if mate is None:
            return          # 還沒確認是隊友（沒收過他的 0x0107）就不記
        if not state:
            mate.buffs.pop(efst, None)
            return
        if total == _NO_TIME_LIMIT or total <= 0 or total > 24 * 60 * 60 * 1000:
            mate.buffs[efst] = (0.0, None)          # 無時限
            return
        left = remain if 0 < remain <= total else total
        mate.buffs[efst] = (self._now() + left / 1000.0, total)

    # ---- 出去 -------------------------------------------------------

    def unnamed(self) -> list[int]:
        """還不知道名字的隊友 AID。呼叫端拿去送 `0x0368` 查。"""
        return [m.aid for m in self._mates.values() if not m.name]

    def mates(self) -> list[Mate]:
        """現在看得到的隊友（太久沒消息的就忘掉）。"""
        now = self._now()
        for aid, mate in list(self._mates.items()):
            if now - mate.seen_at > FORGET_AFTER:
                del self._mates[aid]
        return sorted(self._mates.values(), key=lambda m: m.aid)

    def needs(self, mate: Mate, efst: int, below_ratio: float) -> bool:
        """這個隊友需要補這個 buff 嗎（沒有，或剩不到總時長的 `below_ratio`）。

        ⚠ **「沒看過」也算需要**：隊友的狀態只看得到「開始擷取之後的變化」，
        查不到不等於他沒有。放一次是便宜的（多放一次 buff 而已），
        而放完就會收到 `0x0983`，之後就知道了。
        """
        row = mate.buffs.get(efst)
        if row is None:
            return True
        expire, total = row
        if total is None:
            return False                 # 無時限，不用補
        left = (expire - self._now()) * 1000.0
        return left < total * below_ratio
