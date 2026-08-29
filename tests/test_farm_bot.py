"""自動打怪的決策邏輯測試（不需遊戲，也不會送任何封包）。

FarmBot 只有 start() 才會碰遊戲，建構子不會，所以可以直接測決策。
這裡釘住使用者實際回報過的行為：
  1. 打死一隻後要停一下讓它撿東西，不能馬上換下一隻
  2. 已經開打的怪要打到「確認死亡」，不能一從追蹤消失就跑掉
  3. 還沒開打的怪不見了可以馬上換
  4. 漫遊目標要記住，被打怪打斷後回到同一個遠點，不能每次亂挑
  5. 對著過時座標打空氣要 2 秒內察覺，不能站著發呆
  6. 草與 MVP 不打，但菁英怪要打
"""

from __future__ import annotations

import numpy as np

from ro_toolbox.core.ro_packet import RoPacket
from ro_toolbox.services.entities import MemoryEntity
from ro_toolbox.services.farm_bot import (
    _ATTACK_ACK_SEC,
    _LOOT_PAUSE,
    _LOST_GRACE,
    FarmBot,
    _Aim,
)
from ro_toolbox.services.mapdata import MapTerrain

T0 = 1000.0


def bot_with_map() -> FarmBot:
    bot = FarmBot(pid=0)
    types = np.zeros((400, 400), dtype=np.uint32)  # 全圖可走
    bot._terrain = MapTerrain(name="test", width=400, height=400, types=types)
    bot._world.set_map_size((400, 400))
    return bot


def see(bot: FarmBot, gid: int, x: int, y: int, class_id: int = 1052) -> None:
    bot._world.sync_from_memory([MemoryEntity(gid, class_id, x, y, addr=0)])


def kill(bot: FarmBot, gid: int) -> None:
    """餵一個 0x0080 type=1（伺服器權威死亡訊號）。"""
    payload = gid.to_bytes(4, "little") + bytes([1])
    bot._world.feed(
        RoPacket(seq=1, timestamp=0.0, outbound=False, opcode=0x0080, payload=payload)
    )


def test_pauses_after_kill_so_it_can_loot():
    """換怪太快會來不及撿地上的東西 —— 打死後要停 _LOOT_PAUSE 秒。"""
    bot = bot_with_map()
    see(bot, 5, 10, 10)
    bot._update_aim(T0, (10, 10))
    assert bot._aim.gid == 5
    bot._aim.attacked = True

    kill(bot, 5)
    bot._update_aim(T0 + 1, (10, 10))
    assert bot._aim is None
    assert bot._stats.kills == 1

    see(bot, 6, 11, 11)
    bot._update_aim(T0 + 1 + _LOOT_PAUSE / 2, (10, 10))
    assert bot._aim is None, "撿東西的空檔內不應該急著鎖下一隻"

    bot._update_aim(T0 + 1 + _LOOT_PAUSE + 0.01, (10, 10))
    assert bot._aim.gid == 6


def test_keeps_attacking_until_death_confirmed():
    """已經開打的怪從追蹤裡消失，不代表死了（可能只是漏收封包）。"""
    bot = bot_with_map()
    bot._aim = _Aim(gid=5, since=T0, attacked=True)

    bot._update_aim(T0 + 1, (10, 10))
    assert bot._aim is not None, "一消失就換目標＝打一下就跑"
    bot._update_aim(T0 + 1 + _LOST_GRACE / 2, (10, 10))
    assert bot._aim is not None

    bot._update_aim(T0 + 2 + _LOST_GRACE, (10, 10))
    assert bot._aim is None, "寬限過了還沒回來才放棄"


def test_unattacked_target_is_dropped_at_once():
    """還沒開打就不見了，馬上換一隻，不用等寬限。"""
    bot = bot_with_map()
    see(bot, 5, 10, 10)
    bot._update_aim(T0, (10, 10))
    assert bot._aim.gid == 5

    bot._world.forget_far((300, 300), 1)  # 走遠了，怪從追蹤裡消失
    bot._update_aim(T0 + 0.2, (300, 300))
    assert bot._aim is None


def test_attack_sent_once_and_no_move_after():
    """攻擊只送一次；送出後不再送移動（移動會取消連續攻擊）。"""
    sent: list[bytes] = []
    bot = bot_with_map()
    bot._send = sent.append  # 攔下封包，不會真的送出
    see(bot, 5, 10, 10)
    bot._update_aim(T0, (10, 10))

    bot._fight(T0, (10, 10))
    assert len(sent) == 2, "應該是查詢(0x0368) + 攻擊(0x0437)"
    assert int.from_bytes(sent[0][:2], "little") == 0x0368
    assert int.from_bytes(sent[1][:2], "little") == 0x0437

    bot._walker.set_path([(11, 10), (12, 10)])
    bot._fight(T0 + 0.2, (10, 10))
    assert len(sent) == 2, "已經在打了就不該再送任何封包"
    assert not bot._walker.active, "交戰中要停下走路"


