"""自動補水的測試（用假的角色／背包／socket／封包，不需要遊戲）。"""

from __future__ import annotations

import time

import pytest

from ro_toolbox.core.ro_protocol import CZ_USE_ITEM
from ro_toolbox.services import potion
from ro_toolbox.services.potion import PotionBot, PotionConfig

AID = 0x016B510B
RED_POTION = 501
BLUE_POTION = 505


class FakeStatus:
    def __init__(self, hp_percent: float, sp_percent: float = 100.0):
        self.hp_percent = hp_percent
        self.sp_percent = sp_percent
        self.aid = AID
        self.name = "測試角色"


class FakePacket:
    def __init__(self, opcode: int, payload: bytes):
        self.opcode = opcode
        self.payload = payload
        self.outbound = False


def use_ack(index: int, item_id: int, left: int, result: int = 1, aid: int = AID) -> FakePacket:
    """伺服器的使用道具回應（版面見 GAMEDATA [PKT-036]）。"""
    return FakePacket(
        0x01C8,
        index.to_bytes(2, "little")
        + item_id.to_bytes(4, "little")
        + aid.to_bytes(4, "little")
        + left.to_bytes(2, "little")
        + bytes([result]),
    )


class FakeReader:
    """血量會隨著「喝掉幾瓶」上升，模擬伺服器回補。"""

    def __init__(self, bag, start: float = 30.0, per_potion: float = 10.0):
        self.bag = bag
        self.start = start
        self.per_potion = per_potion
        self.closed = False

    def attach(self, pid, should_stop=None):  # noqa: ARG002
        return True

    def read(self):
        return FakeStatus(min(self.start + self.per_potion * self.bag.drunk, 100.0))

    def close(self):
        self.closed = True


class FakeBag:
    """假的記憶體背包：{格號: (道具編號, 數量)}。"""

    def __init__(self, rows: dict[int, tuple[int, int]], readable: bool = True,
                 shuffle: bool = False):
        self.rows = dict(rows)
        self.readable = readable
        self.shuffle = shuffle       # 每喝一次就把剩下的搬到別格（模擬背包重排）
        self.drunk = 0

    def as_dict(self, pid):  # noqa: ARG002
        return dict(self.rows) if self.readable else {}

    def consume(self, slot):
        if slot not in self.rows:
            return
        item_id, amount = self.rows[slot]
        self.drunk += 1
        del self.rows[slot]
        if amount > 1:
            self.rows[slot + 10 if self.shuffle else slot] = (item_id, amount - 1)


class FakeCapture:
    """假的封包擷取。伺服器的回應由 FakeSocket 在送出時投遞進來。"""

    def __init__(self, pid, on_packet, on_error=None):  # noqa: ARG002
        self.on_packet = on_packet
        self.started = False
        self.stopped = 0
        FakeCapture.latest = self

    def start(self):
        self.started = True
        return True

    def stop(self, timeout: float = 3.0):  # noqa: ARG002
        self.stopped += 1


class FakeSocket:
    """送出使用道具時，依設定投遞（或不投遞）伺服器回應。"""

    def __init__(self, bag: FakeBag, *, reply=True, item_id=RED_POTION, result=1, wrong=None):
        self.bag = bag
        self.reply = reply
        self.item_id = item_id
        self.result = result
        self.wrong = wrong          # 回包謊稱是別的道具（模擬格號被挪動）
        self.sent: list[bytes] = []
        self.closed = 0

    def find_game_socket(self, pid, host, port):  # noqa: ARG002
        return 42

    def send_on_socket(self, sock, data):  # noqa: ARG002
        self.sent.append(data)
        slot = int.from_bytes(data[2:4], "little")
        if not self.reply:
            return len(data)
        real = self.bag.rows.get(slot, (self.item_id, 0))[0]
        if self.result == 1:
            self.bag.consume(slot)
        # 伺服器回的是「那個道具還剩幾個」，跟它現在在第幾格無關
        left = sum(a for i, a in self.bag.rows.values() if i == real)
        capture = FakeCapture.latest
        if capture is not None:
            capture.on_packet(use_ack(slot, self.wrong or real, left, self.result))
        return len(data)

    def close_socket(self, sock):  # noqa: ARG002
        self.closed += 1


