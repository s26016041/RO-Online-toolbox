"""跟 NPC 對話：組封包、解選單、**用文字比對挑選項**。

只送封包、不碰記憶體（CLAUDE.md：RO 掛 GameGuard，寫記憶體會被反制）。

## 整條流程（實測擷取，`封包/跟船員說話傳送到柏伊亞嵐島.txt`，2026-08-27）

    ↑ 0x0090  接觸 NPC     GID(4) + 型別(1)=1
    ↓ 0x00B4  對話文字     長度(2) + GID(4) + cp950 文字
    ↓ 0x00B5  等待輸入     GID(4)                    ← 畫面出現「下一步」
    ↑ 0x00B9  按下一步     GID(4)
    ↓ 0x00B7  選單         長度(2) + GID(4) + cp950 文字，選項用 `:` 分隔
    ↑ 0x00B8  選擇         GID(4) + 選項編號(1)      ← **從 1 開始**

實際內容（船員 GID=91）：

    [船員]
    有艘以超高速航行的船早已準備好隨時出發了，不過它不能保證大家的安全就是了!來吧!我們走!
    選單：'柏伊亞嵐島 -> 150 金幣' : '艾爾貝塔 港口-> 500金幣' : '結束' : ''

## ⛔ 絕對不准猜選項編號

選單內容是**伺服器端腳本**產生的，解包資料裡沒有（[DAT-027] 全部翻過）。
猜錯的代價是把人傳到別的島、或花掉玩家的錢 —— 正是規範說的「很有自信的錯」。

**唯一允許的做法：拿目的地的中文名去比對選項文字**，而且

- 比對前把空白全部去掉（選單寫「艾爾貝塔 港口」，我們的表寫「港都 艾爾貝塔」）；
- 我們的地圖名常有前綴（`港都 艾爾貝塔`、`衛星都市 依斯魯得島`），
  取**最後一段空白分隔的主名**來比（`艾爾貝塔`、`依斯魯得島`）；
- **剛好一個選項對得上才動手**。0 個或 2 個以上一律大聲停 —— 分不出來就不賭。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: 送出：接觸 NPC。payload = GID(4) + 型別(1)
CZ_CONTACTNPC = 0x0090
#: 送出：按「下一步」。payload = GID(4)
CZ_REQ_NEXT_SCRIPT = 0x00B9
#: 送出：選單選了第幾項（**從 1 開始**）。payload = GID(4) + 編號(1)
CZ_CHOOSE_MENU = 0x00B8
#: 送出：關閉對話。payload = GID(4)
CZ_CLOSE_DIALOG = 0x0146

#: 接收：對話文字
ZC_SAY_DIALOG = 0x00B4
#: 接收：等待「下一步」
ZC_WAIT_DIALOG = 0x00B5
#: 接收：選單
ZC_MENU_LIST = 0x00B7
#: 接收：關閉對話
ZC_CLOSE_DIALOG = 0x00B6

#: 伺服器送來的文字編碼。台服是 cp950（實測擷取確認）。
TEXT_ENCODING = "cp950"

#: 接觸 NPC 的型別。實測擷取就是 1。
_CONTACT_TYPE = 1

#: 一次對話最多回答幾層選單。
#:
#: 船員是**一層**（實測）。卡普拉那種「先選傳送服務、再選城市」是兩層以上，
#: 有的還會再問一次「確定嗎」。設上限是怕選單繞圈圈時無限點下去 ——
#: 超過就停手，不要一直亂點別人的 NPC。
MAX_MENUS = 4


def build_contact(gid: int) -> bytes:
    return (
        CZ_CONTACTNPC.to_bytes(2, "little")
        + gid.to_bytes(4, "little")
        + bytes([_CONTACT_TYPE])
    )


def build_next(gid: int) -> bytes:
    return CZ_REQ_NEXT_SCRIPT.to_bytes(2, "little") + gid.to_bytes(4, "little")


def build_choose(gid: int, choice: int) -> bytes:
    """選單第 `choice` 項（**從 1 開始**）。"""
    if not 1 <= choice <= 254:
        raise ValueError(f"選項編號要在 1~254，收到 {choice}")
    return (
        CZ_CHOOSE_MENU.to_bytes(2, "little")
        + gid.to_bytes(4, "little")
        + bytes([choice])
    )


def build_close(gid: int) -> bytes:
    return CZ_CLOSE_DIALOG.to_bytes(2, "little") + gid.to_bytes(4, "little")


def _text(payload: bytes, start: int) -> str:
    """把 payload 從 `start` 起的 cp950 C 字串解出來。"""
    return payload[start:].split(b"\x00")[0].decode(TEXT_ENCODING, "replace")


def parse_menu(payload: bytes) -> tuple[int, list[str]] | None:
    """解 `0x00B7`。回 (NPC GID, 選項清單)。版面不對回 None。

    ⚠ 選項用 `:` 分隔，而且**結尾通常多一個空的**（字串以 `:` 收尾）——
    那個不是選項，算進去會讓編號整個錯掉。
    """
    if len(payload) < 7:
        return None
    gid = int.from_bytes(payload[2:6], "little")
    options = [o.strip() for o in _text(payload, 6).split(":")]
    while options and not options[-1]:
        options.pop()
    if not gid or not options:
        return None
    return gid, options


def parse_say(payload: bytes) -> tuple[int, str] | None:
    """解 `0x00B4`（對話文字）。回 (NPC GID, 文字)。"""
    if len(payload) < 6:
        return None
    return int.from_bytes(payload[2:6], "little"), _text(payload, 6)


def parse_wait(payload: bytes) -> int | None:
    """解 `0x00B5`（等「下一步」）。回 NPC GID。"""
    if len(payload) < 4:
        return None
    return int.from_bytes(payload[0:4], "little") or None


def core_name(display: str) -> str:
    """地圖中文名的**主名**：去掉前綴、去掉所有空白。

    `港都 艾爾貝塔` → `艾爾貝塔`、`衛星都市 依斯魯得島` → `依斯魯得島`。
    沒有前綴的就是它自己（`柏伊亞嵐島`）。
    """
    parts = [p for p in display.replace("　", " ").split(" ") if p]
    return parts[-1] if parts else ""


def _squash(text: str) -> str:
    return text.replace("　", "").replace(" ", "")


def pick_option(options: list[str], display_name: str) -> tuple[int | None, str]:
    """挑出通往 `display_name` 的選項。回 (編號從 1 開始, 說明)。

    **剛好一個對得上才回編號**；0 個或 2 個以上回 None —— 分不出來就不賭
    （猜錯是把人傳到別的島或花掉他的錢）。
    """
    core = _squash(core_name(display_name))
    if not core:
        return None, "沒有可比對的地圖中文名"
    hits = [i for i, opt in enumerate(options, start=1) if core in _squash(opt)]
    if len(hits) == 1:
        return hits[0], f"第 {hits[0]} 項「{options[hits[0] - 1]}」對上「{core}」"
    if not hits:
        return None, f"選單裡沒有「{core}」：{options}"
    return None, f"「{core}」對到 {len(hits)} 個選項，分不出來：{options}"


def cost_of(option: str) -> str:
    """選項裡寫的代價（`150 金幣`）。看不出來回空字串 —— 只是拿來提醒人。"""
    import re

    found = re.search(r"(\d[\d,]*)\s*(金幣|z|zeny)", option, re.IGNORECASE)
    return found.group(0) if found else ""


class NpcTalk:
    """跟一個 NPC 走完一次「選目的地」的對話。

    **純狀態機**：擷取執行緒把收到的封包餵進 `feed()`，主迴圈呼叫
    `next_packet()` 拿要送出去的東西。自己不碰 socket、不碰記憶體，
    所以整條邏輯測得起來（測資就是實機擷取的位元組）。

    ⚠ **「過去了」不由這裡判定。** 這裡最多做到「選單選了第幾項送出去」；
    真的到了沒有，由呼叫端看**地圖名有沒有變**（[DAT-026]）。
    這支只負責把對話走完，或**大聲說走不完**。
    """

    #: 送出之後多久沒有任何回應就放棄。只是放棄的上限，不是成功的依據。
    TIMEOUT = 15.0

    def __init__(self, gid: int, want: str, now=None) -> None:
        import time as _time

        self._gid = gid
        self._want = want
        self._now = now or _time.monotonic
        self._queue: list[bytes] = [build_contact(gid)]
        self._since = self._now()
        self._menus = 0            # 回答過幾層選單
        self.done = False
        self.failed = False
        self.note = f"跟 NPC #{gid} 對話中…"
        self.cost = ""

    # ---- 擷取執行緒 -------------------------------------------------

    def feed(self, opcode: int, payload: bytes) -> None:
        # ⚠ 選完**不停止監聽**：卡普拉那種是多層選單（先「傳送服務」再選城市），
        # 有的還會再問一次「確定嗎」。選完就關耳朵的話第二層永遠等不到。
        # 真的過去了沒有，一律看**地圖名有沒有變**，由呼叫端判定（[DAT-026]）。
        if self.failed:
            return
        if opcode == ZC_WAIT_DIALOG and parse_wait(payload) == self._gid:
            self._push(build_next(self._gid))
            return
        if opcode == ZC_SAY_DIALOG:
            got = parse_say(payload)
            if got and got[0] == self._gid:
                self._since = self._now()   # 有回應就重新計時
            return
        if opcode == ZC_MENU_LIST:
            got = parse_menu(payload)
            if got is None or got[0] != self._gid:
                return
            self._menus += 1
            if self._menus > MAX_MENUS:
                self.failed = True
                self.note = f"⚠ 選單超過 {MAX_MENUS} 層，這不像單純的傳送，停手"
                log.warning("%s", self.note)
                return
            self._on_menu(got[1])

    def _on_menu(self, options: list[str]) -> None:
        index, why = pick_option(options, self._want)
        if index is None:
            # ⛔ 分不出來就**不准賭**：猜錯是把人傳到別的島、或花掉他的錢。
            self.failed = True
            self.note = f"⚠ 看不懂 NPC 的選單，沒有動作：{why}"
            log.warning("%s", self.note)
            return
        self.cost = cost_of(options[index - 1])
        money = f"（要付 {self.cost}）" if self.cost else ""
        self.note = f"選了{why}{money}"
        log.info("%s", self.note)
        self._push(build_choose(self._gid, index))
        self.done = True        # 該送的都送了，剩下等地圖變

    def _push(self, data: bytes) -> None:
        self._queue.append(data)
        self._since = self._now()

    # ---- 主迴圈 -----------------------------------------------------

    def next_packet(self) -> bytes | None:
        """要送出去的下一個封包。沒有就回 None。"""
        if self._queue:
            return self._queue.pop(0)
        if self.failed:
            return None
        if self._now() - self._since > self.TIMEOUT and not self.done:
            self.failed = True
            self.note = f"⚠ 跟 NPC #{self._gid} 對話 {self.TIMEOUT:.0f} 秒沒有回應，放棄"
            log.warning("%s", self.note)
        return None