def test_roam_goal_survives_interruption():
    """被打怪打斷後要走回同一個遠點，不能每次重挑（那看起來就是亂走）。"""
    bot = bot_with_map()
    bot._roam(T0, (200, 200))
    goal = bot._roam_goal
    assert goal is not None
    assert max(abs(goal[0] - 200), abs(goal[1] - 200)) >= 30, "目標要夠遠"

    bot._walker.clear()  # 模擬中途插隊去打怪
    bot._roam(T0 + 5, (205, 205))
    assert bot._roam_goal == goal
    assert bot._walker.active


def test_roam_picks_new_goal_after_arriving():
    bot = bot_with_map()
    bot._roam(T0, (200, 200))
    goal = bot._roam_goal
    bot._roam(T0 + 30, goal)  # 站在目標上 → arrived
    assert bot._roam_goal != goal


def test_missed_attack_is_detected_fast():
    """對著過時的座標打空氣：2 秒內沒打到任何東西就換人，不要站著發呆。"""
    bot = bot_with_map()
    bot._send = lambda data: None
    see(bot, 5, 10, 10)
    bot._update_aim(T0, (10, 10))
    bot._fight(T0, (10, 10))
    assert bot._aim.attacked

    bot._fight(T0 + _ATTACK_ACK_SEC / 2, (10, 10))
    assert bot._aim is not None, "還在等確認，不該這麼早放棄"

    bot._fight(T0 + _ATTACK_ACK_SEC + 0.1, (10, 10))
    assert bot._aim is None
    assert bot._stats.missed == 1
    assert 5 not in bot._world.monster_gids(), "座標是錯的，要從追蹤裡拿掉"


def test_hit_keeps_the_fight_going():
    """有收到『打到它』的訊號就繼續打，不能誤判成打空氣。"""
    bot = bot_with_map()
    bot._send = lambda data: None
    see(bot, 5, 10, 10)
    bot._update_aim(T0, (10, 10))
    bot._fight(T0, (10, 10))

    bot._world._hits[5] = T0 + 0.3  # 傷害封包進來了
    bot._fight(T0 + _ATTACK_ACK_SEC + 0.1, (10, 10))
    assert bot._aim is not None and bot._aim.gid == 5


def test_plants_and_bosses_are_skipped_but_elites_are_not():
    """草跟 MVP 一樣打得動也會掉東西，但不該打；菁英怪要打。"""
    bot = bot_with_map()
    see(bot, 11, 10, 10, class_id=1080)  # 綠草
    see(bot, 12, 10, 11, class_id=1039)  # 巴風特（MVP）
    assert bot._pick_target((10, 10)) is None

    see(bot, 13, 12, 12, class_id=2741)  # 菁英摩卡
    picked = bot._pick_target((10, 10))
    assert picked is not None and picked.gid == 13


# ---- 遠距攻擊：判定「打到空氣」要把伺服器帶路的時間算進去 -------------------


def test_hit_grace_scales_with_the_distance_we_attacked_from():
    """判定「打到空氣」要把伺服器帶路的時間算進去，但不能無限寬容。

    攻擊封包只帶怪物 GID、不帶座標，最後那一小段由伺服器走。
    固定額度的話，稍遠一點送出的攻擊會在角色還在動時就被判打空氣。
    """
    from ro_toolbox.services import farm_bot as mod

    near = mod._ATTACK_ACK_SEC
    far = mod._ATTACK_ACK_SEC + mod._ATTACK_RANGE * mod._WALK_SEC_PER_CELL
    assert far > near, "距離要真的影響額度，否則等於沒算"
    # 貼身打不到還是要在幾秒內換人，不能因為算了路程就永遠不放棄
    assert far <= mod._GIVE_UP_SEC


def test_far_attack_threshold_is_paired_with_continuous_resends():
    """遠距門檻與「持續發送」是**一組的**，只留一半就會退回罰站症狀。

    只送一次的話，角色跨過門檻那一刻多半還在走，那一擊被伺服器忽略之後
    就沒有第二次機會 —— 站在遠處等到放棄計時器到期（[PKT-065]，使用者實測）。
    所以門檻放遠的前提是：放棄額度內送得出好幾發。
    """
    from ro_toolbox.services import farm_bot as mod

    grace = mod._ATTACK_ACK_SEC + mod._ATTACK_RANGE * mod._WALK_SEC_PER_CELL
    assert grace / mod._ATTACK_RETRY_SEC >= 5, "門檻放遠就要送得夠密，否則等於回到只送一次"
    # 13 格使用者實測「有時打不到」（伺服器帶路會半途停下），退到 10 格。
    assert mod._ATTACK_RANGE <= 12, "太遠伺服器不一定帶到位，會站在半路打空氣"


# ---- 攻擊是「一直送到牠死」，不是送一次 -----------------------------------


def _bot_with_aim(monkeypatch, *, last_hit=0.0, dist=10, away=1):
    """準備一個已經送出攻擊的 bot。不會碰遊戲。

    `away` = 角色現在離怪幾格（拿來確認「距離不再影響要不要送」）。
    """
    from ro_toolbox.services import farm_bot as mod
    from ro_toolbox.services.world import Monster

    bot = FarmBot(1234)
    sent: list[bytes] = []
    monkeypatch.setattr(bot, "_send", sent.append)
    monkeypatch.setattr(bot._world, "last_hit", lambda _gid: last_hit)
    monkeypatch.setattr(bot._world, "get", lambda _gid: Monster(777, 1002, away, 0))
    aim = _Aim(gid=777, since=T0)
    aim.attacked = True
    aim.attacked_at = T0
    aim.sent_at = T0
    aim.attacked_dist = dist
    return bot, aim, sent, mod


