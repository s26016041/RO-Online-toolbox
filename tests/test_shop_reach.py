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
