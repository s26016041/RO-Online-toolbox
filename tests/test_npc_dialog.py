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
    talk = nd.NpcTalk(BOAT_GID, "魔法之都 吉芬", now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, "傳送服務", "結束"))
    assert talk.failed is True, "第一層對不上就該停手（不准亂點）"

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
    """⛔ 這是最重要的一條：對不上就**一個封包都不送**。

    亂點的代價是花掉玩家的錢、或被傳到別的地方 —— 而且是安靜地發生。
    """
    talk = nd.NpcTalk(BOAT_GID, "魔法之都 吉芬", now=Clock())
    _drain(talk)
    talk.feed(nd.ZC_MENU_LIST, _menu(BOAT_GID, "傳送服務", "存放物品", "結束"))
    assert _drain(talk) == []
    assert talk.failed is True
