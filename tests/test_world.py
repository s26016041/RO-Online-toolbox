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


def test_moving_monster_records_where_it_is_heading():
    """0x09FD 帶的是 **6 bytes 的「從哪走到哪」**，要記**終點**不是起點。

    以前只解前 3 bytes（起點），記到的永遠是它**開始走之前**的格子。
    實測（prt_fild07，16 個樣本）平均落後 4.2 格、最多 7 格，而 `_ATTACK_RANGE`
    以前只有 2 格 —— 症狀就是「追蹤到怪物移動前位置」「打到空氣」
    「挑到的最近其實不是最近」。

    這份 fixture 自己就是證據：同一隻怪的站立封包說它在 (108,259)，
    而移動封包的**起點**解出來剛好也是 (108,259)，終點是 (102,260)。
    """
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    assert world.get(GID).pos == (108, 259)      # 站著的時候在這裡
    feed(world, MOVE)
    assert world.get(GID).pos == (102, 260)      # 開始走了 → 記它要去的地方


def test_move_packet_is_ignored_when_it_is_too_short():
    """解 6 bytes 就要有 6 bytes。長度不夠寧可整包丟掉，不要拿半截去解。"""
    world = WorldTracker(map_size=MAP)
    feed(world, MOVE[:-4])
    assert world.get(GID) is None


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


def test_a_monster_that_hits_me_comes_back_even_after_we_gave_up_on_it():
    """**被它打到就是它還在那裡的證據**，比我們自己的判斷可信。

    打到空氣時 `forget()` 會把那隻怪放進「已消失」。以前那也會擋住
    `note_monster()`，於是它站在旁邊砍你卻補不回追蹤 ——
    `get(gid)` 永遠是 None，「打我的怪優先」直接跳過它。
    症狀就是「怪物打我但我卻不理他」（使用者實測回報）。
    """
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    assert world.get(GID) is not None

    world.forget(GID)                 # 判定打到空氣，先當它不在
    assert world.get(GID) is None

    world.note_monster(GID)           # 它打了我一下
    assert world.get(GID) is not None, "被它打到還不把它補回來，就會一直不理它"


def test_a_confirmed_kill_is_not_resurrected_by_a_stray_damage_packet():
    """只有**確認擊殺**擋得住 —— 死掉的不會打人，那種封包是雜訊。"""
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    feed(world, bytes.fromhex("8000") + GID.to_bytes(4, "little") + bytes([1]))
    assert world.was_killed(GID)

    world.note_monster(GID)
    assert world.get(GID) is None, "已確認死亡的不該復活"


# ---- 記憶體是主要來源：連刪除也交給它 --------------------------------------


class _Ent:
    """假的 MemoryEntity。"""

    def __init__(self, gid, class_id=1055, x=108, y=259):
        self.gid, self.class_id, self.x, self.y = gid, class_id, x, y


def test_memory_adds_monsters_the_packets_never_announced():
    """站著不動的怪只在「進入視野」時送一次封包 —— bot 啟動前就站在那裡的
    那些，封包這條路**永遠**看不到（RO 沒有「請給我周圍有什麼」的查詢）。"""
    world = WorldTracker(map_size=MAP)
    assert world.monster_gids() == []
    world.sync_from_memory([_Ent(999)])
    assert world.get(999).pos == (108, 259)


def test_memory_removes_a_monster_only_after_repeated_misses():
    """⚠ 掃描偶爾會整批回 0（實測量到「封包看到 11 隻、記憶體同時回 0」）。
    一次抖動就清空會把整片真的怪弄不見 —— 要連續幾次都沒看到才刪。"""
    world = WorldTracker(map_size=MAP)
    world.sync_from_memory([_Ent(999)])

    for _ in range(2):                      # 前兩次沒看到：先記著，不刪
        assert world.sync_from_memory([], pos=(108, 259), view=30, strikes=3) == 0
        assert world.is_present(999)
    assert world.sync_from_memory([], pos=(108, 259), view=30, strikes=3) == 1
    assert not world.is_present(999)


def test_a_single_blank_scan_does_not_wipe_everything():
    """抖動之後又看到了 → 計數要歸零，不能累積到把它刪掉。"""
    world = WorldTracker(map_size=MAP)
    world.sync_from_memory([_Ent(999)])
    world.sync_from_memory([], pos=(108, 259), view=30, strikes=3)      # 抖一下
    world.sync_from_memory([_Ent(999)], pos=(108, 259), view=30)        # 又看到
    world.sync_from_memory([], pos=(108, 259), view=30, strikes=3)      # 再抖一下
    assert world.is_present(999), "中間看到過就該重新計數"


