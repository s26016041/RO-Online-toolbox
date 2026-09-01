"""回程之後自動補藥水的決策（不需遊戲，也不會送任何真的封包）。

釘住使用者指定的四條規則：
  1. **只補 HP**（回程補給完全不碰 SP，2026-08-29 指定）
  2. 買到「現在負重 ＋ 買下去的重量」達到上限 65% 為止（硬上限 70%），
     沒有數量上限；已經到 65% 就連探路那兩瓶都不買
  3. **錢不夠要講出來**（介面要停掉自動打怪並跳通知）
  4. 單位重量是**量出來的**，不是猜的也不是解說明字串
"""

from __future__ import annotations

import struct

from ro_toolbox.services import restock, shop
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
    """把「認人 → 開店 → 收到商品清單」跑完。

    ⚠ 開店是 `0x0090 接觸 ＋ 0x00C5 選買` **一次送出去**（`shop.open_shop()`）
      —— 分開送的話客戶端那一包 `0x09D4` 會插進中間（[PKT-092]）。
    """
    bot.start(LOOK, CELL)
    bot.note_entity(gid=0x1F52, look=LOOK, x=CELL[0], y=CELL[1])
    bot.update()                                       # 送 0x0090 + 0x00C5
    bot.feed(shop.OP_SHOP_LIST, shop_list(*items))     # 收清單 → 買第一個探路
    return sent


def one_order(item_id: int, amount: int) -> bytes:
    """一筆單長什麼樣：**接觸 ＋ 選買 ＋ 下單，一次送**（[PKT-092]）。"""
    return shop.order_packet(0x1F52, [(item_id, amount)])


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
    assert sent == [shop.open_shop(0x1F52)], "接觸＋選買要一次送"


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
    assert sent[-1] == one_order(HP_ITEM, 1), "第一次只買 1 個探路"

    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_ZENY, 1_000_000))
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")
    assert sent[-1] == one_order(HP_ITEM, 1), "第二次還是 1 個"

    bot.feed(*par(shop.SP_WEIGHT, 20100))          # 一瓶 = 100
    bot.feed(shop.OP_BUY_RESULT, b"\x00")
    # 上限 48100 × 65% = 31265，現在 20100 → 還有 11165 → 111 個
    assert sent[-1] == one_order(HP_ITEM, 111)


def test_every_order_opens_the_shop_itself():
    """⚠ 一次開店只能下一筆單（[PKT-079]，實機兩次驗證：第二筆石沉大海）。

    所以每一筆單都要**自己帶著開店** —— 而且要跟下單黏在同一次送出去，
    不然客戶端那一包 `0x09D4` 會插進中間把訂單吃掉（[PKT-092]）。
    """
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    assert sent[-1] == one_order(HP_ITEM, 1)

    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_ZENY, 1_000_000))
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")

    # 第二瓶探路：一樣是「接觸＋選買＋下單」一包，不是光一個 0x00C8
    assert sent[-1] == one_order(HP_ITEM, 1)
    assert sent[-1].startswith(shop.contact_npc(0x1F52)), "自己帶著開店"


def test_running_out_of_money_is_reported_for_the_ui_to_act_on():
    """使用者指定：錢不夠就結束自動打怪並跳通知 —— 這裡要把事實交出去。"""
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_ZENY, 600))              # 只買得起 12 個
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")
    bot.feed(*par(shop.SP_WEIGHT, 20100))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")

    assert bot.stats.broke is True
    assert sent[-1] == one_order(HP_ITEM, 12)


def test_restock_only_ever_buys_hp():
    """使用者 2026-08-29 指定：回程補給**只補 HP**，連 SP 的欄位都不該存在。

    留一個「預設不填」的 SP 欄位，就是留一條「哪天被填到就會買 SP」的路。
    """
    assert RestockOrder(hp_item=HP_ITEM).wanted() == [HP_ITEM]
    assert RestockOrder().wanted() == []
    assert not hasattr(RestockOrder(hp_item=HP_ITEM), "sp_item")