def test_attack_keeps_being_sent_even_while_damage_is_landing(monkeypatch):
    """⚠ 這是 2026-08-29 的實驗：正在互打也照樣補送。

    代價是 `0x0437` 的攻速計時器可能被重置（DPS 掉）。
    要是實測「擊殺變少、打空氣沒變多」，這條測試就是要一起改回去的地方。
    """
    bot, aim, sent, mod = _bot_with_aim(monkeypatch, last_hit=T0 + 0.5)
    bot._keep_attacking(aim, T0 + mod._ATTACK_RETRY_SEC + 0.01)
    assert len(sent) == 1, "打到了也要繼續送 —— 這是這次改版的重點"


def test_attack_waits_for_the_interval(monkeypatch):
    """間隔沒到就不要送 —— 不然變成每一拍（0.2 秒）都送。"""
    bot, aim, sent, mod = _bot_with_aim(monkeypatch)
    bot._keep_attacking(aim, T0 + mod._ATTACK_RETRY_SEC - 0.1)
    assert sent == []


def test_attack_fires_once_the_interval_is_up(monkeypatch):
    bot, aim, sent, mod = _bot_with_aim(monkeypatch)
    bot._keep_attacking(aim, T0 + mod._ATTACK_RETRY_SEC + 0.01)
    assert len(sent) == 1
    assert aim.resends == 1
    assert bot._stats.resent == 1


def test_attack_is_sent_even_while_the_server_is_still_walking_us_over(monkeypatch):
    """還在被伺服器帶過去的路上也要送 —— **那正是舊版漏掉的那一擊**。

    舊版要求「已經走到旁邊」或「時間夠走完那段路」才補，遠距門檻下
    等於整段路都不補，於是被忽略的起手沒人接。
    """
    bot, aim, sent, mod = _bot_with_aim(monkeypatch, away=10, dist=10)
    bot._keep_attacking(aim, T0 + mod._ATTACK_RETRY_SEC + 0.01)
    assert len(sent) == 1


def test_giving_up_is_check_hit_s_job_not_the_resend_gate(monkeypatch):
    """額度用完不是靠「停止補送」收尾，是靠 `_check_hit()` 換目標。

    兩邊都管的話會出現「不送了、但目標還鎖著」的安靜狀態。
    """
    bot, aim, sent, mod = _bot_with_aim(monkeypatch, dist=0)
    bot._aim = aim
    bot._keep_attacking(aim, T0 + bot._hit_grace(aim) + 0.1)
    assert len(sent) == 1, "補送本身沒有額度閘門"
    bot._check_hit(aim, T0 + bot._hit_grace(aim) + 0.1)
    assert bot._aim is None, "一筆傷害都沒有，額度用完就該換人"
    assert bot._stats.missed == 1


# ---- 黑名單會被「又看到它」推翻 --------------------------------------------


def test_a_fresh_sighting_lifts_the_miss_blacklist(monkeypatch):
    """拉黑多半是因為座標過時打到空氣。之後又收到那隻怪的實體封包，
    就代表它真的還在、而且我們拿到新座標了 —— 那比黑名單可信。

    不放行的話，附近幾隻怪一被拉黑，畫面上明明有怪、程式卻說「附近沒怪」，
    而且要等 20 秒（使用者實際回報）。
    """
    from ro_toolbox.services.world import Monster

    bot = FarmBot(1234)
    mob = Monster(gid=777, class_id=1002, x=5, y=5)
    monkeypatch.setattr(bot._world, "monsters", lambda: [mob])
    monkeypatch.setattr(bot._world, "get", lambda gid: mob if gid == 777 else None)
    monkeypatch.setattr(bot._world, "nearest", lambda _pos, skip=None: (
        None if 777 in (skip or set()) else mob
    ))

    # 打到空氣 → 拉黑
    bot._skip[777] = T0 + 20
    bot._skip_at[777] = T0
    mob.seen_at = T0 - 1
    assert bot._pick_target((0, 0)) is None, "剛拉黑就該跳過"

    # 又收到它的實體封包（比拉黑那一刻新）→ 黑名單失效
    mob.seen_at = T0 + 1
    assert bot._pick_target((0, 0)) is mob
    assert 777 not in bot._skip, "放行之後要把黑名單清掉，不然下一拍又被擋"


def test_blacklist_still_holds_without_a_new_sighting(monkeypatch):
    """沒有新的目擊就維持拉黑 —— 這道保護不能白白拆掉。"""
    from ro_toolbox.services.world import Monster

    bot = FarmBot(1234)
    mob = Monster(gid=777, class_id=1002, x=5, y=5)
    mob.seen_at = T0 - 5
    monkeypatch.setattr(bot._world, "monsters", lambda: [mob])
    monkeypatch.setattr(bot._world, "get", lambda gid: mob if gid == 777 else None)
    monkeypatch.setattr(bot._world, "nearest", lambda _pos, skip=None: (
        None if 777 in (skip or set()) else mob
    ))
    bot._skip[777] = T0 + 20
    bot._skip_at[777] = T0
    assert bot._pick_target((0, 0)) is None


