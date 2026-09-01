"""Walker 的行為驗證（不需遊戲）：連續走、被拒絕要換路、卡住要放棄。

三條規則都來自實機量測（GAMEDATA [PKT-030]）：
單次移動 ≤17 格才會被接受、途中改目標不會停頓、被拒絕時伺服器完全不吭聲。
"""

from __future__ import annotations

from ro_toolbox.services.walker import (
    ACK_TIMEOUT,
    LOOKAHEAD,
    MAX_RESEND,
    MAX_STEP,
    RESEND_SEC,
    STUCK_SEC,
    Walker,
    line_cells,
)


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


def test_stuck_resends_before_giving_up():
    """位置停住**不能直接當成走不成**：走路途中被怪打會被打斷，角色停在半路，
    伺服器一樣不吭聲（使用者實測回報）。要先把同一段重送把腳步接回去，
    重送用完而且還是不動，才算真的被地形擋住。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(straight_path(40))
    walker.update((10, 50))
    walker.note_move_ack(fake.sent[-1])

    state = "walking"
    for _ in range(MAX_RESEND + 3):
        fake.now += RESEND_SEC + 0.05
        state = walker.update((10, 50))
        walker.note_move_ack(fake.sent[-1])  # 伺服器每次都接受，但人就是不動
        if state == "blocked":
            break
    assert state == "blocked"
    assert walker.resent == MAX_RESEND       # 放棄之前真的重送過


def test_interrupted_walk_is_picked_back_up_instead_of_replanned():
    """被打斷 → 重送 → 又動了。這種情況不准回報 blocked，
    不然「路上被打一下」就會變成整條路線重新規劃、繞遠路。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(straight_path(40))
    walker.update((10, 50))
    walker.note_move_ack(fake.sent[-1])

    fake.now += RESEND_SEC + 0.05
    assert walker.update((10, 50)) == "walking"   # 停住了 → 先重送
    assert walker.resent == 1
    walker.note_move_ack(fake.sent[-1])

    fake.now += 0.2
    assert walker.update((11, 50)) == "walking"   # 動了 = 接回去了

    # 位置動過就重新起算：後面再被打斷還有完整的重送機會，不會直接放棄
    fake.now += STUCK_SEC + 0.1
    assert walker.update((11, 50)) == "walking"
    assert walker.resent == 2


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


# ---- 每一段中間的路是伺服器走的 --------------------------------------------


#: 一片傳點區。路徑從它西邊繞到北邊，全程一格都沒踩到。
WARP = frozenset((x, y) for x in range(12, 26) for y in range(45, 56))


def detour() -> list[tuple[int, int]]:
    """繞開那片傳點的路：先沿 x=10 往北，再沿 y=44 往東。兩段都在禁區外。"""
    path = [(10, y) for y in range(49, 43, -1)]
    path += [(x, 44) for x in range(11, 35)]
    assert not (set(path) & WARP)
    return path


def test_a_segment_is_shortened_so_the_server_cannot_cut_through_a_warp():
    """⚠ A* 繞開傳點**不夠**：我們一次送 14 格，中間那段路是伺服器自己算的
    （[PKT-030]），它會抄近路直接穿過去 —— 使用者實測回報的
    「自動打怪走一走被傳到別的地圖」就是這樣來的。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(detour(), avoid=WARP)
    assert walker.update((10, 50)) == "walking"
    sent = fake.sent[-1]
    assert all(cell not in WARP for cell in line_cells((10, 50), sent))


def test_without_the_avoid_set_the_old_behaviour_would_cut_through():
    """反證：不給 avoid 的話，送出去的那一段直線真的會穿過傳點。
    這條釘住「問題是真的存在」，不是為了修而修。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path(detour())
    walker.update((10, 50))
    assert any(cell in WARP for cell in line_cells((10, 50), fake.sent[-1]))