@pytest.fixture
def wired(monkeypatch):
    """把 PotionBot 的外部相依全部換成假的。"""
    FakeCapture.latest = None

    def build(rows, *, server=("1.2.3.4", 10000), start=30.0,
              readable=True, shuffle=False, **socket_kwargs):
        fake_bag = FakeBag(rows, readable=readable, shuffle=shuffle)
        reader = FakeReader(fake_bag, start=start)
        sock = FakeSocket(fake_bag, **socket_kwargs)
        monkeypatch.setattr(potion, "find_server", lambda pid: server)  # noqa: ARG005
        monkeypatch.setattr(potion, "CharacterReader", lambda: reader)
        monkeypatch.setattr(potion, "bag", fake_bag)
        monkeypatch.setattr(potion, "game_socket", sock)
        monkeypatch.setattr(potion, "PcapCapture", FakeCapture)
        return fake_bag, reader, sock

    return build


def _run(bot: PotionBot, seconds: float = 2.0) -> None:
    bot.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and bot.running:
        time.sleep(0.02)
    bot.stop()
    assert not bot.running, "執行緒沒有結束"


# ---- 設定 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expect"),
    [(0, 0), (50, 50), (99, 99), (100, 100), (250, 100), (-3, 0)],
)
def test_percent_is_clamped(given, expect):
    """夾在 0~100。判斷式是 `hp% < 門檻`，所以 100 在滿血時不會觸發；
    超過 100 才會變成「永遠低於門檻」而灌光整袋（實測 101 就是這樣）。"""
    assert PotionConfig(hp_item=RED_POTION, hp_percent=given).hp_percent == expect


def test_wants_needs_both_item_and_percent():
    assert PotionConfig(hp_item=RED_POTION, hp_percent=50).wants_hp() is True
    assert PotionConfig(hp_item=None, hp_percent=50).wants_hp() is False
    assert PotionConfig(hp_item=RED_POTION, hp_percent=0).wants_hp() is False


# ---- 喝水 --------------------------------------------------------------


def test_drinks_until_above_threshold(wired):
    """低於門檻就連喝，過線就停 —— 這是使用者要的「快速輪迴」。"""
    fake_bag, _reader, sock = wired({6: (RED_POTION, 20)}, start=30.0)   # 每瓶 +10%
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=60))
    _run(bot, 2.0)
    assert bot.stats.hp_used == 3          # 30% 起跳，要 3 瓶才到 60%
    assert fake_bag.rows[6] == (RED_POTION, 17)
    assert all(pkt[:2] == CZ_USE_ITEM.to_bytes(2, "little") for pkt in sock.sent)


def test_looks_up_the_slot_every_time(wired):
    """設定存的是**道具編號**，格號每次現查 —— 格號會挪動（[MEM-028]）。"""
    fake_bag, _reader, sock = wired({31: (RED_POTION, 9)}, start=30.0)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=40))
    _run(bot, 1.5)
    assert int.from_bytes(sock.sent[0][2:4], "little") == 31


def test_follows_the_item_when_the_slot_moves(wired):
    """道具換格號之後要送新的格號 —— 這就是設定存編號不存格號的理由。"""
    _bag, _reader, sock = wired({6: (RED_POTION, 4)}, start=30.0, shuffle=True)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=60))
    _run(bot, 2.0)
    slots = [int.from_bytes(p[2:4], "little") for p in sock.sent]
    assert slots == [6, 16, 26], f"應該跟著道具換格，實際送了 {slots}"


def test_sends_the_aid(wired):
    _bag, _reader, sock = wired({6: (RED_POTION, 5)})
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=40))
    _run(bot, 1.0)
    assert int.from_bytes(sock.sent[0][4:8], "little") == AID