def test_the_sp_potion_on_the_shelf_is_never_touched():
    """店裡同時賣 SP 藥水也不准去碰它 —— 只補 HP。"""
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (SP_ITEM, 100), (HP_ITEM, 50))
    assert sent[-1] == one_order(HP_ITEM, 1), "第一筆就該是 HP 那瓶"
    assert all(one_order(SP_ITEM, n) not in sent for n in range(1, 300))


def test_an_item_the_shop_does_not_sell_is_skipped_not_substituted():
    """⚠ 清單裡沒有就是沒有 —— 不准挑一個「看起來像」的來買。"""
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (SP_ITEM, 100))   # 只賣別的瓶子

    # 設定的那瓶不在清單裡 → 什麼都不買，不准拿架上那瓶頂替
    assert all(one_order(SP_ITEM, n) not in sent for n in range(1, 300))
    assert bot.update() == "blocked"


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
    bot.feed(shop.OP_SHOP_LIST, b"\x05\x00\x01")     # 壞掉的清單
    assert bot.update() == "blocked"
    assert one_order(HP_ITEM, 1) not in sent


def test_it_will_not_buy_without_knowing_the_weight_limit():
    """讀不到上限或錢就**不敢亂買**（安全退化），不是拿預設值硬算。"""
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")
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


def test_buying_finishes_with_the_total():
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50), (SP_ITEM, 100))
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_ZENY, 10_000_000))

    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(shop.OP_BUY_RESULT, b"\x00")          # 探路第一瓶
    bot.feed(*par(shop.SP_WEIGHT, 20100))           # 一瓶 = 100
    bot.feed(shop.OP_BUY_RESULT, b"\x00")          # 探路第二瓶
    # 上限 48100 × 65% = 31265，現在 20100 → 還有 11165 → 111 個
    assert sent[-1] == one_order(HP_ITEM, 111)
    bot.feed(*par(shop.SP_WEIGHT, 31200))           # 買完之後的負重
    bot.feed(shop.OP_BUY_RESULT, b"\x00")          # 大單成交

    assert bot.update() == "done"
    assert bot.stats.bought == {HP_ITEM: 113}       # 探路 2 ＋ 大單 111
    assert "113" in bot.stats.note


def test_already_heavy_enough_buys_nothing_at_all():
    """⚠ 已經到 65% 就**連探路那兩瓶都不買** —— 探路是為了算「還能買幾個」，
    答案已經是 0 的時候買它就是買過頭，而且會安靜地越過 70%。
    """
    bot, sent, _clock = make()
    bot.start(LOOK, CELL)
    bot.note_entity(gid=0x1F52, look=LOOK, x=CELL[0], y=CELL[1])
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_WEIGHT, 31300))           # 65% 是 31265，已經超過
    bot.update()
    bot.feed(shop.OP_SHOP_LIST, shop_list((HP_ITEM, 50)))

    assert all(not b.startswith(b"\xc8\x00") for b in sent), "一瓶都不准買"
    assert bot.update() == "done", "沒買不是失敗，是不用補"
    assert "不用補" in bot.stats.note


def test_weight_unknown_still_probes():
    """負重只在變動時才送過來（[PKT-074]），沒看過就當**不知道**：照樣探路。

    「讀不到就當滿了」會安靜地什麼都不補，人帶著兩瓶水回去打怪。
    """
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    assert sent[-1] == one_order(HP_ITEM, 1)


# ---- 回程道具：固定補到 20 個（使用者指定）--------------------------------

HOME_ITEM = 602      # 蝴蝶翼


