"""回程之後自動補藥水的決策（不需遊戲，也不會送任何真的封包）。

釘住使用者指定的四條規則：
  1. 買設定裡的那瓶；**沒設 SP 就完全不碰 SP**
  2. 買到「現在負重 ＋ 買下去的重量」達到上限 80% 為止，沒有數量上限
  3. **錢不夠要講出來**（介面要停掉自動打怪並跳通知）
  4. 單位重量是**量出來的**，不是猜的也不是解說明字串
"""

from __future__ import annotations

import struct

from ro_toolbox.services import shop
from ro_toolbox.services.restock import Restocker, RestockOrder

HP_ITEM = 502
SP_ITEM = 503
LOOK = 88
CELL = (12, 132)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def shop_list(*items: tuple[int, int]) -> bytes:
    """組一個 0x00C6 的 payload（去掉 opcode 之後的位元組）。"""
    body = b"".join(
        struct.pack("<IIBI", price, price, 2, item_id) for item_id, price in items
    )
    return struct.pack("<H", len(body) + 4) + body


def par(kind: int, value: int) -> tuple[int, bytes]:
    return shop.OP_PAR_CHANGE, struct.pack("<HI", kind, value)


def make(order: RestockOrder | None = None):
    sent: list[bytes] = []
    clock = Clock()
    bot = Restocker(sent.append, clock, order or RestockOrder(hp_item=HP_ITEM))
    return bot, sent, clock


def walk_up_to_the_shop_list(bot, sent, *items):
    """把「認人 → 講話 → 選買 → 收到商品清單」跑完。"""
    bot.start(LOOK, CELL)
    bot.note_entity(gid=0x1F52, look=LOOK, x=CELL[0], y=CELL[1])
    bot.update()                                   # 送 0x0090
    bot.feed(shop.OP_DEAL_TYPE, b"\x52\x1f\x00\x00")   # 送 0x00C5
    bot.feed(shop.OP_SHOP_LIST, shop_list(*items))     # 收清單 → 買第一個探路
    return sent


def reopen(bot, *items):
    """一次開店只能下一筆單，所以每一筆之前店都會被重開一次。

    ⚠ 這不是測試的裝飾品：實機第二筆 `0x00C8` 送出去**石沉大海**，
      一路等到逾時（兩次都是）。手上唯一那份真人擷取也只有一個 `0x00C8`。
    """
    assert bot.update() == "working"                   # 送 0x0090
    bot.feed(shop.OP_DEAL_TYPE, b"\x52\x1f\x00\x00")   # 送 0x00C5
    bot.feed(shop.OP_SHOP_LIST, shop_list(*items))     # 收清單 → 下一筆


def test_it_only_talks_to_an_npc_that_matches_look_and_cell():
    """⚠ 認人要外觀編號 ＋ 座標**兩個都對上**，不是猜一個 GID。"""
    bot, sent, _clock = make()
    bot.start(LOOK, CELL)

    bot.note_entity(gid=1, look=LOOK + 1, x=CELL[0], y=CELL[1])   # 外觀不對
    bot.note_entity(gid=2, look=LOOK, x=CELL[0] + 50, y=CELL[1])  # 座標差太遠
    assert bot.update() == "working"
    assert sent == [], "認不出來就不該送任何封包"

    bot.note_entity(gid=0x1F52, look=LOOK, x=CELL[0] + 1, y=CELL[1])
    assert bot.update() == "working"
    assert sent == [shop.contact_npc(0x1F52)]


def test_it_gives_up_loudly_when_the_merchant_never_shows_up():
    bot, sent, clock = make()
    bot.start(LOOK, CELL)
    assert bot.update() == "working"

    clock.now += 999
    assert bot.update() == "blocked"
    assert "認不出商人" in bot.stats.note
    assert sent == []


def test_the_unit_weight_is_measured_not_guessed():
    """⚠ 先買 1 瓶、再買 1 瓶，兩次的負重差就是單位重量。

    道具表的重量只寫在說明文字裡（「重量 : 10」），解字串是 CLAUDE.md 禁止的
    那種很有自信的錯；而負重只在變動時才送過來，剛接上可能一次都沒看過 ——
    兩次探路一次解決兩個未知數。
    """
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    assert sent[-1] == shop.buy_packet([(HP_ITEM, 1)]), "第一次只買 1 個探路"

    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_ZENY, 1_000_000))
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")
    reopen(bot, (HP_ITEM, 50))
    assert sent[-1] == shop.buy_packet([(HP_ITEM, 1)]), "第二次還是 1 個"

    bot.feed(*par(shop.SP_WEIGHT, 20100))          # 一瓶 = 100
    bot.feed(shop.OP_BUY_RESULT, b"\x00")
    reopen(bot, (HP_ITEM, 50))
    # 上限 48100 × 80% = 38480，現在 20100 → 還有 18380 → 183 個
    assert sent[-1] == shop.buy_packet([(HP_ITEM, 183)])


