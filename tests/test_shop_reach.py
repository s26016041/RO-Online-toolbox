"""「上次走不到的店排最後」—— 記憶不可以變成「一瓶水都買不到」的原因。

實機 2026-09-01：四次補水**每一次都先挑 prt_in 的道具商人、每一次都走不到**
（室內圖，NPC 傳送的落點跟商人在不同房間），每次白花 1.5~2 分鐘。
`restock_bot` 的 `skip` 只活在那一趟裡，所以下一趟又從頭踩一次。
"""

from __future__ import annotations

from ro_toolbox.services import shop_reach
from ro_toolbox.services.restock_bot import RestockBot


def _use_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(shop_reach, "_path", lambda: tmp_path / "shop_reach.json")


def test_a_shop_we_could_not_reach_is_remembered(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    assert shop_reach.is_bad("prt_in", (126, 76)) is False
    shop_reach.note_bad("prt_in", (126, 76))
    assert shop_reach.is_bad("prt_in", (126, 76)) is True
    # 別家不受影響
    assert shop_reach.is_bad("prt_fild05", (290, 221)) is False


def test_reaching_it_clears_the_record(monkeypatch, tmp_path):
    """走到了就是推翻了 —— 落地點會變，不准永遠記恨。"""
    _use_tmp(monkeypatch, tmp_path)
    shop_reach.note_bad("prt_in", (126, 76))
    shop_reach.note_good("prt_in", (126, 76))
    assert shop_reach.is_bad("prt_in", (126, 76)) is False


def test_the_record_expires(monkeypatch, tmp_path):
    """久了要再給一次機會（改版可能把路打通了）。"""
    _use_tmp(monkeypatch, tmp_path)
    shop_reach.note_bad("prt_in", (126, 76), now=1000.0)
    assert shop_reach.is_bad("prt_in", (126, 76), now=1000.0 + 60) is True
    later = 1000.0 + shop_reach._RETRY_AFTER + 1
    assert shop_reach.is_bad("prt_in", (126, 76), now=later) is False


def test_a_broken_file_is_treated_as_no_memory(monkeypatch, tmp_path):
    """⛔ 一個壞掉的 JSON 不可以擋住補水。"""
    _use_tmp(monkeypatch, tmp_path)
    (tmp_path / "shop_reach.json").write_text("{ 這不是 JSON", encoding="utf-8")
    assert shop_reach.is_bad("prt_in", (126, 76)) is False
    shop_reach.note_bad("prt_in", (126, 76))          # 還要能繼續記
    assert shop_reach.is_bad("prt_in", (126, 76)) is True


def test_the_bot_asks_the_memory_with_the_shop_s_own_cell(monkeypatch, tmp_path):
    """存的是**身分**（地圖＋商人站的格），不是「清單第幾家」。"""
    from ro_toolbox.services import restock_bot as mod

    _use_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "potion_sellers_on",
                        lambda _m: [(126, 76, "道具商人", 83)])
    assert RestockBot._known_bad("prt_in") is False
    shop_reach.note_bad("prt_in", (126, 76))
    assert RestockBot._known_bad("prt_in") is True
    # 資料表改版、商人換了格子 → 舊紀錄自然失效，不會安靜地跳過一家好店
    monkeypatch.setattr(mod, "potion_sellers_on",
                        lambda _m: [(130, 80, "道具商人", 83)])
    assert RestockBot._known_bad("prt_in") is False


def test_a_map_without_sellers_is_never_marked_bad(monkeypatch, tmp_path):
    from ro_toolbox.services import restock_bot as mod

    _use_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "potion_sellers_on", lambda _m: [])
    assert RestockBot._known_bad("mjolnir_07") is False


# ---- 挑店的時候真的有把記憶問過一遍嗎 --------------------------------------


