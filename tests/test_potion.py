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

    def __init__(self, bag, start: float = 30.0, per_potion: float = 10.0,
                 sp_start: float = 100.0):
        self.bag = bag
        self.start = start
        self.per_potion = per_potion
        self.sp_start = sp_start
        self.closed = False

    def attach(self, pid, should_stop=None):  # noqa: ARG002
        return True

    def read(self):
        # 兩條都跟著「喝掉幾瓶」走 —— 假裝伺服器回補（分不出是哪一種藥，
        # 測 SP 時只設一種就好）。
        gain = self.per_potion * self.bag.drunk
        return FakeStatus(
            min(self.start + gain, 100.0), min(self.sp_start + gain, 100.0)
        )

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
        self.generation = 0          # 綁定世代，bump 一次代表舊的 watch 過期
        self.watches: list[FakeWatch] = []

    def as_dict(self, pid):  # noqa: ARG002
        return dict(self.rows) if self.readable else {}

    def BagWatch(self, pid):  # noqa: N802, ARG002 - 冒充 bag 模組裡的類別
        watch = FakeWatch(self)
        self.watches.append(watch)
        return watch

    def expire_watches(self):
        """模擬綁定過期（換地圖、背包重新配置）：已開的 watch 全部失效。"""
        self.generation += 1

    def consume(self, slot):
        if slot not in self.rows:
            return
        item_id, amount = self.rows[slot]
        self.drunk += 1
        del self.rows[slot]
        if amount > 1:
            self.rows[slot + 10 if self.shuffle else slot] = (item_id, amount - 1)


class FakeWatch:
    """假的 `bag.BagWatch`：綁定成功與否跟著 FakeBag 的 readable 走。"""

    def __init__(self, bag: FakeBag):
        self.bag = bag
        self.bound = False
        self.opens = 0
        self.closed = 0
        self.generation = -1

    def open(self) -> bool:
        self.opens += 1
        self.generation = self.bag.generation
        self.bound = self.bag.readable
        return self.bound

    def snapshot(self):
        if not self.bound or not self.bag.readable:
            return {}
        if self.generation != self.bag.generation:
            return {}          # 綁定過期了
        return dict(self.bag.rows)

    def close(self) -> None:
        self.bound = False
        self.closed += 1


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

    def __init__(self, bag: FakeBag, *, reply=True, item_id=RED_POTION, result=1,
                 wrong=None, dies_after=None):
        self.bag = bag
        self.reply = reply
        self.dies_after = dies_after   # 前 N 次正常，之後裝死（不扣、不回）
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
        if self.dies_after is not None and len(self.sent) > self.dies_after:
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

    def build(rows, *, server=("1.2.3.4", 10000), start=30.0, sp_start=100.0,
              readable=True, shuffle=False, **socket_kwargs):
        fake_bag = FakeBag(rows, readable=readable, shuffle=shuffle)
        reader = FakeReader(fake_bag, start=start, sp_start=sp_start)
        sock = FakeSocket(fake_bag, **socket_kwargs)
        monkeypatch.setattr(potion, "find_server", lambda pid: server)  # noqa: ARG005
        monkeypatch.setattr(potion, "CharacterReader", lambda: reader)
        monkeypatch.setattr(potion, "bag", fake_bag)
        monkeypatch.setattr(potion, "game_socket", sock)
        monkeypatch.setattr(potion, "PacketCapture", FakeCapture)
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


# ---- 啟動：不該一試就放棄，訊息要講得出是哪一隻 ---------------------------


def test_socket_lookup_retries_instead_of_giving_up_once(monkeypatch):
    """剛登入／剛換地圖的那幾秒複製不到 socket 是**正常過渡**，不是故障。

    以前只試一次，勾下去的時機不對就整個停用，使用者只看到一行
    「找不到遊戲 socket」，看起來像壞掉（實際回報）。
    """
    from ro_toolbox.services import potion as mod

    tries = []

    def flaky(_pid, _ip, _port):
        tries.append(1)
        return 0 if len(tries) < 3 else 0x1234      # 第三次才成功

    monkeypatch.setattr(mod, "find_server", lambda _pid: ("1.2.3.4", 10000))
    monkeypatch.setattr(mod.game_socket, "find_game_socket", flaky)
    monkeypatch.setattr(mod, "_SOCKET_POLL", 0.0)

    bot = mod.PotionBot(1234, mod.PotionConfig())
    sock, server = bot._wait_for_socket()
    assert sock == 0x1234
    assert server == ("1.2.3.4", 10000)
    assert len(tries) == 3


