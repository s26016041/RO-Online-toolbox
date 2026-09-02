"""「按一下補水」那條路：**認得出商人**才開得了店。

⚠ 這一整支測的是同一件事：實體（NPC）只在**進入視野**時送一次封包
（[PKT-061]）。第二趟補水出發時人**已經站在商人旁邊**，走那 5 格不會有任何人
重新進視野 —— 使用者實機 2026-09-01 連續兩趟都停在
「⚠ 走到了卻認不出商人（外觀 83 @ (290, 221)）」，一瓶藥水都沒補到。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import restock_bot as mod
from ro_toolbox.services.restock_bot import RestockBot, forget_npcs, remember_npcs

PID = 4242
MAP = "prt_fild05"
LOOK = 83
SELLER = (290, 221)
DOOR = (285, 221)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """⚠ **不准碰使用者真實的 `shop_reach.json`。**

    `_find_shop()` 會問「這家店上次走得到嗎」，而那份記憶存在
    `%APPDATA%\\RO-Online-toolbox\\` 底下 —— 不導到暫存目錄的話，測試會照
    開發機當下的紀錄走不同的分支（本機綠、CI 紅，或反過來），
    而且還會把測試用的假地圖寫進使用者的檔案。
    """
    from ro_toolbox.services import shop_reach

    monkeypatch.setattr(shop_reach, "_path", lambda: tmp_path / "shop_reach.json")
    forget_npcs(PID)
    yield
    forget_npcs(PID)


def test_what_we_saw_on_the_first_trip_is_still_known_on_the_second():
    """★ 第一趟走過來時看到的 GID，第二趟要**還記得**。

    第二趟根本走不出新東西（人就站在旁邊），記憶是唯一的來源。
    """
    remember_npcs(PID, MAP, {8016: (LOOK, 290, 221)})
    assert remember_npcs(PID, MAP, {}) == {8016: (LOOK, 290, 221)}

    # 換一張圖、換一個行程都是另一份記憶 —— GID 是伺服器給的，不能亂借。
    assert remember_npcs(PID, "prontera", {}) == {}
    assert remember_npcs(PID + 1, MAP, {}) == {}


def test_a_dead_process_forgets_its_npcs():
    """⚠ PID 會被 Windows 回收給別的視窗。行程沒了就要忘掉，不然會拿到
    上一隻的 GID —— 那是「存身分」也擋不住的，因為身分本身過期了。"""
    remember_npcs(PID, MAP, {8016: (LOOK, 290, 221)})
    forget_npcs(PID)
    assert remember_npcs(PID, MAP, {}) == {}


def test_recognising_needs_both_the_look_and_the_cell():
    """認人要**外觀 ＋ 座標兩個都對上**（[DAT-027]），不是挑一個像的。"""
    assert RestockBot._can_see({1: (LOOK, 290, 221)}, SELLER, LOOK) is True
    assert RestockBot._can_see({1: (LOOK, 291, 222)}, SELLER, LOOK) is True, "差一格算"
    assert RestockBot._can_see({1: (LOOK + 1, 290, 221)}, SELLER, LOOK) is False
    assert RestockBot._can_see({1: (LOOK, 250, 221)}, SELLER, LOOK) is False
    assert RestockBot._can_see({}, SELLER, LOOK) is False


def test_the_remembered_gid_saves_a_trip(monkeypatch):
    """記憶裡有他就**不必走遠再走回來** —— 那一趟要一二十秒。"""
    bot = RestockBot(PID, hp_item=502)
    remember_npcs(PID, MAP, {8016: (LOOK, 290, 221)})
    monkeypatch.setattr(RestockBot, "_walk",
                        lambda *a, **k: pytest.fail("不該再走"))

    known = bot._make_sure_he_is_visible(MAP, DOOR, SELLER, LOOK, "道具商人", {})
    assert 8016 in known


def test_it_walks_out_of_view_and_back_when_nobody_knows_him(monkeypatch):
    """★ 誰都不認得他 → **走遠再走回來**，逼伺服器重送一次進視野的封包。

    舊版在這裡直接回報「走到了卻認不出商人」然後整趟放棄 ——
    而那是**站得越近越容易發生**的失敗（走的距離越短，越沒有人進視野）。
    """
    walks: list[tuple] = []

    def fake_walk(self, where, cell):
        walks.append((where, cell))
        # 走遠那一趟什麼都沒看到；走回來的那一趟他進視野了。
        return {} if len(walks) == 1 else {8016: (LOOK, 290, 221)}

    monkeypatch.setattr(RestockBot, "_walk", fake_walk)
    bot = RestockBot(PID, hp_item=502)
    known = bot._make_sure_he_is_visible(MAP, DOOR, SELLER, LOOK, "道具商人", {})

    assert len(walks) == 2, "一趟走遠、一趟走回來"
    away = walks[0][1]
    assert max(abs(away[0] - SELLER[0]), abs(away[1] - SELLER[1])) >= mod._OUT_OF_VIEW, (
        "要真的走出視野，不然他不會重新進視野"
    )
    assert walks[1][1] == DOOR, "再走回商人腳邊"
    assert 8016 in known


def test_it_does_not_shake_for_ever(monkeypatch):
    """搖不出來也要收手 —— 下一段會大聲說「認不出商人」。"""
    walks = []
    monkeypatch.setattr(RestockBot, "_walk",
                        lambda self, where, cell: walks.append(cell) or {})
    bot = RestockBot(PID, hp_item=502)
    known = bot._make_sure_he_is_visible(MAP, DOOR, SELLER, LOOK, "道具商人", {})

    assert known == {}
    assert len(walks) == mod._SHAKE_ROUNDS * 2, "每輪兩趟，做完就停"


# ---- 「出發前在哪張圖」只准記一次（2026-09-01：補水後角色會亂走）------------


class _Reader:
    """假的角色讀取：每次 `read()` 回下一張圖（模擬換一家店之後人已經移動了）。"""

    def __init__(self, maps: list[str]) -> None:
        self._maps = list(maps)

    def attach(self, _pid) -> bool:
        return True

    def read(self):
        from types import SimpleNamespace

        name = self._maps.pop(0) if len(self._maps) > 1 else self._maps[0]
        return SimpleNamespace(map_name=name)

    def close(self) -> None: ...


def test_home_is_captured_once_not_on_every_retry(monkeypatch):
    """★ 使用者：「補水後角色會亂走」。

    `_find_shop()` 在「走不到就換一家」的迴圈裡會被叫好幾次，而 `here` 是
    **現在**站的地方 —— 第一家走不到的話人已經在城裡了，於是「家」就變成
    城裡那張圖。實機日誌：

        18:30 從 prt_fild04 出發 → 兩家店都走不到
        18:32 補水：買完了，**走回 普隆德拉內部**…     ← 回到城裡的房間
        19:31 補水：買完了，**走回 普隆德拉內部**…     ← 下一趟又從那裡出發

    回到城裡之後自動打怪照樣接回去 → 角色在城裡漫遊，而且會一直滾下去。
    """
    from ro_toolbox.services import character as char_mod

    monkeypatch.setattr(char_mod, "CharacterReader",
                        lambda: _Reader(["mjolnir_07", "prt_in", "prt_mk"]))
    bot = RestockBot(PID, hp_item=502)
    bot._find_shop()
    assert bot.stats.home_map == "mjolnir_07"
    bot._find_shop({("prt_in", (126, 76))})          # 第二家：人已經在城裡了
    bot._find_shop({("prt_in", (126, 76)), ("prt_mk", (0, 0))})
    assert bot.stats.home_map == "mjolnir_07", "家只能是出發前那張圖"


def test_it_walks_home_even_when_every_shop_was_unreachable(monkeypatch):
    """★ 每一家都走不到 → 舊版直接 return，角色就**整晚站在城裡**。

    `came_back` 是 False，所以補給那條不接掛機；`_watch_farm_alive()` 又因為
    「人不在練功地圖上」不敢開；而自動那一趟不跳框 —— 早上起來才看得到。
    買不買得到都要走回去（走不到的店已經記進 `shop_reach`，下一趟先挑別家）。
    """
    plan = ("prt_in", (79, 110), (126, 76), LOOK, "道具商人")
    monkeypatch.setattr(RestockBot, "_find_shop", lambda self, tried=None: plan)
    monkeypatch.setattr(RestockBot, "_walk", lambda self, where, cell: None)
    monkeypatch.setattr(RestockBot, "_buy",
                        lambda *a, **k: pytest.fail("沒走到就不該開店"))
    went: list[str] = []
    monkeypatch.setattr(RestockBot, "_go_back",
                        lambda self: went.append(self.stats.home_map))

    bot = RestockBot(PID, hp_item=502, back_to="mjolnir_07")
    bot.stats.home_map = "mjolnir_07"
    bot._run()

    assert went == ["mjolnir_07"], "買不到也要走回去"
    assert bot.stats.done is False, "沒買到就不准說成功"


def test_the_caller_can_pin_home_explicitly(monkeypatch):
    """自動補給會把練功地圖交進來 —— 那個永遠優先於「現在站哪」。"""
    from ro_toolbox.services import character as char_mod

    monkeypatch.setattr(char_mod, "CharacterReader",
                        lambda: _Reader(["prt_in"]))
    bot = RestockBot(PID, hp_item=502, back_to="mjolnir_07")
    bot._find_shop()
    assert bot.stats.home_map == "mjolnir_07"
