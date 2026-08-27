"""自動打怪機器人：一個行程一台，背景執行緒跑「找怪→打死→撿掉落→繼續找」。

全走封包＋唯讀記憶體（見 GAMEDATA [PKT-022]）：
- 怪物來源：封包（WorldTracker，[PKT-029]）為主，**記憶體掃描（EntityScanner，
  [MEM-014]）為輔**：記憶體偶爾補到封包漏收的那一隻，但實測涵蓋率低於封包，
  所以只增不減，絕不用它去刪掉封包看到的怪。
- 動作：DUP_HANDLE socket 送封包（走路/攻擊/撿物）
- 擊殺確認：0x0080 type=1（伺服器權威死亡訊號，比看畫面 HP 準）
- 走路：Walker 連續送走點＋用 0x0087 確認每一段（[PKT-030]）

不寫遊戲記憶體、不注入、不搶滑鼠鍵盤，GameGuard 看不到。
可隨時 stop()；每次狀態變動透過 on_update 回報（在背景執行緒呼叫，UI 端要轉執行緒）。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from ro_toolbox.core.ro_protocol import (
    build_attack,
    build_move,
    build_pickup,
    build_query,
    unpack_move,
)
from ro_toolbox.services import game_socket
from ro_toolbox.services.character import CharacterReader
from ro_toolbox.services.entities import EntityScanner
from ro_toolbox.services.gamedata import (
    is_farmable,
    item_name,
    item_names,
    mob_name,
    warps_on_map,
)
from ro_toolbox.services.mapdata import GatError, MapTerrain, load_terrain
from ro_toolbox.services.packet_capture import PacketCapture
from ro_toolbox.services.ro_capture import find_server
from ro_toolbox.services.travel import Traveler
from ro_toolbox.services.walker import MAX_STEP, Walker, line_cells
from ro_toolbox.services.world import Monster, WorldTracker

log = logging.getLogger(__name__)

_TICK = 0.2  # 主迴圈一拍。要夠密才能「怪一出現就轉去打」
_GIVE_UP_SEC = 10.0  # 同一隻打太久又沒靠近就放棄（多半打不到）
_SKIP_SEC = 30.0  # 放棄的目標暫時列入黑名單多久
_VIEW_RANGE = 30  # 超過這麼遠的怪視為已離開視野（實測伺服器視野約 24 格）
_ROAM_MIN = 60  # 漫遊目標至少離現在位置這麼遠 —— 一次挑很遠，才不會走走停停
_ROAM_MAX = 160  # 漫遊目標最遠這麼遠
_ROAM_BUDGET = 150_000  # A* 節點上限（150 格的路實測遠低於這個數）
_BAD_GOAL_SEC = 90.0  # 走不到的目標區域冷卻多久
_BAD_GOAL_RADIUS = 8  # 走不到的目標附近多少格內都別再挑
#: 走到離怪這麼近才送攻擊。
#:
#: ⛔ **試過放寬到 13 格，失敗了，不要再試。**
#: 想法是「攻擊封包只帶 GID 不帶座標，最後一段交給伺服器帶」——
#: 單獨實驗確實成立（站穩後從 9~14 格送攻擊，伺服器 4/5 會自己走過去，
#: 見 GAMEDATA [PKT-065]）。但**放進 bot 就壞了**，原因是實驗漏掉的差異：
#:
#:   實驗：角色**站穩**才送攻擊 → 伺服器接手帶路
#:   bot ：角色多半**正在走**的時候跨過門檻，`_walker.clear()` 之後
#:         攻擊在移動中送達 → 伺服器多半忽略它
#:
#: 而 `_fight()` 一旦 `attacked=True` 就再也不走路（[PKT-034]：移動會取消
#: 連續攻擊），於是變成「在 13 格外原地罰站等 grace 到期」——
#: 使用者實測回報「都沒走到怪物旁邊就放攻擊封包，導致原地罰站」。
#:
#: 所以回到「走近再打」。座標本身已經修好了（[PKT-064]），
#: 留 3 格而不是原本的 2 格，是給「讀座標」與「怪又走一步」之間那一拍的餘裕。
_ATTACK_RANGE = 3
_LOOT_PAUSE = 0.4  # 打死一隻之後停這麼久，讓掉落封包進來、撿完再換下一隻
_LOST_GRACE = 4.0  # 已經開打的怪暫時從追蹤裡消失，先寬限這麼久再放棄
#: 送出攻擊後這麼久還沒打到任何東西，就是打到空氣了。
#: ⚠ 這是**基礎**額度，還要加上「伺服器把角色帶過去要走的時間」——
#: 攻擊可以從 `_ATTACK_RANGE` 格外送出，光走過去就要好幾秒（實測 1 格約 0.15 秒）。
#: 固定 2 秒的話，遠距送出的攻擊會在角色還在路上時就被判定打空氣
#: （實測：改成 13 格攻擊後，70 秒內「打到空氣」7 次、擊殺只有 4）。
_ATTACK_ACK_SEC = 2.0
#: 走一格大約要多久（實測，見 GAMEDATA [PKT-030]）。用來換算上面那筆額外額度。
_WALK_SEC_PER_CELL = 0.15
#: 送出攻擊後還沒打到任何東西，隔這麼久補送一次。
#:
#: ⚠ **只在「一筆傷害都還沒收到」時才補。** `0x0437` action=7 是**連續**攻擊，
#: 重送很可能把攻速計時器重置 —— 正在正常互打時補送，DPS 會掉。
#: 補送要解決的是另一種情況：攻擊石沉大海（伺服器沒接、怪剛好跑掉），
#: 那時候一筆傷害都不會有，補一次比乾等到放棄划算。
#:
#: ⚠ 1 秒曾經實測比較差，但**那是攻擊距離還是 13 格的時候**：
#:
#:     2 秒＋走完閘門（13 格）  擊殺 16、打空氣 5 → 0.31
#:     1 秒＋走完閘門（13 格）  擊殺  4、打空氣 8 → 2.00   ← 補送 20 次
#:
#: 當時 1 秒之所以差，是因為伺服器要走 13 格（約 2 秒），1 秒的間隔會補在
#: 路上、把攻速計時器重置。**攻擊距離已經退回 3 格**（走完只要約 0.45 秒），
#: 那個原因不成立了，所以改回 1 秒。若又看到「打空氣」變多，先懷疑這裡。
#:
#: 🔬 **2026-08-27 正在試 0.5 秒**（使用者要求）。
#: 3 格的路伺服器要走約 0.45 秒 —— 0.5 秒等於「剛走完就補」，
#: 只要座標慢個一拍就會補在路上。**預期會變差**，量了才算數。
#: 比較時一定要用「打空氣 ÷ 擊殺」並在同一個地點回測：
#: 單輪 100 秒的擊殺數雜訊很大（同樣的程式碼跑出 3~16，光密度就能造成落差）。
_ATTACK_RETRY_SEC = 0.5
#: 補送前必須先靠這麼近。**這是「等訊號不等時間」的那個訊號**：
#: 伺服器把角色帶過去要時間（13 格約 2 秒），還在路上就補送等於自己打斷起手。
#: 實測 1 秒間隔、不看距離：100 秒補送 17 次，擊殺反而比 2 秒那組略低。
_RESEND_NEAR = 3
#: 用**真的路徑**確認「夠近了」時，允許比直線多繞幾格。
#:
#: ⚠ `distance_from()` 是契比雪夫距離（直線），**中間有牆也照樣算 3 格**。
#: 只看直線的話，隔著石頭的怪會被判成「貼到了」→ 送出攻擊 → 站著打空氣
#: （使用者實測回報）。所以直線夠近之後，再用 A* 確認實際要走的步數也夠近。
_PATH_SLACK = 3
#: 那個確認用的 A* 節點上限。只走幾格，不該花時間；超過就當「繞太遠」。
_NEAR_BUDGET = 3000
#: 傳點周圍幾格內一律不去。
#:
#: ⚠ **踩到傳點會被傳到別張地圖。** 那不只是走錯路 —— 新地圖可能有打不動的怪，
#: 而且 bot 會在那裡繼續打（使用者實測回報「怪在傳點裡面或旁邊，追過去就被傳走」）。
#: 傳點資料（`navi_link_tw.lub` → `assets/warps.json.gz`）只給一格，
#: 但實際的傳點是一片區域，所以要留餘裕。
_WARP_KEEP_OUT = 3
#: 同一張圖上通往**同一張地圖**、又共線、又靠得這麼近的兩個傳點，
#: 當成**同一條傳點帶**，中間整段都不准踩。
#:
#: ⚠ 依據是實測的資料形狀：`navi_link` 對一條傳點帶只取樣幾個點 ——
#: `moc_fild01` 往 `moc_fild02` 是 (301,16)/(321,16)/(341,16) **三筆指向
#: 同一個目的地格**，中間 20 格一段完全沒有資料。只擋取樣點周圍 3 格，
#: 等於在傳點帶上留了兩個 14 格寬的洞，走過去就被傳走。
#: 實測代價很小：prt_fild08 禁區 0.2%→0.3%、moc_fild01 0.2%→0.3%。
_WARP_STRIP_MAX = 60
#: 脫離禁區時，最遠往外找幾格。禁區半徑之外再留一點，免得剛好停在邊界上。
_ESCAPE_MARGIN = 4
#: 被傳走之後，走回原本那張圖最多花多久。逾時就大聲停用。
_RETURN_GIVEUP_SEC = 300.0
#: 一輪裡最多被傳走幾次。超過就停下來喊人 ——
#: 「怪站在傳點上 → 追過去被傳走 → 走回來 → 又看到牠」是會無限輪迴的
#: （使用者自己點出來的）。學到的禁區通常一次就擋掉了，這是最後一道保險。
_RETURN_MAX = 5
_MISS_SKIP_SEC = 20.0  # 打到空氣的目標冷卻多久（座標過時，等它重新出現）
#: 要不要把記憶體掃到的怪也算進來。**開著。**
#:
#: 為什麼需要它：**站著不動的怪只在「進入視野」時送一次封包**。
#: 那隻怪如果在 bot 啟動之前就已經站在旁邊，我們永遠收不到它的封包 ——
#: 螢幕上看得到、程式完全不知道它存在（使用者實測回報「明明有怪卻說沒怪」）。
#: RO 沒有「請給我周圍有什麼」的查詢（[PKT-061]），所以那種怪**只有記憶體看得到**。
#:
#: 為什麼以前關著：[MEM-014] 實測「接進 bot 會讓擊殺數腰斬」。但那是在
#: [MEM-016] 找到存活旗標（`GID-0x24 == 1` 且繪圖指標 `+0x110 != 0`）**之前**測的
#: —— 當時會撈到已釋放的舊結構當幽靈怪，對空氣送攻擊。旗標加上去之後
#: 打到空氣降到 0 次；移動封包也修好了（[PKT-064]）。
#:
#: ⚠ 它是**只增不減**的來源（`WorldTracker.sync_from_memory`），
#: 絕不會拿涵蓋率較低的來源去刪掉封包看到的怪。
_USE_MEMORY_ENTITIES = True
_PICKUP_RANGE = 2  # 這麼近才撿得到
_LOOT_WALK_MAX = 25  # 掉落物超過這麼遠就不特地跑過去
_LOOT_TIMEOUT = 8.0  # 撿不到就放棄這一個，別卡住
#: 怪打我之後多久內還算「正在打我」。太長會去追已經跑掉的怪。
_AGGRO_SEC = 12.0
_FROZEN_SEC = 45.0  # 完全沒進展（沒移動、沒擊殺、沒撿到）這麼久就停下來喊人
_RESYNC_SEC = 2.0  # 多久檢查一次「地圖／連線有沒有換掉」

# 傷害／動作封包：payload[0:4]=攻擊者 GID、[4:8]=目標 GID
_DAMAGE_OPS = (0x08C8, 0x02E1)
_OP_MOVE_ACK = 0x0087  # 伺服器確認「我」要移動：payload[4:10] = 起點+終點


def _warp_strips(by_dest: dict[str, list[tuple[int, int]]]) -> set[tuple[int, int]]:
    """把「同一張圖上通往同一個目的地、又共線、又靠得夠近」的傳點連成一條帶。

    ⚠ 這不是猜的，是**資料形狀本身**告訴我們的：`navi_link_tw.lub` 對一條
    傳點帶只取樣幾個點。實測 `moc_fild01` 往 `moc_fild02` 有三筆
    (301,16)/(321,16)/(341,16) **指向同一個目的地格** —— 那顯然是一條約 40 格
    寬的傳點帶，只被取樣三次。只擋取樣點周圍 3 格的話，中間留了兩個 14 格的洞，
    人走過去照樣被傳走（使用者實測回報「自動打怪走一走被傳到別的地圖」）。

    只連**共線**且距離 `_WARP_STRIP_MAX` 以內的兩點：距離遠的多半是兩個各自
    獨立、剛好通往同一張圖的傳點（實測 `ayo_dun02` 有兩個相隔 252 格的），
    連起來會擋掉一整條沒事的路。
    """
    strip: set[tuple[int, int]] = set()
    for cells in by_dest.values():
        spots = sorted(set(cells))
        for i, a in enumerate(spots):
            for b in spots[i + 1:]:
                if a[0] != b[0] and a[1] != b[1]:
                    continue  # 不共線 = 不是同一條帶
                if max(abs(a[0] - b[0]), abs(a[1] - b[1])) > _WARP_STRIP_MAX:
                    continue
                if a[0] == b[0]:
                    strip.update((a[0], y) for y in range(min(a[1], b[1]), max(a[1], b[1]) + 1))
                else:
                    strip.update((x, a[1]) for x in range(min(a[0], b[0]), max(a[0], b[0]) + 1))
    return strip


@dataclass
class FarmStats:
    running: bool = False
    kills: int = 0
    picked: int = 0
    monsters_near: int = 0
    target: str = ""  # 目前打誰（中文怪名）
    note: str = ""
    last_loot: str = ""  # 最近撿到什麼（中文道具名）
    walk_rejected: int = 0  # 被伺服器忽略的移動次數（診斷用）
    missed: int = 0  # 打到空氣的次數（座標過時，診斷用）
    resent: int = 0  # 補送攻擊的次數（診斷用：接近 0 就代表補送機制沒在用）


@dataclass
class _Aim:
    """目前鎖定的怪。"""

    gid: int
    since: float
    best_distance: int = 1 << 30
    attacked: bool = False
    attacked_at: float = 0.0  # 送出攻擊的時間，用來判斷有沒有打到
    attacked_dist: int = 0  # 送出攻擊時離它多遠（伺服器要走這段路，要多給時間）
    sent_at: float = 0.0  # 最後一次送出攻擊的時間（補送用，跟 attacked_at 分開）
    resends: int = 0  # 補送過幾次（診斷用）
    lost_at: float = 0.0  # 從追蹤裡消失的時間（0 = 還在）


class FarmBot:
    """單一角色的自動打怪。start()/stop() 控制；on_update 回報狀態。"""

    def __init__(
        self,
        pid: int,
        on_update: Callable[[FarmStats], None] | None = None,
        use_memory: bool = _USE_MEMORY_ENTITIES,
    ) -> None:
        self._pid = pid
        self._on_update = on_update
        self._use_memory = use_memory
        self._world = WorldTracker(valid_item_ids=set(item_names()))
        self._capture: PacketCapture | None = None
        self._sock: int | None = None
        self._reader: CharacterReader | None = None
        self._terrain: MapTerrain | None = None
        self._entities: EntityScanner | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stats = FarmStats()
        self._loot: dict[int, int] = {}  # 物品 ID -> 撿取次數
        self._loot_lock = threading.Lock()
        self._walker = Walker(self._send_move)
        self._aim: _Aim | None = None
        self._skip: dict[int, float] = {}  # 打不到的目標 → 黑名單到期時間
        #: gid → 被列入黑名單的時間。用來判斷「之後有沒有再看到它」——
        #: 收到更新的實體封包就是**它還在那裡的證據**，那比我們的黑名單可信。
        self._skip_at: dict[int, float] = {}
        self._bad_goals: list[tuple[tuple[int, int], float]] = []
        self._loot_since: dict[int, float] = {}  # 掉落物 → 開始嘗試撿的時間
        self._loot_until = 0.0  # 剛打死一隻，停到這個時間讓它撿東西
        self._roam_goal: tuple[int, int] | None = None  # 漫遊的遠點，中途打怪不換
        self._progress: tuple | None = None  # (位置, 擊殺, 撿取) —— 用來偵測完全卡住
        self._progress_at = 0.0
        self._map = ""  # 目前綁定的地圖，換圖要重新載地形
        #: 按下自動打怪時人在哪張圖。**被傳走就走回這裡**（使用者指定的行為）。
        self._home_map = ""
        #: 這張圖上最近幾拍的位置。被傳走時用來回推「踩到哪裡出事」。
        self._recent: deque[tuple[int, int]] = deque(maxlen=4)
        #: {地圖: 實際被傳走過的格子}。**量到的事實**，不是猜的 ——
        #: 地圖名變了就是真的被傳走了。只活在這一次執行裡。
        self._learned: dict[str, set[tuple[int, int]]] = {}
        #: 正在走回原圖（None = 沒有）。走回去期間不打怪、不撿東西。
        self._traveler: Traveler | None = None
        self._return_since = 0.0
        self._returns = 0
        #: 這張圖上「不准踩」的格子：傳點與它周圍 `_WARP_KEEP_OUT` 格。
        self._warp_zone: frozenset[tuple[int, int]] = frozenset()
        #: 傳點**本體**（踩到就被傳走）。禁區是本體再加周圍。
        self._warp_cells: frozenset[tuple[int, int]] = frozenset()
        #: 正在往哪裡脫離禁區（None = 沒在脫離）
        self._escape_goal: tuple[int, int] | None = None
        self._server: tuple[str, int] | None = None  # 目前綁定的伺服器端點
        self._resync_at = 0.0
        # 傷害封包分析：學到自己的 GID 後，就能認出「正在打我的怪」優先反擊
        self._my_gid: int | None = None
        # {gid: 最後一次打到我的時間}。帶時間戳才能過期 ——
        # 怪跑掉或被別人打死之後不該永遠留在優先清單裡。
        self._aggro: dict[int, float] = {}
        self._dmg_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> FarmStats:
        return self._stats

    def loot(self) -> dict[int, int]:
        """已撿取的道具 {物品ID: 次數}。快照，可安全在其他執行緒讀。"""
        with self._loot_lock:
            return dict(self._loot)

    # ---- 控制 -------------------------------------------------------

    def start(self) -> bool:
        """啟動自動打怪。

        ⚠ 所有耗時的設定（AOB 定位約 1 秒、列舉數百個 handle 找 socket、開 pcap）
        一律在**背景執行緒**做，不能在 UI 執行緒 —— 否則勾下去介面會凍住、
        被 Windows 判定「未回應」看起來像當機（使用者實際踩過）。
        這裡只起執行緒就立刻返回；成敗透過 on_update 回報。
        """
        if self.running:
            return True
        self._stop.clear()
        self._stats = FarmStats(running=True, note="啟動中…")
        self._emit()
        self._thread = threading.Thread(target=self._run, name=f"farm-{self._pid}", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(5.0)
        self._thread = None
        self._cleanup()
        self._stats.running = False
        self._note("已停止")

    # ---- 背景執行緒 -------------------------------------------------

    def _run(self) -> None:
        """整個生命週期都在這條執行緒：設定 → 主迴圈 → 收尾。全程包例外。"""
        try:
            if not self._setup():
                return
            self._loop()
        except Exception as exc:  # noqa: BLE001 - 背景執行緒絕不能讓例外炸掉整個程式
            log.exception("自動打怪執行緒發生例外")
            self._stats.running = False
            self._note(f"發生錯誤已停止：{exc}")
        finally:
            self._cleanup()

    def _setup(self) -> bool:
        server = find_server(self._pid)
        if server is None:
            self._fail("找不到伺服器連線（還沒登入？）")
            return False

        sock = game_socket.find_game_socket(self._pid, server[0], server[1])
        if not sock:
            self._fail("找不到遊戲 socket，無法送封包")
            return False
        self._sock = sock
        self._server = server

        reader = CharacterReader()
        if not reader.attach(self._pid, should_stop=self._stop.is_set):
            self._fail("角色定位失敗")
            return False
        self._reader = reader
        status = reader.read()
        if status is not None:
            # 自己的 GID 就是 AID（[MEM-017] 已用實測封包核對過）。
            # 一開始就知道，才認得出「怪先打我」—— 以前要等自己先出手才推導得出來，
            # 症狀就是被怪圍毆卻完全不理它們。
            if status.aid:
                self._my_gid = status.aid
                log.info("自己的 GID（AID）=%s", status.aid)
            self._map = status.map_name
            # 按下按鈕時人在哪，那張就是「家」。被傳走要走回這裡。
            self._home_map = status.map_name
            try:
                self._terrain = load_terrain(status.map_name)
                self._world.set_map_size((self._terrain.width, self._terrain.height))
                self._load_warps(status.map_name)
            except GatError as exc:
                self._terrain = None  # 沒地形也能打，只是不會探索走路
                log.warning("載入地形失敗，不會自動漫遊：%s", exc)
            if self._terrain is not None and self._use_memory:
                # 怪物主要來源。開不起來就退回只用封包（會少看到很多怪，但不會壞）
                scanner = EntityScanner(self._terrain, status.map_name, view=_VIEW_RANGE)
                self._entities = scanner if scanner.open(self._pid) else None
                if self._entities is None:
                    log.warning("記憶體掃描開不起來，怪物只能靠封包（會漏看）")
                else:
                    # 找新的怪放背景做，主迴圈只讀已知位址
                    self._entities.start_discovery(self._reader.read_position)

        self._capture = PacketCapture(self._pid, self._on_packet)
        if not self._capture.start():
            self._fail("封包擷取啟動失敗（需要系統管理員）")
            return False

        self._world.clear()
        self._note("自動打怪中" if self._terrain else "自動打怪中（沒有地形，不會漫遊）")
        return True

    def _fail(self, message: str) -> None:
        self._stats.running = False
        self._note(message)

    def _on_packet(self, packet) -> None:  # noqa: ANN001 - RoPacket，避免循環匯入
        """pcap 回呼（擷取執行緒）：餵世界模型、接移動確認、認出打我的怪。

        傷害/動作封包 [0:4]=攻擊者 GID、[4:8]=目標 GID。
        - 我攻擊某隻時會產生「攻擊者=我、目標=該隻」的封包 → 反推出自己的 GID。
        - 之後只要看到「目標=我」的封包，攻擊者就是正在打我的怪 → 標記優先。
        """
        self._world.feed(packet)
        if packet.outbound:
            return
        payload = packet.payload
        if packet.opcode == _OP_MOVE_ACK and len(payload) >= 10:
            _start, dest = unpack_move(payload[4:10])
            self._walker.note_move_ack(dest)
            return
        if packet.opcode not in _DAMAGE_OPS or len(payload) < 8:
            return
        attacker = int.from_bytes(payload[0:4], "little")
        victim = int.from_bytes(payload[4:8], "little")
        aim = self._aim
        if self._my_gid is None:
            # 退路：AID 讀不到時，靠「我正打的那隻挨打了 → 攻擊者就是我」反推。
            # 正常情況 _setup() 已經從記憶體拿到 AID 了（[MEM-017]），
            # 不必等到自己先出手 —— 以前要等，所以「怪先打我」永遠記不到。
            if aim is not None and victim == aim.gid:
                self._my_gid = attacker
                log.info("反推出自己的 GID：%s", attacker)
            return
        if victim == self._my_gid and attacker != self._my_gid:
            self._world.note_monster(attacker)
            with self._dmg_lock:
                self._aggro[attacker] = time.monotonic()

    # ---- 主迴圈 -----------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            self._expire(now)
            if not self._alive(now):
                return
            if not self._keep_in_sync(now):
                return
            pos = self._reader.read_position() if self._reader else None
            if pos is not None:
                # 被傳走時要回推「踩到哪裡出事」，所以隨手記著最近幾拍的位置。
                self._recent.append(pos)

            # ⚠ 被傳到別張圖了：這一拍只做一件事 —— 走回去。
            # **一定要排在 `_escape_warp` 前面**：剛落地時人就站在回程傳點旁邊，
            # 脫離邏輯會把我們往外拉，正好跟「走回去」互相打架。
            if self._traveler is not None:
                self._go_home(now, pos)
                self._emit()
                self._stop.wait(_TICK)
                continue

            if pos is not None:
                if self._entities is not None:
                    # 記憶體是**主要來源**：站著不動的怪只在進入視野時送一次封包，
                    # bot 啟動前就站在那裡的那些，封包永遠看不到（[PKT-061]）。
                    #
                    # ⚠ 這裡走**快路徑**：只讀已經記住的怪物位址（每隻 0x14C bytes），
                    # 不掃記憶體。找新的怪由背景執行緒做（`start_discovery`）——
                    # 掃一輪要 1.5 秒級，放在主迴圈裡每拍都會卡住，
                    # 症狀就是「刷新怪物清單跟打怪太慢」（使用者實測回報）。
                    #
                    # 連刪除也交給記憶體 —— 但要連續幾次沒看到才刪，因為掃描
                    # 偶爾會抖（實測 364 次取樣有 4 次孤島 0，見 [MEM-042]）。
                    self._world.sync_from_memory(
                        self._entities.read_known(pos), pos=pos, view=_VIEW_RANGE
                    )
                # 漏收 0x0080 會留下永遠打不到的幽靈怪，會害 bot 一直鎖它
                self._world.forget_far(pos, _VIEW_RANGE)

            # ⚠ 站在傳點禁區裡的話，這一拍只做一件事：走出去。
            # 不是停下來 —— 叫你別靠近傳點，不是叫你關掉自動戰鬥。
            if self._escape_warp(pos):
                self._emit()
                self._stop.wait(_TICK)
                continue

            # 腳邊的掉落物永遠先撿：怪死在腳邊，等打完下一隻就走開撿不到了
            self._grab_nearby(pos)
            self._update_aim(now, pos)
            self._stats.monsters_near = len(self._world.monster_gids())
            self._stats.walk_rejected = self._walker.rejected

            # 再來：打怪 > 走過去撿遠一點的掉落 > 漫遊找怪
            if self._aim is not None:
                self._fight(now, pos)
            elif now < self._loot_until:
                self._walker.clear()  # 剛打死，停一下讓它撿完再走
            elif not self._collect(now, pos):
                self._roam(now, pos)

            self._emit()
            self._stop.wait(_TICK)

    def _keep_in_sync(self, now: float) -> bool:
        """換地圖／換伺服器頻道之後，重新綁定 socket 與地形。

        踩過的坑：角色中途換圖（或伺服器把連線移到別的地圖伺服器）之後，
        啟動時抓到的 socket 與地形都已經失效 —— 送出去的封包全部石沉大海、
        A* 用的還是舊地圖，**bot 看起來在跑，實際上什麼都沒做**。
        這正是規範說的「安靜地做錯事」，所以要主動偵測並重綁。
        回傳 False = 重綁失敗，要大聲停用。
        """
        if now - self._resync_at < _RESYNC_SEC or self._reader is None:
            return True
        self._resync_at = now
        status = self._reader.read()
        server = find_server(self._pid)
        map_changed = status is not None and status.map_name and status.map_name != self._map
        server_changed = server is not None and server != self._server
        if not (map_changed or server_changed):
            if server is None and self._server is not None:
                self._fail("⚠ 遊戲連線已中斷，自動打怪已停止")
                return False
            return True

        what = []
        if map_changed:
            what.append(f"地圖 {self._map} → {status.map_name}")
        if server_changed:
            what.append(f"連線 {self._server} → {server}")
        log.info("環境變了（%s），重新綁定", "、".join(what))

        if server_changed:
            if self._sock is not None:
                game_socket.close_socket(self._sock)
                self._sock = None
            sock = game_socket.find_game_socket(self._pid, server[0], server[1])
            if not sock:
                self._fail("⚠ 換頻道後找不到新的遊戲 socket，自動打怪已停止")
                return False
            self._sock = sock
            self._server = server

        if map_changed:
            # ⚠ 走回去的途中換圖是**我們自己要的**，不是意外，不能學也不能重來。
            if self._traveler is None and self._map:
                self._learn_warp(self._map)
                if not self._go_home_start(status.map_name, now):
                    return False
            self._map = status.map_name
            try:
                self._terrain = load_terrain(status.map_name)
                self._world.set_map_size((self._terrain.width, self._terrain.height))
                self._load_warps(status.map_name)
            except GatError as exc:
                self._terrain = None
                log.warning("新地圖沒有地形檔，不會漫遊：%s", exc)
            # 換圖之後舊的怪、掉落、走位、漫遊目標全部作廢
            self._world.clear()
            self._walker.clear()
            self._roam_goal = None
            self._escape_goal = None
            self._aim = None
            self._skip.clear()
            self._skip_at.clear()
            self._bad_goals.clear()
            self._loot_since.clear()
            if self._entities is not None:
                self._entities.close()
                self._entities = None
            if self._terrain is not None and self._use_memory:
                scanner = EntityScanner(self._terrain, status.map_name, view=_VIEW_RANGE)
                self._entities = scanner if scanner.open(self._pid) else None
                if self._entities is not None:
                    self._entities.start_discovery(self._reader.read_position)
        self._note("　".join(what) + "，已重新綁定")
        return True

    def _alive(self, now: float) -> bool:
        """還能繼續打嗎？不能就大聲停用。

        **不做低血休息**：這張遊戲主動怪太多，站著回血只會被圍毆，
        停下來反而更危險。低血就繼續打，人自己看 UI 決定要不要收手。
        只有「已經動不了」才停 —— 實測踩過：角色被菁英怪打到 HP 1、
        送四個方向的移動全無反應、12 秒不回血，bot 卻繼續當成「交戰中」
        站了 56 秒還一直送封包，這就是規範說的「安靜地做錯事」（[PKT-033]）。
        """
        if self._reader is None:
            return True
        status = self._reader.read()
        if status is None:
            return True  # 讀不到就不亂判斷（可能正在換地圖）
        if status.hp <= 0:
            self._fail("⚠ 角色已死亡，自動打怪已停止")
            return False

        # 血量沒事卻長時間毫無進展（沒移動、沒擊殺、沒撿到）：多半卡住或狀態異常。
        # 交戰時站著不動是正常的，所以擊殺數也算「有進展」。
        progress = (self._reader.read_position(), self._stats.kills, self._stats.picked)
        if progress != self._progress:
            self._progress = progress
            self._progress_at = now
        elif now - self._progress_at > _FROZEN_SEC:
            self._fail(f"⚠ 角色 {_FROZEN_SEC:.0f} 秒毫無進展（可能死亡或卡住），已停止")
            return False
        return True

    def _expire(self, now: float) -> None:
        for gid in [g for g, until in self._skip.items() if now > until]:
            del self._skip[gid]
            self._skip_at.pop(gid, None)
        with self._dmg_lock:
            for gid in [g for g, at in self._aggro.items() if now - at > _AGGRO_SEC]:
                del self._aggro[gid]
        self._bad_goals = [(cell, until) for cell, until in self._bad_goals if now < until]

    # ---- 打怪 -------------------------------------------------------

    def _update_aim(self, now: float, pos: tuple[int, int] | None) -> None:
        """維護目前鎖定的怪。

        **一旦開打就要打到確認死**：擊殺訊號是伺服器的 `0x0080 type=1`
        （[PKT-021]），在那之前不換目標。怪從追蹤裡消失不代表死了 ——
        可能只是我們漏收封包，所以先寬限 `_LOST_GRACE` 秒；
        還沒開打的才可以一消失就換。否則就會「打一下就跑」。
        """
        aim = self._aim
        if aim is not None:
            if self._world.was_killed(aim.gid):
                self._stats.kills += 1
                self._drop_aggro(aim.gid)
                # 停一下再找下一隻，讓掉落封包進來、腳邊的東西撿完
                self._loot_until = now + _LOOT_PAUSE
                self._note(f"擊殺 {self._stats.kills} 隻")
                self._aim = aim = None
            elif not self._world.is_present(aim.gid):
                if not aim.attacked:
                    self._drop_aggro(aim.gid)
                    self._aim = aim = None  # 還沒開打就不見了，換一隻
                elif not aim.lost_at:
                    aim.lost_at = now
                elif now - aim.lost_at > _LOST_GRACE:
                    self._skip[aim.gid] = now + _SKIP_SEC
                    self._skip_at[aim.gid] = now
                    self._drop_aggro(aim.gid)
                    self._aim = aim = None
            else:
                aim.lost_at = 0.0
                mob = self._world.get(aim.gid)
                if mob is not None and mob.hit_at > aim.since:
                    aim.since = mob.hit_at  # 正在互打，當然不算「打不到」
                distance = mob.distance_from(pos) if (mob and pos) else None
                if distance is not None and distance < aim.best_distance:
                    # 還在接近中就不算打不到，重新計時
                    aim.best_distance = distance
                    aim.since = now
                elif now - aim.since > _GIVE_UP_SEC:
                    # 打太久又沒更靠近＝打不到，黑名單換目標，別卡在這隻
                    self._skip[aim.gid] = now + _SKIP_SEC
                    self._skip_at[aim.gid] = now
                    self._drop_aggro(aim.gid)
                    self._aim = aim = None

        if aim is None and now >= self._loot_until:
            mob = self._pick_target(pos)
            if mob is not None:
                self._aim = _Aim(mob.gid, now)
                self._stats.target = mob_name(mob.class_id)

    def _pick_target(self, pos: tuple[int, int] | None) -> Monster | None:
        """挑目標：先打正在打我的怪（主動怪），再打**最近**的怪。

        MVP 與草一律跳過（`is_farmable`）：它們跟一般怪一樣打得動、也會掉東西，
        但草是浪費時間、MVP 是送死。**菁英怪不算 MVP，照打。**
        """
        # ⚠ **黑名單被新的目擊推翻。** 拉黑多半是因為座標過時打到空氣；
        # 之後又收到那隻怪的實體封包，就代表它真的還在，而且我們拿到新座標了。
        # 不放行的話，附近幾隻怪一被拉黑，畫面上明明有怪、程式卻說「附近沒怪」
        # 而且要等 20 秒（使用者實際回報）。
        for gid, at in list(self._skip_at.items()):
            mob = self._world.get(gid)
            if mob is not None and mob.seen_at > at:
                self._skip.pop(gid, None)
                self._skip_at.pop(gid, None)
        skip = set(self._skip)
        skip.update(m.gid for m in self._world.monsters() if not is_farmable(m.class_id))
        # ⚠ **站在傳點裡（或旁邊）的怪一律不打。** 追過去就會踩到傳點被傳走 ——
        # 新地圖可能有打不動的怪，而 bot 會在那裡繼續打（使用者實測回報）。
        # 連正在打我的怪也不追：被打幾下，好過被傳到不該去的地方。
        in_warp = {m.gid for m in self._world.monsters() if self._near_warp(m.pos)}
        skip.update(in_warp)
        # 正在打我的怪**不受「打到空氣」黑名單限制**：它打得到我就代表它真的在旁邊，
        # 座標不可能過時。以前被黑名單擋住，症狀就是「怪在打我卻不理它」。
        no_hunt = {m.gid for m in self._world.monsters() if not is_farmable(m.class_id)}
        no_hunt |= in_warp
        with self._dmg_lock:
            aggro = sorted(self._aggro.items(), key=lambda kv: -kv[1])
        for gid, _at in aggro:
            if gid in no_hunt:
                continue
            mob = self._world.get(gid)
            if mob is not None:
                self._skip.pop(gid, None)
                return mob
        if pos is None:
            for gid in self._world.monster_gids():
                if gid not in skip:
                    return self._world.get(gid)
            return None
        return self._world.nearest(pos, skip=skip)

    def _fight(self, now: float, pos: tuple[int, int] | None) -> None:
        """交戰。照玩家點怪的順序：**查詢 → 走近 → 攻擊，然後就不要再動**。

        使用者提供的實測封包（[PKT-015]）：左鍵點怪送 `0x0368`(查詢) →
        `0x035F`(走近) → `0x0437`(連續攻擊)，之後客戶端自己打到死。

        「走近」只要走到 `_ATTACK_RANGE` 格內就夠了 —— 最後那一段由伺服器帶，
        它知道怪真正在哪（見 `_ATTACK_RANGE` 的實測說明）。

        **攻擊送出後絕對不能再送移動**：移動會取消連續攻擊，
        症狀就是「打一下就跑掉」。所以 attacked 之後這裡什麼都不做，等擊殺訊號。
        """
        aim = self._aim
        if aim is None:
            return
        if aim.attacked:
            self._walker.clear()  # 已經在打了，站著等 0x0080 確認死亡
            self._resend_attack(aim, now, pos)
            self._check_hit(aim, now)
            return

        # ⚠ 每一拍都重讀它現在在哪 —— 這遊戲的怪移動很頻繁，
        # 用上一拍的位置判斷「夠不夠近」就會在它走開之後對著空地打。
        mob = self._world.get(aim.gid)
        distance = mob.distance_from(pos) if (mob is not None and pos is not None) else None
        if (distance is not None and pos is not None
                and not self._close_enough(pos, mob.pos, distance)):
            if self._approach(pos, mob.pos):
                return  # 還太遠（或中間有牆要繞），先走近一點
        # 貼到了（或算不出路，那就直接打，打不到會被放棄計時器換掉）
        self._walker.clear()
        self._world.note_attacking(aim.gid)
        self._send(build_query(aim.gid))
        self._send(build_attack(aim.gid))
        aim.attacked = True
        aim.attacked_at = now
        aim.sent_at = now
        aim.attacked_dist = distance or 0

    def _resend_attack(self, aim: _Aim, now: float, pos: tuple[int, int] | None) -> None:
        """還沒打到任何東西就補送一次攻擊。**打到了就絕不補。**

        為什麼要有：攻擊可能石沉大海 —— 伺服器沒接、或送出的那一刻怪剛好走掉。
        那種情況一筆傷害都不會有，乾等到放棄額度用完是浪費。

        為什麼「打到了就不補」：`0x0437` action=7 是**連續**攻擊，
        重送很可能把攻速計時器重置，正常互打時補送反而讓 DPS 變差。
        傷害封包一直進來就代表這次攻擊生效了，不要去碰它。

        為什麼還要看距離：攻擊可以從 13 格外送出，伺服器要先把角色帶過去
        （約 2 秒）。只看時間的話會在路上就補送 —— 實測 1 秒間隔、不看距離，
        100 秒補送 17 次，擊殺反而比 2 秒那組略低。走到旁邊了才算數。
        """
        if self._world.last_hit(aim.gid) > aim.attacked_at:
            return  # 已經打到了，別碰攻速計時器
        if now - aim.sent_at < _ATTACK_RETRY_SEC:
            return
        # ⚠ 還在被伺服器帶過去的路上就不要補 —— 那會打斷起手。
        # 兩個都算「路走完了」，符合一個就行：
        #   a) 已經走到怪旁邊（看得到就用，但我們的座標本來就會慢一拍）
        #   b) 送出到現在的時間，已經夠伺服器走完那段距離了
        # 只用 (a) 的話會因為座標落後而一直判「還沒到」，補送幾乎不會發生
        # （實測：打到空氣從 2 次變 6 次）。
        mob = self._world.get(aim.gid)
        distance = mob.distance_from(pos) if (mob is not None and pos is not None) else None
        near = distance is None or distance <= _RESEND_NEAR
        walked = now - aim.attacked_at >= aim.attacked_dist * _WALK_SEC_PER_CELL
        if not (near or walked):
            return
        # 補送也要在放棄額度內 —— 額度用完就該換人，不是無限補
        if now - aim.attacked_at >= self._hit_grace(aim):
            return
        self._send(build_attack(aim.gid))
        aim.sent_at = now
        aim.resends += 1
        self._stats.resent += 1

    @staticmethod
    def _hit_grace(aim: _Aim) -> float:
        """這次攻擊「多久沒打到就算打空氣」。從遠處送的要把帶路時間算進去。"""
        return _ATTACK_ACK_SEC + aim.attacked_dist * _WALK_SEC_PER_CELL

    def _close_enough(
        self, pos: tuple[int, int], goal: tuple[int, int], straight: int
    ) -> bool:
        """真的夠近了嗎？**直線不算數，要用實際走得到的步數。**

        `distance_from()` 是契比雪夫距離，中間隔著石頭、水、牆也照樣算 3 格。
        只看直線的話，隔著障礙的怪會被判成「貼到了」→ 送出攻擊 → 站著打空氣
        （使用者實測回報）。所以直線夠近之後，再用 A* 確認繞過去的步數也夠近。

        貼身（1 格內）直接算數，不必再算路徑。算不出路徑（被牆完全隔開）
        就回 False，讓呼叫端去走 —— 走不成的話 `_approach` 會回 False，
        那時才會直接打，由放棄計時器收尾。
        """
        if straight > _ATTACK_RANGE:
            return False
        if straight <= 1:
            return True
        terrain = self._terrain
        if terrain is None:
            return True  # 沒地形就只能信直線
        path = terrain.find_path(pos, goal, node_budget=_NEAR_BUDGET)
        if path is None:
            # 怪站的那格可能不可走（牠站在斜坡邊之類）—— 改看牠旁邊那格
            beside = self._beside(goal, pos)
            if beside is None or beside == pos:
                return True
            path = terrain.find_path(pos, beside, node_budget=_NEAR_BUDGET)
        if path is None:
            return False
        return len(path) <= _ATTACK_RANGE + _PATH_SLACK

    def _check_hit(self, aim: _Aim, now: float) -> None:
        """攻擊送出後有沒有真的打到？沒有就是對著**過時的座標**打空氣。

        怪的座標來自封包，而封包會漏收，所以記錄的位置可能是舊的。
        以前只能等 10 秒的放棄計時器，症狀就是走過去站著發呆。
        現在只要 2 秒內沒有任何「打到它」的訊號（傷害封包或怪物 HP 變動），
        就把它從追蹤裡拿掉並短暫冷卻 —— 它真的還在的話會再送出現封包。
        """
        # 從遠處送的攻擊，伺服器要先把角色帶過去 —— 那段路的時間要算進去，
        # 不然角色還在走就被判「打到空氣」，白白換掉一個好目標。
        if now - aim.attacked_at <= self._hit_grace(aim):
            return
        if self._world.last_hit(aim.gid) > aim.attacked_at:
            return  # 有打到，繼續打
        if self._world.was_killed(aim.gid):
            return  # 已經死了，交給 _update_aim 記擊殺
        self._stats.missed += 1
        self._world.forget(aim.gid)
        self._skip[aim.gid] = now + _MISS_SKIP_SEC
        self._skip_at[aim.gid] = now
        self._drop_aggro(aim.gid)
        self._aim = None
        self._note(f"打到空氣（座標過時），換下一隻｜共 {self._stats.missed} 次")

    def _approach(self, pos: tuple[int, int], goal: tuple[int, int]) -> bool:
        """走到怪**旁邊**的可走格。回傳 True = 還在路上。"""
        target = self._beside(goal, pos)
        if target is None:
            return False
        current = self._walker.goal
        if current is None or max(abs(current[0] - target[0]), abs(current[1] - target[1])) > 2:
            path = self._plan_path(pos, target)
            if not path:
                return False
            self._walker.set_path(path, avoid=self._warp_zone)
        return self._walker.update(pos) == "walking"

    def _beside(self, goal: tuple[int, int], pos: tuple[int, int]) -> tuple[int, int] | None:
        """挑一個緊鄰怪、離我最近的可走格（怪站的那格也算）。"""
        if self._terrain is None:
            return None
        best, best_distance = None, 1 << 30
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cell = (goal[0] + dx, goal[1] + dy)
                if not self._terrain.is_walkable(*cell):
                    continue
                distance = max(abs(cell[0] - pos[0]), abs(cell[1] - pos[1]))
                if distance < best_distance:
                    best, best_distance = cell, distance
        return best

    def _drop_aggro(self, gid: int) -> None:
        with self._dmg_lock:
            self._aggro.pop(gid, None)

    # ---- 撿東西 -----------------------------------------------------

    def _grab_nearby(self, pos: tuple[int, int] | None) -> None:
        """撿手邊的掉落物 —— 永遠最優先，因為怪就死在腳邊，晚一步就走開了。

        不知道自己在哪、或掉落物沒解出座標時也照送撿物封包：撿不到伺服器就忽略，
        總比因為「不確定位置」而默默放掉整個掉落物好。
        """
        for item in self._world.ground_items():
            if pos is None or item.pos is None:
                self._pick_up(item)
            elif max(abs(item.x - pos[0]), abs(item.y - pos[1])) <= _PICKUP_RANGE:
                self._pick_up(item)

    def _collect(self, now: float, pos: tuple[int, int] | None) -> bool:
        """走過去撿遠一點的掉落物。回傳「這一拍在處理掉落物」（是的話先別漫遊）。"""
        if pos is None:
            return False
        if not self._world.ground_items():
            self._loot_since.clear()
            return False

        reachable = []
        for item in self._world.ground_items():
            distance = max(abs(item.x - pos[0]), abs(item.y - pos[1]))
            started = self._loot_since.setdefault(item.entity_id, now)
            if now - started > _LOOT_TIMEOUT or distance > _LOOT_WALK_MAX:
                self._world.forget_item(item.entity_id)  # 撿不到就別再卡著
                continue
            reachable.append((distance, item))
        if not reachable:
            return False

        distance, item = min(reachable, key=lambda pair: pair[0])
        # 掉在幾格外：走過去再撿（不走過去就白白少撿一半）
        if self._walker.goal != item.pos:
            path = self._plan_path(pos, item.pos)
            if path is None:
                self._world.forget_item(item.entity_id)
                return False
            self._walker.set_path(path, avoid=self._warp_zone)
        if self._walker.update(pos) == "blocked":
            self._world.forget_item(item.entity_id)
        return True

    def _pick_up(self, item) -> None:  # noqa: ANN001 - GroundItem
        self._send(build_pickup(item.entity_id))
        self._world.forget_item(item.entity_id)
        self._loot_since.pop(item.entity_id, None)
        self._stats.picked += 1
        self._stats.last_loot = item_name(item.name_id)
        with self._loot_lock:
            self._loot[item.name_id] = self._loot.get(item.name_id, 0) + 1
        self._note(f"撿到 {self._stats.last_loot}（共 {self._stats.picked} 個）")

    # ---- 漫遊找怪 ---------------------------------------------------

    def _roam(self, now: float, pos: tuple[int, int] | None) -> None:
        """沒怪也沒東西撿：挑一個**很遠**的點，沿算好的路一路走過去。

        目標會**記住**：中途插隊去打怪、或路被擋住重算，回來還是走同一個遠點，
        只有真的走到、或完全到不了才換。每拍都重挑新方向的話，
        看起來就是在原地亂繞（使用者回報的「亂走」）。
        """
        if self._terrain is None or pos is None:
            return
        state = self._walker.update(pos)
        if state == "walking":
            return
        if state == "arrived":
            self._roam_goal = None  # 到了，換下一個遠點
        elif state == "blocked":
            if self._roam_goal is not None:
                self._bad_goals.append((self._roam_goal, now + _BAD_GOAL_SEC))
            self._roam_goal = None
        self._plan_roam(now, pos)

    def _plan_roam(self, now: float, pos: tuple[int, int]) -> None:
        """（重新）算到漫遊目標的路。還沒有目標就先挑一個很遠的。"""
        terrain = self._terrain
        if terrain is None:
            return
        for _ in range(8):
            if self._roam_goal is None:
                dest = terrain.random_walkable(
                    random, near=pos, radius=_ROAM_MAX, min_radius=_ROAM_MIN
                )
                if dest is None or self._is_bad_goal(dest) or self._near_warp(dest):
                    continue   # 漫遊目標也不准挑在傳點上
                self._roam_goal = dest
            path = self._plan_path(pos, self._roam_goal)
            if path:
                self._walker.set_path(path, avoid=self._warp_zone)
                self._walker.update(pos)
                return
            self._bad_goals.append((self._roam_goal, now + _BAD_GOAL_SEC))
            self._roam_goal = None
        # 完全算不出路：往近處走一步，絕不原地不動（但別踩到傳點）。
        # ⚠ 這一步**沒有經過 Walker**，中間那段路完全是伺服器自己走的 ——
        # 所以除了目標格，直線經過的每一格也都要檢查。
        for _ in range(6):
            near = terrain.random_walkable(random, near=pos, radius=MAX_STEP)
            if near is None or near == pos or self._near_warp(near):
                continue
            if any(cell in self._warp_zone for cell in line_cells(pos, near)[1:]):
                continue
            self._send_move(*near)
            return

    def _plan_path(
        self, start: tuple[int, int], goal: tuple[int, int]
    ) -> list[tuple[int, int]] | None:
        """算一條路，而且**繞開傳點** —— 踩上去會被傳到別張地圖。"""
        if self._terrain is None:
            return None
        return self._terrain.find_path(
            start, goal, node_budget=_ROAM_BUDGET, blocked=self._warp_zone
        )

    def _load_warps(self, map_name: str) -> None:
        """記下這張圖上不准踩的格子（傳點與周圍）。

        查不到就是空的 —— 安全退化成「跟以前一樣會踩到」，不會因此不能走路。
        """
        cells: set[tuple[int, int]] = set()
        by_dest: dict[str, list[tuple[int, int]]] = {}
        for x, y, dest, _dx, _dy in warps_on_map(map_name):
            cells.add((x, y))
            by_dest.setdefault(dest, []).append((x, y))
        strip = _warp_strips(by_dest)
        cells |= strip
        learned = self._learned.get(map_name, set())
        cells |= learned
        zone: set[tuple[int, int]] = set()
        for x, y in cells:
            for dx in range(-_WARP_KEEP_OUT, _WARP_KEEP_OUT + 1):
                for dy in range(-_WARP_KEEP_OUT, _WARP_KEEP_OUT + 1):
                    zone.add((x + dx, y + dy))
        self._warp_zone = frozenset(zone)
        # 傳點**本體**那一格。禁區是「不想去」，本體是「踩到就被傳走」——
        # 從禁區裡面往外走時只避開本體，避開整片禁區的話就永遠走不出來。
        self._warp_cells = frozenset(cells)
        log.info("%s 的傳點 %d 格（帶狀補了 %d、實際踩過學到 %d）、禁區 %d 格",
                 map_name, len(self._warp_cells), len(strip), len(learned),
                 len(self._warp_zone))

    def _learn_warp(self, old_map: str) -> None:
        """剛剛真的被傳走了 —— 把「當時正在走的那一段」記成傳點，之後不再踩。

        ⚠ 這是**量到的事實**，不是推論：記憶體裡的地圖名變了，就是真的被傳走了。
        為什麼非學不可：`assets/warps.json.gz`（來自 `navi_link_tw.lub`）每個傳點
        只給**一格**，實際的傳點是一片區域，而且一條傳點帶只被取樣幾次
        （見 `_warp_strips`）。照資料繞開永遠會有漏網的，踩到就把它記起來。

        學的是**一整段**不是一格：座標 0.2 秒才取樣一次，而且每一段中間
        怎麼走是伺服器決定的（[PKT-030]），所以踩進去的確切位置不知道 ——
        只知道在「最近幾拍的位置 → 那一段送出去的目標」這條線上。

        只活在這一次執行裡。存到檔案的話，遊戲改版動了傳點就會擋到沒事的地方，
        而且沒有徵兆 —— 寧可每次重學（一次就夠）。
        """
        points = list(self._recent)
        if not points:
            return
        target = self._walker.target
        if target is not None:
            points.append(target)
        span: set[tuple[int, int]] = set(points)
        for a, b in zip(points, points[1:], strict=False):
            span.update(line_cells(a, b))
        learned = self._learned.setdefault(old_map, set())
        before = len(learned)
        learned |= span
        log.warning("⚠ 在 %s 被傳走了（最後看到 %s，正要走去 %s）——"
                    "把這一段 %d 格記成傳點，這次開著的期間都不再踩",
                    old_map, points[-2] if len(points) > 1 else points[-1], target,
                    len(learned) - before)

    def _go_home_start(self, new_map: str, now: float) -> bool:
        """被傳到別張圖了 —— 開始走回原本那張。回 False = 已經大聲停用。

        ⚠ **輪迴保險**：怪站在傳點上時，「追過去被傳走 → 走回來 → 又看到牠」
        會無限來回（使用者自己點出來的）。正常情況 `_learn_warp` 學到的禁區
        會讓 `_pick_target` 直接不理那隻怪，一次就斷了；`_RETURN_MAX` 是
        萬一還是斷不掉時的最後一道保險 —— 停下來喊人，好過整晚來回踱步。
        """
        if not self._home_map or new_map == self._home_map:
            return True
        self._returns += 1
        if self._returns > _RETURN_MAX:
            self._fail(f"⚠ 已經被傳走 {_RETURN_MAX} 次（現在在 {new_map}），"
                       f"再走回去只會一直輪迴，自動打怪已停止")
            return False
        self._aim = None
        self._roam_goal = None
        self._escape_goal = None
        self._walker.clear()
        traveler = Traveler(self._walker, time.monotonic)
        traveler.set_goal(self._home_map)
        self._traveler = traveler
        self._return_since = now
        self._note(f"被傳到 {new_map} 了，走回 {self._home_map}"
                   f"（第 {self._returns}/{_RETURN_MAX} 次）")
        return True

    def _go_home(self, now: float, pos: tuple[int, int] | None) -> None:
        """走回原本那張圖。到了就接著打，回不去就大聲停用。"""
        traveler = self._traveler
        if traveler is None:
            return
        if now - self._return_since > _RETURN_GIVEUP_SEC:
            self._traveler = None
            self._fail(f"⚠ 走了 {_RETURN_GIVEUP_SEC / 60:.0f} 分鐘還回不去 "
                       f"{self._home_map}，自動打怪已停止")
            return
        status = self._reader.read() if self._reader else None
        if status is None or not status.map_name or pos is None:
            return  # 換圖中間讀不到是正常過渡，這一拍不動
        state = traveler.update(status.map_name, pos)
        if state == "arrived":
            self._traveler = None
            self._walker.clear()
            self._note(f"回到 {self._home_map} 了，繼續打")
            return
        if state == "blocked":
            self._traveler = None
            self._fail(f"⚠ 回不去 {self._home_map}：{traveler.note}")
            return
        if traveler.note:
            self._note(traveler.note)

    def _escape_warp(self, pos: tuple[int, int] | None) -> bool:
        """人在傳點禁區裡就先走出去。回 True 代表這一拍在脫離，別做其他事。

        ⚠ **這是「走開」，不是「停下來」。** 使用者講得很明確：叫你別靠近
        傳點，不是叫你關掉自動戰鬥。

        為什麼要專門一步：禁區是半徑 `_WARP_KEEP_OUT` 的一片，站在中間時
        A* 的每個鄰居都被擋住（起點自己雖然豁免），等於算不出任何路 ——
        然後 45 秒沒進展就被當成卡住，`_fail()` 把自動打怪關掉。
        使用者看到的就是「自己偷偷關閉」。
        """
        terrain = self._terrain
        if terrain is None or pos is None:
            return False
        if self._escape_goal is not None:
            # 已經在往外走了就讓它走完，別每一拍重算一條新路狂送走路封包。
            if self._walker.update(pos) == "walking":
                return True
            self._escape_goal = None
        if not self._near_warp(pos):
            return False
        goal = self._nearest_outside(pos)
        if goal is None:
            return False
        # 只擋傳點本體，不擋整片禁區 —— 不然從裡面出不來。
        path = terrain.find_path(
            pos, goal, node_budget=_NEAR_BUDGET, blocked=self._warp_cells
        )
        if not path:
            return False
        self._escape_goal = goal
        self._walker.set_path(path, avoid=self._warp_cells)
        self._walker.update(pos)
        self._note(f"太靠近傳點，先走開（往 {goal[0]},{goal[1]}）")
        return True

    def _nearest_outside(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        """離 `pos` 最近、又在禁區外面的可走格。由近而遠一圈一圈找。"""
        terrain = self._terrain
        if terrain is None:
            return None
        for radius in range(1, _WARP_KEEP_OUT + _ESCAPE_MARGIN + 1):
            best = None
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue        # 只看這一圈的外環
                    cell = (pos[0] + dx, pos[1] + dy)
                    if cell in self._warp_zone or not terrain.is_walkable(*cell):
                        continue
                    best = cell
                    break
                if best is not None:
                    break
            if best is not None:
                return best
        return None

    def _near_warp(self, cell: tuple[int, int] | None) -> bool:
        return cell is not None and cell in self._warp_zone

    def _is_bad_goal(self, cell: tuple[int, int]) -> bool:
        return any(
            max(abs(cell[0] - bad[0]), abs(cell[1] - bad[1])) <= _BAD_GOAL_RADIUS
            for bad, _until in self._bad_goals
        )

    # ---- 雜項 -------------------------------------------------------

    def _send_move(self, x: int, y: int) -> None:
        self._send(build_move(x, y))

    def _send(self, data: bytes) -> None:
        """送封包。失敗代表 socket 已經失效（多半是換頻道），下一拍會重綁。"""
        if self._sock is None:
            return
        if game_socket.send_on_socket(self._sock, data) < 0:
            log.warning("送封包失敗，socket 可能已失效，強制重新綁定")
            self._server = None  # 下一拍 _keep_in_sync 會重抓
            self._resync_at = 0.0

    def _cleanup(self) -> None:
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        if self._sock is not None:
            game_socket.close_socket(self._sock)
            self._sock = None
        if self._entities is not None:
            self._entities.close()
            self._entities = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def _note(self, text: str) -> None:
        # 提示字一律進**執行日誌**，不放介面（使用者指定）。
        # 只在內容變動時記一筆 —— 這支每拍都會被呼叫，照記會把日誌洗成幾百行一樣的字。
        if text and text != self._stats.note:
            log.info("%s", text)
        self._stats.note = text
        self._emit()

    def _emit(self) -> None:
        if self._on_update is not None:
            self._on_update(self._stats)
