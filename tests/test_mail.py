"""寄信（RODEX）的封包版面。**拿實機擷取當答案卡。**

來源：`封包/寄信10個野豬毛給商狐.txt`（2026-08-30，白狐寄 10 個野豬毛給商狐）。
版面說明見 `services/mail.py` 檔頭與 GAMEDATA [PKT-087]。
"""

from __future__ import annotations

from ro_toolbox.services import mail

# ---- 實機那一趟的原始位元組（從擷取檔抄下來的） ---------------------------

#: ↑ 0x0A13 查收件人「商狐」
REAL_CHECK = bytes.fromhex(
    "b0d3aab0 000001000000000000000000 0000 70000000 ffff".replace(" ", "")
)
#: ↓ 0x0A51 回應：角色ID + 職業 + 等級 + 名字
REAL_NAME_INFO = bytes.fromhex(
    ("213bc742" "0500" "3b00" + "b0d3aab0" + "00" * 20).replace(" ", "")
)
#: ↑ 0x0A04 附加：第 26 格、10 個
REAL_ADD = bytes.fromhex("1a000a00")
#: ↓ 0x0B3F 附加結果（開頭 5 個 byte）
REAL_ADD_RESULT = bytes.fromhex("001a000a00")
#: ↑ 0x0A6E 送出（含 opcode 與長度欄）
REAL_SEND = bytes.fromhex(
    "6e0a" "4b00"
    "b0d3aab0007ffd34e50a0000000000000000000000000000"      # 收件人 + 堆疊垃圾
    "a5d5aab0000000ff000000ff000000ff000000c000000080"      # 寄件人 + 堆疊垃圾
    "0000000000000000"                                       # zeny 0
    "0600" "0100"                                            # 標題長 6、內文長 1
    "213bc742"                                               # 收件人角色 ID
    "5449544c4500" "00"                                      # "TITLE\0" + "\0"
)


def _upto_nul(raw: bytes, start: int, size: int) -> bytes:
    """把名字欄位裡 `\\0` 之後的堆疊垃圾抹掉，才好逐位元組比對。"""
    chunk = raw[start:start + size]
    head = chunk.split(b"\0", 1)[0]
    return head.ljust(size, b"\0")


def _normalised(raw: bytes) -> bytes:
    """兩個名字欄位都只留到第一個 `\\0`（伺服器也只看到那裡）。"""
    return (
        raw[:4]
        + _upto_nul(raw, 4, 24)
        + _upto_nul(raw, 28, 24)
        + raw[52:]
    )


# ---- 送出那一包 ------------------------------------------------------------


def test_the_send_packet_matches_the_real_capture_byte_for_byte():
    """⚠ 這是整個功能的關鍵一包 —— 錯一個欄位東西就寄丟了。"""
    built = mail.build_send(
        "商狐", "白狐", 0x42C73B21, title="TITLE", text="",
    )
    assert _normalised(built) == _normalised(REAL_SEND)
    assert len(built) == 75
    assert int.from_bytes(built[2:4], "little") == 75, "長度欄要含自己"


def test_the_receiver_char_id_comes_from_the_server_not_from_us():
    """⛔ **不准自己算收件人的角色 ID** —— 猜一個號碼就寄給別人了。

    那 4 個 byte 一定要是 `0x0A51` 回應的前 4 個。這條測試把兩邊釘在一起。
    """
    info = mail.parse_name_info(REAL_NAME_INFO)
    assert info is not None
    char_id, job, level, name = info
    assert name == "商狐"
    assert (job, level) == (5, 59), "商狐是商人、59 級 —— 對得上就代表版面沒錯"

    built = mail.build_send("商狐", "白狐", char_id, title="TITLE", text="")
    assert built[64:68] == REAL_SEND[64:68], "送出那包要帶伺服器給的角色 ID"


def test_empty_title_and_text_still_carry_a_terminator():
    """⚠ 長度**含結尾的 \\0**（實機：標題 "TITLE" 寫 6、空內文寫 1）。"""
    built = mail.build_send("商狐", "白狐", 1, title="", text="")
    assert built[60:64] == b"\x01\x00\x01\x00"
    assert built.endswith(b"\0\0")