# ---- 夠不夠近要看實際路徑，不是直線 ----------------------------------------


def _bot_with_terrain(monkeypatch, blocked=None):
    """給一張 60x60 全可走的地形，`blocked` 裡的格子挖掉。"""
    bot = FarmBot(1234)
    side = 60
    types = np.zeros((side, side), np.uint32)
    for x, y in blocked or []:
        types[y, x] = 1
    bot._terrain = MapTerrain(name="t", width=side, height=side, types=types)
    return bot


def test_close_enough_accepts_a_clear_short_hop(monkeypatch):
    bot = _bot_with_terrain(monkeypatch)
    assert bot._close_enough((10, 10), (12, 10), 2) is True


def test_close_enough_rejects_a_monster_behind_a_wall(monkeypatch):
    """直線 2 格，但中間整排是牆 —— 實際要繞一大圈。

    只看直線的話會判成「貼到了」→ 送出攻擊 → 站著打空氣（使用者實測回報）。
    """

    wall = [(11, y) for y in range(0, 60)]      # 一整排牆，只能繞地圖邊
    bot = _bot_with_terrain(monkeypatch, blocked=wall)
    assert bot._close_enough((10, 10), (12, 10), 2) is False


def test_one_rock_in_the_way_is_not_close_enough(monkeypatch):
    """中間**只有一顆石頭**也不算數 —— 條件是「直線上乾淨」，不是「繞得過去」。

    這是舊版（路徑步數 ≤ 直線 + 3）唯一漏掉的形狀：8 方向格子裡斜著閃開
    一格石頭**不會多花步數**，所以「中間有障礙」照樣被判成貼到了。
    """
    bot = _bot_with_terrain(monkeypatch, blocked=[(11, 10)])
    assert bot._close_enough((10, 10), (13, 10), 3) is False


def test_a_detour_is_not_close_enough_either(monkeypatch):
    """繞得過去、但要繞 —— 也不打，先走近。"""
    wall = [(11, y) for y in range(5, 15)]
    bot = _bot_with_terrain(monkeypatch, blocked=wall)
    assert bot._close_enough((10, 10), (12, 10), 2) is False


def test_a_clear_diagonal_line_is_close_enough(monkeypatch):
    """斜的直線也算直線：整條乾淨就可以打。"""
    bot = _bot_with_terrain(monkeypatch)
    assert bot._close_enough((10, 10), (16, 16), 6) is True


def test_a_monster_on_an_unwalkable_cell_uses_the_cell_beside_it(monkeypatch):
    """怪站在不可走的格上（斜坡邊之類）：改看緊鄰牠、離我最近的可走格。"""
    bot = _bot_with_terrain(monkeypatch, blocked=[(14, 10)])
    assert bot._close_enough((10, 10), (14, 10), 4) is True


def test_a_diagonal_that_has_to_squeeze_through_a_corner_is_rejected(monkeypatch):
    """斜線的兩側都是牆＝穿角，實際過不去。規則跟 A* 的不穿角一致。"""
    bot = _bot_with_terrain(monkeypatch, blocked=[(11, 10), (10, 11)])
    assert bot._close_enough((10, 10), (13, 13), 3) is False


def test_no_attack_is_sent_through_a_wall(monkeypatch):
    """牆後面的怪：**不送攻擊，而且當場換一隻**。

    舊版在這裡「算不出路就直接打」，等於隔著牆對空氣送封包。
    後來改成不打、交給 10 秒的放棄計時器 —— 但那 10 秒裡每一拍都在重算
    同一條算不出來的路，看起來就是站在原地發呆（使用者實測回報
    「中間有障礙物比如樹，他會卡住不繞過去」）。現在走不成就當場換目標。
    """
    sent: list[bytes] = []
    bot = _bot_with_terrain(monkeypatch, blocked=[(11, y) for y in range(60)])
    bot._world.set_map_size((60, 60))
    bot._send = sent.append
    see(bot, 5, 13, 10)
    bot._update_aim(T0, (10, 10))
    bot._fight(T0, (10, 10))
    assert sent == [], "隔著牆一個封包都不該送"
    assert bot._aim is None, "繞不過去就換一隻，不要站著重算同一條路"
    assert 5 in bot._skip, "換掉的要進黑名單，免得下一拍又挑到牠"


def test_an_obstacle_it_can_walk_around_is_walked_around(monkeypatch):
    """牆上有缺口就**繞過去**，不是放棄 —— 這才是使用者要的行為。"""
    sent: list[bytes] = []
    wall = [(11, y) for y in range(60) if y != 20]      # y=20 是缺口
    bot = _bot_with_terrain(monkeypatch, blocked=wall)
    bot._world.set_map_size((60, 60))
    bot._send = sent.append
    see(bot, 5, 13, 10)
    bot._update_aim(T0, (10, 10))
    bot._fight(T0, (10, 10))
    assert bot._aim is not None, "繞得過去就不該放棄"
    assert sent, "應該開始走過去"
    assert all(int.from_bytes(p[:2], "little") == 0x035F for p in sent), "只走路，不打"