def test_socket_lookup_gives_up_at_the_deadline(monkeypatch):
    """重試也要有上限 —— 逾時就大聲停用，不能永遠卡在啟動。"""
    from ro_toolbox.services import potion as mod

    monkeypatch.setattr(mod, "find_server", lambda _pid: ("1.2.3.4", 10000))
    monkeypatch.setattr(mod.game_socket, "find_game_socket", lambda *a: 0)
    monkeypatch.setattr(mod, "_SOCKET_POLL", 0.0)
    monkeypatch.setattr(mod, "_SOCKET_WAIT_SEC", 0.05)

    bot = mod.PotionBot(1234, mod.PotionConfig())
    sock, _server = bot._wait_for_socket()
    assert sock is None


def test_failure_message_names_the_character(caplog):
    """多開的時候，一行沒有身分的警告等於沒說。"""
    import logging

    from ro_toolbox.services import potion as mod

    bot = mod.PotionBot(1234, mod.PotionConfig())
    bot._character = "狐狐狸"
    with caplog.at_level(logging.WARNING, logger="ro_toolbox.services.potion"):
        bot._fail("找不到遊戲 socket，無法送封包")
    assert any("狐狐狸" in r.message for r in caplog.records)


def test_failure_message_survives_an_unknown_character(caplog):
    """名字還沒讀到就失敗時不要硬塞空引號，訊息照樣要通順。"""
    import logging

    from ro_toolbox.services import potion as mod

    bot = mod.PotionBot(1234, mod.PotionConfig())
    with caplog.at_level(logging.WARNING, logger="ro_toolbox.services.potion"):
        bot._fail("角色定位失敗")
    assert any("自動補水停用：角色定位失敗" in r.message for r in caplog.records)


# ---- 連喝（藥水沒有冷卻，低於門檻要一路喝到過線）------------------------


def test_burst_never_sends_more_than_the_bag_holds(wired):
    """⚠ 連喝**不准多灌**。

    這條擋的是一個真的踩到的設計錯誤：連喝為了快而不等確認，背包數量還沒
    更新就一直看到「還有 N 瓶」，結果背包只有 2 瓶卻送了 27 次使用道具。
    """
    # 背包裡還有別的東西 —— 真實情況本來就這樣，而且串列要有東西才驗得過
    fake_bag, _reader, sock = wired(
        {6: (RED_POTION, 2), 7: (BLUE_POTION, 9), 8: (909, 3)}, start=10.0
    )
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=99))
    _run(bot, 5.0)
    assert len(sock.sent) == 2, f"背包只有 2 瓶，卻送了 {len(sock.sent)} 次"
    assert fake_bag.drunk == 2


def test_burst_stops_and_counts_a_miss_when_nothing_is_consumed(wired):
    """連喝時數量沒少 = 跟「送了沒回應」同一件事，要計入失敗次數。

    不計的話連喝會變成一條悶著狂送的暗路 —— 主迴圈的保護完全繞過去了。
    """
    _bag, _reader, sock = wired({6: (RED_POTION, 50)}, start=10.0, dies_after=1)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=99))
    _run(bot, 8.0)
    assert bot.stats.failed is True
    assert len(sock.sent) <= potion._MAX_MISS + 1, f"送了 {len(sock.sent)} 次"


def test_burst_is_skipped_when_the_first_drink_did_not_land(wired):
    """第一瓶沒喝到就不准連喝 —— 那些送出會整個繞過失敗計數。"""
    _bag, _reader, sock = wired({6: (RED_POTION, 9)}, start=10.0, result=0)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=99))
    _run(bot, 5.0)
    assert len(sock.sent) == potion._MAX_MISS


# ---- 背包快路徑 --------------------------------------------------------


def test_bag_is_read_through_a_bound_list(wired):
    """喝水要走綁定過的串列，不是每次重跑 AOB 掃描（那是每瓶 0.1 秒）。"""
    fake_bag, _reader, _sock = wired({6: (RED_POTION, 20)}, start=30.0)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=60))
    _run(bot, 2.0)
    assert len(fake_bag.watches) == 1, "只該綁定一次"
    assert fake_bag.watches[0].opens == 1
    assert bot.stats.hp_used == 3


def test_stale_binding_is_relocated_not_trusted(wired):
    """綁定過期（換地圖、背包重配置）要重新定位，不是拿舊資料硬撐。"""
    fake_bag, _reader, _sock = wired({6: (RED_POTION, 40)}, start=30.0)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=60))
    bot.start()
    deadline = time.monotonic() + 3.0
    bumped = False
    while time.monotonic() < deadline and bot.running:
        if not bumped and bot.stats.hp_used >= 1:
            fake_bag.expire_watches()
            bumped = True
        elif bumped and len(fake_bag.watches) > 1:
            break
        time.sleep(0.02)
    bot.stop()
    assert bumped, "測試沒跑到喝水就結束了"
    assert len(fake_bag.watches) > 1, "綁定過期之後應該重新定位"
    assert fake_bag.watches[0].closed >= 1, "舊的綁定要關掉"
    assert bot.stats.failed is False