def test_standing_inside_the_zone_still_moves():
    """人本來就可能站在禁區裡（剛被傳過來、或被怪引過去）。
    起點那一段也算進去的話，每一段都被否決、一步都走不出去 ——
    那是 [MEM-044] 已經踩過的同一個坑。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock)
    walker.set_path([(x, 50) for x in range(21, 45)], avoid=WARP)
    assert walker.update((20, 50)) == "walking"
    assert fake.sent, "站在禁區裡也必須走得出來"


def test_the_resend_window_still_fits_inside_the_stuck_timeout():
    """⚠ 重送變快，次數就要跟著變多。

    判定「這條路走不成」要**重送用完 ＋ 停超過 STUCK_SEC** 兩個條件同時成立。
    如果 `MAX_RESEND × RESEND_SEC` 遠小於 `STUCK_SEC`，重送早早用完，
    後面那段時間就只是乾等 —— 等於**變相縮短了救援時間**，
    跟「被怪打斷時要點快一點」的目的正好相反。
    """
    from ro_toolbox.services import walker as mod

    window = mod.MAX_RESEND * mod.RESEND_SEC
    assert window <= mod.STUCK_SEC, "重送不能拖過放棄時間"
    assert window >= mod.STUCK_SEC * 0.6, "重送用完之後不該剩一大段乾等"


def test_resending_is_fast_enough_to_out_pace_being_hit():
    """使用者實測：被好幾隻怪同時打的時候 0.5 秒太慢，人一直站在原地。

    走路速度約 1 格 / 0.15 秒 —— 重送間隔要接近那個尺度才追得上打斷。
    """
    from ro_toolbox.services import walker as mod

    assert mod.RESEND_SEC <= 0.3


# ---- 學到的步幅不准被重新規劃打掉（使用者實測：連送 21 個一樣的封包）--------


def test_a_shrunk_step_survives_a_new_path():
    """⚠⚠ 移動被拒絕時步幅會對半縮，去找伺服器肯收的長度。

    但呼叫端一發現「這段走不成」就會重新規劃、重新 `set_path()` ——
    舊版在那裡把步幅打回 `MAX_STEP`，等於**永遠學不會**：
    使用者實機 2026-08-28 在 izlu2dun 連送 21 個一模一樣的封包
    （解回來都是同一格），角色一步都沒動。
    """
    from ro_toolbox.services import walker as mod

    walker = mod.Walker(lambda x, y: None)
    walker.set_path([(x, 0) for x in range(1, 40)])
    walker._step = 5                      # 假設已經縮到 5

    walker.set_path([(x, 0) for x in range(1, 40)])
    assert walker._step == 5, "重新規劃不該把學到的步幅丟掉"


def test_a_new_path_never_starts_crawling():
    """縮太小的話新路徑會一格一格爬 —— 至少要從 CARRY_MIN 起跳。"""
    from ro_toolbox.services import walker as mod

    walker = mod.Walker(lambda x, y: None)
    walker.set_path([(x, 0) for x in range(1, 40)])
    walker._step = 1

    walker.set_path([(x, 0) for x in range(1, 40)])
    assert walker._step == mod.CARRY_MIN


def test_a_successful_move_restores_the_full_step():
    """成功一次就回到最大步幅，所以縮了不必怕回不去。"""
    from ro_toolbox.services import walker as mod

    sent = []
    walker = mod.Walker(lambda x, y: sent.append((x, y)))
    walker.set_path([(x, 0) for x in range(1, 40)])
    walker._step = 5
    walker.update((0, 0))
    walker.note_move_ack(sent[-1])
    walker.update((0, 0))
    assert walker._step == mod.MAX_STEP


def test_the_step_is_short_enough_for_diagonal_moves():
    """⚠ [PKT-030] 的「≤17 接受」是**直線**量的，斜走的上限低很多。

    使用者實機 2026-08-28 在 izlu2dun 同一條開闊斜線上逐格量：
    14 格被忽略、12 格被忽略、10 / 8 / 6 格會動。
    """
    from ro_toolbox.services import walker as mod

    assert mod.MAX_STEP <= 10


# ---- 客戶端說「我還在走」的時候不要重送 --------------------------------------


def test_no_resend_while_the_client_says_it_is_still_walking():
    """⚠ 這是「走路一卡一卡」的修法（[MEM-058]）。

    「停住了」的判斷是「兩次取樣讀到同一格」，而主迴圈一拍 0.2 秒起跳、
    還要加上那一拍的工作時間 —— 走得慢一點或剛好卡在斜走那一步就會誤判。
    重送等於叫伺服器從現在這一格重新規劃，角色會頓一下再走。

    客戶端的走路狀態是收到 `0x0087` 之後才寫的，所以它說在走就是真的在走。
    """
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock, moving=lambda: True)
    walker.set_path(straight_path(40))
    walker.update((10, 50))
    walker.note_move_ack(fake.sent[-1])

    for _ in range(MAX_RESEND + 3):
        fake.now += RESEND_SEC + 0.05
        assert walker.update((10, 50)) == "walking"
    assert walker.resent == 0, "客戶端說還在走，就不該重送"


def test_resend_still_happens_when_the_client_says_it_stopped():
    """被怪打斷時客戶端會變回站著 —— 那條救援路徑一定要留著。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock, moving=lambda: False)
    walker.set_path(straight_path(40))
    walker.update((10, 50))
    walker.note_move_ack(fake.sent[-1])
    fake.now += RESEND_SEC + 0.05
    assert walker.update((10, 50)) == "walking"
    assert walker.resent == 1


def test_unknown_moving_state_falls_back_to_the_timer():
    """問不出來（None）不等於站著，也不等於在走 —— 退回原本的計時器判斷。"""
    fake = Fake()
    walker = Walker(fake.send, now=fake.clock, moving=lambda: None)
    walker.set_path(straight_path(40))
    walker.update((10, 50))
    walker.note_move_ack(fake.sent[-1])
    fake.now += RESEND_SEC + 0.05
    walker.update((10, 50))
    assert walker.resent == 1