def test_attack_is_sent_once_the_line_is_clear(monkeypatch):
    """同樣的距離，牆拿掉就要打得出去 —— 別把條件收到連正常情況都不打。"""
    sent: list[bytes] = []
    bot = _bot_with_terrain(monkeypatch)
    bot._world.set_map_size((60, 60))
    bot._send = sent.append
    see(bot, 5, 13, 10)
    bot._update_aim(T0, (10, 10))
    bot._fight(T0, (10, 10))
    assert [int.from_bytes(p[:2], "little") for p in sent] == [0x0368, 0x0437]


def test_line_clear_is_what_answers_the_obstacle_question(monkeypatch):
    """`line_clear()` 自己的單元測試：乾淨、被擋、穿角。"""
    bot = _bot_with_terrain(monkeypatch, blocked=[(11, 10), (10, 11)])
    terrain = bot._terrain
    assert terrain.line_clear((10, 10), (10, 10)) is True     # 原地
    assert terrain.line_clear((20, 10), (20, 20)) is True     # 垂直乾淨
    assert terrain.line_clear((10, 10), (20, 10)) is False    # 水平被石頭擋
    assert terrain.line_clear((10, 10), (13, 13)) is False    # 穿角
    assert terrain.line_clear((20, 20), (26, 23)) is True     # 斜的乾淨


def test_adjacent_never_needs_a_path_check(monkeypatch):
    """貼身就是貼身，不必再花時間算路徑。"""
    bot = _bot_with_terrain(monkeypatch, blocked=[(11, 10)])
    assert bot._close_enough((10, 10), (11, 11), 1) is True


def test_far_targets_are_rejected_before_any_pathfinding(monkeypatch):

    bot = _bot_with_terrain(monkeypatch)
    assert bot._close_enough((10, 10), (40, 40), 30) is False


# ---- 傳點禁區：踩到會被傳到別張地圖 ----------------------------------------


def test_a_monster_standing_in_a_warp_is_not_hunted(monkeypatch):
    """怪站在傳點裡（或旁邊）就不打 —— 追過去會踩到傳點被傳走。

    新地圖可能有打不動的怪，而 bot 會在那裡繼續打（使用者實測回報）。
    """
    from ro_toolbox.services.world import Monster

    bot = FarmBot(1234)
    bot._warp_zone = frozenset({(50, 50), (50, 51), (51, 50)})
    inside = Monster(gid=1, class_id=1002, x=50, y=50)
    outside = Monster(gid=2, class_id=1002, x=80, y=80)
    monkeypatch.setattr(bot._world, "monsters", lambda: [inside, outside])
    monkeypatch.setattr(bot._world, "get", lambda g: inside if g == 1 else outside)
    monkeypatch.setattr(
        bot._world, "nearest",
        lambda _pos, skip=None: next((m for m in (inside, outside)
                                      if m.gid not in (skip or set())), None),
    )
    picked = bot._pick_target((49, 49))
    assert picked is outside, "傳點裡的怪不能挑，就算它比較近"


def test_even_a_monster_hitting_me_is_not_chased_into_a_warp(monkeypatch):
    """被打幾下，好過被傳到不該去的地方。"""
    from ro_toolbox.services.world import Monster

    bot = FarmBot(1234)
    bot._warp_zone = frozenset({(50, 50)})
    attacker = Monster(gid=1, class_id=1002, x=50, y=50)
    monkeypatch.setattr(bot._world, "monsters", lambda: [attacker])
    monkeypatch.setattr(bot._world, "get", lambda _g: attacker)
    monkeypatch.setattr(bot._world, "nearest", lambda _pos, skip=None: None)
    bot._aggro[1] = T0
    assert bot._pick_target((49, 49)) is None


def test_path_planning_routes_around_warps(monkeypatch):
    """A* 要繞開傳點 —— 走過去也會被傳走，不只是站上去。"""
    bot = FarmBot(1234)
    side = 30
    types = np.zeros((side, side), np.uint32)
    bot._terrain = MapTerrain(name="t", width=side, height=side, types=types)
    bot._warp_zone = frozenset({(x, 10) for x in range(0, 29)})   # 橫向一整排傳點
    path = bot._plan_path((5, 5), (5, 20))
    assert path is not None, "還有最右邊那格可以繞過去"
    assert all(cell not in bot._warp_zone for cell in path)


def test_standing_on_a_warp_can_still_walk_out(monkeypatch):
    """站在傳點上時要走得出來 —— 起點不算被擋，不然會把自己關在裡面。"""
    bot = FarmBot(1234)
    side = 30
    types = np.zeros((side, side), np.uint32)
    bot._terrain = MapTerrain(name="t", width=side, height=side, types=types)
    bot._warp_zone = frozenset({(5, 5)})
    path = bot._plan_path((5, 5), (10, 10))
    assert path, "站在傳點上也要算得出離開的路"