def test_the_return_item_is_bought_to_a_fixed_count():
    """回程道具跟藥水是**兩套規則**：藥水買到負重滿，它固定補到 20 個。"""
    order = restock.RestockOrder(hp_item=HP_ITEM, home_item=HOME_ITEM, home_have=6)
    assert order.home_needed() == restock.HOME_TARGET - 6
    # 回程道具**排在藥水前面** —— 它只要幾個，排後面會被負重額度吃光。
    assert order.wanted() == [HOME_ITEM, HP_ITEM]


def test_enough_return_items_means_it_is_not_even_queued():
    order = restock.RestockOrder(hp_item=HP_ITEM, home_item=HOME_ITEM,
                                 home_have=restock.HOME_TARGET)
    assert order.home_needed() == 0
    assert order.wanted() == [HP_ITEM]


def test_no_return_item_selected_changes_nothing():
    order = restock.RestockOrder(hp_item=HP_ITEM)
    assert order.home_needed() == 0
    assert order.wanted() == [HP_ITEM]


def test_the_return_item_skips_the_weight_probe():
    """固定數量不必探路：要幾個是算出來的，不是量出來的。"""
    bot, sent, _clock = make()
    order = restock.RestockOrder(home_item=HOME_ITEM, home_have=5)
    bot = restock.Restocker(lambda data: sent.append(data), _clock, order)
    bot.start(LOOK, CELL)
    bot.note_entity(gid=0x1F52, look=LOOK, x=CELL[0], y=CELL[1])
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(*par(shop.SP_ZENY, 10_000_000))
    bot.update()
    bot.feed(shop.OP_SHOP_LIST, shop_list((HOME_ITEM, 60)))

    assert sent[-1] == one_order(HOME_ITEM, 15), "一次就買足 20-5=15 個"


def test_a_full_bag_still_buys_the_return_item():
    """負重滿了只擋藥水 —— 沒有回程道具就回不了城，而 20 個蝴蝶翼很輕。"""
    bot, sent, _clock = make()
    order = restock.RestockOrder(hp_item=HP_ITEM, home_item=HOME_ITEM, home_have=0)
    bot = restock.Restocker(lambda data: sent.append(data), _clock, order)
    bot.start(LOOK, CELL)
    bot.note_entity(gid=0x1F52, look=LOOK, x=CELL[0], y=CELL[1])
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_WEIGHT, 47000))          # 早就超過 65%
    bot.feed(*par(shop.SP_ZENY, 10_000_000))
    bot.update()
    bot.feed(shop.OP_SHOP_LIST, shop_list((HP_ITEM, 50), (HOME_ITEM, 60)))

    assert sent[-1] == one_order(HOME_ITEM, restock.HOME_TARGET)


def test_a_shop_without_the_return_item_just_skips_it():
    """商店沒賣就跳過，不是失敗（使用者指定）。"""
    bot, sent, _clock = make()
    order = restock.RestockOrder(hp_item=HP_ITEM, home_item=HOME_ITEM, home_have=0)
    bot = restock.Restocker(lambda data: sent.append(data), _clock, order)
    bot.start(LOOK, CELL)
    bot.note_entity(gid=0x1F52, look=LOOK, x=CELL[0], y=CELL[1])
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(*par(shop.SP_ZENY, 10_000_000))
    bot.update()
    bot.feed(shop.OP_SHOP_LIST, shop_list((HP_ITEM, 50)))    # 只賣藥水

    assert sent[-1] == one_order(HP_ITEM, restock.PROBE_AMOUNT), "跳過它，照樣買藥水"


def test_return_item_purchase_ignores_the_weight_target():
    """回程道具不看負重比例 —— 它是「有沒有」的問題，不是「幾成」的問題。"""
    order = restock.RestockOrder(home_item=HOME_ITEM, home_have=0)
    assert order.home_needed() == restock.HOME_TARGET


# ---- 挑哪一家店（實機踩過：挑到高級藥水商人，那家沒有回程道具）--------------