def test_names_are_cp950():
    """繁體客戶端 —— UTF-8 送出去伺服器會找不到人。"""
    built = mail.build_send("商狐", "白狐", 1)
    assert built[4:8] == "商狐".encode("cp950")
    assert built[28:32] == "白狐".encode("cp950")


# ---- 其他幾包 --------------------------------------------------------------


def test_check_name_matches_the_capture():
    built = mail.build_check_name("商狐")
    assert _upto_nul(built, 2, 24) == _upto_nul(b"\0\0" + REAL_CHECK, 2, 24)


def test_add_item_matches_the_capture():
    assert mail.build_add_item(26, 10)[2:] == REAL_ADD


def test_add_result_is_read_correctly():
    ok, index, amount = mail.parse_add_result(REAL_ADD_RESULT)
    assert (ok, index, amount) == (True, 26, 10)


def test_open_and_close_have_no_surprises():
    assert mail.build_open() == mail.OP_OPEN.to_bytes(2, "little") + bytes(24)
    assert mail.build_close() == mail.OP_CLOSE.to_bytes(2, "little")


def test_garbage_after_the_nul_is_ignored_when_reading_names():
    """實機那一包名字後面是沒清乾淨的堆疊垃圾 —— 只能讀到第一個 \\0。"""
    assert mail.read_name(REAL_SEND[4:28]) == "商狐"


# ---- 一整趟 ----------------------------------------------------------------


class _Wire:
    """假的連線：記下送出去的東西，並照實機的順序回應。"""

    def __init__(self, run: mail.MailRun, *, name_ok=True, send_ok=True) -> None:
        self.sent: list[bytes] = []
        self._run = run
        self._name_ok = name_ok
        self._send_ok = send_ok

    def send(self, data: bytes) -> bool:
        self.sent.append(data)
        op = int.from_bytes(data[:2], "little")
        if op == mail.OP_OPEN:
            self._run.feed(mail.OP_OPENED, bytes(24) + b"\x01")
        elif op == mail.OP_CHECK_NAME and self._name_ok:
            self._run.feed(mail.OP_NAME_INFO, REAL_NAME_INFO)
        elif op == mail.OP_ADD_ITEM:
            self._run.feed(mail.OP_ADD_RESULT, REAL_ADD_RESULT)
        elif op == mail.OP_SEND:
            self._run.feed(mail.OP_SEND_RESULT, b"\x00" if self._send_ok else b"\x01")
        return True

    def opcodes(self) -> list[int]:
        return [int.from_bytes(d[:2], "little") for d in self.sent]


class _Clock:
    """假時鐘。**每次 `wait()` 就往前走** —— 不然逾時那條路會永遠跑不完。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def wait(self, seconds: float) -> None:
        self.t += max(seconds, 0.1)


def _run(**kwargs):
    clock = _Clock()
    run = mail.MailRun(lambda _d: True, "白狐", "商狐", now=clock)
    wire = _Wire(run, **kwargs)
    run._send = wire.send
    return run, wire, clock


def test_a_whole_mail_goes_through_in_order():
    run, wire, clock = _run()
    assert run.run(26, 10, wait=clock.wait) is True
    assert wire.opcodes() == [
        mail.OP_OPEN, mail.OP_CHECK_NAME, mail.OP_ADD_ITEM,
        mail.OP_SEND, mail.OP_CLOSE,
    ]
    assert run.done and not run.failed


def test_an_unknown_receiver_never_sends_anything():
    """⛔ 查不到人就**不准送出** —— 那一包要帶角色 ID，沒有就等於亂寄。"""
    run, wire, clock = _run(name_ok=False)
    assert run.run(26, 10, wait=clock.wait) is False
    assert mail.OP_SEND not in wire.opcodes()
    assert "找不到收件人" in run.note


def test_the_window_is_closed_even_when_it_fails():
    """⚠ 對話框開著角色就動不了（跟商店那條同一個坑，[PKT-074]）。"""
    run, wire, clock = _run(name_ok=False)
    run.run(26, 10, wait=clock.wait)
    assert wire.opcodes()[-1] == mail.OP_CLOSE


def test_a_refused_mail_is_reported_not_swallowed():
    run, _wire, clock = _run(send_ok=False)
    assert run.run(26, 10, wait=clock.wait) is False
    assert run.failed and "拒絕" in run.note