def _bot_in_warp_zone(monkeypatch, *, warp=(30, 30)):
    """把角色放在傳點正中央 —— 禁區把它整個包起來的那種情況。"""
    from ro_toolbox.services import farm_bot as mod

    bot = _bot_with_terrain(monkeypatch)
    monkeypatch.setattr(mod, "_warp_cells_of", lambda _m: frozenset({warp}))
    bot._load_warps("t")
    return bot


def test_standing_in_a_warp_walks_out_instead_of_stalling(monkeypatch):
    """⚠ 站在禁區裡要**走出去**，不是站著等到被判定卡住而自動關閉。

    使用者講得很明確：叫你別靠近傳點，不是叫你關掉自動戰鬥。
    禁區是一整片，站在中間時 A* 的每個鄰居都被擋住（起點自己雖然豁免），
    所以非得有一條「只避開傳點本體」的脫離路線不可。
    """
    bot = _bot_in_warp_zone(monkeypatch)
    sent = []
    monkeypatch.setattr(bot, "_send_move", lambda x, y: sent.append((x, y)))
    assert bot._escape_warp((30, 30)) is True
    assert bot._escape_goal is not None
    assert bot._escape_goal not in bot._warp_zone, "目標要在禁區外面"
    assert bot._walker._path, "應該規劃出一條往外走的路"


def test_the_escape_route_never_steps_on_the_warp_itself(monkeypatch):
    """禁區可以借道，傳點**本體**不行 —— 踩到就被傳到別張地圖。"""
    bot = _bot_in_warp_zone(monkeypatch, warp=(30, 30))
    bot._escape_warp((31, 30))
    assert (30, 30) not in bot._walker._path


def test_escape_is_not_replanned_every_tick(monkeypatch):
    """已經在往外走就別重算 —— 每拍一條新路等於狂送走路封包。"""
    bot = _bot_in_warp_zone(monkeypatch)
    assert bot._escape_warp((30, 30)) is True
    first = bot._escape_goal
    assert bot._escape_warp((30, 31)) is True, "還在禁區裡，應該繼續走原本那條"
    assert bot._escape_goal == first


def test_leaving_the_zone_ends_the_escape(monkeypatch):
    """出了禁區就把脫離狀態清掉，回去正常打怪。"""
    bot = _bot_in_warp_zone(monkeypatch)
    bot._escape_warp((30, 30))
    bot._walker.clear()
    assert bot._escape_warp((50, 50)) is False
    assert bot._escape_goal is None


def test_escape_does_nothing_outside_the_zone(monkeypatch):
    bot = _bot_in_warp_zone(monkeypatch)
    assert bot._escape_warp((10, 10)) is False


# ---- 傳點：資料只給取樣點，真正的傳點是一片 --------------------------------


def test_sampled_warp_strip_is_filled_in():
    """`navi_link` 對一條傳點帶只取樣幾個點 —— 實測 moc_fild01 往 moc_fild02
    是 (301,16)/(321,16)/(341,16) **三筆指向同一個目的地格**。
    只擋取樣點周圍 3 格的話，中間留了兩個 14 格寬的洞，走過去照樣被傳走。"""
    from ro_toolbox.services.warpzone import warp_strips as _warp_strips

    strip = _warp_strips({"moc_fild02": [(301, 16), (321, 16), (341, 16)]})
    assert (311, 16) in strip, "取樣點之間那一段也是傳點"
    assert (301, 16) in strip and (341, 16) in strip
    assert (311, 17) not in strip, "只補同一條線，不亂擴張"


def test_two_far_apart_portals_are_not_joined():
    """相隔很遠、剛好通往同一張圖的兩個傳點是**各自獨立**的
    （實測 ayo_dun02 有兩個相隔 252 格的）。連起來會擋掉一整條沒事的路。"""
    from ro_toolbox.services.warpzone import warp_strips as _warp_strips

    assert _warp_strips({"ayo_dun01": [(24, 22), (276, 22)]}) == set()


def test_getting_warped_teaches_the_cell(monkeypatch):
    """地圖名變了就是**真的**被傳走了 —— 那是量到的事實，要學起來。

    資料永遠會有漏網的（傳點是一片、只取樣幾點），所以踩到就記住，
    這次開著的期間都不再走那一段。"""
    bot = FarmBot(1234)
    bot._recent.extend([(10, 10), (12, 10), (14, 10)])
    monkeypatch.setattr(type(bot._walker), "target",
                        property(lambda _self: (20, 10)))
    bot._learn_warp("prt_fild08")
    learned = bot._learned["prt_fild08"]
    assert (14, 10) in learned and (20, 10) in learned
    assert (17, 10) in learned, "中間那段是伺服器走的，也要記"


def test_learned_cells_go_into_the_no_go_zone(monkeypatch):
    from ro_toolbox.services import farm_bot as mod

    bot = FarmBot(1234)
    monkeypatch.setattr(mod, "_warp_cells_of", lambda _m: frozenset())
    bot._learned["prt_fild08"] = {(100, 100)}
    bot._load_warps("prt_fild08")
    assert (100, 100) in bot._warp_cells
    assert (102, 101) in bot._warp_zone, "學到的格子也要有禁區"


