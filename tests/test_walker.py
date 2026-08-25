"""Walker 的行為驗證（不需遊戲）：連續走、被拒絕要換路、卡住要放棄。

三條規則都來自實機量測（GAMEDATA [PKT-030]）：
單次移動 ≤17 格才會被接受、途中改目標不會停頓、被拒絕時伺服器完全不吭聲。
"""

from __future__ import annotations

from ro_toolbox.services.walker import ACK_TIMEOUT, LOOKAHEAD, MAX_STEP, STUCK_SEC, Walker


class Fake:
    """假的時間與 socket：可以精準控制「送了什麼、過了多久」。"""

    def __init__(self) -> None:
        self.now = 100.0
        self.sent: list[tuple[int, int]] = []

    def clock(self) -> float:
        return self.now

    def send(self, x: int, y: int) -> None:
        self.sent.append((x, y))


def straight_path(length: int, y: int = 50) -> list[tuple[int, int]]:
    return [(10 + i, y) for i in range(1, length + 1)]


def test_first_update_sends_farthest_reachable_step():
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(straight_path(40))
    assert walker.update((10, 50)) == "walking"
    assert fake.sent == [(10 + MAX_STEP, 50)]


def test_sends_next_leg_before_arriving():
    """快到才送下一段 —— 這就是「不停頓」：不必等走到定點。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(straight_path(40))
    walker.update((10, 50))
    walker.note_move_ack((10 + MAX_STEP, 50))

    # 還沒到、離目標還遠 → 不重送（重送會讓角色重新起步、反而卡頓）
    fake.now += 1.0
    walker.update((10 + MAX_STEP - LOOKAHEAD - 2, 50))
    assert len(fake.sent) == 1

    # 剩下 LOOKAHEAD 步就先把下一段送出去
    fake.now += 1.0
    walker.update((10 + MAX_STEP - LOOKAHEAD, 50))
    assert len(fake.sent) == 2
    assert fake.sent[1][0] > fake.sent[0][0]


def test_rejected_move_shrinks_step_then_gives_up():
    """伺服器不理會的移動是靜默的，只能靠『沒收到 0x0087』判斷。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(straight_path(40))
    walker.update((10, 50))

    state = "walking"
    for _ in range(8):
        fake.now += ACK_TIMEOUT + 0.01
        state = walker.update((10, 50))
        if state == "blocked":
            break
    assert state == "blocked"
    assert walker.rejected >= 1
    # 放棄前有先試過更短的段
    assert len({abs(x - 10) for x, _y in fake.sent}) > 1


def test_ack_restores_full_step():
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(straight_path(40))
    walker.update((10, 50))
    fake.now += ACK_TIMEOUT + 0.01
    walker.update((10, 50))  # 第一段被拒 → 步幅減半
    short = abs(fake.sent[-1][0] - 10)
    assert short < MAX_STEP

    walker.note_move_ack(fake.sent[-1])
    fake.now += 0.1
    walker.update((10 + short - LOOKAHEAD, 50))
    assert abs(fake.sent[-1][0] - (10 + short - LOOKAHEAD)) == MAX_STEP


def test_arrived_when_goal_reached():
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    path = straight_path(10)
    walker.set_path(path)
    walker.update((10, 50))
    walker.note_move_ack(path[-1])
    fake.now += 2.0
    assert walker.update(path[-1]) == "arrived"
    assert not walker.active


def test_stuck_is_blocked():
    """伺服器接受了移動，但實際被地形擋住走不到 —— 位置不會變。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(straight_path(40))
    walker.update((10, 50))
    walker.note_move_ack(fake.sent[-1])
    fake.now += STUCK_SEC + 0.1
    assert walker.update((10, 50)) == "blocked"


def test_off_path_is_blocked():
    """被傳送／擊退到別的地方 → 這條路已經不對了，要重新規劃。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(straight_path(40))
    walker.update((10, 50))
    assert walker.update((200, 200)) == "blocked"


def test_unrelated_ack_is_ignored():
    """別人的／被擊退造成的 0x0087 不能算成我這次移動的確認。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(straight_path(40))
    walker.update((10, 50))
    walker.note_move_ack((300, 300))
    fake.now += ACK_TIMEOUT + 0.01
    walker.update((10, 50))
    assert walker.rejected == 1