def test_the_plain_shop_comes_first():
    """名字剛好等於關鍵字的排前面 —— `restock_bot` 拿 `[0]` 當目標。

    izlude_in 有三個：高級藥水商人(558)、忍術道具商人(636)、道具商人(47)。
    使用者要的是**一般**那家（貨架長、有回程道具），不是「高級藥水商人」。
    """
    from ro_toolbox.services.gamedata import potion_sellers_on

    for map_name in ("izlude_in", "prt_in"):
        rows = potion_sellers_on(map_name)
        assert rows, f"{map_name} 應該有藥水商人"
        assert rows[0][2] == "道具商人", f"{map_name} 挑錯家了：{rows[0][2]}"


def test_special_shops_are_still_listed_just_not_first():
    """特殊商人不是丟掉，只是排後面 —— 有些地圖可能只有他們。"""
    from ro_toolbox.services.gamedata import potion_sellers_on

    names = [r[2] for r in potion_sellers_on("izlude_in")]
    assert "高級藥水商人" in names
    assert names.index("道具商人") < names.index("高級藥水商人")


# ---- 買完要把商店關掉（不然角色不能移動）----------------------------------


def test_the_shop_is_closed_when_the_buying_is_done():
    """⚠ 商店對話開著的時候**角色不能移動** —— 買完不關就走不回練功點。

    使用者實測回報。跟 [PKT-075] 的「最後那個『離開』不按掉，傳送永遠不會
    發生」是同一類問題。封包出處：`封包/購買藥水.txt` #29/#30。
    """
    bot, sent, _clock = make()
    bot.start(LOOK, CELL)
    bot.note_entity(gid=0x1F52, look=LOOK, x=CELL[0], y=CELL[1])
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_WEIGHT, 47000))          # 早就超過目標 → 一瓶都不買
    bot.feed(*par(shop.SP_ZENY, 1_000_000))
    bot.update()
    bot.feed(shop.OP_SHOP_LIST, shop_list((HP_ITEM, 50)))

    assert bot.update() == "done"
    assert sent[-1] == shop.close_shop(), "收尾一定要關店"


def test_the_shop_is_closed_even_when_it_fails():
    """失敗的路徑也要關 —— 那時候商店多半也開著。"""
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (611, 40))   # 只賣不相干的東西

    assert bot.update() == "blocked"
    assert sent[-1] == shop.close_shop()


def test_the_close_packet_is_just_the_opcode():
    """`0x09D4` 的 payload 是空的（長度表說 2 = 只有 opcode）。"""
    assert shop.close_shop() == b"\xd4\x09"


def test_a_shop_that_never_opened_is_not_closed():
    """沒開過就不要送關閉 —— 送一包沒必要的封包不是無害的。"""
    bot, sent, clock = make()
    bot.start(LOOK, CELL)
    clock.now += 999
    assert bot.update() == "blocked"
    assert shop.close_shop() not in sent


# ---- 客戶端自己關店造成的時序賽跑（[PKT-092]，2026-09-01 實機證明）--------


def item_added(item_id: int, amount: int, slot: int = 0x1C) -> bytes:
    """組一個 `0x0B41` 的 payload：格號(2) + 數量(2) + 道具編號(4) + 其餘。"""
    return struct.pack("<HHI", slot, amount, item_id) + bytes(58)


def test_an_order_is_one_send_so_nothing_can_get_between():
    """★★ **一筆單 = 接觸 ＋ 選買 ＋ 下單，一次送出去。**

    實機證明（2026-09-01，狐狐狸 @ prt_fild05 道具商人）：在「商品清單」與
    「下單」中間插一個 `0x09D4`，那一筆**完全沒有回應** —— 沒有 `0x0B41`、
    沒有 `0x00CA`，跟使用者回報的「等買賣結果逾時」一模一樣。
    而那一包**不是我們送的**：客戶端會自己關掉它的商店視窗。

    同一次 `send()` 寫進 socket 的位元組是連續的，客戶端插不進來 ——
    分開送 0/3 成交、併成一包 **3/3 成交**。
    ⚠ 所以「只送 `0x00C8`」的單子一個都不准出現。
    """
    bot, sent, _clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))

    assert sent[-1] == (
        shop.contact_npc(0x1F52) + shop.choose_buy(0x1F52)
        + shop.buy_packet([(HP_ITEM, 1)])
    ), "三包要黏在同一次送出去"
    assert all(
        packet != shop.buy_packet([(HP_ITEM, n)]) for packet in sent for n in (1, 111)
    ), "不准有單獨的 0x00C8 —— 那個縫隙就是訂單被吃掉的地方"