# ---- 被傳走 → 走回原本那張圖 ----------------------------------------------


def test_being_warped_starts_a_trip_home():
    """使用者選的是「走回原本那張圖繼續打」。"""
    bot = FarmBot(1234)
    bot._home_map = "prt_fild08"
    assert bot._go_home_start("moc_fild01", T0) is True
    assert bot._traveler is not None
    assert bot._traveler.goal_map == "prt_fild08"
    assert bot._aim is None and bot._roam_goal is None


def test_coming_home_is_not_treated_as_getting_warped():
    bot = FarmBot(1234)
    bot._home_map = "prt_fild08"
    assert bot._go_home_start("prt_fild08", T0) is True
    assert bot._traveler is None


def test_endless_ping_pong_stops_loudly():
    """怪站在傳點上時「追過去被傳走 → 走回來 → 又看到牠」會無限來回。
    學到的禁區通常一次就斷了，這是最後一道保險：停下來喊人。"""
    from ro_toolbox.services.farm_bot import _RETURN_MAX

    bot = FarmBot(1234)
    bot._home_map = "prt_fild08"
    for _ in range(_RETURN_MAX):
        bot._traveler = None
        assert bot._go_home_start("moc_fild01", T0) is True
    bot._traveler = None
    assert bot._go_home_start("moc_fild01", T0) is False
    assert bot._traveler is None
    assert "輪迴" in bot._stats.note


# ---- 負重 90% 就收工（使用者 2026-08-29 指定：回程並關掉，掛機不補給）------


def _weigh(bot, weight: int, max_weight: int) -> None:
    """餵一包 0x00B0（負重只在變動時送，見 [PKT-074]）。"""
    import struct

    from ro_toolbox.services.farm_bot import _OP_PAR_CHANGE, _SP_MAX_WEIGHT, _SP_WEIGHT

    for kind, value in ((_SP_MAX_WEIGHT, max_weight), (_SP_WEIGHT, weight)):
        bot._on_packet(_pkt(_OP_PAR_CHANGE, struct.pack("<HI", kind, value)))


def _pkt(opcode: int, payload: bytes):
    from ro_toolbox.core.ro_packet import RoPacket

    return RoPacket(seq=1, timestamp=0.0, outbound=False, opcode=opcode, payload=payload)


def test_no_weight_packet_means_no_judgement(monkeypatch):
    """負重只在變動時送，剛啟動可能一次都沒看過 —— 那時候不做判斷，不是當成 0。"""
    bot = _bot_with_terrain(monkeypatch)
    assert bot._too_heavy() is False


def test_ninety_percent_is_the_line(monkeypatch):
    bot = _bot_with_terrain(monkeypatch)
    _weigh(bot, 43289, 48100)                 # 89.99%
    assert bot._too_heavy() is False
    _weigh(bot, 43290, 48100)                 # 90.0% —— 含
    assert bot._too_heavy() is True


def test_a_zero_max_weight_is_not_a_division_by_zero(monkeypatch):
    bot = _bot_with_terrain(monkeypatch)
    _weigh(bot, 100, 0)
    assert bot._too_heavy() is False


# ---- 脫離傳點禁區：走不出去要換方向，不能一直撞同一面牆 --------------------


def _warp_bot() -> FarmBot:
    """一隻站在傳點禁區正中央的 bot。"""
    bot = bot_with_map()
    zone = {(x, y)
            for x in range(198, 203)
            for y in range(198, 203)}
    bot._warp_zone = frozenset(zone)
    bot._warp_cells = frozenset({(200, 200)})
    return bot


def test_an_escape_goal_that_cannot_be_reached_is_not_tried_again(monkeypatch):
    """⚠ 使用者實測回報「掛機自己停了」，日誌最後一句是「太靠近傳點，先走開」。

    `_nearest_outside()` 永遠回「最近的那一格」，所以走不成的話下一拍算出來
    還是同一格、同一條路 —— 一直撞同一面牆，直到 45 秒沒進展的保護把自動
    打怪關掉。走不成的目標要記下來，換一個方向。
    """
    bot = _warp_bot()
    pos = (200, 200)

    first = bot._nearest_outside(pos)
    assert first is not None and first not in bot._warp_zone

    # 第一次挑好目標、開始走
    assert bot._escape_warp(pos) is True
    assert bot._escape_goal == first

    # 這一段走不成（伺服器不收／中間有樹）
    monkeypatch.setattr(bot._walker, "update", lambda _p: "blocked")
    bot._escape_warp(pos)
    assert bot._is_bad_goal(first), "走不到的那一格要記起來"

    # 下一次要換一個方向，不能又挑同一格
    monkeypatch.undo()
    again = bot._nearest_outside(pos)
    assert again != first


def test_a_reachable_escape_goal_is_left_alone(monkeypatch):
    """走得成就不要亂記 —— `_bad_goals` 記太多會沒有地方可以去。"""
    bot = _warp_bot()
    pos = (200, 200)
    assert bot._escape_warp(pos) is True
    goal = bot._escape_goal

    monkeypatch.setattr(bot._walker, "update", lambda _p: "walking")
    assert bot._escape_warp(pos) is True
    assert bot._escape_goal == goal
    assert not bot._is_bad_goal(goal)


