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
