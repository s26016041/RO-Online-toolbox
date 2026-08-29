"""寄信（RODEX）—— 封包版面與一次寄信的完整流程。

版面全部來自**實機擷取**（`封包/寄信10個野豬毛給商狐.txt`，2026-08-30，
白狐寄 10 個野豬毛給商狐），見 GAMEDATA **[PKT-087]**。

## 一次寄信是五步，每一步都有回應可以確認

    ① ↑ 0x0A08  24 個 0                    開寫信視窗
      ↓ 0x0A12  …最後一個 byte 01           開好了
    ② ↑ 0x0A13  收件人名字[24]              查這個人在不在
      ↓ 0x0A51  角色ID(4) 職業(2) 等級(2) 名字[24]
    ③ ↑ 0x0A04  格號(2) 數量(2)             附加道具
      ↓ 0x0B3F  結果(1)=0 格號(2) 數量(2)   附好了
    ④ ↑ 0x0A6E  （見下）                    送出
      ↓ 0x09ED  結果(1)=0                   寄出去了
    ⑤ ↑ 0x0A03  （沒有內容）                關掉視窗

## `0x0A6E` 的版面

    長度(2)
    收件人名字[24]
    寄件人名字[24]
    zeny(8)
    標題長度(2)     ← 含結尾的 \\0
    內文長度(2)     ← 含結尾的 \\0
    收件人角色ID(4) ← **從 ② 的 0x0A51 回應拿**，不是自己算的
    標題[標題長度]
    內文[內文長度]

⚠⚠ **那個角色 ID 是這個版面跟標準版唯一的差別**，也是最容易漏掉的一欄。
兩件事互相印證：實機那 4 個 byte（`21 3b c7 42`）跟 `0x0A51` 回應的前 4 個
byte 一模一樣；而客戶端長度表說 `0x0A6E` 的表頭是 **68**，標準版算出來是 64
（2+2+24+24+8+2+2），剛好差這 4 個。

⚠ **名字用 cp950**（這是繁體客戶端）。24 bytes 不足補 0；實機那一包名字後面
是沒清乾淨的堆疊垃圾，所以**收件人比對只能看到第一個 \\0 為止**。

⛔ **不准自己算收件人的角色 ID**。一定要送 `0x0A13` 問、等 `0x0A51` 回 ——
猜一個號碼寄出去，東西就寄給別人了（而且拿不回來）。
"""

from __future__ import annotations

import logging
import struct
import time

log = logging.getLogger(__name__)

#: ↑ 開寫信視窗。24 bytes 全 0（實機就是這樣）。
OP_OPEN = 0x0A08
#: ↓ 開好了。最後一個 byte 是 1 代表可以寫。
OP_OPENED = 0x0A12
#: ↑ 查收件人在不在：名字[24]。
OP_CHECK_NAME = 0x0A13
#: ↓ 查名字的回應：角色ID(4) + 職業(2) + 等級(2) + 名字[24]。
OP_NAME_INFO = 0x0A51
#: ↑ 附加道具：格號(2) + 數量(2)。
OP_ADD_ITEM = 0x0A04
#: ↓ 附加的結果：結果(1) + 格號(2) + 數量(2) + …（結果 0 = 成功）。
OP_ADD_RESULT = 0x0B3F
#: ↑ 送出。
OP_SEND = 0x0A6E
#: ↓ 送出的結果：1 byte，0 = 成功。
OP_SEND_RESULT = 0x09ED
#: ↑ 關掉寫信視窗。沒有內容。
OP_CLOSE = 0x0A03

_NAME_BYTES = 24
#: 名字與標題內文的編碼。繁體客戶端是 cp950（[PKT-012] 同一套）。
ENCODING = "cp950"


def _fixed_name(name: str) -> bytes:
    """名字欄位：cp950、24 bytes、不足補 0。"""
    raw = name.encode(ENCODING, "ignore")[:_NAME_BYTES]
    return raw.ljust(_NAME_BYTES, b"\0")


