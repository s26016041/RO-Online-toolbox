"""WorldTracker 用**實機抓到的真封包**驗證（不需遊戲）。

下面的 hex 是 2026-08-24 在 moc_fild01 用 Npcap 實際擷取到的位元組
（狐狐狸，怪 GID 3493 = 摩卡 class 1055），不是手編的。
封包版面改掉的話這些測試會直接紅，正好當改版偵測。
"""

from __future__ import annotations

from ro_toolbox.core.ro_packet import RoPacket
from ro_toolbox.core.ro_protocol import pack_position
from ro_toolbox.services.world import WorldTracker


def _hex(text: str) -> bytes:
    """把分組的 hex 字面值轉成 bytes（分組只是為了看得懂，不影響內容）。"""
    return bytes.fromhex("".join(text.split()))


# 站著的怪進入視野（0x09FF）：GID 3493、class 1055、座標 (108,259)
STAND = _hex(
    "ff09580005a50d00 00000000002c0100 000000000000001f 0400000000000000 "
    "0000000000000000 0000000000000000 0000000000000000 000000000000001b "
    "1030000000170000 00ffffffffffffff ff000000bcafa564 "
)

# 同一隻開始移動（0x09FD）：起點一樣是 (108,259)
MOVE = _hex(
    "fd095e0005a50d00 00000000002c0100 000000000000001f 0400000000000000 "
    "0000000000468e26 1d00000000000000 0000000000000000 0000000000000000 "
    "0000001b10319904 78000017000000ff ffffffffffffff00 0000bcafa564 "
)

VANISH_GONE = bytes.fromhex("8000a50d000000")  # type=0：走出視野
VANISH_DEAD = bytes.fromhex("8000a50d000001")  # type=1：死亡
STOPMOVE = bytes.fromhex("88000b516b017f00f700")  # GID 23810315 停在 (127,247)

GID = 3493
CLASS_MUKA = 1055
MAP = (400, 400)


def feed(world: WorldTracker, raw: bytes) -> None:
    """把一整段 TCP 內容餵進去（擷取層只把最前面 2 bytes 當 opcode）。"""
    world.feed(
        RoPacket(
            seq=1,
            timestamp=0.0,
            outbound=False,
            opcode=int.from_bytes(raw[:2], "little"),
            payload=raw[2:],
        )
    )


def test_standing_monster_is_seen():
    """站著不動的怪只會送 0x09FF —— 漏掉這個就是「旁邊有怪卻不打」。"""
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    mob = world.get(GID)
    assert mob is not None
    assert mob.class_id == CLASS_MUKA
    assert mob.pos == (108, 259)


def test_moving_monster_has_same_position():
    """0x09FD 的座標偏移不同（多了 moveStartTime），解出來要對得上 0x09FF。"""
    world = WorldTracker(map_size=MAP)
    feed(world, MOVE)
    assert world.get(GID).pos == (108, 259)


def test_glued_packets_are_all_parsed():
    """伺服器會把好幾包黏在同一段 TCP 送，只看開頭 opcode 會整包漏掉。"""
    world = WorldTracker(map_size=MAP)
    feed(world, STAND + VANISH_GONE)
    assert world.monster_gids() == []  # 出現又消失，兩包都要吃到


def test_vanish_type1_counts_kill():
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    feed(world, VANISH_DEAD)
    assert world.was_killed(GID)
    assert world.kill_count == 1
    assert world.monster_gids() == []


def test_vanish_type0_not_kill():
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    feed(world, VANISH_GONE)
    assert world.kill_count == 0
    assert not world.was_killed(GID)


def test_stopmove_updates_position():
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    stop = bytes.fromhex("8800") + GID.to_bytes(4, "little") + bytes.fromhex("7f00f700")
    feed(world, stop)
    assert world.get(GID).pos == (127, 247)


def test_stopmove_of_unknown_entity_ignored():
    """0x0088 的 GID 不是我們追蹤的怪（例如自己）→ 不該憑空生出一隻怪。"""
    world = WorldTracker(map_size=MAP)
    feed(world, STOPMOVE)
    assert world.monster_gids() == []


def test_unknown_class_id_rejected():
    """class ID 不在怪物表裡 → 不確定是什麼，不當成怪（NPC／傳送點就是這樣擋掉的）。"""
    world = WorldTracker(map_size=MAP)
    fake = bytearray(STAND)
    fake[2 + 21 : 2 + 23] = (60000).to_bytes(2, "little")  # payload[21:23] = class ID
    feed(world, bytes(fake))
    assert world.monster_gids() == []
    assert world.rejected == 1


def test_non_monster_objtype_ignored():
    world = WorldTracker(map_size=MAP)
    fake = bytearray(STAND)
    fake[2 + 2] = 0  # objtype 0 = 其他玩家
    feed(world, bytes(fake))
    assert world.monster_gids() == []


def test_nearest_picks_closest():
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)  # (108,259)
    other = bytearray(STAND)
    other[2 + 3 : 2 + 7] = (4242).to_bytes(4, "little")
    other[2 + 61 : 2 + 64] = pack_position(110, 259)
    feed(world, bytes(other))
    assert world.nearest((111, 259)).gid == 4242
    assert world.nearest((105, 259)).gid == GID


def test_forget_far_drops_ghosts():
    """漏收消失封包會留下幽靈怪；離太遠就要自己丟掉。"""
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    assert world.forget_far((108, 259), 30) == 0
    assert world.forget_far((10, 10), 30) == 1
    assert world.monster_gids() == []


# 實機掉落：0x0ADD 黏在 0x0ACB 後面（偏移 60），角色當時在 (196,179)
DROP_GLUED = _hex(
    "cb0a0100dc050000 00000000cc0a0b51 6b01980100000000 000001000000cb0a "
    "0200231600000000 0000cc0a0b516b01 3801000000000000 02000000dd0a0325 "
    "01009d0300000300 01c300b300030301 00010000 "
)


def test_item_drop_detected_even_when_concatenated():
    """0x0ADD 幾乎總是黏在別的封包後面 —— 只看開頭 opcode 就整個漏掉。"""
    world = WorldTracker(valid_item_ids={925}, map_size=MAP)
    feed(world, DROP_GLUED)

    items = world.ground_items()
    assert len(items) == 1
    assert items[0].entity_id == 75011
    assert items[0].name_id == 925  # 鳥嘴
    assert items[0].pos == (195, 179)  # 就掉在角色旁邊 1 格


def test_invalid_item_id_ignored():
    world = WorldTracker(valid_item_ids={952}, map_size=MAP)
    fake = (
        bytes([0xDD, 0x0A])
        + (12345).to_bytes(4, "little")
        + (60000).to_bytes(2, "little")
        + bytes(8)
    )
    feed(world, bytes([0xCB, 0x0A]) + fake)
    assert world.ground_items() == []


def test_note_monster_adds_attacker():
    """主動怪可能沒被解析到就先打我，靠傷害封包補進來（沒有座標）。"""
    world = WorldTracker(map_size=MAP)
    world.note_monster(777)
    assert world.is_present(777)
    assert world.get(777).pos is None