def test_every_order_reopens_the_shop_first():
    """⚠ 一次開店只能下一筆單（實機兩次驗證：第二筆石沉大海）。

    所以每買一次就要重來一輪 0x0090 → 0x00C4 → 0x00C5 → 0x00C6，
    拿到清單才准送下一個 0x00C8。
    """
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    assert sent[-1] == shop.buy_packet([(HP_ITEM, 1)])

    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_ZENY, 1_000_000))
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    before = len(sent)
    bot.feed(shop.OP_BUY_RESULT, b"\x00")
    # ⚠ 收到結果之後**不准**直接再送 0x00C8
    assert len(sent) == before, "第二筆不能直接送，要先重開店"
    assert bot.update() == "working"
    assert sent[-1] == shop.contact_npc(0x1F52), "要重新接觸 NPC"


def test_running_out_of_money_is_reported_for_the_ui_to_act_on():
    """使用者指定：錢不夠就結束自動打怪並跳通知 —— 這裡要把事實交出去。"""
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_ZENY, 600))              # 只買得起 12 個
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")
    reopen(bot, (HP_ITEM, 50))
    bot.feed(*par(shop.SP_WEIGHT, 20100))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")
    reopen(bot, (HP_ITEM, 50))

    assert bot.stats.broke is True
    assert sent[-1] == shop.buy_packet([(HP_ITEM, 12)])


def test_no_sp_potion_configured_means_sp_is_never_touched():
    """使用者指定：沒設定 SP 藥水就不用買 SP。"""
    order = RestockOrder(hp_item=HP_ITEM, sp_item=None)
    assert order.wanted() == [HP_ITEM]

    both = RestockOrder(hp_item=HP_ITEM, sp_item=SP_ITEM)
    assert both.wanted() == [HP_ITEM, SP_ITEM]


def test_an_item_the_shop_does_not_sell_is_skipped_not_substituted():
    """⚠ 清單裡沒有就是沒有 —— 不准挑一個「看起來像」的來買。"""
    bot, sent, _clock = make(RestockOrder(hp_item=HP_ITEM, sp_item=SP_ITEM))
    walk_up_to_the_shop_list(bot, sent, (SP_ITEM, 100))   # 只賣 SP 那瓶

    # HP 那瓶不在清單裡 → 跳過，直接去探 SP 那瓶
    assert sent[-1] == shop.buy_packet([(SP_ITEM, 1)])


def test_nothing_we_want_is_a_loud_stop():
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (611, 40))   # 只賣不相干的東西
    assert bot.update() == "blocked"
    assert "沒有你設定的藥水" in bot.stats.note


def test_a_broken_shop_list_buys_nothing():
    """版面對不上就什麼都不買（`parse_shop_list` 回空的）。"""
    bot, sent, _clock = make()
    bot.start(LOOK, CELL)
    bot.note_entity(gid=1, look=LOOK, x=CELL[0], y=CELL[1])
    bot.update()
    bot.feed(shop.OP_DEAL_TYPE, b"\x01\x00\x00\x00")
    bot.feed(shop.OP_SHOP_LIST, b"\x05\x00\x01")     # 壞掉的清單
    assert bot.update() == "blocked"
    assert shop.buy_packet([(HP_ITEM, 1)]) not in sent


def test_it_will_not_buy_without_knowing_the_weight_limit():
    """讀不到上限或錢就**不敢亂買**（安全退化），不是拿預設值硬算。"""
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")
    reopen(bot, (HP_ITEM, 50))
    bot.feed(*par(shop.SP_WEIGHT, 20100))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")           # 沒有上限也沒有 zeny
    assert bot.update() == "blocked"
    assert "不敢亂買" in bot.stats.note


def test_a_rejected_purchase_stops_instead_of_retrying():
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    bot.feed(shop.OP_BUY_RESULT, b"\x01")           # 被拒絕
    assert bot.update() == "blocked"
    assert "被拒絕" in bot.stats.note


def test_buying_both_potions_reports_the_total():
    bot, sent, _clock = make(RestockOrder(hp_item=HP_ITEM, sp_item=SP_ITEM))
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50), (SP_ITEM, 100))
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_ZENY, 10_000_000))

    catalog = ((HP_ITEM, 50), (SP_ITEM, 100))
    for item, unit in ((HP_ITEM, 100), (SP_ITEM, 50)):
        base = 20000 if item == HP_ITEM else 30000
        bot.feed(*par(shop.SP_WEIGHT, base))
        bot.feed(shop.OP_BUY_RESULT, b"\x00")       # 探路第一瓶
        reopen(bot, *catalog)
        bot.feed(*par(shop.SP_WEIGHT, base + unit))
        bot.feed(shop.OP_BUY_RESULT, b"\x00")       # 探路第二瓶
        reopen(bot, *catalog)
        assert sent[-1].startswith(b"\xc8\x00")
        bot.feed(shop.OP_BUY_RESULT, b"\x00")       # 大單成交
        if item == HP_ITEM:
            reopen(bot, *catalog)                   # 換下一瓶也要重開店

    assert bot.update() == "done"
    assert set(bot.stats.bought) == {HP_ITEM, SP_ITEM}
