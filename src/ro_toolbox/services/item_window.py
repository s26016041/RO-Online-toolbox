"""從記憶體讀「道具說明小視窗現在顯示的是哪個道具」。

使用者要的就是這個（2026-09-04）：「我右鍵會出現該物品說明小視窗，
肯定記憶體有東西說是哪個物品」、「不准用圖片辨識」、「讀記憶體」。
他還提示了關鍵那一句：「**文字也可以找找看，不一定是 ID**」——
編號和名字都找不到，**說明文找得到**。

## 原理（實機量出來的，見 GAMEDATA [DAT-070]）

視窗開起來時，客戶端把那個道具的說明文**拆成一行一行**複製到字串堆，
做成一個**間距 0x18 的行記錄陣列**，每筆的第一個欄位是那一行的文字指標：

    +0x00  「植物細長的梗,可以當做藥材,」   ← 說明文（會自動折行）
    +0x18  「可向收集商購買。」
    +0x30  「_」                          ← 空行
    +0x48  「重量 : ^7777771^000000」      ← 最後一行一定是重量

而說明文**每個道具獨一無二**，`assets/items.json.gz` 的 `desc` 就有 ——
所以拿記憶體裡那幾行接起來去比對，就得到道具編號。

## ⛔ 這幾條都試過了，全部落空（不要再走一次）

| 找什麼 | 結果 |
|---|---|
| 道具編號 int32 / uint16 / float32 / float64 | 「舊=955、新=757」**四種全 0 個** |
| 道具**名字**字串 | 出現次數與位址前後一模一樣，視窗沒複製名字 |
| 指向名字的指標 | 0 個 |
| 指向「編號附近」的指標 | 6 個，全是**音訊 PCM 波形**誤中 |
| 關掉視窗看誰消失 | 沒用 —— **被釋放的記憶體會留著舊值** |
| 從 Ragexe 靜態位址過來的 2 層指標鏈 | 關掉重開後**一條都不成立**（視窗物件每次重配） |

## 位址一個都沒寫死

錨點是**字串內容**與**結構偏移**（`_LINE_STRIDE`），不是位址：

- `重量 : ^` 帶顏色碼 —— 那是**渲染中的文字行**才有的形式。
- 行記錄間距 `0x18`：同一個結構內部的欄位距離，屬 CLAUDE.md 允許寫死的
  「結構偏移」（大更新才會壞），出處就是上面那張表。

⚠ **對不上就回空的**，不准挑一個最像的湊數 —— 加錯一樣道具的後果是
「從此再也不撿它」，而且完全不會有人發現。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import numpy as np

from ro_toolbox.services.gamedata import _ITEM_TABLE, _load
from ro_toolbox.services.memory_scan import MemoryScanner

log = logging.getLogger(__name__)

#: 行記錄的間距。結構偏移 —— 出處見模組說明的那張版面圖。
LINE_STRIDE = 0x18
#: 最後一行「重量」往前最多找幾行說明文。RO 的說明文沒有那麼長。
MAX_LINES = 16
#: 渲染中的文字行才有的形式（道具表裡是純文字 `重量 : 1`）。
WEIGHT_MARK = "重量 : ^"
#: 一行文字最多讀這麼多 byte。
_LINE_BYTES = 200
#: 顏色碼 `^RRGGBB`。比對前要拿掉。
_COLOUR = re.compile(r"\^[0-9a-fA-F]{6}")
#: 客戶端**每個道具**都預先建好一模一樣的行陣列（實測 22565 個錨、55970 個槽），
#: 所以光靠結構分不出「哪一筆是現在顯示的」。區別在**字串放在哪個區段**：
#: 道具表那幾個大區段裡有 300~677 個錨；視窗那筆在活的字串堆，錨稀稀落落。
#: 超過這個數就當成道具表，不是視窗。
MAX_TABLE_ANCHORS = 32
#: 找到之後，下一次先在這個範圍內找（視窗物件都從同一個池子配出來，
#: 實測兩次分別落在 0x182A6250 與 0x182A6798，差 0x548）。
#: 命中的話就不必再整份掃 —— 整份掃要十幾秒，這裡是毫秒。
HINT_SPAN = 0x40000


def _norm(text: str) -> str:
    """比對用的正規化：去顏色碼、去空白、去換行、去空行標記。"""
    return _COLOUR.sub("", text).replace(" ", "").replace("\n", "").replace("_", "")


def _body_of(desc: str) -> str:
    """道具說明文**扣掉最後那行重量**之後的內容（正規化過）。"""
    lines = [ln for ln in (desc or "").split("\n") if ln.strip() not in ("", "_")]
    while lines and lines[-1].replace(" ", "").startswith("重量"):
        lines.pop()
    return _norm("".join(lines))


def descriptions(item_ids) -> dict[str, list[int]]:
    """{正規化後的說明文: [道具編號…]}。只收有說明文的。

    ⚠ 好幾樣道具共用同一段說明文是常態（卡片、附魔石）—— 所以值是**清單**，
    分不出來的時候要回傳全部讓呼叫端問人，不准自己挑第一個。
    """
    table = _load(_ITEM_TABLE)
    out: dict[str, list[int]] = {}
    for item_id in item_ids:
        entry = table.get(str(int(item_id)))
        if not entry:
            continue
        body = _body_of(entry.get("desc") or "")
        if len(body) >= 6:          # 太短的比不出東西（「重量:0.1」會對到一堆）
            out.setdefault(body, []).append(int(item_id))
    return out


@dataclass(frozen=True, slots=True)
class Shown:
    """說明視窗現在顯示的東西。

    `items` 空的 = 認不出來（呼叫端要照實說，不准猜）；
    兩個以上 = 好幾樣共用同一段說明文，**要問人**。
    """

    items: tuple[int, ...] = ()
    #: 找到那筆記錄的位址。下一次拿它當提示，就不必整份掃。
    at: int = 0
    #: 讀到的說明文（診斷用，也可以顯示給使用者看）。
    text: str = ""
    why: str = ""
    seconds: float = 0.0
    ranked: tuple[str, ...] = field(default=())


class ItemWindowReader:
    """讀說明視窗顯示的道具。**唯讀**，不寫記憶體、不注入。"""

    def __init__(self, scanner: MemoryScanner) -> None:
        self._scan = scanner
        #: 上一次找到的位址。下一次先在附近找（見 `HINT_SPAN`）。
        self._hint = 0

    # ---- 讀字 -------------------------------------------------------

    def _text(self, addr: int) -> str:
        if not 0x10000 < addr < 0xF0000000:
            return ""
        raw = self._scan.read_region(addr, _LINE_BYTES)
        if raw is None:
            return ""
        try:
            return bytes(raw).split(b"\x00")[0].decode("cp950", errors="replace")
        except Exception:           # noqa: BLE001 - 讀到亂碼不該讓功能掛掉
            return ""

    def _dword(self, addr: int) -> int | None:
        raw = self._scan.read_region(addr, 4)
        if raw is None or len(raw) < 4:
            return None
        return int(np.frombuffer(bytes(raw), dtype="<u4", count=1)[0])

    def _bodies(self, slot: int) -> list[str]:
        """從「重量」那一筆往前，一行一行接出**所有可能的說明文**（正規化過）。

        ⚠ **不能碰到空行就停。** 說明文與「重量」之間夾著一行空的（`_`）——
        實機踩過：往回走第一步就讀到空字串，直接 break，視窗那筆整個被跳過，
        結果認成道具表裡的另一樣（很有自信的錯）。

        ⚠ 也**不能只試一種長度**：說明文幾行不固定（會依視窗寬度自動折行），
        所以每一種長度都拼一份出來讓呼叫端去比對。
        """
        parts: list[str] = []
        out: list[str] = []
        for step in range(1, MAX_LINES + 1):
            ptr = self._dword(slot - LINE_STRIDE * step)
            if not ptr:
                break
            parts.append(self._text(ptr))
            body = _norm("".join(reversed(parts)))
            if len(body) >= 6:
                out.append(body)
        return out

    # ---- 找記錄 -----------------------------------------------------

    def _weight_slots(self, regions, slot_regions=None) -> tuple[list[int], dict]:
        """找出所有「指著一個 `重量 : ^…` 字串」的欄位。

        回 `(欄位清單, {區段起點: 那個區段有幾個錨})` —— 密度是用來把
        **道具表**跟**視窗**分開的唯一線索（見 `MAX_TABLE_ANCHORS`）。
        """
        needle = WEIGHT_MARK.encode("cp950")
        anchors: list[int] = []
        density: dict[int, int] = {}
        for base, size in regions:
            raw = self._scan.read_region(base, size)
            if raw is None:
                continue
            buf = bytes(raw)
            i = buf.find(needle)
            count = 0
            while i >= 0:
                anchors.append(base + i)
                count += 1
                i = buf.find(needle, i + 1)
            if count:
                density[base] = count
        if not anchors:
            return [], {}
        # ⚠ 錨點字串（`重量 : ^…`）跟**指著它的欄位**通常不在同一個區段 ——
        #   所以錨要整份找，只有「找欄位」這一步可以縮小範圍（那也是最貴的一步）。
        if slot_regions is None:
            slot_regions = regions
        table = np.array(sorted(set(anchors)), dtype="<u4")
        slots: list[int] = []
        for base, size in slot_regions:
            raw = self._scan.read_region(base, size)
            if raw is None:
                continue
            count = len(raw) // 4
            if not count:
                continue
            arr = np.frombuffer(raw, dtype="<u4", count=count)
            # ⚠ 用 searchsorted 不用 `np.isin`：兩萬個錨對一億個 dword，
            #   `isin` 實測要 26 秒，這裡是幾秒。
            pos = np.searchsorted(table, arr)
            pos[pos >= len(table)] = len(table) - 1
            hit = np.nonzero(table[pos] == arr)[0]
            slots.extend(base + int(i) * 4 for i in hit)
        return slots, density

    def read(self, candidates, hint: bool = True) -> Shown:
        """說明視窗現在顯示的是 `candidates`（道具編號）裡的哪一樣。

        `candidates` 應該是**背包裡真的有的東西** —— 拿整份兩萬筆去比不只慢，
        還會把機會給一堆身上根本沒有的道具。
        """
        started = time.monotonic()
        wanted = descriptions(candidates)
        if not wanted:
            return Shown(why="讀不到背包，沒有可以比對的說明文")

        regions = self._scan.regions(writable_only=True)
        if hint and self._hint:
            # ① 最快：上次那個位址還是不是同一筆記錄（同一個道具再看一次、
            #    或視窗物件配回同一格）。只要讀十幾個 dword，是毫秒等級。
            if self._text(self._dword(self._hint) or 0).startswith(WEIGHT_MARK[:4]):
                got = self._match([self._hint], None, wanted, regions, started)
                if got.items:
                    return got
            # ② 次快：錨照樣整份找（那步只要幾秒），但**只在上次那一帶找欄位** ——
            #    視窗物件都從同一個池子配出來（實測兩次差 0x548）。
            near = [
                (b, size) for b, size in regions
                if b < self._hint + HINT_SPAN and b + size > self._hint - HINT_SPAN
            ]
            got = self._match(
                *self._weight_slots(regions, near), wanted, regions, started)
            if got.items:
                self._hint = got.at
                return got

        got = self._match(*self._weight_slots(regions), wanted, regions, started)
        if got.items:
            self._hint = got.at
        return got

    def _region_of(self, addr: int, regions) -> int:
        for base, size in regions:
            if base <= addr < base + size:
                return base
        return 0

    def _match(self, slots, density, wanted, regions, started: float) -> Shown:
        """挑出**視窗**那一筆。

        ⚠ 命中好幾筆是常態（客戶端每個道具都預先建了一模一樣的行陣列）——
        區別是**字串放在哪個區段**：道具表那幾個大區段一個就有 300~677 個錨，
        視窗那筆在活的字串堆，稀稀落落。實機踩過：不分辨就挑到表裡的另一樣
        道具，而且完全不會有人發現（安靜地把錯的東西加進黑名單）。
        """
        best: tuple[int, int, tuple[int, ...], str] | None = None
        seen: list[str] = []
        for slot in slots:
            bodies = self._bodies(slot)
            if not bodies:
                continue
            found = next((wanted[b] for b in bodies if b in wanted), None)
            if not found:
                seen.append(bodies[-1][:30])
                continue
            # density is None = 這一筆是上次確認過的位址，不必再驗區段密度
            # （不給豁免的話 `.get()` 會回預設的「超大」，把它當成道具表擋掉 ——
            #   實機踩過：最快的那條路永遠走不到，每次都退回慢的）。
            anchor = self._dword(slot) or 0
            count = (0 if density is None
                     else density.get(self._region_of(anchor, regions), 1 << 30))
            body = next(b for b in bodies if b in wanted)
            if best is None or count < best[0]:
                best = (count, slot, tuple(found), body)
        if best is None:
            return Shown(why="認不出說明視窗顯示的是什麼（視窗開著嗎？）",
                         seconds=time.monotonic() - started,
                         ranked=tuple(seen[:5]))
        count, slot, items, body = best
        if count > MAX_TABLE_ANCHORS:
            # 全部命中都落在道具表那幾個大區段 —— 那就是**沒有視窗開著**。
            # 這時候硬挑一個就是安靜地做錯事，寧可回「認不出來」。
            return Shown(why="只找到道具表的資料，沒看到開著的說明視窗",
                         seconds=time.monotonic() - started,
                         ranked=tuple(seen[:5]))
        return Shown(items=items, at=slot, text=body,
                     seconds=time.monotonic() - started)


# ---- 什麼時候去讀 -----------------------------------------------------------


def wait_for_right_click(should_stop=None, poll: float = 0.025) -> bool:
    """等一次滑鼠**右鍵的按下緣**。回 False = 被叫停。

    ⚠ 只問我們自己的輸入狀態（`GetAsyncKeyState`），**完全不碰遊戲行程** ——
    要攔遊戲自己的滑鼠事件得 hook 它的 UI，那是注入，GameGuard 會當機／封號。
    同一招 `auto_login` 學「同意」按鈕已經在用。

    ⚠ 只認右鍵（使用者指定）：左鍵在背包裡是拿起／拖曳，等的期間順手一點
    就會誤加，而黑名單沒有開關 —— 錯加的那一樣會安靜地一直生效。
    """
    import ctypes
    import time as _time

    user32 = ctypes.windll.user32
    user32.GetAsyncKeyState.restype = ctypes.c_short

    def down() -> bool:
        return bool(user32.GetAsyncKeyState(0x02) & 0x8000)   # VK_RBUTTON

    was = down()
    while True:
        if should_stop is not None and should_stop():
            return False
        now = down()
        if now and not was:
            return True
        was = now
        _time.sleep(poll)
