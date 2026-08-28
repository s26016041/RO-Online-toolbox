"""自動尋路：讀出遊戲導航目標，用我們自己的傳點表走過去。

整條路的來源與界線（跟自動打怪同一套，見 GAMEDATA [PKT-022]）：

- **目的地**：記憶體唯讀（`navigation.NavigationReader`）—— 按下遊戲尋路鍵時
  客戶端一個封包都沒送，箭頭是它自己算的，只能從記憶體讀。
- **路線**：`travel.plan_route()`，資料是客戶端自己的 `navi_link_tw.lub`
  （＝遊戲畫箭頭用的同一份），所以走的是同一條路。
- **走路**：`Walker` 送 `0x035F`、等 `0x0087` 確認（[PKT-030]）。
- **換圖確認**：記憶體裡的地圖名變了才算過去，不靠睡覺。

不寫遊戲記憶體、不注入、不搶滑鼠鍵盤。

**純趕路**：途中不打怪、不撿東西，抵達就停。要打怪請自己開自動打怪 ——
兩個同時送走路封包會互相打架，所以 UI 端負責不讓它們同時開。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ro_toolbox.core.ro_protocol import build_move, unpack_move, unpack_position
from ro_toolbox.services import game_socket, mapdata, npc_dialog
from ro_toolbox.services.character import CharacterReader
from ro_toolbox.services.gamedata import map_display_name, npc_links_on_map
from ro_toolbox.services.navigation import NavigationReader
from ro_toolbox.services.packet_capture import PacketCapture
from ro_toolbox.services.ro_capture import find_server
from ro_toolbox.services.travel import Traveler
from ro_toolbox.services.walker import Walker

log = logging.getLogger(__name__)

_TICK = 0.2
_RESYNC_SEC = 2.0  # 多久檢查一次「連線有沒有換掉」
_OP_MOVE_ACK = 0x0087  # 伺服器確認「我」要移動：payload[4:10] = 起點+終點
#: 為了問出自己的座標而「推一下」的間隔與次數上限。
#: 逾時只是**放棄的上限**：真正的成功依據是伺服器回的 0x0087 起點。
_NUDGE_EVERY_SEC = 2.0
_NUDGE_TRIES = 5
#: 實體進入視野的封包（版面見 services/world.py 的欄位表）
_OP_ENTITY = (0x09FF, 0x09FE, 0x09FD)
#: 走多遠才確定出了 NPC 的視野。RO 的視野約 14 格，抓 22 有餘裕。
_OUT_OF_VIEW = 22
#: 「走遠再走回來」最多做幾輪。做不到就跳警告交給人，不要一直來回踱步。
_SHAKE_ROUNDS = 2
#: 每一步的逾時：走不到就當這一輪失敗。只是放棄的上限。
_SHAKE_STEP_SEC = 30.0
#: 走遠／走回來算路徑的節點上限。只走幾十格，不該花時間。
_NEAR_BUDGET = 8000
_ENT_OBJTYPE = 2    # 1 byte：0=其他玩家、6=NPC（實測登入擷取確認）
_OBJTYPE_NPC = 6
_ENT_GID = 3        # uint32
_ENT_CLASS = 21     # uint16 外觀編號
_ENT_POS = 61       # 3-byte 壓縮座標
#: NPC 座標容許差幾格。Navi_Npc 給的是他站的格，實際可能差一點。
_NPC_SNAP = 3


@dataclass
class TravelStats:
    running: bool = False
    goal: str = ""  # 目的地圖（內部名）
    goal_label: str = ""  # 目的地中文名（顯示用）
    here: str = ""  # 現在在哪張圖
    hops_left: int = 0  # 還要換幾張圖
    note: str = ""
    arrived: bool = False


class TravelBot:
    """把角色帶到指定地圖。start()/stop() 控制；on_update 回報狀態。

    `destination` 是**地圖名**（穩定的身分），不是座標也不是「第幾段路」——
    路線每次換圖都從當下位置重算，所以中途走錯、被傳送、甚至自己走回頭，
    都只會多繞路，不會走到別的地方去（CLAUDE.md：存身分不存位置）。
    """

    def __init__(
        self,
        pid: int,
        destination: str | None = None,
        on_update: Callable[[TravelStats], None] | None = None,
    ) -> None:
        """`destination` 不給就去讀**遊戲導航視窗現在指的地圖**（一般用法）。
        給了就走去那張圖 —— 測試與日後的地圖選單用得到。"""
        self._pid = pid
        self._destination = destination
        self._on_update = on_update
        self._sock: int | None = None
        self._server: tuple[str, int] | None = None
        self._reader: CharacterReader | None = None
        self._capture: PacketCapture | None = None
        #: 正在跟哪隻 NPC 講話（None = 沒有）
        self._talk: npc_dialog.NpcTalk | None = None
        #: 要找的 NPC：(外觀編號, x, y)。認人要兩個欄位同時對上。
        self._npc_want: tuple[int, int, int] | None = None
        self._npc_gid: int | None = None
        #: 「走遠再走回來」的進度：None／"away"／"back"
        self._shake = None
        self._shake_round = 0
        self._shake_since = 0.0
        self._shake_cell: tuple[int, int] | None = None
        #: 這一段的對話已經談不下去了（看不懂選單），別再重試
        self._dialog_dead: tuple | None = None
        #: 已經提醒過哪一段要人手動（避免每拍洗版）
        self._asked: tuple[str, int, int] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._walker = Walker(self._send_move)
        self._traveler = Traveler(self._walker, time.monotonic)
        self._resync_at = 0.0
        #: 伺服器最近一次在 0x0087 裡說「你在這裡」。換圖後記憶體座標會過期，
        #: 那時只有這個可信（見 `_trusted_position`）。
        self._server_pos: tuple[int, int] | None = None
        #: 上次為了「問出自己在哪」而推一下的時間，以及推了幾次。
        self._nudged_at = 0.0
        self._nudges = 0
        self._terrain_name = ""
        self._terrain = None
        self._stats = TravelStats(
            goal=destination or "",
            goal_label=map_display_name(destination) if destination else "",
        )

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> TravelStats:
        return self._stats

    # ---- 控制 -------------------------------------------------------

    def start(self) -> bool:
        """啟動。所有耗時的設定（AOB 定位、找 socket、開 pcap）都在背景執行緒做 ——
        放在 UI 執行緒會讓介面凍住、被 Windows 判定「未回應」。"""
        if self.running:
            return True
        self._stop.clear()
        self._stats = TravelStats(
            running=True,
            goal=self._stats.goal,
            goal_label=self._stats.goal_label,
            note="讀取導航目標…" if self._destination is None else "啟動中…",
        )
        self._emit()
        self._thread = threading.Thread(
            target=self._run, name=f"travel-{self._pid}", daemon=True
        )
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
        self._emit()

    # ---- 背景執行緒 -------------------------------------------------

    def _run(self) -> None:
        try:
            if not self._setup():
                return
            self._loop()
        except Exception as exc:  # noqa: BLE001 - 背景執行緒絕不能讓例外炸掉整個程式
            log.exception("自動尋路執行緒發生例外")
            self._stats.running = False
            self._note(f"發生錯誤已停止：{exc}")
        finally:
            self._cleanup()

    def _setup(self) -> bool:
        if self._destination is None and not self._read_navigation():
            return False

        self._note("正在找遊戲連線…")
        server = find_server(self._pid)
        if server is None:
            return self._fail("找不到伺服器連線（還沒登入？）")
        # ⚠ 剛連上的那幾秒複製不到，要重試（[PKT-072]）——
        # 舊版叫一次就放棄，症狀是「按下自動尋路就死在這裡」。
        self._note("正在複製遊戲連線（剛上線的幾秒可能要等一下）…")
        sock = game_socket.open_game_socket(
            self._pid, server[0], server[1], should_stop=self._stop.is_set,
        )
        if not sock:
            return self._fail("找不到遊戲 socket，無法送封包（等了也沒出現）")
        self._sock, self._server = sock, server

        # AOB 定位要一秒上下。不講一聲的話這段就是完全的沉默，
        # 使用者按下按鈕之後看不到任何字，會以為是沒反應（CLAUDE.md：狀態要跟得上行為）。
        self._note("正在定位角色（AOB 特徵掃描）…")
        reader = CharacterReader()
        if not reader.attach(self._pid, should_stop=self._stop.is_set):
            return self._fail("角色定位失敗")
        if not reader.position_located:
            # 沒有座標就沒有「我在哪」，整條尋路都不成立。與其每一拍空轉，
            # 不如立刻說清楚（CLAUDE.md：定位失敗要大聲）。
            reader.close()
            return self._fail("⚠ 角色座標定位失敗（遊戲可能已改版），自動尋路停用")
        self._reader = reader

        # Walker 靠 0x0087 判斷每一段有沒有被接受；沒有擷取就只能瞎送（[PKT-030]）
        self._note("正在啟動封包擷取…")
        capture = PacketCapture(self._pid, self._on_packet)
        if not capture.start():
            return self._fail("封包擷取啟動失敗（需要系統管理員）")
        self._capture = capture

        self._traveler.set_goal(self._destination)
        self._note(f"前往 {self._stats.goal_label or self._destination}"
                   f"（{self._destination}），正在計算路線…")
        return True

    def _read_navigation(self) -> bool:
        """把遊戲導航視窗現在指的地圖讀出來當目的地。

        讀不到就**大聲停用**，絕不退回「隨便走一張圖」——
        走錯地方比不走糟得多（CLAUDE.md：失效只能大聲停用或安全退化）。
        """
        self._note("正在讀遊戲的尋路目標…")
        reader = NavigationReader()
        try:
            if not reader.attach(self._pid):
                return self._fail("⚠ 導航目標定位失敗（遊戲可能已改版），自動尋路停用")
            destination = reader.destination()
            blank = reader.blank
        finally:
            reader.close()
        if not destination:
            if blank:
                return self._fail(
                    "⚠ 遊戲的尋路目標是空的 —— 請先在遊戲的尋路視窗選一個目的地"
                )
            return self._fail("⚠ 讀不到導航目標 —— 請先在遊戲的尋路視窗設定目的地")
        self._destination = destination
        self._stats.goal = destination
        self._stats.goal_label = map_display_name(destination)
        log.info("導航目標 = %s（%s）", destination, self._stats.goal_label)
        return True

    def _fail(self, message: str) -> bool:
        """整條停掉，並且**大聲**講。

        ⚠ 以前這裡跟一般進度一樣走 `INFO`，於是「角色座標定位失敗，自動尋路停用」
        這種硬失敗在使用者的設定下**一個字都看不到** —— 症狀就是
        「按了沒反應、不知道是在算還是壞了」（使用者實際回報）。
        CLAUDE.md：定位失敗要大聲，失效模式只准「大聲停用」或「安全退化」。
        """
        self._stats.running = False
        self._note(message, logging.WARNING)
        return False

    def _on_packet(self, packet) -> None:  # noqa: ANN001 - RoPacket，避免循環匯入
        if packet.outbound:
            return
        if packet.opcode == _OP_MOVE_ACK and len(packet.payload) >= 10:
            start, dest = unpack_move(packet.payload[4:10])
            # ⚠ **起點不要丟掉。** 這是伺服器認定「我現在在哪」——
            # 換地圖之後記憶體裡的座標會停在上一張圖（[MEM-022]），
            # 那時候這個值是唯一可信的來源（見 `_trusted_position`）。
            self._server_pos = start
            self._walker.note_move_ack(dest)
            return
        talk = self._talk
        if talk is not None:
            talk.feed(packet.opcode, packet.payload)
            return
        # 還沒開始對話：實體進入視野時把「那隻 NPC」的 GID 記下來。
        # 認人靠**兩個欄位同時對上**（外觀編號 ＋ 座標，都來自 RODATA 的
        # Navi_Npc，見 [DAT-027]），不是靠猜一個編號。
        if packet.opcode in _OP_ENTITY and self._npc_want is not None:
            self._note_entity(packet.payload)

    def _note_entity(self, payload: bytes) -> None:
        want = self._npc_want
        if want is None or len(payload) < _ENT_POS + 3:
            return
        # objtype 6 = NPC（實測登入擷取：0=其他玩家、6=NPC、GID 只有兩三位數）
        if payload[_ENT_OBJTYPE] != _OBJTYPE_NPC:
            return
        class_id = int.from_bytes(payload[_ENT_CLASS:_ENT_CLASS + 2], "little")
        if class_id != want[0]:
            return
        x, y, _dir = unpack_position(payload[_ENT_POS:_ENT_POS + 3])
        if max(abs(x - want[1]), abs(y - want[2])) > _NPC_SNAP:
            return
        gid = int.from_bytes(payload[_ENT_GID:_ENT_GID + 4], "little")
        if gid and self._npc_gid != gid:
            self._npc_gid = gid
            log.info("認出 NPC：外觀 %s 在 (%s,%s)，GID=%s", class_id, x, y, gid)

    # ---- 主迴圈 -----------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            if not self._keep_in_sync(now):
                return
            status = self._reader.read() if self._reader else None
            pos = self._reader.read_position() if self._reader else None
            if status is not None and status.hp <= 0:
                self._fail("⚠ 角色已死亡，自動尋路已停止")
                return
            # 座標讀不到（換圖中、或定位失效）就這一拍不動 ——
            # 絕不拿讀不到當成「在原點」，那正是 [MEM-039] 踩過的坑。
            if status is None or not status.map_name or pos is None:
                self._stop.wait(_TICK)
                continue

            pos = self._trusted_position(status.map_name, pos)
            if pos is None:
                self._stop.wait(_TICK)
                continue

            self._stats.here = status.map_name
            self._watch_next_npc()
            state = self._traveler.update(status.map_name, pos)
            if state == "waiting":
                # 走到 NPC 面前了。有外觀編號就自己跟他講話；
                # 對話走不完（看不懂選單、沒回應）就退回「等你手動做」。
                self._run_dialog()
            self._stats.hops_left = len(self._traveler.route)
            # ⚠ 這裡要走 `_note()`，不能直接指派：`Traveler` 一路算出來的狀態
            # （正在計算路線、還要換幾張圖、踩傳點、等座標更新…）本來只寫進
            # `stats.note`，而 `stats.note` 只有介面在看 —— 介面又刻意不顯示它。
            # 結果整段趕路過程在執行日誌裡是**全白的**，使用者只看得到頭尾。
            # `_note()` 自己會擋掉重複，每拍呼叫也不會洗版。
            if self._traveler.note:
                self._note(self._traveler.note)

            if state == "arrived":
                self._stats.arrived = True
                self._stats.running = False
                self._note(f"已抵達 {self._stats.goal_label or self._destination}")
                return
            if state == "blocked":
                self._stats.running = False
                self._note(self._traveler.note or "⚠ 到不了目的地，已停止")
                return

            self._emit()
            self._stop.wait(_TICK)

    # ---- 自己跟 NPC 講話 --------------------------------------------

    def _shake_view(self, hop) -> None:  # noqa: ANN001 - travel.Hop
        """走遠再走回來，逼遊戲重送 NPC 的「進入視野」封包。

        ⚠ 為什麼非這樣不可：接觸 NPC 的封包要 **GID**，而
        - GID 是**伺服器執行時**給的：RODATA 沒有（實測 `Navi_Npc` 給 izlude
          船員的編號是 18518，實際封包用的是 **91**）；
        - 記憶體的怪物掃描器**看不到 NPC**（靠 `alive==1` 當錨，[MEM-047]）；
        - 實體只在**進入視野**時送一次封包（[PKT-061]）。

        ⚠ **一定要走 `Walker`，不能自己送一個很遠的走路封包**：
        單次移動有距離上限，**超過 17 格伺服器直接忽略**（[PKT-030]）——
        實測踩過，人就站在卡普拉旁邊發呆，一個錯誤訊息都沒有。
        `Walker` 會把路徑切成 14 格一段送。

        每一步都等**讀得到的訊號**（自己的座標到了沒），不是等秒數；
        逾時只是放棄的上限。做滿 `_SHAKE_ROUNDS` 輪還認不出來就交給人。
        """
        pos = self._reader.read_position() if self._reader else None
        terrain = self._traveler.terrain
        if pos is None or terrain is None:
            return
        npc = (hop.x, hop.y)
        now = time.monotonic()
        far = max(abs(pos[0] - npc[0]), abs(pos[1] - npc[1]))

        if self._shake is None:
            cell = terrain.random_walkable(
                random, near=npc, radius=_OUT_OF_VIEW + 8, min_radius=_OUT_OF_VIEW
            )
            if cell is None or not self._walk_to(pos, cell):
                return
            self._shake_round += 1
            self._shake, self._shake_since = "away", now
            self._note(f"認不出「{hop.npc}」，先走遠一點讓他重新進視野"
                       f"（第 {self._shake_round} 次，往 {cell[0]},{cell[1]}）")
            return

        state = self._walker.update(pos)
        if now - self._shake_since > _SHAKE_STEP_SEC:
            log.info("走遠／走回來這一段超時（目前離 %s 有 %d 格），重來", hop.npc, far)
            self._shake = None
            self._walker.clear()
            return

        if self._shake == "away":
            if far >= _OUT_OF_VIEW:
                self._shake, self._shake_since = "back", now
                self._note(f"出了「{hop.npc}」的視野，走回去")
                self._walk_to(pos, npc)
            elif state in ("arrived", "blocked"):
                self._shake = None          # 走到了卻還不夠遠 → 換個目標重來
                self._walker.clear()
            return

        if far <= _NPC_SNAP or state == "arrived":
            # 回到他旁邊了。他重新進視野時那一包就會到，`_note_entity` 會接住。
            self._shake = None
            self._walker.clear()
            log.info("回到「%s」旁邊了，等他的實體封包", hop.npc)

    def _walk_to(self, start: tuple[int, int], goal: tuple[int, int]) -> bool:
        """把一條路交給 `Walker` 去走。算不出路回 False。"""
        terrain = self._traveler.terrain
        if terrain is None:
            return False
        path = terrain.find_path(start, goal, node_budget=_NEAR_BUDGET)
        if not path:
            log.info("走不到 %s，這一輪放棄", goal)
            return False
        self._walker.set_path(path)
        self._walker.update(start)
        return True

    def _ask_for_help(self, hop) -> None:  # noqa: ANN001 - travel.Hop
        """認不出那隻 NPC —— 講清楚要你做什麼，而且**講出要選哪張地圖**。

        什麼時候會這樣：按下自動尋路時人**已經站在他視野內**（約 14 格），
        那一包早就送完了。實體只在「進入視野」時送一次（[PKT-061]），
        而 GID 只有那一包給得起 —— RODATA 沒有（實測 `Navi_Npc` 給 18518、
        實際封包用 91），怪物掃描器也看不到 NPC（[MEM-047]）。

        ⚠ **只講一次**，不要每拍洗版；到了（地圖名一變）就自動接手。
        """
        if self._asked == (hop.from_map, hop.x, hop.y):
            return
        self._asked = (hop.from_map, hop.x, hop.y)
        where = map_display_name(hop.to_map) or hop.to_map
        log.warning(
            "⚠ 認不出「%s」（你按下按鈕時已經站在他旁邊，進視野那一包早就送完了）。"
            "請自己跟他講話，選「%s」—— 到了我就自動繼續走。"
            "（下次先站遠一點再按，或先過一張圖，我就能自己講。）",
            hop.npc, where,
        )

    def _watch_next_npc(self) -> None:
        """路線上**下一段**如果要跟 NPC 講話，現在就開始留意他的實體封包。

        ⚠ 為什麼不能等走到才開始盯：**實體只在「進入視野」時送一次封包**
        （[PKT-061]）。那一包是**走路途中**送來的，等站到他面前才開始看
        就永遠等不到 —— 實測就是這樣，走到船員面前卻不開口。

        （記憶體那條走不通：怪物掃描器靠 `alive==1` 當錨，那是怪物專用的旗標，
        實測掃 40 輪一個 NPC 都看不到，見 [MEM-047]。）
        """
        route = self._traveler.route
        hop = route[0] if route else None
        want = (hop.npc_id, hop.x, hop.y) if hop is not None and hop.npc_id else None
        if want != self._npc_want:
            self._npc_want = want
            self._npc_gid = None
            self._talk = None
            self._asked = None
            self._dialog_dead = None
            self._shake = None
            self._shake_round = 0
            if want is not None:
                log.info("留意「%s」（外觀 %s，在 %s,%s）的實體封包",
                         hop.npc, want[0], want[1], want[2])

    def _run_dialog(self) -> None:
        """把 `Traveler` 停下來等的那一段 NPC 對話走完。

        ⚠ **只送封包、不碰記憶體**，而且**不判定「過去了」** ——
        真的到了沒有一律看地圖名有沒有變（`Traveler` 負責，[DAT-026]）。
        對話失敗不當成致命：退回「停著等你手動做」，那條路本來就是好的。
        """
        hop = self._traveler.npc_hop
        if hop is None or not hop.npc_id:
            return
        key = (hop.from_map, hop.x, hop.y)
        if self._dialog_dead == key:
            # 已經跟他講過而且講不下去了（看不懂選單之類）。**不要再回頭**
            # 走「認不出他」那條路 —— 那會印出「你站在他旁邊」這種完全不對的話，
            # 而且會一直來回走位。已經跟人講清楚了，就安靜等他處理。
            return
        if self._talk is None:
            if self._npc_gid is None:
                # 先自己想辦法：走遠再走回來，逼他重新進一次視野。
                # 兩輪都不行才跳警告叫人 —— 能自己做的不要麻煩使用者。
                if self._shake_round < _SHAKE_ROUNDS or self._shake is not None:
                    self._shake_view(hop)
                else:
                    self._ask_for_help(hop)
                return
            want = map_display_name(hop.to_map)
            # 這隻 NPC（同一個座標）在我們的資料裡只通往一個地方嗎？
            # 只有這種時候，「使用 / 結束」那種**沒有地名的確認選單**才准點 ——
            # 有好幾個目的地卻跳確認，代表我們看漏了什麼，寧可停手。
            here = [
                link for link in npc_links_on_map(hop.from_map)
                if (link[0], link[1]) == (hop.x, hop.y)
            ]
            self._talk = npc_dialog.NpcTalk(
                self._npc_gid, want, npc=hop.npc, sole=len(here) == 1
            )
            log.info("「%s」在資料裡有 %d 個目的地", hop.npc, len(here))
            log.info("開始跟「%s」(GID %s) 對話，想去 %s",
                     hop.npc, self._npc_gid, want)
        talk = self._talk
        while (data := talk.next_packet()) is not None:
            self._send(data)
        if talk.failed:
            # ⚠ 保留 `_npc_want` 與 `_npc_gid`：認人是成功的，失敗的是「看不懂選單」。
            # 清掉的話下一拍會重新走「認不出他」那條路，講出完全不對的原因。
            self._note(f"{talk.note} —— 請自己跟「{hop.npc}」講話，我在這裡等")
            self._talk = None
            self._dialog_dead = key
        elif talk.done:
            self._note(f"{talk.note}；等換圖…")

    def _keep_in_sync(self, now: float) -> bool:
        """換頻道／換地圖伺服器之後要重綁 socket，否則封包全部石沉大海。

        踩過的坑見 [PKT-038]／farm_bot 的同名函式：換圖時伺服器會把連線移到
        另一台地圖伺服器，舊 socket 送出去的東西**不會報錯，只是沒人收**。
        """
        if now - self._resync_at < _RESYNC_SEC:
            return True
        self._resync_at = now
        server = find_server(self._pid)
        if server is None:
            if self._server is not None:
                return self._fail("⚠ 遊戲連線已中斷，自動尋路已停止")
            return True
        if server == self._server:
            return True
        log.info("連線 %s → %s，重新綁定", self._server, server)
        if self._sock is not None:
            game_socket.close_socket(self._sock)
            self._sock = None
        sock = game_socket.open_game_socket(
            self._pid, server[0], server[1],
            timeout=game_socket.SOCKET_REBIND_SEC, should_stop=self._stop.is_set,
        )
        if not sock:
            return self._fail("⚠ 換頻道後找不到新的遊戲 socket，自動尋路已停止")
        self._sock, self._server = sock, server
        return True

    # ---- 雜項 -------------------------------------------------------

    def _send(self, data: bytes) -> bool:
        """送一個封包（走路、對話都走這裡）。回 False 代表 socket 可能失效了。

        走**複製出來的遊戲 socket**，全程不碰記憶體（CLAUDE.md：RO 掛
        GameGuard，寫記憶體會被反制）。
        """
        if self._sock is None:
            return False
        if game_socket.send_on_socket(self._sock, data) < 0:
            log.warning("送封包失敗，socket 可能已失效，強制重新綁定")
            self._server = None
            self._resync_at = 0.0
            return False
        return True

    def _trusted_position(self, map_name: str, pos: tuple[int, int]):
        """回一個**確定在這張圖上**的座標；問不出來回 None（這一拍不動）。

        ## 為什麼需要

        [MEM-022]：**換地圖之後記憶體裡的座標會停在上一張圖的最後位置**，
        要等角色再走一步客戶端才會寫新的。被傳進一間店（或任何小圖）而人又
        沒動的時候，那個舊值會一直掛在那裡。

        使用者實機（2026-08-28）：被傳進 `s_atelier`（**200×140**）之後，
        座標一直是 `(271, 108)` —— 那是上一張 prontera（312×392）的位置。
        `Traveler` 正確地判斷「這座標不在這張圖上」，但反應是**停掉**：

            ⚠ 進 s_atelier 後 10 秒，座標 (271, 108) 仍不在這張圖上，已停止

        判斷沒錯，錯的是沒有辦法問出真正的位置。

        ## 怎麼問

        伺服器在 `0x0087` 裡同時給**起點**與終點 —— 起點就是它認定我們在哪。
        本來我們只取終點，起點丟掉了。現在留著（見 `_on_packet`）。

        站著不動的時候不會有 `0x0087`，所以**推一下**：往這張圖上任何一格
        可走的地方送一次移動。伺服器回的那包就會告訴我們真正的起點。
        推的那一步走去哪不重要 —— 知道位置之後 `Traveler` 會重新規劃。
        """
        terrain = self._terrain_for(map_name)
        if terrain is None:
            return pos          # 沒有地形就沒得驗，照舊用記憶體的值
        # ⚠ 只看**在不在這張圖的範圍內**，不看走不走得過 ——
        # 站在傳點上、站在邊界格上都是合法的位置。
        if 0 <= pos[0] < terrain.width and 0 <= pos[1] < terrain.height:
            self._nudges = 0
            return pos
        server = self._server_pos
        if server is not None and (
            0 <= server[0] < terrain.width and 0 <= server[1] < terrain.height
        ):
            self._note(f"記憶體座標 {pos} 不在 {map_name} 上，改用伺服器說的 {server}")
            self._nudges = 0
            return server
        self._nudge(terrain, map_name)
        return None

    def _nudge(self, terrain, map_name: str) -> None:
        """往任意一格可走的地方送一次移動，逼伺服器回報我們在哪。"""
        now = time.monotonic()
        if now - self._nudged_at < _NUDGE_EVERY_SEC:
            return
        self._nudged_at = now
        if self._nudges >= _NUDGE_TRIES:
            self._fail(
                f"⚠ 進 {map_name} 之後問不出自己的座標（推了 {self._nudges} 次），"
                "自動尋路已停止"
            )
            return
        target = self._any_walkable(terrain)
        if target is None:
            self._fail(f"⚠ {map_name} 上找不到任何可走的格子，自動尋路已停止")
            return
        self._nudges += 1
        self._note(f"座標還停在上一張圖，往 {target} 走一步問出真正的位置…")
        self._send_move(*target)

    @staticmethod
    def _any_walkable(terrain):
        """這張圖上離中心最近的一格可走的地方。找不到回 None。"""
        import numpy as np

        cells = np.argwhere(terrain.walkable)
        if not len(cells):
            return None
        centre = np.array([terrain.height / 2, terrain.width / 2])
        best = cells[np.argmin(((cells - centre) ** 2).sum(axis=1))]
        return int(best[1]), int(best[0])

    def _terrain_for(self, map_name: str):
        """這張圖的地形（記著上一張，不要每拍重讀）。讀不到回 None。"""
        if map_name != self._terrain_name:
            self._terrain_name = map_name
            self._terrain = None
            try:
                self._terrain = mapdata.load_terrain(map_name)
            except Exception as exc:  # noqa: BLE001 - 沒地形就退回舊行為
                log.debug("讀不到 %s 的地形：%s", map_name, exc)
        return self._terrain

    def _send_move(self, x: int, y: int) -> None:
        self._send(build_move(x, y))

    def _cleanup(self) -> None:
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        if self._sock is not None:
            game_socket.close_socket(self._sock)
            self._sock = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    @property
    def destination(self) -> str | None:
        """已經解析出來的目的地地圖。還沒讀到回 None。

        自動回連要用它做快照 —— 重連之後遊戲的導航目標不一定還在，
        所以要把**答案本身**記下來，不是記「回來再去問遊戲」。
        """
        return self._destination

    def _note(self, text: str, level: int = logging.INFO) -> None:
        """提示字一律進**執行日誌**，不放介面（使用者指定）。

        只在內容變動時記一筆 —— 這支每拍都會被呼叫，照記會把日誌洗成幾百行一樣的字。

        `level`：**停用等級的壞消息要用 `WARNING`**（見 `_fail`）。
        進度用 INFO 就好。
        """
        if text and text != self._stats.note:
            log.log(level, "%s", text)
        self._stats.note = text
        self._emit()

    def _emit(self) -> None:
        if self._on_update is not None:
            self._on_update(self._stats)