def _fake_world(monkeypatch, sellers_by_map: dict, here: str) -> None:
    """一個只有這幾張圖、這幾個商人的世界（挑店那一段不碰真實地形／導航）。"""
    from types import SimpleNamespace

    from ro_toolbox.services import character as char_mod
    from ro_toolbox.services import restock_bot as mod

    class _Reader:
        def attach(self, _pid) -> bool:
            return True

        def read(self):
            return SimpleNamespace(map_name=here)

        def close(self) -> None: ...

    monkeypatch.setattr(char_mod, "CharacterReader", _Reader)
    monkeypatch.setattr(mod, "potion_sellers_on", lambda m: sellers_by_map.get(m, []))
    monkeypatch.setattr(mod, "maps_with_potion_sellers", lambda: list(sellers_by_map))
    monkeypatch.setattr(mod, "nearest_map_with",
                        lambda _here, maps: (["路線"], sorted(maps)[0]) if maps else None)
    monkeypatch.setattr(mod, "load_terrain", lambda _m: object())
    monkeypatch.setattr(mod, "nearest_walkable", lambda _t, cell: cell)
    # ⚠ 現在走的目標是「商人旁邊那一格」（`cell_beside`，見 [DAT-066]）——
    #   假世界裡沒有地形，直接回商人那一格，測試看的是**挑了誰**不是站哪。
    monkeypatch.setattr(mod, "cell_beside", lambda _t, cell: cell)


def test_the_map_we_are_standing_on_also_asks_the_memory(monkeypatch, tmp_path):
    """★ 舊版只對**別的**地圖查黑名單。

    第一次走失敗會把人丟在 prt_in 裡面，於是重試那一次 `here` 就是 prt_in ——
    「腳下這張圖有商人嗎」直接跳過記憶，又挑了同一家。
    記憶在它專門要解的那個情境裡失效，再白走 1.5~2 分鐘。
    """
    _use_tmp(monkeypatch, tmp_path)
    _fake_world(monkeypatch, {
        "prt_in": [(126, 76, "道具商人", 83)],
        "izlude_in": [(50, 50, "道具商人", 83)],
    }, here="prt_in")
    shop_reach.note_bad("prt_in", (126, 76))

    plan = RestockBot(4242, hp_item=502)._find_shop()
    assert plan is not None and plan[0] == "izlude_in"


def test_a_second_seller_on_the_same_map_still_gets_a_turn(monkeypatch, tmp_path):
    """★ 一張圖可以有兩個藥水商人：裡面那個走不到，門口那個走得到。

    舊版只看 `sellers[0]`，於是整張好圖被寫掉，門口那個永遠沒被試過。
    """
    _use_tmp(monkeypatch, tmp_path)
    _fake_world(monkeypatch, {
        "prt_in": [(126, 76, "裡面那個", 83), (99, 100, "門口那個", 83)],
    }, here="prt_in")
    shop_reach.note_bad("prt_in", (126, 76))

    plan = RestockBot(4242, hp_item=502)._find_shop()
    assert plan is not None and plan[2] == (99, 100)
    assert RestockBot._known_bad("prt_in") is False, "只有一個走不到 ≠ 整張圖不行"


def test_the_memory_never_becomes_the_reason_we_buy_nothing(monkeypatch, tmp_path):
    """⛔ **降級不是刪除**：全部被記成走不到的時候還是要有東西可以試。"""
    _use_tmp(monkeypatch, tmp_path)
    _fake_world(monkeypatch, {"prt_in": [(126, 76, "道具商人", 83)]}, here="prt_in")
    shop_reach.note_bad("prt_in", (126, 76))

    plan = RestockBot(4242, hp_item=502)._find_shop()
    assert plan is not None and plan[2] == (126, 76)


def test_the_memory_is_read_once_per_search(monkeypatch, tmp_path):
    """⚠ 挑店會問每一張有藥水商人的圖 —— 一家一次開檔的話是幾十次磁碟讀取。"""
    _use_tmp(monkeypatch, tmp_path)
    _fake_world(monkeypatch, {
        f"map{i}": [(i, i, "道具商人", 83)] for i in range(12)
    }, here="map0")

    reads = []
    real_load = shop_reach._load
    monkeypatch.setattr(shop_reach, "_load",
                        lambda: reads.append(1) or real_load())
    RestockBot(4242, hp_item=502)._find_shop()
    assert len(reads) == 1


# ---- ⛔ 只准記「真的走不到」（2026-09-03：藥水商人都找錯）-------------------


def test_a_v1_file_is_thrown_away(monkeypatch, tmp_path):
    """★ 舊格式整份丟掉重學。

    v1 是「任何失敗都記一筆」的年代：使用者的檔案裡混著斷線那一拍寫進去的
    假紀錄（三秒鐘記掉三家道具商人），而檔案裡只有時間，分不出哪筆是真的。
    留著等於讓每一台已經裝過的機器繼續挑錯商人一整週。
    """
    _use_tmp(monkeypatch, tmp_path)
    (tmp_path / "shop_reach.json").write_text(
        '{"izlude_in:57,110": 1788403835.35}', encoding="utf-8"
    )
    assert shop_reach.is_bad("izlude_in", (57, 110)) is False
    # 還要能繼續記（新格式）
    shop_reach.note_bad("izlude_in", (57, 110))
    assert shop_reach.is_bad("izlude_in", (57, 110)) is True


