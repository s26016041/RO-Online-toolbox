"""撿取黑名單：加進去的東西，掛機**永遠**不會去撿。

使用者指定（2026-09-04）：「只要加入黑名單就不會去撿他」、
「這個是永遠開啟的，所以不會有開關」、「同時也要記錄起來」。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from ro_toolbox.services import loot_store
from ro_toolbox.services.farm_bot import FarmBot
from ro_toolbox.services.gamedata import find_items
from ro_toolbox.services.mapdata import MapTerrain
from ro_toolbox.services.world import GroundItem

JELLOPY = 909      # 弄不壞的東西：不想撿的典型
RED = 501          # 紅色藥水


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(loot_store, "user_data_dir", lambda: tmp_path)
    return tmp_path / "loot_blacklist.json"


def drop(bot: FarmBot, entity_id: int, name_id: int, x: int, y: int) -> None:
    """在地上放一個掉落物（不經過封包，直接餵給追蹤器）。"""
    bot._world._items[entity_id] = GroundItem(entity_id, name_id, x, y)


def bot_with_map(blacklist=()) -> FarmBot:
    bot = FarmBot(pid=0, blacklist=blacklist)
    types = np.zeros((400, 400), dtype=np.uint32)  # 全圖可走
    bot._terrain = MapTerrain(name="test", width=400, height=400, types=types)
    bot._world.set_map_size((400, 400))
    return bot


# ---- 存檔 ------------------------------------------------------------------


def test_it_is_shared_by_every_character(store):
    """使用者指定：「全部角色共用，不區分角色，大家都讀同一個」。"""
    loot_store.save([JELLOPY, RED])
    assert loot_store.get() == frozenset({JELLOPY, RED})
    assert json.loads(store.read_text(encoding="utf-8")) == {"items": [RED, JELLOPY]}


def test_no_settings_means_pick_everything_up(store):
    """安全退化的方向只有一個：不知道就照撿。"""
    assert loot_store.get() == frozenset()


def test_a_broken_file_does_not_stop_the_farm(store):
    """壞檔案 = 沒有黑名單，不是「什麼都不撿」——
    後者會讓角色打了一整晚什麼都沒帶回來，而且全程不報錯。"""
    store.write_text("{ 這不是 json", encoding="utf-8")
    assert loot_store.get() == frozenset()


def test_junk_values_are_dropped_but_the_good_ones_stay(store):
    """一個壞值不該整份放棄 —— 頂多多撿到一樣東西。"""
    store.write_text(
        json.dumps({"items": [JELLOPY, "紅色藥水", -1, 0, 999999, True, None]}),
        encoding="utf-8",
    )
    assert loot_store.get() == frozenset({JELLOPY})


def test_the_old_per_character_file_is_carried_over(store):
    """⚠ 舊版是**依角色存**的。改成共用時要把各角色的名單**聯集**接過來 ——
    直接丟掉的話使用者只會發現「設定不見了」，而且不會知道是改版造成的。"""
    store.write_text(
        json.dumps({"商狐": [JELLOPY], "白狐": [RED, JELLOPY]}), encoding="utf-8"
    )
    assert loot_store.get() == frozenset({JELLOPY, RED})


def test_saving_replaces_the_old_format(store):
    """接過來之後就寫成新格式，不要留著兩份會不一致的東西。"""
    store.write_text(json.dumps({"商狐": [JELLOPY]}), encoding="utf-8")
    loot_store.save(loot_store.get() | {RED})
    assert json.loads(store.read_text(encoding="utf-8")) == {"items": [RED, JELLOPY]}
    assert loot_store.get() == frozenset({JELLOPY, RED})


# ---- 搜尋 ------------------------------------------------------------------


def test_searching_finds_items_by_name():
    hits = dict(find_items("紅色藥水"))
    assert hits.get(RED) == "紅色藥水"


def test_searching_by_id_works_too():
    """名字對不上的時候編號是唯一還找得到的路。"""
    assert RED in dict(find_items("501"))


def test_an_empty_search_lists_nothing():
    """⚠ 兩萬多筆全部列出來會把視窗卡死。"""
    assert find_items("") == []
    assert find_items("   ") == []


# ---- 真的不撿 --------------------------------------------------------------


def test_blacklisted_loot_at_our_feet_is_not_picked_up():
    """腳邊的東西平常是**最優先**撿的 —— 黑名單要蓋過這條。"""
    bot = bot_with_map(blacklist=[JELLOPY])
    sent = []
    bot._send = sent.append
    drop(bot, 1, JELLOPY, 100, 100)
    bot._grab_nearby((100, 100))
    assert sent == []
    assert bot._stats.picked == 0


def test_everything_else_is_still_picked_up():
    """黑名單只擋名單裡的那幾樣，其他照撿。"""
    bot = bot_with_map(blacklist=[JELLOPY])
    sent = []
    bot._send = sent.append
    drop(bot, 1, JELLOPY, 100, 100)
    drop(bot, 2, RED, 100, 100)
    bot._grab_nearby((100, 100))
    assert bot._stats.picked == 1
    assert bot.loot() == {RED: 1}


def test_we_do_not_walk_over_to_blacklisted_loot():
    """不只是「走過去不撿」—— 根本不該為了它走過去。

    `_collect` 回 False 才輪得到漫遊；回 True 的話人就站在那裡等一個
    永遠不會撿的東西。
    """
    bot = bot_with_map(blacklist=[JELLOPY])
    bot._send = lambda data: None
    drop(bot, 1, JELLOPY, 108, 108)
    assert bot._collect(1000.0, (100, 100)) is False
    assert bot._walker.goal is None


def test_it_still_walks_over_to_things_we_do_want():
    bot = bot_with_map(blacklist=[JELLOPY])
    bot._send = lambda data: None
    drop(bot, 1, JELLOPY, 108, 108)
    drop(bot, 2, RED, 104, 104)
    assert bot._collect(1000.0, (100, 100)) is True
    assert bot._walker.goal == (104, 104)


def test_changing_the_list_while_it_runs_takes_effect_at_once():
    """改完要立刻算數：黑名單沒有開關可以關掉再打開，
    不立刻生效的話使用者只會看到它繼續撿，以為設定沒存到。"""
    bot = bot_with_map()
    sent = []
    bot._send = sent.append
    bot.set_blacklist([JELLOPY])
    drop(bot, 1, JELLOPY, 100, 100)
    bot._grab_nearby((100, 100))
    assert sent == []
