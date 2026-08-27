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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ro_toolbox.core.ro_protocol import build_move, unpack_move, unpack_position
from ro_toolbox.services import game_socket, npc_dialog
from ro_toolbox.services.character import CharacterReader
from ro_toolbox.services.gamedata import map_display_name
from ro_toolbox.services.navigation import NavigationReader
from ro_toolbox.services.packet_capture import PacketCapture
from ro_toolbox.services.ro_capture import find_server
from ro_toolbox.services.travel import Traveler
from ro_toolbox.services.walker import Walker

log = logging.getLogger(__name__)

_TICK = 0.2
_RESYNC_SEC = 2.0  # 多久檢查一次「連線有沒有換掉」
_OP_MOVE_ACK = 0x0087  # 伺服器確認「我」要移動：payload[4:10] = 起點+終點
#: 實體進入視野的封包（版面見 services/world.py 的欄位表）
_OP_ENTITY = (0x09FF, 0x09FE, 0x09FD)
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
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._walker = Walker(self._send_move)
        self._traveler = Traveler(self._walker, time.monotonic)
        self._resync_at = 0.0
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

        server = find_server(self._pid)
        if server is None:
            return self._fail("找不到伺服器連線（還沒登入？）")
        sock = game_socket.find_game_socket(self._pid, server[0], server[1])
        if not sock:
            return self._fail("找不到遊戲 socket，無法送封包")
        self._sock, self._server = sock, server

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
        capture = PacketCapture(self._pid, self._on_packet)
        if not capture.start():
            return self._fail("封包擷取啟動失敗（需要系統管理員）")
        self._capture = capture

        self._traveler.set_goal(self._destination)
        self._note(f"前往 {self._stats.goal_label or self._destination}")
        return True

    def _read_navigation(self) -> bool:
        """把遊戲導航視窗現在指的地圖讀出來當目的地。

        讀不到就**大聲停用**，絕不退回「隨便走一張圖」——
        走錯地方比不走糟得多（CLAUDE.md：失效只能大聲停用或安全退化）。
        """
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
        self._stats.running = False
        self._note(message)
        return False

    def _on_packet(self, packet) -> None:  # noqa: ANN001 - RoPacket，避免循環匯入
        if packet.outbound:
            return
        if packet.opcode == _OP_MOVE_ACK and len(packet.payload) >= 10:
            _start, dest = unpack_move(packet.payload[4:10])
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

            self._stats.here = status.map_name
            state = self._traveler.update(status.map_name, pos)
            if state == "waiting":
                # 走到 NPC 面前了。有外觀編號就自己跟他講話；
                # 對話走不完（看不懂選單、沒回應）就退回「等你手動做」。
                self._run_dialog()
            self._stats.hops_left = len(self._traveler.route)
            self._stats.note = self._traveler.note

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

    def _run_dialog(self) -> None:
        """把 `Traveler` 停下來等的那一段 NPC 對話走完。

        ⚠ **只送封包、不碰記憶體**，而且**不判定「過去了」** ——
        真的到了沒有一律看地圖名有沒有變（`Traveler` 負責，[DAT-026]）。
        對話失敗不當成致命：退回「停著等你手動做」，那條路本來就是好的。
        """
        hop = self._traveler.npc_hop
        if hop is None or not hop.npc_id:
            return
        if self._npc_want != (hop.npc_id, hop.x, hop.y):
            # 換了一段 NPC：重新開始認人
            self._npc_want = (hop.npc_id, hop.x, hop.y)
            self._npc_gid = None
            self._talk = None
        if self._talk is None:
            if self._npc_gid is None:
                return       # 還沒認出那隻 NPC（等他進視野的封包）
            want = map_display_name(hop.to_map)
            self._talk = npc_dialog.NpcTalk(self._npc_gid, want)
            log.info("開始跟「%s」(GID %s) 對話，想去 %s",
                     hop.npc, self._npc_gid, want)
        talk = self._talk
        while (data := talk.next_packet()) is not None:
            self._send(data)
        if talk.failed:
            self._note(f"{talk.note} —— 請自己跟「{hop.npc}」講話，我在這裡等")
            self._talk = None
            self._npc_want = None      # 別一直重試，交給人
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
        sock = game_socket.find_game_socket(self._pid, server[0], server[1])
        if not sock:
            return self._fail("⚠ 換頻道後找不到新的遊戲 socket，自動尋路已停止")
        self._sock, self._server = sock, server
        return True

    # ---- 雜項 -------------------------------------------------------

    def _send_move(self, x: int, y: int) -> None:
        if self._sock is None:
            return
        if game_socket.send_on_socket(self._sock, build_move(x, y)) < 0:
            log.warning("送封包失敗，socket 可能已失效，強制重新綁定")
            self._server = None
            self._resync_at = 0.0

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