def read_name(raw: bytes) -> str:
    """從封包裡讀名字。**只讀到第一個 \\0** —— 後面是沒清乾淨的堆疊垃圾。"""
    return raw.split(b"\0", 1)[0].decode(ENCODING, "ignore").strip()


def build_open() -> bytes:
    """① 開寫信視窗。"""
    return OP_OPEN.to_bytes(2, "little") + bytes(_NAME_BYTES)


def build_check_name(name: str) -> bytes:
    """② 問伺服器「這個人在不在」。回應是 `0x0A51`（帶角色 ID）。"""
    return OP_CHECK_NAME.to_bytes(2, "little") + _fixed_name(name)


def parse_name_info(payload: bytes):
    """拆 `0x0A51`。回 `(角色ID, 職業, 等級, 名字)`；長度不對回 None。

    ⚠ **角色 ID 是寄信唯一的用途** —— 送出那一包要帶它（見檔頭）。
    """
    if len(payload) < 8 + _NAME_BYTES:
        return None
    char_id, job, level = struct.unpack_from("<IHH", payload, 0)
    name = read_name(payload[8:8 + _NAME_BYTES])
    return char_id, job, level, name


def build_add_item(index: int, amount: int) -> bytes:
    """③ 附加道具。`index` 是**背包格號**（跟喝水那包同一套編號，[PKT-036]）。"""
    return OP_ADD_ITEM.to_bytes(2, "little") + struct.pack("<HH", index, amount)


def parse_add_result(payload: bytes):
    """拆 `0x0B3F`。回 `(成功嗎, 格號, 數量)`；長度不對回 None。"""
    if len(payload) < 5:
        return None
    result, index, amount = struct.unpack_from("<BHH", payload, 0)
    return result == 0, index, amount


def build_send(
    receiver: str,
    sender: str,
    receiver_char_id: int,
    title: str = "",
    text: str = "",
    zeny: int = 0,
) -> bytes:
    """④ 送出。版面見檔頭。

    ⚠ 標題與內文的長度**含結尾的 `\\0`**（實機：標題 "TITLE" 長度寫 6、
    空內文長度寫 1）。所以空字串也要送一個 `\\0`，長度 1。
    """
    title_raw = title.encode(ENCODING, "ignore") + b"\0"
    text_raw = text.encode(ENCODING, "ignore") + b"\0"
    body = (
        _fixed_name(receiver)
        + _fixed_name(sender)
        + struct.pack("<q", zeny)
        + struct.pack("<HH", len(title_raw), len(text_raw))
        + struct.pack("<I", receiver_char_id)
        + title_raw
        + text_raw
    )
    total = 2 + 2 + len(body)
    return OP_SEND.to_bytes(2, "little") + total.to_bytes(2, "little") + body


def build_close() -> bytes:
    """⑤ 關掉寫信視窗。

    ⚠ 跟商店一樣：**RO 的每一個對話框都要自己關掉**，不關就卡在那裡
    （[PKT-074] 那條「買完沒關商店，角色動不了」是同一類問題）。
    """
    return OP_CLOSE.to_bytes(2, "little")