def test_monsters_out_of_scan_range_are_not_removed():
    """記憶體只掃視野內。視野外看不到是正常的，不能因此刪掉。"""
    world = WorldTracker(map_size=MAP)
    world.sync_from_memory([_Ent(999)])
    for _ in range(5):
        world.sync_from_memory([], pos=(300, 300), view=30, strikes=3)
    assert world.is_present(999)


def test_a_monster_that_hit_me_is_never_removed_by_memory():
    """座標不明的怪（傷害封包補進來的）算不出距離 ——
    而且「它剛剛打到我」本身就是它存在的證據，不該被記憶體掃描刪掉。"""
    world = WorldTracker(map_size=MAP)
    world.note_monster(777)
    for _ in range(5):
        world.sync_from_memory([], pos=(108, 259), view=30, strikes=3)
    assert world.is_present(777)


# ---- 確認擊殺會過期：伺服器會重用 GID --------------------------------------


def test_a_reused_gid_is_visible_again_after_the_protection_window(monkeypatch):
    """**伺服器會重用 GID。** 永久記住「這隻死了」的話，同一個 GID 的新怪
    會被永遠當成死人 —— 怪站在旁邊打你、bot 說附近沒怪，**重開才會好**
    （因為 WorldTracker 是新的）。使用者實測回報。
    """
    from ro_toolbox.services import world as mod

    clock = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])

    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    feed(world, VANISH_DEAD)
    assert world.was_killed(GID)
    assert world.kill_count == 1

    feed(world, STAND)                       # 保護期內：不准復活
    assert world.get(GID) is None

    clock[0] += mod.KILL_PROTECT_SEC + 1     # 保護期過了
    feed(world, STAND)
    assert world.get(GID) is not None, "GID 被重用時要看得到那隻新的怪"
    assert not world.was_killed(GID)
    assert world.kill_count == 1, "重新看到不該再算一次擊殺"


def test_kill_confirmation_still_works_right_after_the_kill(monkeypatch):
    """保護期是為了讓『剛送出的那次擊殺確認』不被同一拍的殘留封包蓋掉。"""
    from ro_toolbox.services import world as mod

    clock = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    feed(world, VANISH_DEAD)
    clock[0] += mod.KILL_PROTECT_SEC - 0.5
    assert world.was_killed(GID), "保護期內一定要還認得出剛剛那次擊殺"


# ---- 座標以記憶體為準（[MEM-058]，使用者要求）--------------------------------


def test_memory_position_wins_over_a_move_packet():
    """`0x09FD` 帶的是**終點**，怪還在半路上；記憶體讀到的是牠現在那一格。

    兩個來源打架時要聽記憶體的 —— 不然走過去打的是牠等一下才會到的位置。
    """
    world = WorldTracker(map_size=MAP)
    world.sync_from_memory([_Ent(GID, x=100, y=100)])
    feed(world, MOVE)
    assert world.get(GID).pos == (100, 100), "封包不准蓋掉剛讀到的記憶體座標"


def test_packet_position_is_used_once_memory_goes_quiet():
    """記憶體看不到牠了（超過 MEMORY_TRUST_SEC）就換封包說了算 ——
    有個舊一點的座標，還是比完全沒有座標好。"""
    from ro_toolbox.services import world as mod

    world = WorldTracker(map_size=MAP)
    world.sync_from_memory([_Ent(GID, x=100, y=100)])
    world.get(GID).mem_at -= mod.MEMORY_TRUST_SEC + 1
    feed(world, MOVE)
    assert world.get(GID).pos != (100, 100)


def test_memory_only_deletes_what_memory_has_seen():
    """⚠ 背景掃描要輪過整份記憶體才會發現新配置的實體（幾秒），這段期間
    封包已經看到牠了 —— 這時候刪掉牠等於「因為我還沒找到，所以牠不存在」。"""
    world = WorldTracker(map_size=MAP)
    feed(world, STAND)
    here = world.get(GID).pos
    for _ in range(10):
        world.sync_from_memory([], pos=here, view=30, strikes=3)
    assert world.is_present(GID), "記憶體沒看過牠，就不該由記憶體判牠死刑"
