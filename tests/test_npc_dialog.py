"""跟 NPC 對話的封包與選項比對。

測資是**實機擷取的真位元組**（`封包/跟船員說話傳送到柏伊亞嵐島.txt`，
2026-08-27，依斯魯得島的船員送去柏伊亞嵐島），不是我編的。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import npc_dialog as nd

# --- 實機位元組（去掉每筆前面的長度欄之後就是 payload）---------------------

BOAT_GID = 91

#: ↓ 0x00B7 選單。長度(2)=0x003D + GID(4)=91 + cp950 文字
MENU = bytes.fromhex(
    "3D005B000000AC66A5ECA8C8B450AE71202D3E2031353020AAF7B9F43A"
    "A6E3BAB8A8A9B6F020B4E4A4662D3E20353030AAF7B9F43AB5B2A7F43A00"
)

#: ↓ 0x00B4 對話：`[船員]`
SAY = bytes.fromhex("0F005B0000005BB2EEADFB5D00")

WAIT = bytes.fromhex("5B000000")


def test_menu_decodes_the_real_capture():
    """真的封包解出來要是那三個選項（結尾那個空的不算）。"""
    got = nd.parse_menu(MENU)
    assert got is not None
    gid, options = got
    assert gid == BOAT_GID
    assert options == ["柏伊亞嵐島 -> 150 金幣", "艾爾貝塔 港口-> 500金幣", "結束"]


def test_trailing_empty_option_is_dropped():
    """⚠ 選單字串以 `:` 收尾，結尾那個空的不是選項 —— 算進去編號會整個錯掉。"""
    _gid, options = nd.parse_menu(MENU)
    assert "" not in options


def test_say_and_wait_decode():
    assert nd.parse_say(SAY) == (BOAT_GID, "[船員]")
    assert nd.parse_wait(WAIT) == BOAT_GID


# ---- 送出的封包 ----------------------------------------------------------


def test_contact_matches_the_real_bytes():
    """實機送出的是 90 00 5B 00 00 00 01。"""
    assert nd.build_contact(BOAT_GID).hex() == "90005b00000001"


def test_next_matches_the_real_bytes():
    assert nd.build_next(BOAT_GID).hex() == "b9005b000000"


def test_choose_matches_the_real_bytes():
    """實機選的是第 1 項：b8 00 5b 00 00 00 01。"""
    assert nd.build_choose(BOAT_GID, 1).hex() == "b8005b00000001"


@pytest.mark.parametrize("bad", [0, -1, 255, 300])
def test_choose_refuses_impossible_numbers(bad):
    with pytest.raises(ValueError):
        nd.build_choose(BOAT_GID, bad)


# ---- 挑選項：**只准比對文字，不准猜編號** --------------------------------


def test_picks_the_option_that_matches_the_destination():
    _gid, options = nd.parse_menu(MENU)
    index, why = nd.pick_option(options, "柏伊亞嵐島")
    assert index == 1, why


def test_prefix_in_our_name_does_not_break_the_match():
    """我們的表寫「港都 艾爾貝塔」，選單寫「艾爾貝塔 港口」——
    取主名（去前綴、去空白）才對得上。"""
    _gid, options = nd.parse_menu(MENU)
    index, why = nd.pick_option(options, "港都 艾爾貝塔")
    assert index == 2, why


def test_no_match_refuses_instead_of_guessing():
    """⚠ 選單裡沒有那個地方就**不准選** —— 猜錯是把人傳到別的島。"""
    _gid, options = nd.parse_menu(MENU)
    index, why = nd.pick_option(options, "魔法之都 吉芬")
    assert index is None
    assert "吉芬" in why


def test_ambiguous_menu_refuses_too():
    """對到兩個以上也不准賭，一樣大聲拒絕。"""
    index, why = nd.pick_option(["去吉芬 100z", "去吉芬 200z", "結束"], "吉芬")
    assert index is None
    assert "分不出來" in why


def test_empty_name_refuses():
    index, _why = nd.pick_option(["柏伊亞嵐島"], "   ")
    assert index is None


def test_core_name_strips_the_prefix():
    assert nd.core_name("港都 艾爾貝塔") == "艾爾貝塔"
    assert nd.core_name("衛星都市 依斯魯得島") == "依斯魯得島"
    assert nd.core_name("柏伊亞嵐島") == "柏伊亞嵐島"


def test_cost_is_surfaced_so_nobody_pays_by_surprise():
    """要錢的選項要講出來 —— 安靜地花掉玩家的錢是最糟的做法。"""
    _gid, options = nd.parse_menu(MENU)
    assert "150" in nd.cost_of(options[0])
    assert "500" in nd.cost_of(options[1])
    assert nd.cost_of("結束") == ""


# ---- 壞資料一律安全退化 --------------------------------------------------


@pytest.mark.parametrize("junk", [b"", b"\x01", b"\x05\x00\x00\x00\x00\x00"])
def test_broken_packets_return_none(junk):
    assert nd.parse_menu(junk) is None


def test_menu_with_only_empties_is_not_a_menu():
    payload = b"\x0a\x00" + (7).to_bytes(4, "little") + b":::\x00"
    assert nd.parse_menu(payload) is None


# ---- 對話狀態機 ----------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _drain(talk):
    out = []
    while (data := talk.next_packet()) is not None:
        out.append(data)
    return out


def test_full_conversation_matches_the_real_capture():
    """整條走完，送出去的封包要跟實機那三筆一模一樣。"""
    talk = nd.NpcTalk(BOAT_GID, "柏伊亞嵐島", now=Clock())
    assert [p.hex() for p in _drain(talk)] == ["90005b00000001"]   # 接觸

    talk.feed(nd.ZC_SAY_DIALOG, SAY)
    talk.feed(nd.ZC_WAIT_DIALOG, WAIT)
    assert [p.hex() for p in _drain(talk)] == ["b9005b000000"]     # 下一步

    talk.feed(nd.ZC_MENU_LIST, MENU)
    assert [p.hex() for p in _drain(talk)] == ["b8005b00000001"]   # 選第 1 項
    assert talk.done is True and talk.failed is False
    assert "150" in talk.cost, "要付的錢要講出來"


def test_it_refuses_a_menu_it_cannot_match():
    """⚠ 看不懂就**什麼都不送** —— 猜錯是把人傳到別的島。"""
    talk = nd.NpcTalk(BOAT_GID, "魔法之都 吉芬", now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, MENU)
    assert _drain(talk) == [], "看不懂還送封包是最糟的做法"
    assert talk.failed is True
    assert "吉芬" in talk.note


def test_packets_for_another_npc_are_ignored():
    """別隻 NPC 的對話不能拿來當自己的 —— 附近有別人在講話很正常。"""
    talk = nd.NpcTalk(999, "柏伊亞嵐島", now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_WAIT_DIALOG, WAIT)      # 這是 GID 91 的
    talk.feed(nd.ZC_MENU_LIST, MENU)
    assert _drain(talk) == []
    assert talk.done is False


def test_silence_gives_up_loudly():
    """沒有回應要放棄並說出來，不准無限掛著。"""
    clock = Clock()
    talk = nd.NpcTalk(BOAT_GID, "柏伊亞嵐島", now=clock)
    _drain(talk)
    clock.now += nd.NpcTalk.TIMEOUT + 1
    assert _drain(talk) == []
    assert talk.failed is True
    assert "沒有回應" in talk.note


def test_nothing_more_is_sent_after_it_failed():
    talk = nd.NpcTalk(BOAT_GID, "不存在的地方", now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, MENU)
    talk.feed(nd.ZC_WAIT_DIALOG, WAIT)      # 失敗之後再餵也不該有動作
    assert _drain(talk) == []


# ---- 實體封包：認出「哪一隻是 NPC」---------------------------------------
#
# 版面與 objtype 的值來自實機登入擷取（`封包/全整登入.txt`，2026-08-27）：
#   objtype 0 = 其他玩家（GID 兩千多萬、外觀是職業編號）
#   objtype 6 = NPC（GID 只有兩三位數）
# 進圖時伺服器會把那張圖的實體**全部**送一次 —— 所以只要跟 NPC 講話前有過圖，
# GID 一定拿得到；站在他旁邊才按按鈕才會漏掉（那一包早就送完了）。


def test_entity_layout_matches_the_login_capture():
    from ro_toolbox.core.ro_protocol import unpack_position
    from ro_toolbox.services import travel_bot as mod

    # 實機那 23 筆裡的一筆 NPC：objtype=6、外觀=105、GID=170、(29,200)
    payload = bytearray(70)
    payload[0:2] = (70).to_bytes(2, "little")
    payload[mod._ENT_OBJTYPE] = 6
    payload[mod._ENT_GID:mod._ENT_GID + 4] = (170).to_bytes(4, "little")
    payload[mod._ENT_CLASS:mod._ENT_CLASS + 2] = (105).to_bytes(2, "little")
    # 3-byte 打包（unpack_position 的反運算）：x 佔 10 bit、y 佔 10 bit、方向 4 bit
    x, y = 29, 200
    payload[mod._ENT_POS:mod._ENT_POS + 3] = bytes([
        x >> 2, ((x & 0x03) << 6) | ((y >> 4) & 0x3F), (y & 0x0F) << 4,
    ])

    assert payload[mod._ENT_OBJTYPE] == mod._OBJTYPE_NPC
    assert int.from_bytes(payload[mod._ENT_GID:mod._ENT_GID + 4], "little") == 170
    assert int.from_bytes(payload[mod._ENT_CLASS:mod._ENT_CLASS + 2], "little") == 105
    x, y, _d = unpack_position(bytes(payload[mod._ENT_POS:mod._ENT_POS + 3]))
    assert (x, y) == (29, 200)


# ---- 多層選單（卡普拉那種：先選傳送服務、再選城市）------------------------


def _menu(gid: int, *options: str) -> bytes:
    """組一個 0x00B7 選單封包（跟實機同版面：長度 + GID + cp950，`:` 分隔）。"""
    text = ":".join(options).encode("cp950") + b"\x00"
    return (len(text) + 6).to_bytes(2, "little") + gid.to_bytes(4, "little") + text


def test_a_second_menu_is_still_listened_to():
    """⚠ 選完**不能關耳朵** —— 卡普拉的第二層在第一層之後才來。"""
    talk2 = nd.NpcTalk(BOAT_GID, "魔法之都 吉芬", now=Clock())
    _drain(talk2)
    talk2.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, "普隆德拉", "吉芬 1200z", "結束"))
    assert [p.hex() for p in _drain(talk2)] == ["b8005b00000002"]
    # 第二層又來一個（例如確認畫面）：還要收得到，不能因為已經選過就不理
    talk2.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, "吉芬", "取消"))
    assert [p.hex() for p in _drain(talk2)] == ["b8005b00000001"]


def test_it_stops_after_too_many_menus():
    """選單繞圈圈時要停手 —— 不要一直亂點別人的 NPC。"""
    clock = Clock()
    talk = nd.NpcTalk(BOAT_GID, "吉芬", now=clock)
    _drain(talk)
    for _ in range(nd.MAX_MENUS + 1):
        talk.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, "吉芬", "結束"))
        _drain(talk)
    assert talk.failed is True
    assert "層" in talk.note


def test_it_never_clicks_an_option_it_cannot_match():
    """⛔ 這是最重要的一條：對不上、又不在白名單裡，就**一個封包都不送**。

    亂點的代價是花掉玩家的錢、或改掉他的存檔點 —— 而且是安靜地發生。
    """
    talk = nd.NpcTalk(BOAT_GID, "魔法之都 吉芬", now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, "記憶點", "倉庫服務", "結束"))
    assert _drain(talk) == [], "記憶點會改掉玩家的重生點，絕對不能點"
    assert talk.failed is True


# ---- 卡普拉：兩層選單（測資是實機 `封包/卡普拉傳送到吉芬.txt`）------------

KAFRA_GID = 145

#: 第一層：記憶點 / 倉庫服務 / 傳送服務 / 手推車服務 / 查詢其他資訊 / 結束
KAFRA_MENU1 = bytes.fromhex(
    "3F0091000000B04FBED0C2493AADDCAE77AA41B0C83AB6C7B065AA41"
    "B0C83AA4E2B1C0A8AEAA41B0C83AAC64B8DFA8E4A54CB8EAB0543AB5"
    "B2A7F43A00"
)
#: 第二層：吉芬 / 斐揚 / 夢羅克 / 艾爾帕蘭 / 取消
KAFRA_MENU2 = bytes.fromhex(
    "5C0091000000A64EAAE220202020202020202D3E20313230207A3AB4"
    "B4B4AD20202020202D3E20313230207A3AB9DAC3B9A74A2020202020"
    "2D3E20313230207A3AA6E3BAB8A9ACC4F520202D3E20313830207A3A"
    "A8FAAEF83A00"
)


def test_the_two_kafra_menus_decode():
    assert nd.parse_menu(KAFRA_MENU1)[1] == [
        "記憶點", "倉庫服務", "傳送服務", "手推車服務", "查詢其他資訊", "結束",
    ]
    assert nd.parse_menu(KAFRA_MENU2)[1][0].startswith("吉芬")


def test_kafra_two_level_warp_end_to_end():
    """第一層沒有目的地 → 點「傳送服務」；第二層才選吉芬。"""
    talk = nd.NpcTalk(KAFRA_GID, "魔法之都 吉芬", npc="卡普拉職員", now=Clock())
    _drain(talk)                                   # 接觸
    talk.feed(nd.ZC_MENU_LIST, KAFRA_MENU1)
    assert [p.hex() for p in _drain(talk)] == ["b80091000000" + "03"], "第 3 項＝傳送服務"
    talk.feed(nd.ZC_MENU_LIST, KAFRA_MENU2)
    assert [p.hex() for p in _drain(talk)] == ["b80091000000" + "01"], "第 1 項＝吉芬"
    assert talk.done is True
    assert "120" in talk.cost


def test_the_note_says_who_where_and_how_much():
    """付錢是這趟路本來就要的花費，不是警告 —— 講清楚「找誰、去哪、多少」。"""
    talk = nd.NpcTalk(KAFRA_GID, "魔法之都 吉芬", npc="卡普拉職員", now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, KAFRA_MENU1)
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, KAFRA_MENU2)
    _drain(talk)
    assert "卡普拉職員" in talk.note
    assert "魔法之都 吉芬" in talk.note
    assert "120" in talk.note


def test_the_whitelist_never_contains_the_dangerous_ones():
    """⛔ 白名單是這整套的安全根據 —— 這幾個永遠不准進去。"""
    for danger in ("記憶點", "倉庫服務", "手推車服務", "結束", "取消"):
        assert not any(danger in key for key in nd.SUBMENU_OPTIONS), danger


def test_a_kafra_menu_without_our_destination_stops(caplog):
    """第二層沒有我們要去的城市 → 停手。已經點開子選單無所謂，那不花錢。"""
    talk = nd.NpcTalk(KAFRA_GID, "首都 普隆德拉", npc="卡普拉職員", now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, KAFRA_MENU1)
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, KAFRA_MENU2)
    assert _drain(talk) == []
    assert talk.failed is True


# ---- 名字對不起來：兩邊都可能比較長 --------------------------------------
#
# 使用者實測炸過：我們的表把 prontera 叫「盧恩 米德加茲王國  首都普隆德拉」，
# 取空格後的主名是「首都普隆德拉」，而卡普拉的選單寫「普隆德拉 -> 120 z」——
# 多了「首都」兩個字（中間沒空格），所以怎麼比都對不上。


@pytest.mark.parametrize(
    ("ours", "options", "want"),
    [
        # 我們的比較長（主名還黏著「首都」）—— 實機那一份
        ("盧恩 米德加茲王國  首都普隆德拉",
         ["普隆德拉 -> 120 z", "艾爾帕蘭 -> 120 z", "獸人洞窟 -> 170 z", "取消"], 1),
        # 選單的比較長（多了「港口」）
        ("港都 艾爾貝塔",
         ["柏伊亞嵐島 -> 150 金幣", "艾爾貝塔 港口-> 500金幣", "結束"], 2),
        # 一模一樣
        ("柏伊亞嵐島",
         ["柏伊亞嵐島 -> 150 金幣", "艾爾貝塔 港口-> 500金幣", "結束"], 1),
        # 我們有前綴、選單沒有
        ("魔法之都 吉芬",
         ["吉芬        -> 120 z", "斐揚     -> 120 z", "取消"], 1),
        ("運河之都 艾爾帕蘭",
         ["普隆德拉 -> 120 z", "艾爾帕蘭 -> 120 z", "取消"], 2),
        ("獸人村",
         ["普隆德拉 -> 120 z", "艾爾帕蘭 -> 120 z", "獸人村 -> 170 z", "取消"], 3),
    ],
)
def test_real_menus_all_match(ours, options, want):
    index, why = nd.pick_option(options, ours)
    assert index == want, why


def test_an_exact_match_beats_a_longer_lookalike():
    """⚠ 選單同時有「吉芬」跟「吉芬地城」時要挑**完全相同**的那個。"""
    index, why = nd.pick_option(
        ["吉芬地城 -> 400 z", "吉芬 -> 120 z", "取消"], "魔法之都 吉芬"
    )
    assert index == 2, why


def test_two_lookalikes_with_no_exact_match_stop():
    """兩個都只是「包含」而且沒有完全相同的 —— 分不出來就不賭。"""
    index, why = nd.pick_option(
        ["吉芬地城 -> 400 z", "吉芬野外 -> 120 z", "取消"], "魔法之都 吉芬"
    )
    assert index is None
    assert "分不出來" in why


def test_place_of_strips_the_price():
    assert nd.place_of("普隆德拉 -> 120 z") == "普隆德拉"
    assert nd.place_of("艾爾貝塔 港口-> 500金幣") == "艾爾貝塔港口"
    assert nd.place_of("取消") == "取消"


# ---- 只通一個地方的 NPC：選單是「確定嗎」，不是「選去哪」------------------
#
# 使用者實測回報（2026-08-27）：只能傳去依斯魯得島的那隻，選單是
# ['使用', '結束'] —— 沒有地名可以比對，因為根本沒得選。


def test_a_sole_destination_npc_can_be_confirmed():
    talk = nd.NpcTalk(BOAT_GID, "衛星都市 依斯魯得島", npc="船員",
                      sole=True, now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, "使用", "結束"))
    assert [p.hex() for p in _drain(talk)] == ["b8005b00000001"]
    assert talk.done is True and talk.failed is False


def test_the_same_menu_is_refused_when_the_npc_has_several_destinations():
    """⚠ 有好幾個目的地卻跳「確定嗎」—— 代表我們看漏了什麼，停手。"""
    talk = nd.NpcTalk(BOAT_GID, "衛星都市 依斯魯得島", npc="船員",
                      sole=False, now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, "使用", "結束"))
    assert _drain(talk) == []
    assert talk.failed is True


def test_confirm_never_picks_the_way_out():
    """⛔ 結束／取消永遠不准被選到 —— 那等於自己把對話關掉還以為成功了。"""
    for danger in ("結束", "取消"):
        assert danger not in nd.CONFIRM_OPTIONS
    index, _why = nd.pick_confirm(["結束", "取消"])
    assert index is None


def test_a_real_destination_still_wins_over_confirm():
    """選單裡有地名時走原本那條路，不要被確認白名單搶走。"""
    talk = nd.NpcTalk(KAFRA_GID, "魔法之都 吉芬", npc="卡普拉職員",
                      sole=True, now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, KAFRA_MENU2)
    assert [p.hex() for p in _drain(talk)] == ["b80091000000" + "01"]


@pytest.mark.parametrize("options", [
    ["使用", "結束"],        # 實機：只通依斯魯得島的那隻
    ["回去", "結束"],        # 實機：另一隻，同樣只通一個地方
    ["出發吧", "取消"],      # 沒見過的說法 —— 排除法一樣接得住
])
def test_any_two_option_confirm_works_when_there_is_only_one_destination(options):
    """⚠ 不要每遇到一個新的確認詞就回來加白名單。

    只通一個地方、而且只剩一個「不是離開」的選項時，那個必然就是「做這件事」。
    """
    talk = nd.NpcTalk(BOAT_GID, "衛星都市 依斯魯得島", npc="船員",
                      sole=True, now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, *options))
    assert [p.hex() for p in _drain(talk)] == ["b8005b00000001"], options


def test_several_unknown_options_still_stop():
    """剩兩個以上不知道是什麼的選項 —— 分不出來就不賭。

    （認得出來的確認詞另當別論：`使用` 就算旁邊還有別的選項也選得下去，
    那是白名單，不是排除法。）
    """
    talk = nd.NpcTalk(BOAT_GID, "衛星都市 依斯魯得島", npc="船員",
                      sole=True, now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, "購買", "說明", "結束"))
    assert _drain(talk) == []
    assert talk.failed is True


def test_a_known_confirm_word_wins_even_with_other_options():
    talk = nd.NpcTalk(BOAT_GID, "衛星都市 依斯魯得島", npc="船員",
                      sole=True, now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, "說明", "使用", "結束"))
    assert [p.hex() for p in _drain(talk)] == ["b8005b00000002"]


def test_a_menu_of_only_exits_picks_nothing():
    index, _why = nd.pick_confirm(["結束", "取消"])
    assert index is None
