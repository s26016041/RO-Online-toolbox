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


def test_attack_range_stays_close_enough_to_actually_walk_there():
    """⛔ 放寬到 13 格試過，失敗了（GAMEDATA [PKT-065]）。

    `_fight()` 一旦送出攻擊就不再走路（移動會取消連續攻擊，[PKT-034]），
    所以門檻多遠，角色就會在多遠的地方站著等。單獨實驗裡「站穩後遠距攻擊」
    伺服器會接手帶路，但 bot 多半是**正在走**的時候跨過門檻，
    攻擊在移動中送達就被忽略 —— 症狀是原地罰站（使用者實測回報）。
    """
    from ro_toolbox.services import farm_bot as mod

    assert mod._ATTACK_RANGE <= 4, "太遠就會變成站在遠處等，不會走過去"
    assert mod._ATTACK_RANGE >= 2, "留一點餘裕給『讀座標』與『怪又走一步』的落差"


# ---- 補送攻擊：只在「一筆傷害都還沒收到」時補 -----------------------------


def _bot_with_aim(monkeypatch, *, last_hit=0.0, dist=10, near=True):
    """準備一個已經送出攻擊、但還沒確認打到的 bot。不會碰遊戲。

    `near` = 角色已經走到怪旁邊了（補送的前提之一）。
    """
    from ro_toolbox.services import farm_bot as mod
    from ro_toolbox.services.world import Monster

    bot = FarmBot(1234)
    sent: list[bytes] = []
    monkeypatch.setattr(bot, "_send", sent.append)
    monkeypatch.setattr(bot._world, "last_hit", lambda _gid: last_hit)
    away = 1 if near else mod._RESEND_NEAR + 5
    monkeypatch.setattr(bot._world, "get", lambda _gid: Monster(777, 1002, away, 0))
    aim = _Aim(gid=777, since=T0)
    aim.attacked = True
    aim.attacked_at = T0
    aim.sent_at = T0
    aim.attacked_dist = dist
    return bot, aim, sent, mod


def test_resend_is_skipped_once_we_are_actually_hitting_it(monkeypatch):
    """傷害封包一直進來就代表攻擊生效了 —— 重送會把攻速計時器重置，DPS 反而掉。"""
    bot, aim, sent, mod = _bot_with_aim(monkeypatch, last_hit=T0 + 0.5)
    bot._resend_attack(aim, T0 + 10.0, (0, 0))
    assert sent == [], "已經打到了還補送，等於自己打斷自己"


def test_resend_waits_for_the_interval(monkeypatch):
    bot, aim, sent, mod = _bot_with_aim(monkeypatch)
    bot._resend_attack(aim, T0 + mod._ATTACK_RETRY_SEC - 0.1, (0, 0))
    assert sent == []


def test_resend_fires_when_nothing_has_been_hit(monkeypatch):
    """攻擊石沉大海（伺服器沒接、怪剛好走掉）—— 補一次比乾等到放棄划算。"""
    bot, aim, sent, mod = _bot_with_aim(monkeypatch)
    bot._resend_attack(aim, T0 + mod._ATTACK_RETRY_SEC, (0, 0))
    assert len(sent) == 1
    assert aim.resends == 1


def test_resend_holds_off_while_the_server_is_still_walking_us_over(monkeypatch):
    """攻擊可以從 13 格外送出，伺服器要先把角色帶過去（約 2 秒）。

    還在路上就補送＝自己打斷自己的起手。實測 1 秒間隔、不看距離：
    100 秒補送 17 次，擊殺反而比 2 秒那組略低。
    """
    bot, aim, sent, mod = _bot_with_aim(monkeypatch, near=False, dist=20)
    # 20 格 × 0.15 秒 = 3 秒的路；才過 2 秒，伺服器還在帶
    bot._resend_attack(aim, T0 + mod._ATTACK_RETRY_SEC, (0, 0))
    assert sent == [], "還在路上就補送，會打斷起手"

    # 路走完了卻還是一筆傷害都沒有 —— 這一擊真的沒生效，補一次
    bot._resend_attack(aim, T0 + 20 * mod._WALK_SEC_PER_CELL + 0.1, (0, 0))
    assert len(sent) == 1


def test_resend_stops_once_the_give_up_budget_is_spent(monkeypatch):
    """補送也要在放棄額度內 —— 額度用完就該換人，不是無限補。"""
    bot, aim, sent, mod = _bot_with_aim(monkeypatch, dist=0)
    bot._resend_attack(aim, T0 + bot._hit_grace(aim) + 0.1, (0, 0))
    assert sent == []


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
    from ro_toolbox.services import farm_bot as mod

    wall = [(11, y) for y in range(0, 60)]      # 一整排牆，只能繞地圖邊
    bot = _bot_with_terrain(monkeypatch, blocked=wall)
    assert bot._close_enough((10, 10), (12, 10), 2) is False


def test_adjacent_never_needs_a_path_check(monkeypatch):
    """貼身就是貼身，不必再花時間算路徑。"""
    bot = _bot_with_terrain(monkeypatch, blocked=[(11, 10)])
    assert bot._close_enough((10, 10), (11, 11), 1) is True


def test_far_targets_are_rejected_before_any_pathfinding(monkeypatch):
    from ro_toolbox.services import farm_bot as mod

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