def test_a_close_packet_does_not_derail_anything():
    """客戶端隨時會送 `0x09D4`。**現在無所謂**，但不准把流程弄壞。"""
    bot, sent, clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_ZENY, 1_000_000))
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(shop.OP_CLOSE_SHOP, b"")              # 客戶端關掉它的視窗
    bot.feed(shop.OP_BUY_RESULT, b"\x00")

    assert bot.stats.bought == {HP_ITEM: 1}
    assert sent[-1] == one_order(HP_ITEM, 1), "下一筆照樣送得出去"
    assert bot.update() == "working"


def test_an_order_the_server_swallows_is_ordered_again():
    """訂單被安靜地丟掉 → **重下一次**，不是整趟放棄。

    使用者實機（2026-09-01 14:09）：買到蝴蝶翅膀 1 個之後，藥水那一筆
    石沉大海，舊版直接停用 —— 於是角色一瓶水都沒有，回頭又被判定成
    「沒水要補給」，一分鐘來一次，整晚都在原地。
    """
    bot, sent, clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    assert sent[-1] == one_order(HP_ITEM, 1)
    before = len(sent)

    clock.now += restock.STEP_TIMEOUT + 1
    assert bot.update() == "working", "沒回應不准整趟放棄"
    assert len(sent) == before + 1, "同一筆要重下"
    assert sent[-1] == one_order(HP_ITEM, 1)


def test_it_gives_up_loudly_after_the_retries_run_out():
    """重試不是無限的：試完還是沒回應就**大聲**停下來。"""
    bot, sent, clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    for _ in range(restock.ORDER_RETRIES):
        clock.now += restock.STEP_TIMEOUT + 1
        assert bot.update() == "working"
    clock.now += restock.STEP_TIMEOUT + 1
    assert bot.update() == "blocked"
    assert "沒有任何回應" in bot.stats.note


def test_goods_in_the_bag_beat_a_missing_result():
    """`0x00CA` 沒來、但 `0x0B41` 說東西進背包了 → **照成交算**。

    逾時只是放棄的上限，不是「什麼都沒發生」的證據。重下一次會多買一份，
    所以要先看答案卡（哪個道具、進來幾個），不要憑逾時下結論。
    """
    bot, sent, clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    bot.feed(*par(shop.SP_MAX_WEIGHT, 48100))
    bot.feed(*par(shop.SP_ZENY, 1_000_000))
    bot.feed(*par(shop.SP_WEIGHT, 20000))
    bot.feed(shop.OP_ITEM_ADDED, item_added(HP_ITEM, 1))   # 東西真的進來了

    clock.now += restock.STEP_TIMEOUT + 1
    assert bot.update() == "working"
    assert bot.stats.bought == {HP_ITEM: 1}, "進背包了就是買到了"


def test_an_item_added_for_something_else_is_not_our_order():
    """別的道具進背包不算這一筆成交 —— 不准亂認。"""
    bot, sent, clock = make()
    walk_up_to_the_shop_list(bot, sent, (HP_ITEM, 50))
    bot.feed(shop.OP_ITEM_ADDED, item_added(SP_ITEM, 3))

    clock.now += restock.STEP_TIMEOUT + 1
    bot.update()
    assert bot.stats.bought == {}, "不是我們訂的那個道具"
