"""從 inbound 封包維護「當前世界」：附近的怪物（含座標）與地上掉落物。

事件驅動：餵 RoPacket 進來就更新狀態，不用輪詢、不卡頓。
資料全來自伺服器推送的封包（見 GAMEDATA [PKT-018/020/029]），不碰記憶體。

**一律掃整段 TCP 內容，不只看開頭的 opcode。** 伺服器會把好幾個封包黏在同一段
TCP 裡送（實測抓到兩個 0x0088 黏在一起、0x0ADD 黏在 0x0ACB 後面），
而擷取層只把每段的前 2 bytes 當 opcode —— 只看開頭就會**整包漏掉**後面的
怪物出現／消失封包，症狀就是「旁邊有怪卻沒反應」。

每個掃到的候選都要通過驗證才採用（長度欄位對得上、objtype 是怪、
class ID 在怪物表裡、座標在地圖範圍內），驗不過就丟掉 —— 寧可少看到一隻怪，
也不要把雜訊當成怪去打（CLAUDE.md：驗不過就退安全預設）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ro_toolbox.core.ro_packet import RoPacket
from ro_toolbox.core.ro_protocol import unpack_move, unpack_position
from ro_toolbox.services.gamedata import is_mob

# ---- opcode（實測，見 GAMEDATA [PKT-029]）---------------------------
OP_ENTITY_STAND = 0x09FF  # 站著的實體進入視野
OP_ENTITY_NEW = 0x09FE    # 新出現的實體（版面同 0x09FF，尚未實測到）
OP_ENTITY_MOVE = 0x09FD   # 移動中的實體
OP_VANISH = 0x0080        # 實體消失，[0:4]=GID、[4]=type（1=死）
OP_STOPMOVE = 0x0088      # 實體停止移動，[0:4]=GID、[4:6]=x、[6:8]=y（純 uint16）
OP_ITEM_DROP = 0x0ADD     # 道具掉地上，[2:6]=實體ID、[6:8]=物品ID、[13:15]=x、[15:17]=y
OP_MOB_HP = 0x0A36        # 怪物剩餘 HP，[0:4]=GID、[4]=HP（[PKT-025]）
OP_DAMAGE = (0x08C8, 0x02E1)  # 傷害/動作，[0:4]=攻擊者、[4:8]=目標（[PKT-027]）

# 實體封包的欄位偏移（payload = opcode 之後的內容）
_OFF_LEN = 0        # uint16 整包長度（含 opcode）
_OFF_OBJTYPE = 2    # 5 = 怪物
_OFF_GID = 3        # uint32，攻擊封包用的就是這個
_OFF_CLASS = 21     # uint16 class ID，拿去怪物表交叉驗證
_OBJTYPE_MOB = 5
# 3-byte 打包座標的位置：移動封包多了 4 bytes 的 moveStartTime，所以往後 4
_POS_OFFSET = {OP_ENTITY_STAND: 61, OP_ENTITY_NEW: 61, OP_ENTITY_MOVE: 65}
_ENTITY_LEN_RANGE = (60, 200)

_DAMAGE_LEN = 12
_MOB_HP_LEN = 7
_VANISH_LEN = 7
_VANISH_DEAD = 1
_STOPMOVE_LEN = 10

# 掉落封包的座標偏移：實測 4 筆掉落都落在角色旁 1 格，唯一符合的偏移（[PKT-031]）
_OFF_DROP_X = 13
_DROP_LEN = 17
_ITEM_MIN_ID = 501  # 低於這個的不是道具編號，掃到就是雜訊
_MAX_MAP_SIDE = 512  # RO 地圖邊長上限，沒載到地形時用來擋離譜座標
#: 確認擊殺之後，這個 GID 保護多久不被「新的目擊」推翻。
#:
#: ⚠ 為什麼不能永久保護：**伺服器會重用 GID**。那隻怪死了、我們記下它的 GID，
#: 之後同一個 GID 被新生成的怪拿去用 —— 永久保護的話我們就**永遠看不到那隻新的怪**。
#: 症狀：怪站在旁邊打你，bot 說附近沒怪；**重開自動戰鬥就找得到**
#: （因為 WorldTracker 是新的，集合被清空）。使用者實測回報。
#:
#: 保護這幾秒是為了「剛送出的那次擊殺確認」不被同一拍的殘留封包蓋掉。
KILL_PROTECT_SEC = 8.0
#: 記憶體給過座標之後，這麼久之內**封包不准覆蓋它**。
#:
#: ⚠⚠ 為什麼要有這條：`0x09FD`（移動中的實體）帶的是「牠要走去哪」，
#: 那是**未來**的格子 —— 牠實際還在半路上。記憶體讀到的是客戶端當下算出來的
#: 那一格。實機同一份 60 秒樣本（177 拍、以伺服器封包為對照）：
#: 記憶體整數格與封包終點的差**中位 0 格、最大 4 格**，差的那幾格全是
#: 「封包說終點、記憶體說現在」。兩個來源打架時要聽記憶體的
#: （使用者要求：「不要看官方封包，完全看我們記憶體」）。
#:
#: 只保護這麼短是因為記憶體每一拍（0.2 秒）都會更新；超過就代表這隻怪
#: 記憶體那邊已經看不到了，那時封包給的座標比沒有座標好。
MEMORY_TRUST_SEC = 1.0


@dataclass(frozen=True, slots=True)
class GroundItem:
    """地上的一個掉落物。座標解不出來時 x/y 是 None（照撿，只是不會跑過去）。"""

    entity_id: int
    name_id: int
    x: int | None
    y: int | None

    @property
    def pos(self) -> tuple[int, int] | None:
        return None if self.x is None or self.y is None else (self.x, self.y)


@dataclass(slots=True)
class Monster:
    """視野內的一隻怪。座標優先來自記憶體，其次才是封包。"""

    gid: int
    class_id: int | None = None
    x: int | None = None
    y: int | None = None
    seen_at: float = field(default_factory=time.monotonic)
    hit_at: float = 0.0  # 最後一次「有人打到它」的時間 —— 證明它真的在
    hp: int | None = None  # 0x0A36 給的剩餘 HP（0~某上限）
    #: 記憶體最後一次給這隻座標的時間（0 = 從來沒有）。見 `MEMORY_TRUST_SEC`。
    mem_at: float = 0.0

    @property
    def pos(self) -> tuple[int, int] | None:
        return None if self.x is None or self.y is None else (self.x, self.y)

    def distance_from(self, pos: tuple[int, int]) -> int | None:
        """契比雪夫距離（RO 可斜走，這才是真正的步數）。座標未知回 None。"""
        here = self.pos
        if here is None:
            return None
        return max(abs(here[0] - pos[0]), abs(here[1] - pos[1]))


class WorldTracker:
    """餵封包進來，維護怪物與地上掉落物。執行緒安全。"""

    def __init__(
        self,
        valid_item_ids: set[int] | None = None,
        map_size: tuple[int, int] | None = None,
    ) -> None:
        self._monsters: dict[int, Monster] = {}
        self._items: dict[int, GroundItem] = {}
        self._killed = 0
        self._killed_gids: dict[int, float] = {}  # gid → 確認擊殺的時間
        self._gone_gids: set[int] = set()
        # GID → 最後一次「有人打到它」的時間。**故意不跟 _monsters 綁**：
        # 怪可能因為座標過時被 forget_far 移掉，但傷害封包還是會進來；
        # 綁在一起的話就會把「正在打的怪」誤判成「打到空氣」。
        self._hits: dict[int, float] = {}
        # gid → 記憶體連續幾次沒看到它。連續夠多次才刪（掃描偶爾會整批回 0）。
        self._absent: dict[int, int] = {}
        self._valid_items = valid_item_ids or set()
        self._map_size = map_size
        self._lock = threading.Lock()
        #: 掃到但驗不過的實體封包數（診斷用：一直增加代表封包版面變了）
        self.rejected = 0

    def set_map_size(self, size: tuple[int, int] | None) -> None:
        with self._lock:
            self._map_size = size

    # ---- 餵封包 -----------------------------------------------------

    def feed(self, packet: RoPacket) -> None:
        """掃整段內容，把所有認得出來的封包都吃掉（不只開頭那一個）。"""
        if packet.outbound:
            return
        raw = packet.opcode.to_bytes(2, "little") + packet.payload
        with self._lock:
            self._scan(raw)

    def _scan(self, raw: bytes) -> None:
        for i in range(len(raw) - 3):
            opcode = raw[i] | (raw[i + 1] << 8)
            if opcode in _POS_OFFSET:
                self._take_entity(raw, i, opcode)
            elif opcode == OP_VANISH:
                self._take_vanish(raw, i)
            elif opcode == OP_STOPMOVE:
                self._take_stopmove(raw, i)
            elif opcode == OP_ITEM_DROP:
                self._take_drop(raw, i)
            elif opcode == OP_MOB_HP:
                self._take_mob_hp(raw, i)
            elif opcode in OP_DAMAGE:
                self._take_damage(raw, i)

    # ---- 各種封包的解析＋驗證 ---------------------------------------

    def _take_entity(self, raw: bytes, i: int, opcode: int) -> None:
        """實體出現／移動。驗不過就直接丟，不動任何狀態。"""
        length = raw[i + 2] | (raw[i + 3] << 8)
        lo, hi = _ENTITY_LEN_RANGE
        if not (lo <= length <= hi) or i + length > len(raw):
            return
        payload = raw[i + 2 : i + length]
        pos_off = _POS_OFFSET[opcode]
        need = 6 if opcode == OP_ENTITY_MOVE else 3
        if len(payload) < pos_off + need:
            return
        if payload[_OFF_OBJTYPE] != _OBJTYPE_MOB:
            return  # NPC／其他玩家／傳送點：認得出來，但不是我們的目標
        class_id = int.from_bytes(payload[_OFF_CLASS : _OFF_CLASS + 2], "little")
        if not is_mob(class_id):
            self.rejected += 1
            return  # class ID 不在怪物表裡 → 不確定是什麼，不當成怪
        if opcode == OP_ENTITY_MOVE:
            # ⚠ 移動封包帶的是 **6 bytes 的「從哪走到哪」**，不是 3 bytes 的定點。
            # 以前只解前 3 bytes，記到的永遠是它**開始走之前**的格子 ——
            # 實測平均落後 4.2 格、最多 7 格（當時要走到 2 格內才准送攻擊），
            # 症狀就是「追蹤到怪物移動前位置」「打到空氣」「挑到的最近其實不是最近」。
            #
            # 實測證據（prt_fild07，16 個樣本）：
            #   - 6-byte 解出來的終點 **16/16** 在地圖範圍內且落在可走格
            #   - **前一包的終點 == 下一包的起點**，整條鏈對得上
            #   - 3-byte 版把後半當成 direction，解出來一直是 2 或 3（那其實是座標的位元）
            _start, (x, y) = unpack_move(payload[pos_off : pos_off + 6])
        else:
            x, y, _direction = unpack_position(payload[pos_off : pos_off + 3])
        if not self._in_map(x, y):
            self.rejected += 1
            return
        gid = int.from_bytes(payload[_OFF_GID : _OFF_GID + 4], "little")
        if self._recently_killed(gid):
            return  # 剛確認死掉的不復活（保護期過了就放行，因為 GID 會被重用）
        self._killed_gids.pop(gid, None)
        mob = self._monsters.get(gid)
        if mob is None:
            self._monsters[gid] = Monster(gid, class_id, x, y)
        else:
            now = time.monotonic()
            mob.class_id = class_id
            # ⚠ 記憶體剛給過座標就不要用封包蓋掉（見 `MEMORY_TRUST_SEC`）：
            # 移動封包帶的是**終點**，怪還在半路上。
            if now - mob.mem_at >= MEMORY_TRUST_SEC:
                mob.x, mob.y = x, y
            mob.seen_at = now
        self._gone_gids.discard(gid)

    def _take_vanish(self, raw: bytes, i: int) -> None:
        if i + _VANISH_LEN > len(raw):
            return
        gid = int.from_bytes(raw[i + 2 : i + 6], "little")
        kind = raw[i + 6]
        # 只處理認得的 GID —— 0x0080 只有 2 bytes 特徵，太容易誤中
        if gid in self._monsters:
            del self._monsters[gid]
            self._gone_gids.add(gid)
            if kind == _VANISH_DEAD:
                self._killed += 1
                self._killed_gids[gid] = time.monotonic()
        self._items.pop(gid, None)  # 撿起／消失的地上物

    def _take_stopmove(self, raw: bytes, i: int) -> None:
        """實體停下來：帶純 uint16 座標，是最準的一筆位置更新。"""
        if i + _STOPMOVE_LEN > len(raw):
            return
        gid = int.from_bytes(raw[i + 2 : i + 6], "little")
        mob = self._monsters.get(gid)
        if mob is None:
            return
        x = int.from_bytes(raw[i + 6 : i + 8], "little")
        y = int.from_bytes(raw[i + 8 : i + 10], "little")
        if not self._in_map(x, y):
            return
        now = time.monotonic()
        # 「停下來」帶的是純 uint16 定點座標，是封包裡最準的一筆；
        # 但記憶體更即時，所以一樣讓記憶體優先（見 `MEMORY_TRUST_SEC`）。
        if now - mob.mem_at >= MEMORY_TRUST_SEC:
            mob.x, mob.y = x, y
        mob.seen_at = now

    def _take_mob_hp(self, raw: bytes, i: int) -> None:
        """怪物 HP 變動 = 有人正在打它，也就證明它真的在那裡。"""
        if i + _MOB_HP_LEN > len(raw):
            return
        gid = int.from_bytes(raw[i + 2 : i + 6], "little")
        now = time.monotonic()
        mob = self._monsters.get(gid)
        if mob is not None:
            mob.hp = raw[i + 6]
            mob.hit_at = now
            self._hits[gid] = now

    def _take_damage(self, raw: bytes, i: int) -> None:
        """傷害封包：目標是我們追蹤中的怪，就記一筆「打到了」。

        這是「攻擊有沒有生效」唯一可靠的訊號 —— 對著過時座標打空氣時，
        這裡永遠不會更新，bot 才知道要放棄（見 [PKT-035]）。
        """
        if i + _DAMAGE_LEN > len(raw):
            return
        victim = int.from_bytes(raw[i + 6 : i + 10], "little")
        now = time.monotonic()
        mob = self._monsters.get(victim)
        if mob is not None:
            mob.hit_at = now
        if mob is not None or victim in self._hits:
            # 只記「認得的怪」或「已經打過的」，避免把自己挨打也記進來
            self._hits[victim] = now

    def _take_drop(self, raw: bytes, i: int) -> None:
        if i + _DROP_LEN > len(raw):
            return
        name_id = int.from_bytes(raw[i + 6 : i + 8], "little")
        if name_id < _ITEM_MIN_ID:
            return
        if self._valid_items and name_id not in self._valid_items:
            return
        entity = int.from_bytes(raw[i + 2 : i + 6], "little")
        x = int.from_bytes(raw[i + _OFF_DROP_X : i + _OFF_DROP_X + 2], "little")
        y = int.from_bytes(raw[i + _OFF_DROP_X + 2 : i + _OFF_DROP_X + 4], "little")
        if not self._in_map(x, y):
            # 座標不合理就當作不知道位置：還是要撿（原地掉的多半撿得到），
            # 只是不會為了它走過去 —— 絕不因為解不出座標就默默丟掉掉落物。
            x = y = None
        self._items[entity] = GroundItem(entity, name_id, x, y)

    def _in_map(self, x: int, y: int) -> bool:
        if self._map_size is None:
            return 0 < x < _MAX_MAP_SIDE and 0 < y < _MAX_MAP_SIDE
        return 0 <= x < self._map_size[0] and 0 <= y < self._map_size[1]

    # ---- 查詢 -------------------------------------------------------

    def monsters(self) -> list[Monster]:
        with self._lock:
            return list(self._monsters.values())

    def monster_gids(self) -> list[int]:
        with self._lock:
            return list(self._monsters)

    def get(self, gid: int) -> Monster | None:
        with self._lock:
            return self._monsters.get(gid)

    def nearest(self, pos: tuple[int, int], skip: set[int] | None = None) -> Monster | None:
        """離 pos 最近的怪。座標未知的排最後（還是能打，只是不知道多遠）。"""
        skip = skip or set()
        with self._lock:
            best: Monster | None = None
            best_key = (2, 1 << 30)
            for mob in self._monsters.values():
                if mob.gid in skip:
                    continue
                distance = mob.distance_from(pos)
                key = (1, 0) if distance is None else (0, distance)
                if key < best_key:
                    best, best_key = mob, key
            return best

    def sync_from_memory(
        self,
        entities,  # noqa: ANN001 - MemoryEntity
        pos: tuple[int, int] | None = None,
        view: int | None = None,
        strikes: int = 3,
    ) -> int:
        """用記憶體掃描的結果更新怪物集合。回傳刪掉幾隻。

        **記憶體是主要來源。** 封包會漏：站著不動的怪只在「進入視野」時送一次，
        bot 啟動之前就站在旁邊的那些，封包這條路**永遠**看不到
        （RO 沒有「請給我周圍有什麼」的查詢，[PKT-061]）。記憶體看得到。
        存活旗標（`GID-0x24 == 1` 且繪圖指標 `+0x110 != 0`，[MEM-016]）
        會把已釋放的舊結構擋掉，所以它看到的就是畫面上真的存在的。

        給了 `pos` 與 `view` 就**連刪除也交給記憶體**：視野內、記憶體連續
        `strikes` 次都沒看到的怪就丟掉。

        ⚠ **為什麼要連續好幾次而不是一次就刪**：實測記憶體掃描偶爾會整批回 0
        （量到一次「封包看到 11 隻、記憶體同時回 0」，下一拍又回 11）。
        一次抖動就清空的話，會把整片真的怪弄不見 —— 那比慢一拍刪掉糟得多。

        ⚠ 座標不明的怪（傷害封包補進來的，見 `note_monster`）**不參與刪除**：
        算不出距離就無從判斷它在不在視野內，而「它剛剛打到我」本身就是證據。
        """
        now = time.monotonic()
        removed = 0
        with self._lock:
            seen: set[int] = set()
            for entity in entities:
                seen.add(entity.gid)
                if self._recently_killed(entity.gid):
                    continue
                self._killed_gids.pop(entity.gid, None)
                mob = self._monsters.get(entity.gid)
                if mob is None:
                    mob = Monster(entity.gid, entity.class_id, entity.x, entity.y)
                    self._monsters[entity.gid] = mob
                else:
                    mob.class_id, mob.x, mob.y = entity.class_id, entity.x, entity.y
                    mob.seen_at = now
                mob.mem_at = now      # 這一拍記憶體看過牠了 → 封包不准蓋掉座標
                self._gone_gids.discard(entity.gid)
                self._absent.pop(entity.gid, None)

            if pos is None or view is None:
                return 0
            for gid, mob in list(self._monsters.items()):
                if gid in seen:
                    continue
                if not mob.mem_at:
                    # ⚠ **記憶體只能收回自己講過的話。** 背景掃描要輪過整份記憶體
                    # 才會發現新配置的實體（幾秒），這段期間封包已經看到牠了 ——
                    # 這時候刪掉牠等於「因為我還沒找到，所以牠不存在」。
                    # 沒有座標的幽靈由 `forget_far()` 與 `0x0080` 負責。
                    continue
                distance = mob.distance_from(pos)
                if distance is None or distance > view:
                    self._absent.pop(gid, None)   # 看不到的不算數
                    continue
                count = self._absent.get(gid, 0) + 1
                self._absent[gid] = count
                if count >= strikes:
                    del self._monsters[gid]
                    self._absent.pop(gid, None)
                    self._gone_gids.add(gid)
                    removed += 1
        return removed

    def note_attacking(self, gid: int) -> None:
        """送出攻擊時登記一下，之後這隻的傷害封包才會被記進 last_hit。"""
        with self._lock:
            self._hits.setdefault(gid, 0.0)

    def last_hit(self, gid: int) -> float:
        """最後一次有人打到這隻的時間（0 = 從來沒打到過）。"""
        with self._lock:
            return self._hits.get(gid, 0.0)

    def forget(self, gid: int) -> None:
        """明確地把一隻怪從追蹤裡拿掉（例如打過去發現它根本不在那裡）。"""
        with self._lock:
            if self._monsters.pop(gid, None) is not None:
                self._gone_gids.add(gid)

    def forget_far(self, pos: tuple[int, int], max_dist: int) -> int:
        """丟掉離我太遠的怪，回傳丟掉幾隻。

        怪走出視野時伺服器會送 0x0080，但那一包可能黏在別的封包裡被漏掉；
        漏掉就會留下一隻永遠打不到的幽靈怪，害 bot 一直鎖它。
        超過視野範圍就當它不在 —— 真的還在的話，靠近時會再收到出現封包。
        """
        with self._lock:
            gone = [
                gid
                for gid, mob in self._monsters.items()
                if (distance := mob.distance_from(pos)) is not None and distance > max_dist
            ]
            for gid in gone:
                del self._monsters[gid]
                self._gone_gids.add(gid)
            return len(gone)

    def ground_items(self) -> list[GroundItem]:
        with self._lock:
            return list(self._items.values())

    def forget_item(self, entity_id: int) -> None:
        """送出撿物後呼叫，避免重複撿同一個。"""
        with self._lock:
            self._items.pop(entity_id, None)

    def note_monster(self, gid: int) -> None:
        """外部發現的怪 —— **它剛剛打到我**，用這個把它補進追蹤讓 bot 能反擊。

        ⚠ **「以為它走了」不能擋住這條路。** 這個方法只有一個呼叫端：
        傷害封包顯示「攻擊者是它、目標是我」。**被它打到就是它還在那裡的證據**，
        比我們自己的判斷可信。

        以前這裡連 `_gone_gids` 也擋：打到空氣時 `forget()` 會把那隻怪放進
        `_gone_gids`，之後它就**再也補不回來** —— 於是它站在旁邊砍你，
        `get(gid)` 永遠是 None，`_pick_target` 的「打我的怪優先」直接跳過它。
        症狀就是「怪物打我但我卻不理他」（使用者實測回報）。

        只有**確認擊殺**（`0x0080 type=1`，伺服器權威訊號）才擋 —— 死掉的不會打人。
        沒有 class ID 與座標，所以只知道它存在。
        """
        with self._lock:
            if self._recently_killed(gid):
                return
            self._killed_gids.pop(gid, None)
            self._gone_gids.discard(gid)
            self._monsters.setdefault(gid, Monster(gid))

    def is_present(self, gid: int) -> bool:
        with self._lock:
            return gid in self._monsters

    def _recently_killed(self, gid: int) -> bool:
        """這個 GID 剛剛被確認擊殺（還在保護期內）嗎？**呼叫端要自己持鎖。**"""
        at = self._killed_gids.get(gid)
        return at is not None and time.monotonic() - at < KILL_PROTECT_SEC

    def was_killed(self, gid: int) -> bool:
        """這隻怪是不是剛被確認擊殺（0x0080 type=1）—— 100% 死亡訊號。

        ⚠ 只在保護期內回 True。伺服器**會重用 GID**，永久記住的話，
        同一個 GID 的新怪會被永遠當成死人（見 `KILL_PROTECT_SEC`）。
        """
        with self._lock:
            return self._recently_killed(gid)

    @property
    def kill_count(self) -> int:
        with self._lock:
            return self._killed

    def clear(self) -> None:
        with self._lock:
            self._monsters.clear()
            self._items.clear()
            self._killed = 0
            self._killed_gids.clear()
            self._gone_gids.clear()
            self._hits.clear()
            self.rejected = 0
