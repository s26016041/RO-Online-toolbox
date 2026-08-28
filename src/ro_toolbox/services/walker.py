"""沿 A* 路徑「連續」走路：不停頓、每一段都要伺服器確認。

為什麼要獨立一個元件（三個實測結論，見 GAMEDATA [PKT-030]）：

1. **單次移動有上限**：≤17 格伺服器接受、18 格直接被忽略。要走遠只能切成
   一段一段送，所以需要沿路徑往前挑下一個目標。
2. **走到才送下一段 = 停頓**。實測走路速度約 1 格 / 0.15 秒，等「走到」再送
   下一段，中間會空掉一個判斷週期，角色就是走一小段停一下。
   實測**走路途中改送新目標，伺服器照樣確認、角色不會停**，
   所以剩幾步就先把下一段送出去，走起來是連續的。
3. **被拒絕的移動是靜默的**：伺服器不回任何錯誤，就是不動。實測有角色對著
   一個到不了的點連送 12 次、原地站 46 秒。所以送出後要等 `0x0087`
   （伺服器確認移動）；沒等到就是被拒絕，要立刻改別條路，不能傻等。
4. **走路會被打斷**：途中被怪打，角色就停在半路，而且**伺服器一樣不吭聲**
   （使用者實測回報）。所以「位置停住」不能直接當成走不成 ——
   要先把這一段重送、把腳步接回去；重送幾次都救不回來才算真的被擋住。
   判斷依據一律是**讀得到的訊號**（自己的座標有沒有在動），不是睡幾秒。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

#: 單次移動最多幾格。
#:
#: ⚠⚠ **舊值 14 是錯的**，而且錯在一個很難發現的地方：[PKT-030] 的「≤17 接受、
#: 18 忽略」是**直線**量出來的，**斜走的上限低很多**。
#: 使用者實機 2026-08-28 在 `izlu2dun` 現場逐格量（同一條開闊的斜線）：
#:
#:     斜走 14 格 → **被忽略**      斜走 10 格 → 動了
#:     斜走 12 格 → **被忽略**      斜走 8 / 6 格 → 動了
#:
#: 而被忽略是**靜默**的（伺服器不回任何錯誤），所以症狀是「角色站著發呆」。
#: 取 10 —— 量到會動的最大值。
MAX_STEP = 10
#: 換新路徑時，**學到的步幅至少留到這麼大**（見 `set_path`）。
#: 不留的話每次重新規劃都把步幅打回 `MAX_STEP`，等於永遠學不會。
CARRY_MIN = 4
#: 剩幾步到目前走點就先送下一段（不停頓的關鍵）。
LOOKAHEAD = 4
#: 送出移動後多久沒收到 0x0087 就當被拒絕。實測確認延遲約 0.04 秒。
ACK_TIMEOUT = 0.4
#: 位置多久沒變就先**把同一段再送一次**。
#:
#: 為什麼要重送：RO 走路途中被怪打會被打斷 —— 角色停在半路，
#: 伺服器**不會送任何錯誤**，畫面上就是站著不動（使用者實測回報）。
#: 沒有重送的話要等 `STUCK_SEC` 才判定「這條路走不成」，然後整條路線重規劃：
#: 明明只是被打了一下，卻變成繞遠路，路上怪多的時候會一直重來。
#:
#: ⚠ 使用者實測（2026-08-28）：**被好幾隻怪同時打的時候 0.5 秒太慢** ——
#: 打斷來得比重送快，人就一直站在原地。0.25 秒 ≈ 一格半的時間，
#: 正常走路每一拍位置都在變，不會誤觸；被打斷時等於「手動點快一點」。
#: 這是玩家本來就會做的事（連點），不是洗封包：只在**停住**時才送，
#: 最多每 0.25 秒一次。
RESEND_SEC = 0.25
#: 連續重送幾次（位置完全沒動）才准放棄這條路。
#:
#: ⚠ 重送變快了，次數就要跟著變多，否則「重送用完」會比 `STUCK_SEC` 早太多，
#: 等於變相縮短了救援時間 —— 那跟使用者要的正好相反。
#: 6 × 0.25 = 1.5 秒，仍在 `STUCK_SEC` 之內。
MAX_RESEND = 6
#: 位置多久沒變就當走不動（走路速度約 1 格/0.15 秒）。
#: ⚠ 重送用完**而且**停超過這個時間才判定 blocked ——
#: 「被打斷」與「真的被擋住」的差別就是重送有沒有救回來。
STUCK_SEC = 2.0
#: 偏離路徑超過幾格就當這條路走不成，重新規劃。
OFF_PATH = 5


def line_cells(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    """a→b 的直線經過哪些格（含兩端）。Bresenham。

    用途見 `Walker._clear_line`：我們送出去的是「走到這一格」，
    **中間那段路是伺服器自己算的**，不會照我們的 A* 走。
    直線是伺服器路徑最好的近似。
    """
    (x0, y0), (x1, y1) = a, b
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    out = [(x0, y0)]
    while (x0, y0) != (x1, y1):
        err2 = err * 2
        if err2 > -dy:
            err -= dy
            x0 += sx
        if err2 < dx:
            err += dx
            y0 += sy
        out.append((x0, y0))
    return out


class Walker:
    """把一條逐格路徑走完。呼叫端每拍呼叫 `update(pos)`，看回傳的狀態決定下一步。

    狀態：
        idle      沒有路徑
        walking   正在走（含剛送出新的一段）
        arrived   走到終點了
        blocked   這條路走不成（被拒絕或卡住）→ 呼叫端該重新規劃
    """

    def __init__(
        self,
        send_move: Callable[[int, int], None],
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send_move = send_move
        self._now = now
        self._path: list[tuple[int, int]] = []
        self._index = 0
        self._target: tuple[int, int] | None = None
        self._sent_at = 0.0
        self._acked = True
        self._step = MAX_STEP
        self._pos: tuple[int, int] | None = None
        self._pos_at = 0.0
        self._resends = 0      # 這一次「停住」已經重送幾次
        self._resent_at = 0.0
        #: 這一段路不准經過的格子（自動打怪拿它擋傳點）。見 `_clear_line`。
        self._avoid: frozenset[tuple[int, int]] = frozenset()
        self._ack_lock = threading.Lock()
        self._ack_dest: tuple[int, int] | None = None
        #: 診斷用計數
        self.sent = 0
        self.rejected = 0
        self.resent = 0

    # ---- 由封包執行緒呼叫 -------------------------------------------

    def note_move_ack(self, dest: tuple[int, int]) -> None:
        """收到 0x0087（伺服器確認我要移動到哪）時呼叫。"""
        with self._ack_lock:
            self._ack_dest = dest

    # ---- 由主迴圈呼叫 -----------------------------------------------

    @property
    def active(self) -> bool:
        return bool(self._path)

    @property
    def goal(self) -> tuple[int, int] | None:
        return self._path[-1] if self._path else None

    @property
    def target(self) -> tuple[int, int] | None:
        """最後送出去的那一段要走到哪。還沒送回 None。

        被傳走之後要用它推「踩到哪裡出事」—— 我們 0.2 秒才取樣一次座標，
        中間那段是伺服器走的，只知道在「最後看到的位置 → 這個目標」之間。
        """
        return self._target

    def set_path(
        self,
        cells: list[tuple[int, int]],
        avoid: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None,
    ) -> None:
        """交一條路徑給它走。

        `avoid` 是「這一段直線不准經過」的格子（自動打怪用它擋傳點）。
        ⚠ 光是把 A* 算得繞開傳點**不夠**：我們一次送 14 格，
        中間那段路是伺服器自己算的（[PKT-030]），它會抄近路穿過去。
        """
        self._avoid = frozenset(avoid) if avoid else frozenset()
        self._path = list(cells)
        self._index = 0
        self._target = None
        self._acked = True
        # ⚠⚠ **不要把步幅打回 MAX_STEP。**
        # 移動被拒絕時 `update()` 會把步幅對半縮（14→7→3…）去找伺服器肯收的
        # 長度，但呼叫端一發現「這段走不成」就會重新規劃、重新 `set_path()` ——
        # 打回原值等於把剛學到的東西丟掉，於是**同一個太遠的目標送到天荒地老**。
        # 使用者實機 2026-08-28：izlu2dun 上連送 21 個一模一樣的封包
        #（解回來都是同一格 (130,78)），角色一步都沒動。
        # 成功一次（收到 0x0087）就會自己回到 MAX_STEP，所以不必怕縮了回不去。
        self._step = min(MAX_STEP, max(self._step, CARRY_MIN))
        self._pos = None
        self._resends = 0
        self._resent_at = 0.0

    def clear(self) -> None:
        self._path = []
        self._index = 0
        self._target = None
        self._acked = True
        self._resends = 0

    def update(self, pos: tuple[int, int]) -> str:
        if not self._path:
            return "idle"
        now = self._now()

        if pos != self._pos:
            self._pos = pos
            self._pos_at = now
            self._resends = 0   # 動了就是接回去了，重送次數重新起算

        index = self._progress(pos)
        if index is None:
            log.debug("走路偏離路徑，重新規劃")
            self.clear()
            return "blocked"
        self._index = index

        if self._reached_goal(pos):
            self.clear()
            return "arrived"

        # 送出去的那一段有沒有被伺服器接受？沒有就是被靜默拒絕了。
        if self._target is not None and not self._acked:
            if self._take_ack(self._target):
                self._acked = True
                self._step = MAX_STEP  # 這一段成功了，下一段恢復用最大步幅
            elif now - self._sent_at > ACK_TIMEOUT:
                self.rejected += 1
                self._step = self._step // 2
                self._target = None
                if self._step < 2:
                    self.clear()
                    return "blocked"
                log.debug("移動被拒絕，改用 %d 格一段", self._step)

        # 停住了。**先假設是被打斷，把同一段再送一次**（被怪打是最常見的原因，
        # 伺服器不會吭聲）；重送用完而且還是不動，才當這條路真的走不成。
        if self._target is not None and now - self._pos_at > RESEND_SEC:
            if self._resends >= MAX_RESEND and now - self._pos_at > STUCK_SEC:
                self.clear()
                return "blocked"
            if self._resends < MAX_RESEND and now - self._resent_at >= RESEND_SEC:
                self._resends += 1
                self._resent_at = now
                self.resent += 1
                log.debug("停住 %.1f 秒，重送這一段（第 %d 次）",
                          now - self._pos_at, self._resends)
                # 從**現在站的地方**重挑目標，不是把舊的原封不動再送一次：
                # 被擊退／被拉走的話舊目標可能已經超過單次移動上限。
                self._target = None
                self._send_next(pos, now)
                if not self._path:
                    return "blocked"
                return "walking"

        if self._needs_next(pos):
            self._send_next(pos, now)
            if not self._path:  # 路徑上找不到還能走的下一段
                return "blocked"
        return "walking"

    # ---- 內部 -------------------------------------------------------

    def _take_ack(self, expect: tuple[int, int] | None = None) -> bool:
        """取走待處理的確認。expect 有給就要對得上（容忍伺服器微調落點）。"""
        with self._ack_lock:
            dest = self._ack_dest
            if dest is None:
                return False
            if expect is not None:
                if max(abs(dest[0] - expect[0]), abs(dest[1] - expect[1])) > 3:
                    return False  # 不是我這次要求的移動（例如被擊退）
            self._ack_dest = None
            return True

    def _progress(self, pos: tuple[int, int]) -> int | None:
        """目前走到路徑的第幾格。偏離太遠回 None。"""
        best_index, best_distance = self._index, 1 << 30
        end = min(len(self._path), self._index + MAX_STEP + OFF_PATH + 1)
        for i in range(self._index, end):
            cell = self._path[i]
            distance = max(abs(cell[0] - pos[0]), abs(cell[1] - pos[1]))
            if distance < best_distance:
                best_index, best_distance = i, distance
        return None if best_distance > OFF_PATH else best_index

    def _reached_goal(self, pos: tuple[int, int]) -> bool:
        goal = self._path[-1]
        return max(abs(goal[0] - pos[0]), abs(goal[1] - pos[1])) <= 1

    def _needs_next(self, pos: tuple[int, int]) -> bool:
        if self._target is None:
            return True
        left = max(abs(self._target[0] - pos[0]), abs(self._target[1] - pos[1]))
        return left <= LOOKAHEAD

    def _clear_line(self, start: tuple[int, int], goal: tuple[int, int]) -> bool:
        """從 start 直線走到 goal 會不會經過禁區。

        ⚠ 為什麼要看直線：路徑是我們算的，但**每一段中間怎麼走是伺服器決定的**
        （[PKT-030]）。A* 繞得再漂亮，一段送 14 格照樣可能被伺服器帶著抄近路
        穿過傳點 —— 使用者實測回報的「打怪走一走被傳走」就是這樣來的。
        碰到禁區就把這一段縮短，短到伺服器沒有近路可抄。

        **起點附近那一段不算**：人本來就可能站在禁區裡（剛被傳過來、
        或怪把我們引過去了），算進去的話每一段都被否決、一步都走不出去 ——
        那是 [MEM-044] 已經踩過的同一個坑。
        """
        if not self._avoid:
            return True
        left = False
        for cell in line_cells(start, goal):
            if cell in self._avoid:
                if left:
                    return False
            else:
                left = True
        return True

    def _send_next(self, pos: tuple[int, int], now: float) -> None:
        """挑路徑上「還在單次移動上限內、而且直線過去不會踩到禁區」的最遠一格。"""
        target = None
        limit = min(len(self._path) - 1, self._index + self._step)
        for i in range(limit, self._index - 1, -1):
            cell = self._path[i]
            if max(abs(cell[0] - pos[0]), abs(cell[1] - pos[1])) > self._step:
                continue
            if not self._clear_line(pos, cell):
                continue  # 這一段伺服器可能抄近路穿過傳點 —— 換近一點的
            target = cell
            break
        if target is None and self._index + 1 < len(self._path):
            # 每一段都被否決：至少往前挪一格（下一格就在 A* 算好的路上，
            # 它本來就繞開了禁區）。絕不原地不動 —— 那會被當成卡住。
            nxt = self._path[self._index + 1]
            if nxt != pos:
                target = nxt
        if target is None or target == pos:
            self.clear()
            return
        self._take_ack()  # 丟掉舊的確認，只認這次送出後收到的
        self._send_move(*target)
        self._target = target
        self._sent_at = now
        self._acked = False
        self.sent += 1