def test_the_frozen_message_says_what_it_was_doing():
    """只說「毫無進展」的話事後查不出原因 —— 細節都在 DEBUG，
    使用者手上的檔案只有 INFO。"""
    bot = _warp_bot()
    assert bot._doing() == "沒有目標"

    bot._escape_goal = (203, 200)
    assert "傳點禁區" in bot._doing()

    bot._escape_goal = None
    bot._roam_goal = (250, 250)
    assert "漫遊" in bot._doing()


def test_an_escape_that_takes_too_long_changes_direction(monkeypatch):
    """⚠⚠ 實機 2026-08-29：白狐在傳點禁區裡卡了 45 秒，日誌從頭到尾**沒有**
    出現「換一個方向」—— `Walker` 一路回報 "walking"（送得出去、也收得到
    確認），只是人沒有真的前進。於是脫離這件事沒有任何出口，一直到
    「45 秒毫無進展」把自動打怪關掉（使用者：「他在船點前面自己關掉」）。

    所以時間也要看，不能只信走路那一支說的「走不成」。
    """
    from ro_toolbox.services import farm_bot as mod

    bot = _warp_bot()
    pos = (200, 200)
    clock = {"now": 5000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bot._walker, "update", lambda _p: "walking")

    assert bot._escape_warp(pos) is True
    first = bot._escape_goal
    assert first is not None

    # 走路一路說「還在走」，但人沒動
    clock["now"] += mod._ESCAPE_GIVE_UP_SEC / 2
    assert bot._escape_warp(pos) is True
    assert bot._escape_goal == first, "還沒超時就別急著換"

    clock["now"] += mod._ESCAPE_GIVE_UP_SEC
    bot._escape_warp(pos)
    assert bot._is_bad_goal(first), "超時了就要記起來換一個方向"


def test_an_unreachable_escape_goal_is_not_picked_again(monkeypatch):
    """算不出路也是「這一格不行」—— 不記的話每一拍重算同一條算不出來的路。"""
    bot = _warp_bot()
    pos = (200, 200)
    goal = bot._nearest_outside(pos)
    # MapTerrain 是 frozen dataclass，改實例會被擋 —— 改類別。
    monkeypatch.setattr(MapTerrain, "find_path", lambda *a, **k: [])

    assert bot._escape_warp(pos) is False
    assert bot._is_bad_goal(goal)


def test_a_stale_position_is_woken_up_before_anything_else(monkeypatch):
    """⚠⚠ 實機 2026-08-30（白狐走到 mjolnir_07 按自動打怪）：

        00:01:08  太靠近傳點，先走開（往 20,376）
        00:01:32  ⚠ 已經 30 秒找不到角色的移動元件，現在回報的是**進圖座標**

    剛換圖時讀到的是進圖座標，角色跑再遠它都不會變；而移動元件要等角色
    **真的走一步**才找得到 —— 走路又要先知道自己在哪，死結。
    脫離傳點那一段用假座標算，而且它回 True 會把這一拍其他事情全部跳過，
    於是角色一步都沒走、元件永遠找不到。出口是**推一步**。
    """
    from ro_toolbox.services import farm_bot as mod

    bot = _warp_bot()
    clock = {"now": 9000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    bot._reader = type("R", (), {"position_live": False})()
    moves = []
    monkeypatch.setattr(bot, "_send_move", lambda x, y: moves.append((x, y)))

    pos = (200, 200)
    assert bot._wake_position(clock["now"], pos) is False, "座標不可信就不要往下做"
    assert moves == [], "剛換圖的頭幾拍讀不到是正常的，先別急"

    clock["now"] += mod._STALE_POS_SEC + 0.1
    assert bot._wake_position(clock["now"], pos) is False
    assert len(moves) == 1, "撐過去就要推一步把元件逼出來"
    assert moves[0] not in bot._warp_zone, "推的那一步也不准踩進傳點禁區"

    # 節流：不要每拍都推
    clock["now"] += 0.1
    bot._wake_position(clock["now"], pos)
    assert len(moves) == 1


def test_a_live_position_is_left_alone(monkeypatch):
    bot = _warp_bot()
    bot._reader = type("R", (), {"position_live": True})()
    moves = []
    monkeypatch.setattr(bot, "_send_move", lambda x, y: moves.append((x, y)))
    assert bot._wake_position(9000.0, (200, 200)) is True
    assert moves == []


def test_the_wake_up_is_fast_enough_to_not_look_dead():
    """⚠ 使用者實測回報「直接卡死」然後把自動打怪關掉 —— 而日誌顯示它其實
    有在動，只是按下按鈕之後**站著不動 4 秒**（等 3 秒 ＋ 一拍）才推那一步。

    按下按鈕的那一刻座標本來就一定是舊的（角色還沒走過路），等那麼久等於白站。
    """
    from ro_toolbox.services import farm_bot as mod

    assert mod._STALE_POS_SEC <= 1.0, "按下去到動起來不能超過一秒級"
    assert mod._STALE_POS_SEC > 0, "還是要擋一下單次讀取失敗的抖動"
