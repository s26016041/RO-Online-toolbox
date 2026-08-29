"""跟 NPC 商店買東西：版面與算式（不需遊戲，也不會送任何封包）。

樣本是**實機量的**：使用者 2026-08-28 在道具商人那裡手動買 300 瓶藥水
的擷取（GAMEDATA [PKT-074]）。位元組直接抄進來當測試資料 ——
擷取檔本身不進版控（RO 封包是明文），但版面對不對一定要有東西釘住。
"""

from __future__ import annotations

from ro_toolbox.services import shop

#: 實機那一包的商品清單，取前 6 筆重新封成一個合法的封包（去掉 opcode 之後的位元組）
SHOP_LIST_6 = bytes.fromhex(
    "520028000000280000000263020000e8030000e8030000029f5b0000fa000000"
    "fa000000029e5b00000a0000000a00000002f05a0000e8030000e803000002f8"
    "5a000020030000200300000285020000"
)
#: 實機下單那一包（買 300 個道具 502），同樣去掉 opcode
BUY_300 = bytes.fromhex("0a002c01f6010000")


def test_shop_list_matches_the_real_capture():
    """⚠ 道具編號是 **4 位元組**。照舊版寫成 2 位元組會買到別的東西，
    而且看起來像成功 —— 那正是「安靜地做錯事」。"""
    items = shop.parse_shop_list(SHOP_LIST_6)
    assert len(items) == 6
    assert items[0].item_id == 611
    assert items[0].price == 40
    assert [i.item_id for i in items] == [611, 23455, 23454, 23280, 23288, 645]


def test_a_broken_layout_buys_nothing_instead_of_guessing():
    """版面對不上就回空的。買錯東西比不買糟得多。"""
    assert shop.parse_shop_list(SHOP_LIST_6[:-1]) == []
    assert shop.parse_shop_list(b"") == []
    assert shop.parse_shop_list(b"\x05\x00\x01") == []


def test_the_buy_packet_is_byte_for_byte_what_the_client_sent():
    """實機買 300 瓶送出去的就是這串。組錯一個位元組就是買錯數量或買錯東西。"""
    assert shop.buy_packet([(502, 300)]) == b"\xc8\x00" + BUY_300


def test_contact_and_choose_buy_layouts():
    assert shop.contact_npc(0x1F52) == bytes.fromhex("9000521f000001")
    assert shop.choose_buy(0x1F52) == bytes.fromhex("c500521f000000")


def test_buy_result_zero_is_success():
    assert shop.parse_buy_result(b"\x00") == shop.RESULT_OK
    assert shop.parse_buy_result(b"") is None


def test_par_change_reads_weight_and_zeny():
    """實機那一包裡量到的：負重 45304 / 上限 48100 / zeny 3858。

    ⚠ 負重的原始值是**畫面的十倍**（畫面顯示 4530 / 4810）。
    兩種單位混用會算出十倍的購買量。
    """
    assert shop.parse_par_change(0x00B0, bytes.fromhex("180018b00000")) == (24, 45080)
    assert shop.parse_par_change(0x00B0, bytes.fromhex("1900c4bb0000")) == (25, 48068)
    assert shop.parse_par_change(0x00B1, bytes.fromhex("1400120f0000")) == (20, 3858)
    assert shop.parse_par_change(0x0087, b"\x00" * 6) is None
    assert shop.display_weight(45304) == 4530


# ---- 「該買幾個」：買到負重 65%，硬上限 70%（使用者指定）------------------


def test_buys_up_to_the_target_share_of_max_weight():
    """使用者 2026-08-29 指定：現在負重 ＋ 買下去的重量，達到上限的 65% 為止。"""
    # 上限 48100 → 65% = 31265；現在 4260*10 = 42600 已經超過 → 一個都不買
    assert shop.plan_purchase(42600, 48100, 100, 999999, 50).amount == 0
    # 現在 20000，65% 是 31265，還有 11265 的空間，一個 100 → 112 個
    plan = shop.plan_purchase(20000, 48100, 100, 999999, 50)
    assert plan.amount == 112
    assert plan.limited_by == "weight"


def test_seventy_percent_is_a_hard_cap_no_matter_what_ratio_is_passed():
    """⚠ 使用者指定「不可超過 70%」—— 傳再大的 ratio 也不准越過。

    夾不住的話會買到走不動，而且看起來像成功（買賣結果照樣回 0）。
    """
    assert shop.fill_target(48100, 0.9) == shop.fill_target(48100, 0.70) == 33670
    plan = shop.plan_purchase(20000, 48100, 100, 999999, 50, ratio=0.9)
    assert plan.amount == 136                      # (33670 - 20000) // 100
    assert 20000 + plan.amount * 100 <= 48100 * 0.70


def test_buying_that_many_never_lands_over_the_target():
    """買下去之後的負重也要在目標以內（`//` 向下取整，不是四捨五入）。

    ⚠ 起始負重**掃過目標的兩側**：已經超標時正確答案是「買 0 個」，
    那時候當然還是超標的 —— 斷言要放過那一種，不然改個比例就假性失敗。
    """
    target = shop.fill_target(48100)
    for weight in range(0, 48100, 997):
        for unit in (7, 100, 313):
            plan = shop.plan_purchase(weight, 48100, unit, 10**9, 1)
            if weight >= target:
                assert plan.amount == 0, "已經超過目標就一個都不該買"
                continue
            assert weight + plan.amount * unit <= target


def test_running_out_of_money_is_reported_not_hidden():
    """錢不夠要**講出來** —— 使用者要求那時候停掉自動打怪並跳通知。"""
    plan = shop.plan_purchase(20000, 48100, 100, 500, 50)
    assert plan.amount == 10
    assert plan.limited_by == "zeny"


def test_no_quantity_cap_beyond_the_packet_field():
    """使用者指定「沒有數量上限」，唯一的夾限是封包的 u16 欄位。"""
    plan = shop.plan_purchase(0, 100_000_000, 1, 10**12, 1)
    assert plan.amount == 0xFFFF


def test_garbage_inputs_buy_nothing():
    """量不到重量／價錢是 0 就**不買**，不要拿 0 去做除法或猜一個值。"""
    assert shop.plan_purchase(0, 48100, 0, 9999, 50).amount == 0
    assert shop.plan_purchase(0, 48100, 100, 9999, 0).amount == 0
    assert shop.plan_purchase(0, 0, 100, 9999, 50).amount == 0


def test_find_item_never_substitutes():
    items = shop.parse_shop_list(SHOP_LIST_6)
    assert shop.find_item(items, 611) is not None
    assert shop.find_item(items, 502) is None, "清單裡沒有就是沒有，不准挑像的"