def test_relocating_is_throttled(wired):
    """重新定位要跑一次 AOB 掃描（0.1 秒）—— 不能在 10 ms 輪詢裡一直跑。"""
    fake_bag, _reader, _sock = wired({6: (RED_POTION, 40)}, start=30.0)
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=60))
    bot.start()
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and bot.running:
        fake_bag.expire_watches()      # 每次都讓綁定失效，逼它想重新定位
        time.sleep(0.01)
    bot.stop()
    # 1.5 秒 ÷ _RELOCATE_SEC 上限約 2~3 次；沒有限流會是幾十次
    assert len(fake_bag.watches) <= 4, f"重新定位了 {len(fake_bag.watches)} 次"


# ---- 水用完回程 --------------------------------------------------------

WING = 602          # 蝴蝶翅膀


def test_going_home_when_the_hp_potion_runs_out(wired):
    """HP 藥喝完就用選好的道具回程 —— 沒水還留在原地，下一波怪就是送死。"""
    fake_bag, _reader, sock = wired(
        {6: (RED_POTION, 1), 7: (WING, 5), 8: (909, 3)}, start=10.0
    )
    bot = PotionBot(
        1, PotionConfig(hp_item=RED_POTION, hp_percent=99, home_item=WING)
    )
    _run(bot, 5.0)
    slots = [int.from_bytes(p[2:4], "little") for p in sock.sent]
    assert slots == [6, 7], f"應該喝掉最後一瓶再用翅膀，實際 {slots}"
    assert bot.stats.went_home is True
    assert fake_bag.rows[7][1] == 4, "翅膀要真的用掉一個"


def test_going_home_also_triggers_on_sp(wired):
    """**HP 或 SP 任一種**用完就回程，不必等兩種都用完。"""
    _bag, _reader, sock = wired(
        {6: (RED_POTION, 9), 7: (BLUE_POTION, 1), 8: (WING, 5)},
        start=100.0, sp_start=10.0,
    )
    bot = PotionBot(
        1, PotionConfig(hp_item=RED_POTION, hp_percent=50,
                        sp_item=BLUE_POTION, sp_percent=99, home_item=WING)
    )
    _run(bot, 5.0)
    slots = [int.from_bytes(p[2:4], "little") for p in sock.sent]
    assert slots == [7, 8]
    assert bot.stats.went_home is True


def test_no_return_item_means_the_old_behaviour(wired):
    """沒勾回程就照舊：關掉那一項，沒別的設定才停 —— 不准自己回程。"""
    _bag, _reader, sock = wired(
        {6: (RED_POTION, 1), 7: (WING, 5), 8: (909, 3)}, start=10.0
    )
    bot = PotionBot(1, PotionConfig(hp_item=RED_POTION, hp_percent=99))
    _run(bot, 5.0)
    slots = [int.from_bytes(p[2:4], "little") for p in sock.sent]
    assert slots == [6], "沒勾回程就不該去動翅膀"
    assert bot.stats.went_home is False
    assert "用完" in bot.stats.note


def test_missing_return_item_fails_loudly(wired):
    """回程道具也沒了 —— 大聲停用，不准安靜地留在野外。"""
    _bag, _reader, sock = wired(
        {6: (RED_POTION, 1), 8: (909, 3), 9: (910, 2)}, start=10.0
    )
    bot = PotionBot(
        1, PotionConfig(hp_item=RED_POTION, hp_percent=99, home_item=WING)
    )
    _run(bot, 5.0)
    assert bot.stats.failed is True
    assert bot.stats.went_home is False
    assert "回程道具" in bot.stats.note


def test_unconfirmed_return_is_not_reported_as_home(wired):
    """⚠ 送了封包不等於回去了。沒看到數量少一個就**不准**說已回程 ——
    那會讓人以為安全了，實際人還在野外。"""
    _bag, _reader, _sock = wired(
        {6: (RED_POTION, 1), 7: (WING, 5), 8: (909, 3)}, start=10.0, dies_after=1
    )
    bot = PotionBot(
        1, PotionConfig(hp_item=RED_POTION, hp_percent=99, home_item=WING)
    )
    _run(bot, 6.0)
    assert bot.stats.went_home is False
    assert bot.stats.failed is True



# ---- 水剩 5 瓶就回去補（使用者 2026-08-29 指定，不要等到 0）----------------


def test_low_stock_goes_home_before_running_dry():
    """喝到最後一瓶才想回城，那一路上就已經沒水可喝了。"""
    from ro_toolbox.services.potion import LOW_STOCK

    assert LOW_STOCK == 5


def test_low_stock_does_nothing_without_a_return_item():
    """沒勾回程就照舊 —— 剩幾瓶都繼續喝，喝到 0 才關掉那一項。"""
    from ro_toolbox.services.potion import PotionBot, PotionConfig

    bot = PotionBot(1234, PotionConfig(hp_item=501, hp_percent=50))
    assert bot._low_stock(501, 1) is None
    assert bot._low_stock(501, 0) is None