def test_does_nothing_above_threshold(wired):
    _bag, _reader, sock = wired({6: (RED_POTION, 5)}, start=90.0)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=50))
    _run(bot, 0.6)
    assert sock.sent == []
    assert bot.stats.hp_used == 0


def test_disabled_when_percent_zero(wired):
    _bag, _reader, sock = wired({6: (RED_POTION, 5)}, start=10.0)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=0))
    _run(bot, 0.6)
    assert sock.sent == []


# ---- 失效模式 ----------------------------------------------------------


def test_stops_when_the_bag_cannot_be_read(wired):
    """讀不到背包就不能猜格號 —— 大聲停用。"""
    _bag, _reader, sock = wired({6: (RED_POTION, 5)}, start=10.0, readable=False)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=50))
    _run(bot, 1.0)
    assert bot.stats.failed is True
    assert "背包" in bot.stats.note
    assert sock.sent == []


def test_stops_loudly_when_there_is_no_reply(wired):
    """送了封包但伺服器完全沒回 → 不能悶著一直灌，要大聲停用。"""
    _bag, _reader, sock = wired({6: (RED_POTION, 9)}, start=10.0, reply=False)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=50))
    _run(bot, 5.0)
    assert bot.stats.failed is True
    assert "連續" in bot.stats.note
    assert len(sock.sent) == potion._MAX_MISS


def test_stops_loudly_when_server_refuses(wired):
    _bag, _reader, sock = wired({6: (RED_POTION, 9)}, start=10.0, result=0)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=50))
    _run(bot, 5.0)
    assert bot.stats.failed is True
    assert len(sock.sent) == potion._MAX_MISS


def test_stops_when_the_reply_names_a_different_item(wired):
    """回包說喝到別的東西 → 立刻停，喝錯比不喝糟。"""
    _bag, _reader, sock = wired(
        {6: (RED_POTION, 9)}, start=10.0, wrong=BLUE_POTION
    )
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=50))
    _run(bot, 3.0)
    assert bot.stats.failed is True
    assert "不是你選的" in bot.stats.note
    assert len(sock.sent) == 1, "發現不對就要馬上停"


def test_exhausted_item_stops_cleanly(wired):
    """喝完最後一瓶背包裡就找不到它了，那是成功，不是「喝不到」。"""
    fake_bag, _reader, _sock = wired({6: (RED_POTION, 2)}, start=10.0)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=99))
    _run(bot, 5.0)
    assert bot.stats.hp_used == 2
    assert 6 not in fake_bag.rows
    assert bot.stats.failed is True          # 沒有其他設定，整個停止
    assert "用完" in bot.stats.note


def test_exhausted_hp_keeps_sp_running(wired):
    """HP 那個用完不該把 SP 那項也關掉。"""
    fake_bag, _reader, _sock = wired(
        {6: (RED_POTION, 1), 7: (BLUE_POTION, 5)}, start=10.0
    )
    bot = PotionBot(
        1, PotionConfig(hp_item=RED_POTION, hp_percent=99,
                        sp_item=BLUE_POTION, sp_percent=50)
    )
    bot.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and 6 in fake_bag.rows:
        time.sleep(0.02)
    time.sleep(0.3)
    still_running = bot.running and not bot.stats.failed
    bot.stop()
    assert 6 not in fake_bag.rows
    assert bot.stats.hp_used == 1
    assert still_running is True
    assert "用完" in bot.stats.note


def test_stops_when_connection_is_gone(wired):
    _bag, _reader, _sock = wired({6: (RED_POTION, 5)}, server=None)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=50))
    _run(bot, 1.0)
    assert bot.stats.failed is True
    assert "連線" in bot.stats.note


def test_cleanup_releases_everything(wired):
    _bag, reader, sock = wired({6: (RED_POTION, 5)}, start=90.0)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=50))
    _run(bot, 0.5)
    assert reader.closed is True
    assert sock.closed >= 1
    assert FakeCapture.latest.stopped >= 1
    assert bot.stats.running is False