def test_a_shop_without_our_potion_is_remembered_per_item(monkeypatch, tmp_path):
    """★ 開了店才知道沒賣 —— 記下來，但**要連道具編號一起記**。

    izlude_in 的「高級藥水商人」就在道具商人旁邊三格，貨架上沒有紅色藥水，
    但每一趟都要走到底、開了店才知道。同一家對紅色藥水沒貨不代表對白色藥水
    也沒貨，所以換一種藥水就當沒記過（安全退化：最多多走一趟）。
    """
    _use_tmp(monkeypatch, tmp_path)
    mem = shop_reach.snapshot()
    assert mem.skip("izlude_in", (59, 113), (501,)) is False

    shop_reach.note_no_stock("izlude_in", (59, 113), [501])
    mem = shop_reach.snapshot()
    assert mem.skip("izlude_in", (59, 113), (501,)) is True
    assert mem.skip("izlude_in", (59, 113), (502,)) is False, "換一種藥水＝沒記過"
    assert mem.is_bad("izlude_in", (59, 113)) is False, "沒賣 ≠ 走不到"
    # 旁邊那個道具商人不受影響
    assert mem.skip("izlude_in", (57, 110), (501,)) is False


def test_only_missing_every_wanted_item_counts(monkeypatch, tmp_path):
    """⚠ 要 `all` 不要 `any`：只有一樣沒貨，另一樣買得到，那還是值得去。"""
    _use_tmp(monkeypatch, tmp_path)
    shop_reach.note_no_stock("prt_in", (126, 76), [501])
    mem = shop_reach.snapshot()
    assert mem.lacks("prt_in", (126, 76), (501, 602)) is False
    assert mem.lacks("prt_in", (126, 76), (501,)) is True
    assert mem.lacks("prt_in", (126, 76), ()) is False, "沒指定要買什麼＝不排除"


def test_buying_it_there_clears_the_no_stock_record(monkeypatch, tmp_path):
    """買到了就是推翻了（改版可能把貨補上了）。"""
    _use_tmp(monkeypatch, tmp_path)
    shop_reach.note_no_stock("prt_in", (126, 76), [501])
    shop_reach.note_good("prt_in", (126, 76), {501: 30})
    assert shop_reach.snapshot().skip("prt_in", (126, 76), (501,)) is False


def test_the_no_stock_record_also_expires(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    shop_reach.note_no_stock("prt_in", (126, 76), [501], now=1000.0)
    later = 1000.0 + shop_reach._RETRY_AFTER + 1
    assert shop_reach.snapshot(1000.0 + 60).skip("prt_in", (126, 76), (501,)) is True
    assert shop_reach.snapshot(later).skip("prt_in", (126, 76), (501,)) is False


def test_a_seller_who_has_no_stock_loses_his_turn(monkeypatch, tmp_path):
    """★ 使用者 2026-09-03：「自動補水 藥水商人都找錯」。

    izlude_in 三個商人裡，道具商人排第一（`potion_sellers_on()` 的順序）。
    高級藥水商人被記成「沒賣 501」之後，挑的還是道具商人；反過來，道具商人
    走不到的時候才輪到高級藥水商人 —— **降級不是刪除**。
    """
    from ro_toolbox.services import restock_bot as mod

    _use_tmp(monkeypatch, tmp_path)
    _fake_world(monkeypatch, {
        "izlude_in": [(57, 110, "道具商人", 47), (59, 113, "高級藥水商人", 558)],
    }, here="izlude_in")

    shop_reach.note_no_stock("izlude_in", (59, 113), [501])
    plan = RestockBot(4242, hp_item=501)._find_shop()
    assert plan is not None and plan[2] == (57, 110)

    # 兩個都被記過 → **降級不是刪除**，還是要有東西可試。退回原本的順序
    # （道具商人在前）：「走不到」是會過期的事實（落地點會變），
    # 「沒賣」是貨架上量到的 —— 賭前者比賭後者划算。
    shop_reach.note_bad("izlude_in", (57, 110))
    plan = RestockBot(4242, hp_item=501)._find_shop()
    assert plan is not None and plan[2] == (57, 110)
    assert mod is not None