class MailRun:
    """一次寄信。餵封包進來，每一步都等**讀得到的回應**才往下走。

    不自己開連線、不自己讀背包 —— 送封包與現查格號都由呼叫端注入
    （跟 `BuffKeeper` 同一個形狀）。

    ⚠ **不准用「等幾秒」當作成功的依據**（CLAUDE.md）：每一步都有回應，
    逾時只是放棄的上限。
    """

    #: 每一步最多等多久。
    STEP_TIMEOUT = 5.0

    def __init__(self, send, sender: str, receiver: str, now=time.monotonic) -> None:
        self._send = send
        self._sender = sender
        self._receiver = receiver
        self._now = now
        self.step = "idle"
        self.note = ""
        self.done = False
        self.failed = False
        #: 收件人的角色 ID（`0x0A51` 給的）。**沒有它不准送出。**
        self.receiver_char_id: int | None = None
        self._opened = False
        self._attached: tuple[int, int] | None = None
        self._sent_ok: bool | None = None
        self._since = 0.0

    # ---- 收封包 -----------------------------------------------------

    def feed(self, opcode: int, payload: bytes) -> None:
        if opcode == OP_OPENED:
            self._opened = bool(payload and payload[-1])
        elif opcode == OP_NAME_INFO:
            info = parse_name_info(payload)
            if info is None:
                return
            char_id, _job, _level, name = info
            # ⚠ 名字要對得上才收 —— 同一拍可能有別人的查詢回應飛過來。
            if name == self._receiver:
                self.receiver_char_id = char_id
        elif opcode == OP_ADD_RESULT:
            parsed = parse_add_result(payload)
            if parsed is not None:
                ok, index, amount = parsed
                self._attached = (index, amount) if ok else None
        elif opcode == OP_SEND_RESULT:
            self._sent_ok = bool(payload) and payload[0] == 0

    # ---- 跑一趟 -----------------------------------------------------

    def run(self, index: int, amount: int, should_stop=None, wait=None) -> bool:
        """把一封信寄完。回 True = 真的寄出去了。

        `index` 是**現查**的背包格號（[MEM-028]：存編號、格號現查），
        呼叫端在叫這一支之前才去查 —— 不能用上一拍記下來的。

        每一步都等回應；等不到就大聲失敗並把視窗關掉。
        **失敗的路徑也要關窗**（[PKT-074] 的教訓）。
        """
        stop = should_stop or (lambda: False)
        pause = wait or (lambda seconds: time.sleep(seconds))

        def hold(check, what: str) -> bool:
            deadline = self._now() + self.STEP_TIMEOUT
            while self._now() < deadline:
                if stop():
                    return False
                if check():
                    return True
                pause(0.1)
            self._fail(f"⚠ 寄信卡在「{what}」—— 伺服器沒有回應")
            return False

        try:
            self.step = "open"
            if not self._send(build_open()):
                return self._fail("⚠ 寄信送不出去（連線斷了？）")
            if not hold(lambda: self._opened, "開寫信視窗"):
                return False

            self.step = "check"
            if not self._send(build_check_name(self._receiver)):
                return self._fail("⚠ 寄信送不出去（連線斷了？）")
            if not hold(lambda: self.receiver_char_id is not None,
                        f"查收件人「{self._receiver}」"):
                # 查不到多半是**名字打錯**或那個角色不在這個伺服器。
                self.note = f"⚠ 找不到收件人「{self._receiver}」，沒有寄出"
                return False

            self.step = "attach"
            if not self._send(build_add_item(index, amount)):
                return self._fail("⚠ 寄信送不出去（連線斷了？）")
            if not hold(lambda: self._attached is not None, "附加道具"):
                return False

            self.step = "send"
            data = build_send(
                self._receiver, self._sender, self.receiver_char_id or 0,
                title=self.title, text=self.text,
            )
            if not self._send(data):
                return self._fail("⚠ 寄信送不出去（連線斷了？）")
            if not hold(lambda: self._sent_ok is not None, "送出"):
                return False
            if not self._sent_ok:
                return self._fail("⚠ 伺服器拒絕了這封信（信箱滿了？重量超過？）")

            self.done = True
            self.note = f"寄出 {amount} 個給「{self._receiver}」"
            return True
        finally:
            # ⚠ **不管成功失敗都要關窗** —— 對話框開著角色就動不了
            #   （跟商店那條同一個坑，[PKT-074]）。
            self.step = "close"
            try:
                self._send(build_close())
            except Exception as exc:  # noqa: BLE001 - 收尾失敗只記錄
                log.debug("關寫信視窗失敗：%s", exc)

    #: 信件的標題與內文。空的也要送（長度 1 的 "\0"），見 `build_send`。
    title = "TITLE"
    text = ""

    def _fail(self, message: str) -> bool:
        self.failed = True
        self.note = message
        log.warning("%s", message)
        return False
