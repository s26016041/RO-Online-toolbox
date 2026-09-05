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
import struct
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ro_toolbox.core.ro_protocol import (
    build_attack,
    build_move,
    build_pickup,
    build_query,
    unpack_move,
)
from ro_toolbox.services import cast_lock
from ro_toolbox.services.entities import EntityScanner
from ro_toolbox.services.game_link import GameLink
from ro_toolbox.services.gamedata import (
    is_boss,
    is_farmable,
    item_name,
    item_names,
    mob_name,
)
from ro_toolbox.services.mapdata import GatError, MapTerrain, load_terrain
from ro_toolbox.services.ro_capture import find_server
from ro_toolbox.services.travel import Traveler
from ro_toolbox.services.walker import MAX_STEP, Walker, line_cells
from ro_toolbox.services.warpzone import (
    KEEP_OUT as _WARP_KEEP_OUT,
)
from ro_toolbox.services.warpzone import (
    NO_FIGHT as _WARP_NO_FIGHT,
)
from ro_toolbox.services.warpzone import (
    keep_out as _keep_out,
)
from ro_toolbox.services.warpzone import (
    warp_cells as _warp_cells_of,
)
from ro_toolbox.services.world import Monster, WorldTracker

log = logging.getLogger(__name__)

_TICK = 0.2  # 主迴圈一拍。要夠密才能「怪一出現就轉去打」
_GIVE_UP_SEC = 10.0  # 同一隻打太久又沒靠近就放棄（多半打不到）
#: 追怪時**角色還在走**（繞路中）就不算「打不到」—— 繞一座山脊直線距離
#: 十幾秒都不會變小，舊版就在半路把牠丟掉（[DAT-076]）。這是硬上限：
#: 走了這麼久還沒貼到才放棄。mjolnir_05 實測最長繞路 102 格 ≈ 15 秒。
_CHASE_CAP_SEC = 25.0
#: 走不成（`Walker` 說 blocked）時先從**現在站的地方**重新規劃幾次，
#: 而不是第一次就把牠拉黑 —— 伺服器帶的路跟我們算的不一樣、被打的硬直、
#: 一段被拒絕，這三種都不是「這隻怪繞不過去」（[DAT-076]）。
_APPROACH_REPLANS = 3
#: 鎖定了目標、還沒開打、而角色**這麼久一步都沒動** —— 印一次幾何狀態。
#: 使用者看到的「盯著怪發呆」就是這個時刻；沒有這一行，下一次回報還是只能猜。
_STARE_WARN_SEC = 3.0
#: 「剛被打」的定義：最近這麼久內有傷害封包打在我身上（硬直大約幾百毫秒）。
_HIT_LOCK_SEC = 0.6
_SKIP_SEC = 30.0  # 放棄的目標暫時列入黑名單多久
_VIEW_RANGE = 30  # 超過這麼遠的怪視為已離開視野（實測伺服器視野約 24 格）
_ROAM_MIN = 60  # 漫遊目標至少離現在位置這麼遠 —— 一次挑很遠，才不會走走停停
_ROAM_MAX = 160  # 漫遊目標最遠這麼遠
_ROAM_BUDGET = 150_000  # A* 節點上限（150 格的路實測遠低於這個數）
_BAD_GOAL_SEC = 90.0  # 走不到的目標區域冷卻多久
_BAD_GOAL_RADIUS = 8  # 走不到的目標附近多少格內都別再挑
#: 走到離怪這麼近才送攻擊。**攻擊封包只帶 GID、不帶座標**，最後那一段由伺服器帶。
#: ⚠ 距離只是條件之一 —— **那條直線上還不能有障礙物**，見 `_close_enough()`。
#:
#: 🔬 **2026-08-29 改成 10 格（使用者要求），還沒完整實測 —— 變差先懷疑這裡。**
#: 先試 13 格，使用者實測回報「有時打不到」，退到 10 格。
#: [PKT-065] 的單獨實驗也支持這個數：9~14 格裡真正命中的兩次是 9 格與 10 格，
#: 14 格那次伺服器只把角色帶到 5 格外就停了、一筆傷害都沒有。
#:
#: 遠距門檻本身試過一次、失敗過（GAMEDATA [PKT-065]）。單獨實驗是成立的
#: （站穩後從 9~14 格送攻擊，伺服器 4/5 會自己走過去，[PKT-072]），
#: 但放進 bot 就變成原地罰站。當時的根因是**只送一次**：
#: bot 多半是「正在走」的時候跨過門檻，`_walker.clear()` 之後那一擊
#: 在移動中送達 → 伺服器忽略它 → 之後再也沒有第二次機會
#: （舊的補送只在「一筆傷害都沒有」且「路已走完」才動，實測幾乎不會發生）。
#:
#: 這次連同根因一起改：送出後**每 `_ATTACK_RETRY_SEC` 秒無條件再送一次**，
#: 被忽略的那一擊會被下一擊蓋掉，所以門檻才敢放遠。兩者是一組的，
#: 只留一半（放遠門檻但不持續送）就會退回 [PKT-065] 那個罰站症狀。
#: 勾了「遠離王」時，王（MVP）周圍這麼多格內一律不踩、不打、不靠近。
#:
#: ⚠ 為什麼比傳點的 `NO_FIGHT`（8）大很多：王**會主動攻擊、會用技能、走得快**，
#: 使用者要的是「連靠近都不行」。而且王的座標是即時的（會移動），這一片每一拍
#: 重算 —— 不像傳點是固定的。王本來就**永遠不打**（`is_farmable` 擋掉 MVP）；
#: 這一片多做的是「連走過去、站旁邊都不要」，避開牠的自動攻擊範圍。
#: 🔬 調大＝更安全但王附近整片不去（小地圖上王一晃過來會擋掉一大塊）；
#: 調小就有機會被王的技能掃到。只在勾了才生效，沒勾＝空集合、完全不影響。
_BOSS_KEEP_OUT = 12
_ATTACK_RANGE = 10
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
#: 鎖定之後每隔這麼久就再送一次攻擊封包 —— **不管有沒有打到、不管人走到哪**。
#:
#: 🔬 **2026-08-29 改成「持續發送」（使用者要求），還沒實測。**
#: 間隔先試 0.3 秒，使用者改回 **0.5 秒** —— 送太密本來就是重置攻速計時器的最大風險，
#: 而「錯過唯一那一擊」的洞是靠「無條件送」補起來的，不是靠間隔短。
#: 舊版是「只在一筆傷害都還沒收到、而且伺服器已經把路走完」時補一次，
#: 理由是 `0x0437` action=7 是**連續**攻擊，重送很可能把攻速計時器重置。
#: 那個顧慮沒有消失 —— 若實測 DPS 掉了（擊殺變少、但「打到空氣」沒變多），
#: 第一個回頭看的就是這裡：把間隔調大，或恢復「打到了就不送」。
#:
#: 歷史數據（都是**只補一次**的舊機制，只能當對照，不能直接比）：
#:     2 秒＋13 格門檻   擊殺 16、打空氣 5 → 0.31
#:     1 秒＋13 格門檻   擊殺  4、打空氣 8 → 2.00
#: 回測一定要用「打空氣 ÷ 擊殺」並在同一個地點比：
#: 單輪 100 秒的擊殺數雜訊很大（同樣的程式碼跑出 3~16，光怪的密度就有這個落差）。
_ATTACK_RETRY_SEC = 0.5
#: 走近用的 A* 節點上限（脫離傳點禁區也用它）。只走幾格，不該花時間。
_NEAR_BUDGET = 3000
#: ⚠ 「傳點周圍不准踩」與「一條傳點帶要補起來」現在**只有一份定義**，
#: 在 `services/warpzone.py`（`KEEP_OUT` / `STRIP_MAX` / `warp_strips`）。
#: 以前自動打怪有、自動尋路沒有 —— 尋路的 A* 因此大方地穿過傳點，
#: 出門就在門邊、下一步又踩回去，來回刷到被伺服器斷線（使用者實測）。
#: 脫離禁區時，最遠往外找幾格。禁區半徑之外再留一點，免得剛好停在邊界上。
_ESCAPE_MARGIN = 4
#: 脫離傳點禁區最多花這麼久。超過就換一個方向。
#:
#: ⚠⚠ **不能只看 `Walker` 說不說「走不成」。** 實機 2026-08-29：白狐卡在
#: mjolnir_07 的傳點禁區 45 秒，日誌從頭到尾沒有出現「走不到…換一個方向」——
#: 走路那一支一路回報「walking」，只是人沒有真的前進。於是脫離這件事
#: **沒有任何出口**，一直到「45 秒毫無進展」的保護把自動打怪關掉
#: （使用者回報「他在船點前面自己關掉」）。
#:
#: 所以這裡自己抓時間：不管走路怎麼說，這麼久還在同一個目標就換一個。
#: 要比 `_FROZEN_SEC`(45) 小很多，才來得及在被判定卡死之前試過好幾個方向。
_ESCAPE_GIVE_UP_SEC = 12.0
#: 座標「還不是即時的」超過這麼久就推一步，把移動元件逼出來。
#:
#: ⚠⚠ 剛換圖時 `read_position()` 回的是**進圖座標**：角色跑再遠它都不會變。
#: 移動元件要等角色**真的走一步**才找得到 —— 而走路又要先知道自己在哪，
#: 這是個死結。實機 2026-08-30（白狐走到 mjolnir_07 按自動打怪）：
#: 落在 (19,377) 剛好在傳點禁區裡，脫離邏輯每一拍都用那個假座標算，
#: 而且 `_escape_warp()` 回 True 會**把這一拍其他事情全部跳過** ——
#: 於是角色一步都沒走、元件永遠找不到，30 秒後才印出那句警告。
#: ⚠ **本來是 3 秒，太久了。** 使用者按下自動打怪之後角色站著不動 4 秒
#: （3 秒 ＋ 一拍），實測回報「直接卡死」就把它關掉了 —— 而日誌顯示它
#: 其實有在動，只是慢。按下按鈕的那一刻座標本來就一定是舊的（還沒走過路），
#: 等於白站。留 0.5 秒只是為了避開「單次讀取剛好失敗」那種抖動，
#: 推出去的那一步本身很便宜（一個移動封包），而且 `_WAKE_EVERY_SEC` 有節流。
_STALE_POS_SEC = 0.5
#: 推一步的節流。伺服器要幾百毫秒才回，推太密只是洗封包。
_WAKE_EVERY_SEC = 1.0
#: 被傳走之後，走回原本那張圖最多花多久。逾時就大聲停用。
_RETURN_GIVEUP_SEC = 300.0
#: 一輪裡最多被傳走幾次。超過就停下來喊人 ——
#: 「怪站在傳點上 → 追過去被傳走 → 走回來 → 又看到牠」是會無限輪迴的
#: （使用者自己點出來的）。學到的禁區通常一次就擋掉了，這是最後一道保險。
_RETURN_MAX = 5
_MISS_SKIP_SEC = 20.0  # 打到空氣的目標冷卻多久（座標過時，等它重新出現）
#: 座標未知的怪「最遠可能在幾格外」。
#:
#: 這種怪唯一的來源是傷害封包（`WorldTracker.note_monster`）——
#: **它打得到我**，所以它一定在自己的攻擊距離內。RO 一般怪最遠的射程約 9~10 格，
#: 這裡取 12 留餘裕。用途只有一個：沒有座標時，改成證明「我周圍這個圓裡
#: 完全沒有傳點禁區」，那不管牠站在圓裡哪一格，伺服器把我帶過去都踩不到。
#: 🔬 調大 = 更保守（傳點附近更大一片都不還手）；調小就有機會被帶過傳點。
_BLIND_REACH = 12
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
#: 漫遊／走去撿東西的時候，「該在走卻沒動」超過這麼久就當這條路走不成。
#:
#: ⚠⚠ **為什麼不能只靠 `Walker` 說「走不成」。** 2026-09-01 實機：兩隻分身
#: 一小時內安靜地站著 30 次、每次 45 秒 —— 日誌從頭到尾一行都沒有，
#: 只有保護機制那句「45 秒沒進展」。原因是 `Walker.update()` 一路回報
#: `walking`（客戶端的走路旗標卡在「正在走」），而 `_roam()` 看到 walking
#: 就 `return` —— 沒有人送封包、沒有人重新規劃、也沒有人抱怨。
#:
#: 走路那一支已經補上信任上限（`walker.MOVING_TRUST_SEC`），但**呼叫端不能
#: 把自己的健康完全託付給下一層**：這裡自己看一個讀得到的訊號（座標有沒有變），
#: 不管下面回報什麼。
#:
#: 四秒是量出來的：實機取樣（`tools/probe_walk_freeze.py`）裡「座標沒變」的
#: 區間中位數 **0.12 秒**、p99 **2.1~3.6 秒**，7 分鐘內超過 4 秒的只有 7 段，
#: 其中 3 段就是那個 45 秒的卡死。門檻壓在 p99 之上、災難之下。
#:
#: ⚠ 只在「本來就該移動」的時候算數 —— 交戰中站著打、讓路給詠唱、
#: 打死之後停下來撿東西都是**故意**站著，那些時候要把時鐘往前推
#: （見 `_stand_still()`），不然會把正常行為誤判成卡住。
_STALL_SEC = 4.0
#: 多久撈一次 TCP 表看「連線有沒有換掉」。**只管連線，不管地圖** ——
#: 地圖名是一次記憶體讀取，每一拍都看得起，而且非看不可（見 `_keep_in_sync`）。
_RESYNC_SEC = 2.0
#: 相鄰兩拍的座標最多可能差幾格。一拍 `_TICK` 秒、一步最多 `MAX_STEP` 格，
#: 再放寬一倍當餘裕。
#:
#: ⚠⚠ **超過這個距離的一段不是走出來的**，是其中一點已經在**別張地圖**了。
#: 2026-08-30 實機踩過：`_learn_warp` 把這種段連起來，一次記了 347 格、
#: 652 格「傳點」到 mjolnir_07／mjolnir_08 上（真的傳點帶只有幾十格），
#: 禁區再往外擴 `KEEP_OUT` 格之後整張圖幾乎算不出路，45 秒沒進展就
#: `_fail()` —— 使用者看到的是「他自動關閉自動戰鬥」。
_LEARN_MAX_JUMP = MAX_STEP * 2
#: 一張圖最多學到幾格傳點。學過頭代表判斷本身壞了，繼續學只會把圖封死；
#: 那時候寧可**不再學**並大聲說一句，也不要安靜地把地圖變成不能走。
_LEARN_MAX_CELLS = 400
#: 負重到幾成就收工（使用者 2026-08-29 指定：**90% 含**就回程並關掉自動打怪）。
#:
#: ⚠ 掛機**不補給**：撿到走不動就該回城，繼續打只是把撿到的東西丟在地上。
#: 負重只能從封包 `0x00B0` 拿（記憶體裡沒有這個欄位），而它**只在值變動時送**
#: （[PKT-074]）—— 打怪一直在撿東西，所以值會一直更新。
OVERWEIGHT_RATIO = 0.90

# 傷害／動作封包：payload[0:4]=攻擊者 GID、[4:8]=目標 GID
_DAMAGE_OPS = (0x08C8, 0x02E1)
#: 負重／負重上限（`0x00B0` 的兩個 kind，見 services/shop.py）。
_SP_WEIGHT = 24
_SP_MAX_WEIGHT = 25
_OP_PAR_CHANGE = 0x00B0
_OP_MOVE_ACK = 0x0087  # 伺服器確認「我」要移動：payload[4:10] = 起點+終點


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
    resent: int = 0  # 第一發之後又送出的攻擊封包數（診斷用：現在是持續發送，會一直長）
    #: 角色死了。⚠ 跟一般的「停下來」分開報：使用者要求死亡要**跳通知窗**
    #: （按確定才消失），而且**只**關掉自動打怪，別的什麼都不要做。
    died: bool = False
    #: 負重滿了（>= `OVERWEIGHT_RATIO`）。⚠ 介面要**回程並關掉自動打怪**
    #: （使用者指定：掛機不補給）。
    overweight: bool = False


@dataclass
class _Aim:
    """目前鎖定的怪。"""

    gid: int
    since: float
    best_distance: int = 1 << 30
    #: 追這隻的期間，角色最後一次**真的移動**是什麼時候／在哪（[DAT-076]）。
    moved_at: float = 0.0
    last_pos: tuple[int, int] | None = None
    #: 走不成之後重新規劃過幾次（上限 `_APPROACH_REPLANS`）。
    replans: int = 0
    #: 「發呆」那一行印過了沒（一隻只印一次）。
    stare_said: bool = False
    attacked: bool = False
    attacked_at: float = 0.0  # 送出攻擊的時間，用來判斷有沒有打到
    attacked_dist: int = 0  # 送出攻擊時離它多遠（伺服器要走這段路，要多給時間）
    sent_at: float = 0.0  # 最後一次送出攻擊的時間（補送用，跟 attacked_at 分開）
    resends: int = 0  # 這隻怪身上又送了幾發（診斷用）
    lost_at: float = 0.0  # 從追蹤裡消失的時間（0 = 還在）


class FarmBot:
    """單一角色的自動打怪。start()/stop() 控制；on_update 回報狀態。"""

    def __init__(
        self,
        pid: int,
        on_update: Callable[[FarmStats], None] | None = None,
        use_memory: bool = _USE_MEMORY_ENTITIES,
        blacklist: Iterable[int] = (),
    ) -> None:
        self._pid = pid
        self._on_update = on_update
        self._use_memory = use_memory
        #: 不撿的道具編號（使用者的撿取黑名單，見 `services/loot_store`）。
        #:
        #: ⚠ 存**編號**不存名字：地上的掉落物封包給的就是編號
        #: （`world.GroundItem.name_id`），兩邊直接對得上。
        #:
        #: ⚠ 這一份會被 UI 執行緒用 `set_blacklist()` **整份換掉**（不是就地改）
        #: —— 換一個 frozenset 是一次屬性指定，跑在打怪迴圈裡的讀取
        #: 不可能讀到改到一半的名單，所以不用鎖。
        self._blacklist: frozenset[int] = frozenset(blacklist)
        self._world = WorldTracker(valid_item_ids=set(item_names()))
        #: socket ／ 角色定位 ／ 封包擷取三條線共用同一份規則
        #: （`services/game_link.py`）。⚠ 以前這一段 travel_bot 抄一份、
        #: 這裡抄一份 —— [PKT-072] 就是「剛連上複製不到 socket 要重試」
        #: 抄了四份、漏了兩份才炸的。
        self._link = GameLink(
            pid,
            on_packet=self._on_packet,
            should_stop=lambda: self._stop.is_set(),
        )
        self._terrain: MapTerrain | None = None
        self._entities: EntityScanner | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stats = FarmStats()
        self._loot: dict[int, int] = {}  # 物品 ID -> 撿取次數
        self._loot_lock = threading.Lock()
        # ⚠ `moving` 讓走路那一支問得到「客戶端認為我還在走嗎」—— 沒有它就只能
        #   靠計時器猜，走得慢一點（或那一拍做的事比較多）就會被誤判成停住而重送，
        #   症狀就是「走路一卡一卡」。見 `walker.Walker.update`。
        self._walker = Walker(
            self._send_move, moving=self._client_moving,
            hit_locked=lambda: self._recently_hit(time.monotonic()),
        )
        self._aim: _Aim | None = None
        self._skip: dict[int, float] = {}  # 打不到的目標 → 黑名單到期時間
        #: gid → 被列入黑名單的時間。診斷與過期用。
        self._skip_at: dict[int, float] = {}
        #: gid → 「打到空氣」那一刻我們**以為**牠在哪（座標未知則 None）。
        #:
        #: ⚠⚠ 只有這一種黑名單可以被推翻，而且推翻的條件是**座標真的變了**。
        #: 以前的條件是「之後又看到牠」（`seen_at` 比拉黑的時間新），
        #: 那在封包時代成立 —— 收到新的實體封包確實是新證據。但記憶體變成
        #: 主來源之後，`sync_from_memory` **每一拍**都會更新 `seen_at`，
        #: 於是這條規則等於「下一拍就解除黑名單」：鎖定 → 打空氣 → 解除 →
        #: 再鎖定同一隻，每 3 秒循環一次（實機日誌 08:31:31/34/37 連三次）。
        #: 座標沒變就代表我們手上的還是同一份錯資料，再打一次還是空氣。
        self._miss_pos: dict[int, tuple[int, int] | None] = {}
        #: 跑進傳點範圍而收手的怪 → 冷卻到期時間。
        #:
        #: ⚠ **故意跟 `_skip` 分開**：`_skip` 有一條「再看到牠就取消黑名單」的
        #: 規則（那是為了「座標過時打到空氣」設計的，新的目擊確實比黑名單可信）。
        #: 但這裡拉黑的理由不是座標不準，是**牠站的地方我們不去** ——
        #: 再看到牠一百次也不會改變這件事。混在一起的話，牠在範圍邊緣走來走去，
        #: 我們就跟著鎖定→收手→鎖定，每次都白送一個移動封包。
        self._warp_skip: dict[int, float] = {}
        self._bad_goals: list[tuple[tuple[int, int], float]] = []
        self._loot_since: dict[int, float] = {}  # 掉落物 → 開始嘗試撿的時間
        self._loot_until = 0.0  # 剛打死一隻，停到這個時間讓它撿東西
        self._roam_goal: tuple[int, int] | None = None  # 漫遊的遠點，中途打怪不換
        self._progress: tuple | None = None  # (位置, 擊殺, 撿取) —— 用來偵測完全卡住
        self._progress_at = 0.0
        #: 負重與上限（原始值，畫面顯示是它的 1/10，見 [PKT-074]）。
        #: None = 還沒收到過那一包 —— 那時候**不做判斷**，不是當成 0。
        self._weight: int | None = None
        self._max_weight: int | None = None
        self._map = ""  # 目前綁定的地圖，換圖要重新載地形
        #: 按下自動打怪時人在哪張圖。**被傳走就走回這裡**（使用者指定的行為）。
        self._home_map = ""
        #: 最近幾拍的 `(地圖, 位置)`。被傳走時用來回推「踩到哪裡出事」。
        #: ⚠ **一定要帶地圖名**：座標與地圖名是兩次獨立的記憶體讀取，
        #: 換圖那一瞬間可能只更新了其中一個，不帶名字就會把新地圖的座標
        #: 當成舊地圖的傳點學進去（見 `_LEARN_MAX_JUMP`）。
        self._recent: deque[tuple[str, tuple[int, int]]] = deque(maxlen=4)
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
        #: 「遠離王」勾了沒（使用者設定，UI 執行緒用 `set_avoid_boss()` 換）。
        #: 王本來就不打（`is_farmable` 擋 MVP）；勾了多做的是**連靠近都避開**
        #: （王會自動攻擊）。
        self._avoid_boss = False
        #: 王（MVP）周圍不去的那一片。**每一拍重算**（王會移動），沒勾＝空集合。
        self._boss_zone: frozenset[tuple[int, int]] = frozenset()
        #: 這張圖上已經講過「有王」的 GID，免得每一拍洗版。換圖清掉。
        self._boss_said: set[int] = set()
        #: 這片裡面的怪**一律不打**（`warpzone.NO_FIGHT`，比走路禁區大）。
        #: 為什麼要另外一片：打怪的最後一段路是**伺服器**帶的，我們的 A*
        #: 繞得再漂亮也管不到 —— 詳見 `warpzone.NO_FIGHT` 的說明。
        self._no_fight_zone: frozenset[tuple[int, int]] = frozenset()
        #: 正在往哪裡脫離禁區（None = 沒在脫離）
        self._escape_goal: tuple[int, int] | None = None
        #: 這個脫離目標是什麼時候挑的（見 `_ESCAPE_GIVE_UP_SEC`）。
        self._escape_since = 0.0
        #: 座標從什麼時候開始「不是即時的」（0 = 現在是即時的）。
        self._stale_since = 0.0
        #: 卡住重來過幾次（見 `_unstick`）。只拿來寫日誌，不當停止條件。
        self._stuck = 0
        #: 最後一次「角色動了，或**故意**站著」的時刻。見 `_STALL_SEC`。
        #: 0 = 還沒開始（第一次讀到座標時才起算）。
        self._moved_at = 0.0
        #: 上一次讀到的座標（拿來判斷「動了沒」）。
        self._was_at: tuple[int, int] | None = None
        #: 上次為了「把移動元件逼出來」推的那一步是什麼時候。
        self._woke_at = 0.0
        self._resync_at = 0.0
        # 傷害封包分析：學到自己的 GID 後，就能認出「正在打我的怪」優先反擊
        self._my_gid: int | None = None
        # {gid: 最後一次打到我的時間}。帶時間戳才能過期 ——
        # 怪跑掉或被別人打死之後不該永遠留在優先清單裡。
        self._aggro: dict[int, float] = {}
        self._dmg_lock = threading.Lock()

    # ⚠ 這四個是 `GameLink` 的門面。留著是因為呼叫端與測試都這樣用；
    # 真正的規則（怎麼取得、怎麼重綁）只有 GameLink 一份。
    @property
    def _sock(self):
        return self._link.sock

    @_sock.setter
    def _sock(self, value) -> None:
        self._link.sock = value

    @property
    def _server(self):
        return self._link.server

    @_server.setter
    def _server(self, value) -> None:
        self._link.server = value

    @property
    def _reader(self):
        return self._link.reader

    @_reader.setter
    def _reader(self, value) -> None:
        self._link.reader = value

    @property
    def _capture(self):
        return self._link.capture

    @_capture.setter
    def _capture(self, value) -> None:
        self._link.capture = value

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> FarmStats:
        return self._stats

    @property
    def link_dead(self) -> bool:
        """連線已經確定送不出東西了嗎（見 `GameLink.dead`）。

        介面要拿它判斷「斷線了」—— **不能只看 `find_server()`**：伺服器把連線
        reset 之後那條連線還留在 TCP 表裡，查得到卻送不出去（[PKT-082]）。
        """
        return self._link.dead

    def set_blacklist(self, item_ids: Iterable[int]) -> None:
        """換一份「不撿的道具編號」。**跑的時候改也立刻生效**。

        使用者在視窗裡改完就該馬上算數 —— 要求重開掛機才生效的話，
        他會以為設定沒存到（而且黑名單是「永遠開啟」的，沒有開關可以重按）。
        """
        self._blacklist = frozenset(item_ids)

    def set_avoid_boss(self, on: bool) -> None:
        """換「遠離王」的開關。**跑的時候改也立刻生效**（下一拍就重算 `_boss_zone`）。

        關掉時要**立刻**把王的禁區清掉，不然勾一次就永遠繞著一個早就走掉的王。
        （一次屬性指定是原子的，跟 `set_blacklist` 同理，不必上鎖。）
        """
        self._avoid_boss = bool(on)
        if not on:
            self._boss_zone = frozenset()

    def _unwanted(self, item) -> bool:  # noqa: ANN001 - GroundItem
        """這一個在黑名單裡嗎（＝不撿）。"""
        return item.name_id in self._blacklist

    def loot(self) -> dict[int, int]:
        """已撿取的道具 {物品ID: 次數}。快照，可安全在其他執行緒讀。"""
        with self._loot_lock:
            return dict(self._loot)

    def reset_loot(self) -> None:
        """把「道具總攬」的統計歸零 —— 使用者按「歸零重算」時從現在起重新計算。

        ⚠ **只清每個道具的累計次數**（`_loot`），不動 `stats.picked` ——
        後者是防卡住的進度訊號之一（`_alive()` 的 progress 三元組），
        歸零它會被下游誤讀成「剛剛有進展」。道具總攬只看 `_loot`（見
        `farm_page._refresh_loot`），清它就夠。
        """
        with self._loot_lock:
            self._loot.clear()

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
        problem = self._link.open()
        if problem:
            self._fail(problem)
            return False
        reader = self._link.reader
        status = reader.read() if reader is not None else None
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
        if packet.opcode == _OP_PAR_CHANGE and len(payload) >= 6:
            # 負重／上限。⚠ **只在值變動時送**（[PKT-074]）——
            # 打怪一直在撿東西，所以值會一直更新，不必自己去問。
            kind, value = struct.unpack_from("<HI", payload, 0)
            if kind == _SP_WEIGHT:
                self._weight = value
            elif kind == _SP_MAX_WEIGHT:
                self._max_weight = value
            return
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
            if not self._link_alive():
                return
            if self._too_heavy():
                percent = self._weight / self._max_weight * 100
                self._stats.overweight = True
                self._fail(f"⚠ 負重 {percent:.0f}%，自動打怪已停止（要回城卸貨）")
                return
            if not self._keep_in_sync(now):
                return
            # ⚠⚠ **幫隊友放 buff 的時候讓路**（使用者 2026-08-29 指定：
            # 「自動戰鬥時也要幫隊友放，並且是最高優先，高於打怪跟尋路」）。
            #
            # RO 裡**移動與攻擊都會打斷詠唱**，而這裡一路在送走路封包 ——
            # 不讓路的話 buff 每一次都被自己人打斷，實機日誌是連續三次
            # 「沒上身」然後退避重試（使用者：「反應很慢」）。
            #
            # 讓路只是**這一拍不動**，不是等待：`held()` 有到期時間，
            # 補 buff 那條收到上身就馬上放行（見 `services/cast_lock.py`）。
            if cast_lock.held(self._pid):
                self._stand_still(now)   # 讓路是**故意**站著，不是卡住
                self._stop.wait(_TICK)
                continue
            pos = self._reader.read_position() if self._reader else None
            self._note_moved(now, pos)
            if pos is not None:
                # 被傳走時要回推「踩到哪裡出事」，所以隨手記著最近幾拍的位置。
                # ⚠ 記的時候就把**當下的地圖**綁上去，事後才分得出哪幾點可信。
                self._recent.append((self._map, pos))

            # ⚠ 被傳到別張圖了：這一拍只做一件事 —— 走回去。
            # **一定要排在 `_escape_warp` 前面**：剛落地時人就站在回程傳點旁邊，
            # 脫離邏輯會把我們往外拉，正好跟「走回去」互相打架。
            if self._traveler is not None:
                self._stand_still(now)   # 走回原圖那條有自己的逾時，同上
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

            # ⚠⚠ **座標還不是即時的時候不准做「靠座標決定」的事。**
            # 剛換圖時讀到的是進圖座標，角色跑再遠都不會變；移動元件要等
            # 角色真的走一步才找得到。用假座標去算脫離傳點，會得到一個假的
            # 目標、送一步假的路，然後每一拍重來 —— 角色一步都沒走，
            # 元件永遠找不到（實機：30 秒後才印出「回報的是進圖座標」）。
            #
            # 出口跟自動尋路那邊一樣：**推一步**。角色一動客戶端就把座標寫回去，
            # 元件也跟著找得到，下一拍所有判斷就都是真的了。
            if not self._wake_position(now, pos):
                self._emit()
                self._stop.wait(_TICK)
                continue

            # ⚠ 站在傳點禁區裡的話，這一拍只做一件事：走出去。
            # 不是停下來 —— 叫你別靠近傳點，不是叫你關掉自動戰鬥。
            if self._escape_warp(pos):
                # 脫離自己有 `_ESCAPE_GIVE_UP_SEC` 那道出口，不歸這裡的停滯偵測管
                self._stand_still(now)
                self._emit()
                self._stop.wait(_TICK)
                continue

            # 王（MVP）在哪、要不要繞開 —— 每一拍重算（王會移動）。
            self._refresh_boss(pos)
            # 腳邊的掉落物永遠先撿：怪死在腳邊，等打完下一隻就走開撿不到了
            self._grab_nearby(pos)
            self._update_aim(now, pos)
            self._stats.monsters_near = len(self._world.monster_gids())
            self._stats.walk_rejected = self._walker.rejected

            # 再來：打怪 > 走過去撿遠一點的掉落 > 漫遊找怪
            # ⚠ 前兩條（交戰中、撿東西的停頓）**站著是應該的** ——
            #   要把「該在走卻沒動」的時鐘往前推，見 `_STALL_SEC`。
            if self._aim is not None:
                self._stand_still(now)
                self._fight(now, pos)
            elif now < self._loot_until:
                self._stand_still(now)
                self._walker.clear()  # 剛打死，停一下讓它撿完再走
            elif not self._collect(now, pos):
                self._roam(now, pos)

            self._emit()
            self._stop.wait(_TICK)

    def _too_heavy(self) -> bool:
        """負重到頂了嗎。**收不到那一包就當作不知道**，不是當成 0。

        負重只在變動時才送過來（[PKT-074]），剛啟動可能一次都沒看過 ——
        那時候拿 0 去算會得到「0%」，永遠不會收工。
        """
        if self._weight is None or self._max_weight is None or self._max_weight <= 0:
            return False
        return self._weight / self._max_weight >= OVERWEIGHT_RATIO

    def _link_alive(self) -> bool:
        """連線還活著嗎。死了就大聲停用 —— 不准繼續空轉送封包。

        ⚠ 這一條**每拍都要問**，不能等到 `_keep_in_sync` 那兩秒一次的節流：
        實測連線被 reset 之後，bot 每拍照送、每拍失敗，一小時 5,185 行錯誤
        而且從頭到尾沒有人喊停（[PKT-082]）。
        """
        if not self._link.dead:
            return True
        self._fail("⚠ 遊戲連線已中斷（送不出封包），自動打怪已停止")
        return False

    def _keep_in_sync(self, now: float) -> bool:
        """換地圖／換伺服器頻道之後，重新綁定 socket 與地形。

        踩過的坑：角色中途換圖（或伺服器把連線移到別的地圖伺服器）之後，
        啟動時抓到的 socket 與地形都已經失效 —— 送出去的封包全部石沉大海、
        A* 用的還是舊地圖，**bot 看起來在跑，實際上什麼都沒做**。
        這正是規範說的「安靜地做錯事」，所以要主動偵測並重綁。
        回傳 False = 重綁失敗，要大聲停用。
        """
        if self._reader is None:
            return True
        # ⚠⚠ **地圖名每一拍都要看。** 以前它跟連線一起被 `_RESYNC_SEC` 節流，
        # 換圖最慢 2 秒才發現 —— 但座標是 `_TICK`（0.2 秒）取樣一次的，
        # 那 2 秒裡 `_recent` 早就裝滿**新地圖**的座標了，`_learn_warp` 再把
        # 新舊兩張圖的座標連成一條線，一次記幾百格假傳點（2026-08-30 實機）。
        # 讀地圖名只是一次記憶體讀取，很便宜；貴的是 `find_server()`（撈 TCP 表），
        # 那個才需要節流。
        status = self._reader.read()
        map_changed = status is not None and status.map_name and status.map_name != self._map
        # ⚠ 「我手上這份 socket 還活著嗎」是微秒級的唯讀查詢，**不受節流管**：
        #   換地圖伺服器時遊戲會 `closesocket()` 舊連線，而重連到同一台的話
        #   (ip, port) 一模一樣，比對端點看不出來（[PKT-096]）。
        stale = not self._link.alive()
        due = stale or now - self._resync_at >= _RESYNC_SEC
        server = find_server(self._pid) if due else None
        if due:
            self._resync_at = now
        server_changed = server is not None and (server != self._server or stale)
        if not (map_changed or server_changed):
            if due and server is None and self._server is not None:
                self._fail("⚠ 遊戲連線已中斷，自動打怪已停止")
                return False
            return True

        what = []
        if map_changed:
            what.append(f"地圖 {self._map} → {status.map_name}")
        if server_changed:
            what.append("複製的 socket 已被遊戲關掉" if server == self._server
                        else f"連線 {self._server} → {server}")
        log.info("環境變了（%s），重新綁定", "、".join(what))

        if server_changed:
            # 上面已經讀過一次連線了，直接交給它 —— 再讀一次不只浪費，
            # 兩次讀到的還可能不一樣（TCP 表是快照）。
            problem = self._link.resync(server)
            if problem:
                self._fail(f"{problem}，自動打怪已停止")
                return False

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
            # ⚠ `_recent` 也要清：留著的話，新圖上頭幾拍會跟舊圖的座標混在一起，
            # 萬一 0.8 秒內又被傳走，回推出來的又是一條橫跨兩張圖的假線。
            self._recent.clear()
            self._world.clear()
            self._walker.clear()
            self._roam_goal = None
            self._escape_goal = None
            self._aim = None
            self._skip.clear()
            self._skip_at.clear()
            self._miss_pos.clear()
            self._warp_skip.clear()
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
            # ⚠ 使用者指定：死了就**跳通知窗＋關掉自動打怪，別的都不要做**
            # （不要自己回城、不要重連、不要繼續打）。`died` 讓介面分得出來。
            self._stats.died = True
            self._fail("⚠ 角色已死亡，自動打怪已停止")
            return False

        # 血量沒事卻長時間毫無進展（沒移動、沒擊殺、沒撿到）：多半卡住或狀態異常。
        # 交戰時站著不動是正常的，所以擊殺數也算「有進展」。
        #
        # ⚠⚠ **座標不是即時的時候不准拿它當「有沒有在動」**（[MEM-054]）。
        # 剛換圖還沒走過路時讀到的是**進圖座標**：角色跑再遠它也不會變，
        # 於是「位置沒變」永遠成立 —— 只要 45 秒內剛好沒擊殺沒撿到就被判定卡住，
        # 而角色其實好好地在走。使用者實測回報過「無法自動打怪」就是這個。
        # 那時候改看「送出去幾個移動封包」：bot 還在動就不算卡住，
        # 真的卡死（連移動都不送）照樣抓得到。
        where = self._reader.read_position()
        if not self._reader.position_live:
            where = ("moves", self._walker.sent)
        progress = (where, self._stats.kills, self._stats.picked)
        if progress != self._progress:
            self._progress = progress
            self._progress_at = now
        elif now - self._progress_at > _FROZEN_SEC:
            # ⚠⚠ **卡住不是收工的理由。** 使用者訂的規則（2026-08-31）：
            #   「自動戰鬥只有死掉會關閉，或者回程補給會暫時關閉」。
            #   舊版在這裡 `_fail()` 把自動打怪關掉 —— 實機日誌：
            #
            #       16:40:45 太靠近傳點，先走開（往 153,20）
            #       16:41:30 ⚠ 角色 45 秒毫無進展（正在脫離傳點禁區…），已停止
            #
            #   使用者掛了一整晚，回來看到的就是「它自己關掉了」。
            #   卡住是**要處理的狀況**：清掉狀態重來，並且大聲留紀錄。
            self._unstick(now)
        return True

    def _unstick(self, now: float) -> None:
        """卡住了：把當下的目標全部作廢、重新開始。**不關掉自動打怪。**

        會做的事都是「把錯的假設丟掉」：

        - 走不到的脫離目標／漫遊目標記進 `_bad_goals`（下次不要再挑它）
        - 鎖定的怪、走路佇列、座標喚醒的節流通通歸零

        ⚠ 這裡**不碰** `_traveler`（被傳走時走回原圖那條）——那條自己有
        重新規劃的機制，從外面清掉反而會讓它重來一次已經走過的路。
        """
        self._stuck += 1
        doing = self._doing()
        for goal in (self._escape_goal, self._roam_goal):
            if goal is not None:
                self._bad_goals.append((goal, now + _BAD_GOAL_SEC))
        # ⚠ WARNING 級：使用者手上的預設層級就是 WARNING，這一行必須看得到
        #   （[ENV-...]：INFO 在他的日誌裡一行都不會出現）。
        # ⚠⚠ **把當下的狀態一起印出來，而且要在清掉之前印。**
        #   2026-09-01 實機一小時卡了 30 次，45 秒裡日誌一行都沒有 ——
        #   只知道「在漫遊」，不知道是客戶端說在走、伺服器不收、還是路算不出來，
        #   三種的修法完全不同（見 `_STALL_SEC`）。多印這一行幾乎免費：
        #   45 秒才一次。
        log.warning(
            "[自動打怪] 卡住了（%s）—— 清掉狀態重來，第 %d 次"
            "｜位置 %s（即時=%s，%.1f 秒沒動）｜附近 %d 隻怪｜%s",
            doing, self._stuck, self._was_at,
            self._reader.position_live if self._reader is not None else None,
            (now - self._moved_at) if self._moved_at else -1.0,
            self._stats.monsters_near, self._walker.debug_state(now),
        )
        self._escape_goal = None
        self._roam_goal = None
        self._aim = None
        self._walker.clear()
        self._stale_since = 0.0
        self._woke_at = 0.0
        self._progress_at = now
        self._stand_still(now)
        self._note(
            f"⚠ {_FROZEN_SEC:.0f} 秒沒進展（{doing}）—— 換個目標重來"
            f"（第 {self._stuck} 次，**沒有**關掉自動打怪）"
        )

    def _note_moved(self, now: float, pos: tuple[int, int] | None) -> None:
        """角色動了沒。**這是唯一沒被任何旗標污染的進度證據**（見 `_STALL_SEC`）。"""
        if pos is None:
            return
        if pos != self._was_at or not self._moved_at:
            self._was_at = pos
            self._moved_at = now

    def _stand_still(self, now: float) -> None:
        """這一拍**故意**站著（交戰、讓路詠唱、撿東西的停頓）—— 不算沒進展。"""
        self._moved_at = now

    def _stalled(self, now: float) -> float:
        """「該在走卻沒動」多久了。0 = 沒有（剛動過，或還沒起算）。"""
        if not self._moved_at:
            return 0.0
        held = now - self._moved_at
        return held if held > _STALL_SEC else 0.0

    def _doing(self) -> str:
        """卡住的時候在做什麼 —— 給日誌看的一句話。"""
        if self._traveler is not None:
            return "正在走回原本的地圖"
        if self._escape_goal is not None:
            return f"正在脫離傳點禁區（往 {self._escape_goal}）"
        if self._aim is not None:
            mob = self._world.get(self._aim.gid)
            who = getattr(mob, "name", "") or f"GID {self._aim.gid}"
            return f"正在打「{who}」"
        if self._roam_goal is not None:
            return f"正在漫遊（往 {self._roam_goal}）"
        return "沒有目標"

    def _expire(self, now: float) -> None:
        for gid in [g for g, until in self._skip.items() if now > until]:
            del self._skip[gid]
            self._skip_at.pop(gid, None)
            self._miss_pos.pop(gid, None)
        for gid in [g for g, until in self._warp_skip.items() if now > until]:
            del self._warp_skip[gid]
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
            elif self._no_fight(self._pos_of(aim.gid)) or self._blind_near_warp(aim, pos):
                # ⚠⚠ **每一拍都要重驗，不能只在挑目標時看一次。**
                # 怪會自己走 —— 挑的時候離傳點很遠，打到一半牠往傳點跑，
                # 而 `0x0437` 是**連續**攻擊：牠走到哪，伺服器就把角色拉到哪。
                # 這就是使用者回報的「自動戰鬥會因為打怪物跑到傳送點傳出去」：
                # 從頭到尾我們沒有送過任何一個往傳點的移動封包。
                self._break_off(aim, now, pos)
                aim = None
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
                if pos is not None and pos != aim.last_pos:
                    aim.last_pos, aim.moved_at = pos, now
                mob = self._world.get(aim.gid)
                if mob is not None and mob.hit_at > aim.since:
                    aim.since = mob.hit_at  # 正在互打，當然不算「打不到」
                distance = mob.distance_from(pos) if (mob and pos) else None
                if distance is not None and distance < aim.best_distance:
                    # 還在接近中就不算打不到，重新計時
                    aim.best_distance = distance
                    aim.since = now
                elif now - aim.since > _GIVE_UP_SEC and (
                    aim.attacked
                    or now - aim.moved_at > _STARE_WARN_SEC
                    or now - aim.since > _CHASE_CAP_SEC
                ):
                    # 打太久又沒更靠近＝打不到，黑名單換目標，別卡在這隻。
                    # ★ 還在走（繞路中）的不算 —— 直線距離在繞山脊的時候
                    #   十幾秒都不會變小，那不是打不到（[DAT-076]）；
                    #   只有站著沒動、或走超過硬上限，才放棄。
                    # ⚠ 以前這裡**一個字都不印**：使用者看到角色發呆十秒然後
                    #   換目標，日誌裡什麼都沒有。
                    log.info(
                        "「%s」%s %.0f 秒都沒更靠近（距離 %s、%s），先換一隻",
                        mob_name(self._class_of(aim.gid)),
                        "打了" if aim.attacked else "追了",
                        now - aim.since, distance,
                        self._geometry(pos, mob.pos if mob else None),
                    )
                    self._skip[aim.gid] = now + _SKIP_SEC
                    self._skip_at[aim.gid] = now
                    self._drop_aggro(aim.gid)
                    self._walker.clear()
                    self._aim = aim = None

        if aim is None and now >= self._loot_until:
            mob = self._pick_target(pos)
            if mob is not None:
                self._aim = _Aim(mob.gid, now, moved_at=now, last_pos=pos)
                self._stats.target = mob_name(mob.class_id)

    def _recently_hit(self, now: float, within: float = _HIT_LOCK_SEC) -> bool:
        """最近 `within` 秒內有沒有怪打到我（被打有硬直，移動會被伺服器吃掉）。"""
        with self._dmg_lock:
            return any(now - at <= within for at in self._aggro.values())

    def _geometry(self, pos, goal) -> str:
        """一句話講清楚「我跟牠之間」：直線乾不乾淨、牠站的格可不可走、旁邊格在哪。

        給發呆與放棄那兩行日誌用 —— 「隔著障礙物」到底是哪一種，光看座標猜不出來。
        """
        if pos is None or goal is None or self._terrain is None:
            return f"我在 {pos}、牠在 {goal}"
        beside = self._beside(goal, pos)
        return (
            f"我在 {pos}、牠在 {goal}、牠那格可走={self._terrain.is_walkable(*goal)}、"
            f"直線乾淨={self._terrain.line_clear(pos, goal)}、旁邊格={beside}"
            f"（直線乾淨={self._terrain.line_clear(pos, beside) if beside else None}）、"
            f"被 {len(self._aggro)} 隻打、{self._walker.debug_state()}"
        )

    def _pick_target(self, pos: tuple[int, int] | None) -> Monster | None:
        """挑目標：先打正在打我的怪（主動怪），再打**最近**的怪。

        MVP 與草一律跳過（`is_farmable`）：它們跟一般怪一樣打得動、也會掉東西，
        但草是浪費時間、MVP 是送死。**菁英怪不算 MVP，照打。**
        """
        # ⚠ **「打到空氣」的黑名單被新座標推翻。** 那種拉黑的理由是「我們手上的
        # 座標過時了」，所以推翻它的證據只有一個：**座標真的變了**。
        # 不放行的話，附近幾隻怪一被拉黑，畫面上明明有怪、程式卻說「附近沒怪」
        # 而且要等 20 秒（使用者實際回報）；放行條件太鬆則會每 3 秒重打同一隻空氣
        # （見 `_miss_pos` 的說明）。
        for gid, where in list(self._miss_pos.items()):
            mob = self._world.get(gid)
            if mob is not None and mob.pos is not None and mob.pos != where:
                self._skip.pop(gid, None)
                self._skip_at.pop(gid, None)
                self._miss_pos.pop(gid, None)
        skip = set(self._skip) | set(self._warp_skip)
        skip.update(m.gid for m in self._world.monsters() if not is_farmable(m.class_id))
        # ⚠ **站在傳點那一片裡的怪一律不打**（`warpzone.NO_FIGHT`，比走路禁區大）。
        # 追過去就會踩到傳點被傳走 —— 新地圖可能有打不動的怪，
        # 而 bot 會在那裡繼續打（使用者實測回報）。
        # 連正在打我的怪也不追：被打幾下，好過被傳到不該去的地方。
        in_warp = {m.gid for m in self._world.monsters() if self._no_fight(m.pos)}
        skip.update(in_warp)
        # 正在打我的怪**不受「打到空氣」黑名單限制**：它打得到我就代表它真的在旁邊，
        # 座標不可能過時。以前被黑名單擋住，症狀就是「怪在打我卻不理它」。
        no_hunt = {m.gid for m in self._world.monsters() if not is_farmable(m.class_id)}
        no_hunt |= in_warp | set(self._warp_skip)
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

        「走近」只要走到 `_ATTACK_RANGE` 格內、**而且中間那條直線沒有障礙物**
        就夠了 —— 最後那一段由伺服器帶，它知道怪真正在哪
        （見 `_ATTACK_RANGE` 的實測說明與 `_close_enough()`）。

        **攻擊送出後絕對不能再送移動**：移動會取消連續攻擊，
        症狀就是「打一下就跑掉」。所以 attacked 之後這裡不走路，只做兩件事：
        每 `_ATTACK_RETRY_SEC` 秒補一發攻擊（`_keep_attacking`）、
        以及檢查有沒有真的打到（`_check_hit`），等 0x0080 擊殺訊號。
        """
        aim = self._aim
        if aim is None:
            return
        if aim.attacked:
            self._walker.clear()  # 已經在打了，站著等 0x0080 確認死亡
            self._keep_attacking(aim, now)
            self._check_hit(aim, now)
            return

        # ⚠ 每一拍都重讀它現在在哪 —— 這遊戲的怪移動很頻繁，
        # 用上一拍的位置判斷「夠不夠近」就會在它走開之後對著空地打。
        mob = self._world.get(aim.gid)
        distance = mob.distance_from(pos) if (mob is not None and pos is not None) else None
        if (distance is not None and pos is not None
                and not self._close_enough(pos, mob.pos, distance)):
            # 還太遠、或中間有障礙 —— **繞過去**。**走不成也不准打**：
            # 舊版在這裡「算不出路就直接打」，等於隔著牆對空氣送封包。
            #
            # ⚠ 走不成要**當場換一隻**，不能只是 return。舊版忽略 `_approach()`
            # 的回傳值，於是「怪在樹後面、繞不過去」的時候每一拍都重算一次
            # 同一條算不出來的路，站在原地耗到 10 秒的放棄計時器 ——
            # 使用者實測回報「中間有障礙物比如樹，他會卡住不繞過去」。
            # 繞得過去的照樣繞（`_approach` 走的是 A*，本來就會繞開障礙）；
            # 這裡處理的是**真的繞不過去**那一種。
            #
            # ⚠⚠ 理由由 `_approach()` 給 —— 它分得出「真的過不去」「牠在傳點
            # 禁區裡」「已經走到最近那一格了」。舊版一律寫「繞不過去」，
            # 連「剛好走到了」也算進去（見 `_approach` 的說明）。
            why = self._approach(pos, mob.pos)
            if why is not None:
                self._give_up_target(aim, now, why)
                return
            if (not aim.stare_said and aim.moved_at
                    and now - aim.moved_at >= _STARE_WARN_SEC):
                # ★ 使用者說的「盯著怪發呆」就是這一刻。印一次，把能量到的
                #   全部攤開 —— 下一次回報不必再猜是哪一種（[DAT-076]）。
                aim.stare_said = True
                log.warning(
                    "[自動打怪] 對著「%s」發呆 %.1f 秒（距離 %s）：%s",
                    mob_name(self._class_of(aim.gid)), now - aim.moved_at,
                    distance, self._geometry(pos, mob.pos),
                )
            return
        # ⚠⚠ **送攻擊等於把方向盤交給伺服器。** 攻擊封包只帶 GID、不帶座標，
        # 最後那一段路是伺服器帶的（`_ATTACK_RANGE` 放到 10 格就是靠這個），
        # 而它走的是自己算的路，不是我們那條繞開傳點的 A*。
        # 所以送出去之前要先問一句「這一路上會不會踩到傳點」——
        # 怪站在傳點帶的另一側、中間直線又乾淨時，`_close_enough()` 會說
        # 「貼到了」，然後伺服器帶著角色直直穿過去（使用者實測回報的症狀）。
        #
        # ⚠⚠ **座標未知的怪要另外處理，不能把 None 丟進去。**
        # 只從傷害封包知道它存在的怪（`WorldTracker.note_monster`：它打到我了，
        # 但我們沒有它的實體封包、記憶體也還沒掃到）`pos` 是 None，
        # 舊版直接丟給 `line_cells()`，整個自動打怪執行緒當場炸掉
        # （TypeError: cannot unpack non-iterable NoneType；使用者實測
        #  2026-09-01 十分鐘內炸了 20 次，每次都靠「連線回來了」重啟）。
        # 沒有座標就驗不了那條線，改用**能證明的事**：那隻怪打得到我，
        # 代表它在我身邊 `_BLIND_REACH` 格內；這個圓裡沒有任何禁區的話，
        # 伺服器不管把我帶到圓裡哪一格都踩不到傳點。
        if mob is not None and mob.pos is None:
            if not self._warp_free_around(pos):
                self._give_up_blind(aim, now)
                return
        elif pos is not None and mob is not None and self._crosses_warp(pos, mob.pos):
            self._give_up_target(aim, now, "打過去會被伺服器帶著穿過傳點")
            return
        # 貼到了：直線 _ATTACK_RANGE 格內，而且中間乾淨
        self._walker.clear()
        self._world.note_attacking(aim.gid)
        self._send(build_query(aim.gid))
        self._send(build_attack(aim.gid))
        aim.attacked = True
        aim.attacked_at = now
        aim.sent_at = now
        aim.attacked_dist = distance or 0

    def _give_up_target(self, aim: _Aim, now: float, why: str) -> None:
        """放棄這一隻，換下一個。黑名單一下子，免得下一拍又挑到牠。"""
        log.info("%s：%s，換下一隻", mob_name(self._class_of(aim.gid)), why)
        self._skip[aim.gid] = now + _SKIP_SEC
        self._skip_at[aim.gid] = now
        self._drop_aggro(aim.gid)
        self._walker.clear()
        self._aim = None

    def _give_up_blind(self, aim: _Aim, now: float) -> None:
        """座標未知、又在傳點附近 —— 這一隻先放掉。

        ⚠ 拉黑要用 `_warp_skip` 而不是 `_skip`：這種怪幾乎都是**正在打我**的怪，
        而 `_pick_target()` 的「打我的怪優先」那一段**故意不看 `_skip`**
        （見那裡的說明）。用 `_skip` 的話下一拍又挑回同一隻，
        每一拍放棄一次，日誌洗到爆而角色一步都不會動。
        """
        log.info(
            "%s：還不知道牠在哪，而這附近有傳點，先不打",
            mob_name(self._class_of(aim.gid)),
        )
        self._warp_skip[aim.gid] = now + _SKIP_SEC
        self._drop_aggro(aim.gid)
        self._walker.clear()
        self._aim = None

    def _break_off(
        self, aim: _Aim, now: float, pos: tuple[int, int] | None
    ) -> None:
        """鎖定的怪跑進傳點範圍了 —— **當場收手，而且要主動把連續攻擊掐掉**。

        ⚠⚠ 這裡跟 `_give_up_target()` 差在**多送一個移動封包**，那不是順手，
        是這整件事的重點：`0x0437` action=7 是**連續**攻擊，送出去之後
        「角色走去哪」就交給伺服器了 —— 怪往傳點走，伺服器就把角色帶上去。
        只把 `_aim` 設成 None 的話，我們這邊看起來已經放棄了，
        **伺服器那邊還在追**（使用者實測：整段日誌沒有任何往傳點的移動，
        人卻被傳走了）。移動封包會取消連續攻擊（見 `_fight` 的說明），
        所以收手＝送一步走開。

        走去哪不重要，重要的是「有送出去」，所以目標挑最保守的：
        旁邊一格、在**走路禁區外**、而且盡量離那隻怪遠一點。
        """
        who = mob_name(self._class_of(aim.gid))
        why = "跑進傳點範圍" if self._pos_of(aim.gid) is not None else "座標不明又走到傳點附近"
        # ⚠ WARNING 級：使用者手上的預設層級就是 WARNING —— 這件事正是他回報的
        # 那個災難，被降級成 INFO 的話等於沒有記錄。
        log.warning("[自動打怪] 「%s」%s，收手（取消連續攻擊）", who, why)
        self._warp_skip[aim.gid] = now + _SKIP_SEC
        self._drop_aggro(aim.gid)
        self._walker.clear()
        self._aim = None
        self._note(f"「{who}」{why}，不追")
        if not aim.attacked:
            return  # 還沒開打，伺服器手上沒有這隻，不必掐
        step = self._step_away(pos, self._pos_of(aim.gid))
        if step is not None:
            self._send_move(*step)

    def _step_away(
        self, pos: tuple[int, int] | None, away_from: tuple[int, int] | None
    ) -> tuple[int, int] | None:
        """挑一格「走開」用的目標：旁邊一格、禁區外、離 `away_from` 越遠越好。

        找不到就退回 `_nearest_outside()`（它一圈一圈往外找）—— 那條也沒有的話
        就回 None，呼叫端不送封包。**不准退而求其次挑禁區裡的格**：
        為了取消攻擊而自己踩上傳點，比不取消還糟。
        """
        terrain = self._terrain
        if terrain is None or pos is None:
            return None
        best, best_distance = None, -1
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cell = (pos[0] + dx, pos[1] + dy)
                if cell == pos or not terrain.is_walkable(*cell):
                    continue
                if self._near_warp(cell):
                    continue
                distance = (
                    max(abs(cell[0] - away_from[0]), abs(cell[1] - away_from[1]))
                    if away_from is not None
                    else 0
                )
                if distance > best_distance:
                    best, best_distance = cell, distance
        return best if best is not None else self._nearest_outside(pos)

    def _pos_of(self, gid: int) -> tuple[int, int] | None:
        mob = self._world.get(gid)
        return mob.pos if mob is not None else None

    def _class_of(self, gid: int) -> int | None:
        mob = self._world.get(gid)
        return mob.class_id if mob is not None else None

    def _keep_attacking(self, aim: _Aim, now: float) -> None:
        """鎖定之後每 `_ATTACK_RETRY_SEC` 秒再送一次攻擊。**無條件，一直送到牠死。**

        為什麼無條件：`_ATTACK_RANGE` 放到 10 格之後，最後那一段是伺服器帶的，
        而角色跨過門檻的那一刻多半還在走 —— 那一擊會被伺服器忽略。
        舊版「只在一筆傷害都沒有、而且路已走完，才補一次」在這種情況幾乎不會動
        （診斷計數器 `resent` 接近 0），結果就是站在遠處罰站到放棄計時器到期
        （[PKT-065] 那個症狀）。一直送就沒有「錯過唯一那一擊」的問題。

        ⚠ 代價：`0x0437` action=7 是**連續**攻擊，重送很可能重置攻速計時器，
        正常互打時一直送、理論上 DPS 會掉。這是 2026-08-29 的實驗，
        用「打空氣 ÷ 擊殺」在同一個地點回測；變差就把間隔調大，
        或恢復「`_world.last_hit(gid) > aim.attacked_at` 就不送」。

        沒有「額度用完就停送」的閘門：放棄由 `_check_hit()` 負責換目標，
        兩邊都管會變成「停了但目標還在」的安靜狀態。
        """
        if now - aim.sent_at < _ATTACK_RETRY_SEC:
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
        """可以送攻擊了嗎？條件是**直線 `_ATTACK_RANGE` 格內，而且中間沒有障礙物**
        （使用者 2026-08-29 指定的條件）。

        `distance_from()` 是契比雪夫距離，中間隔著石頭、水、牆也照樣算 3 格。
        只看直線的話，隔著障礙的怪會被判成「貼到了」→ 送出攻擊 → 站著打空氣
        （使用者實測回報）。所以直線夠近之後，還要 `line_clear()` 確認那條直線
        每一格都能走。

        ⚠ **不要退回用 A* 的長度來判斷。** 舊版是「路徑步數 ≤ 直線 + 3」，
        兩個問題：一是允許繞路，二是 8 方向格子裡斜著閃開一顆石頭**不會多花步數**，
        所以「中間有障礙」照樣通過。要問的是「這條直線乾不乾淨」，
        那就直接量直線（`MapTerrain.line_clear()`），不要拿別的東西近似。

        貼身（1 格內）直接算數。怪站在不可走的格上（斜坡邊之類）就改看
        緊鄰牠、離我最近的可走格。驗不過一律回 False，呼叫端會走近再說 ——
        **不准隔著牆送攻擊**。
        """
        if straight > _ATTACK_RANGE:
            return False
        if straight <= 1:
            return True
        terrain = self._terrain
        if terrain is None:
            # 沒地形＝驗不了障礙物。這是啟動時就大聲說過的降級模式
            # （狀態列會寫「沒有地形」），不是安靜地放行。
            return True
        if terrain.is_walkable(*goal) and terrain.line_clear(pos, goal):
            return True
        beside = self._beside(goal, pos)
        if beside is None:
            return False  # 牠站的那格跟周圍九格都不可走 —— 過不去
        if max(abs(beside[0] - pos[0]), abs(beside[1] - pos[1])) <= 1:
            return True
        return terrain.line_clear(pos, beside)

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
        # ⚠ 先把「我們以為牠在哪」記下來，再 forget —— 順序反了就記到 None，
        # 那條黑名單就永遠解不開了。
        self._miss_pos[aim.gid] = self._pos_of(aim.gid)
        self._world.forget(aim.gid)
        self._skip[aim.gid] = now + _MISS_SKIP_SEC
        self._skip_at[aim.gid] = now
        self._drop_aggro(aim.gid)
        self._aim = None
        self._note(f"打到空氣（座標過時），換下一隻｜共 {self._stats.missed} 次")

    def _approach(self, pos: tuple[int, int], goal: tuple[int, int]) -> str | None:
        """走到怪**旁邊**的可走格。

        回 `None` ＝ 沒問題（還在路上，或這一段已經走完了）；
        回字串 ＝ 放棄這一隻的**理由**，呼叫端拿去寫日誌。

        ## ⚠ 為什麼不是 True/False

        舊版是 `return self._walker.update(pos) == "walking"`，於是
        `"arrived"`（走到那一格了）與 `"idle"`（沒路可走＝已經站在目標上）
        通通被當成失敗，呼叫端一律報「繞不過去」並把那隻怪**列入 30 秒黑名單**。

        使用者實測回報：「繞路有問題，常常會說繞不過去害我卡住」——
        真正在發生的事是**我們剛好走到了**，然後就把牠丟掉了。
        `_plan_path()` 在「起點就是終點」時回空 list，`if not path` 也踩同一個坑。

        現在把「走完了」當成正常：下一拍會用新的距離重新判斷，多半直接開打。
        真的過不去的三種情況各有各的說法，日誌看得出是哪一種。
        """
        target = self._beside(goal, pos)
        if target is None:
            return "牠站的地方四周都不能走"
        if target == pos:
            # 已經站在最靠近牠的那一格了，還被判定打不到 —— 再走也沒有用
            # （多半是牠站在不可走的格上，直線過不去）。
            return "站到最近的那一格了還是打不到"
        if self._no_go(target):
            # 過去要踩進傳點禁區、或王的禁區 —— 那是**不去**，不是走不到。
            return "牠在傳點禁區裡" if target in self._warp_zone else "牠在王旁邊，避開"
        current = self._walker.goal
        if current is None or max(abs(current[0] - target[0]), abs(current[1] - target[1])) > 2:
            path = self._plan_path(pos, target)
            if path is None:
                return "繞不過去"
            if path:
                self._walker.set_path(path, avoid=self._walk_block())
        state = self._walker.update(pos)
        if state in ("walking", "arrived", "idle"):
            return None
        # ★ 走不成**先從現在站的地方重新規劃**，不要第一次就把牠拉黑。
        #   會走到這裡的三種原因 —— 伺服器帶的路跟我們算的不一樣（偏離路徑）、
        #   被打的硬直吃掉移動、某一段被拒絕 —— 都不是「這隻怪繞不過去」。
        #   實機一份日誌裡「走到一半被擋住，換下一隻」17 次，全在擊殺後 1~2 秒
        #   （旁邊的怪還在打人）。清掉路徑，下一拍 `current is None` 就會重算。
        aim = self._aim
        if aim is not None and aim.replans < _APPROACH_REPLANS:
            aim.replans += 1
            log.info("往「%s」的路走不成（%s）—— 從 %s 重新規劃（第 %d 次）",
                     mob_name(self._class_of(aim.gid)), self._walker.debug_state(),
                     pos, aim.replans)
            self._walker.clear()
            return None
        return f"重新規劃 {_APPROACH_REPLANS} 次還是走到一半被擋住（{self._geometry(pos, goal)}）"

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

        ⚠ 唯一的例外是使用者的撿取黑名單（`_unwanted`）—— 那是他明講不要的東西。
        """
        for item in self._world.ground_items():
            if self._unwanted(item):
                continue
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
            if self._unwanted(item):
                # ⚠ 黑名單的東西**不 `forget_item`**：忘掉之後下一包實體同步
                # 又會把它放回來，等於每一拍白跑一次。留在清單裡、每次跳過
                # 最便宜，而且「地上有什麼」的診斷資訊也不會被吃掉。
                continue
            if item.pos is None:
                continue  # 座標解不出來就沒得走過去（`_grab_nearby` 已經照撿了）
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
        # ⚠ 走不成、或「該在走卻沒動」（見 `_STALL_SEC`）都算撿不到 ——
        #   不放掉的話這一支每拍都回 True，漫遊永遠輪不到，人就站在那裡。
        if self._walker.update(pos) == "blocked" or self._stalled(now):
            self._world.forget_item(item.entity_id)
            self._walker.clear()
            self._stand_still(now)
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
        stalled = self._stalled(now)
        if state == "walking" and not stalled:
            return
        if state == "walking":
            # ⚠⚠ 走路那一支說「還在走」，但**座標好幾秒沒變過**。
            # 以讀得到的訊號為準（CLAUDE.md：做→讀→確認），當成這條路走不成。
            # 這裡要**大聲**：它是安靜站著 45 秒那件事唯一的線索。
            log.warning(
                "[自動打怪] 漫遊 %.1f 秒沒有前進（往 %s，走路狀態說還在走）"
                "—— 換一條路｜%s",
                stalled, self._roam_goal, self._walker.debug_state(now),
            )
            self._walker.clear()
            self._stand_still(now)      # 重新起算，不然下一拍又報一次
            state = "blocked"
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
                if dest is None or self._is_bad_goal(dest) or self._no_go(dest):
                    continue   # 漫遊目標也不准挑在傳點或王的禁區上
                self._roam_goal = dest
            path = self._plan_path(pos, self._roam_goal)
            if path:
                self._walker.set_path(path, avoid=self._walk_block())
                self._walker.update(pos)
                return
            self._bad_goals.append((self._roam_goal, now + _BAD_GOAL_SEC))
            self._roam_goal = None
        # 完全算不出路：往近處走一步，絕不原地不動（但別踩到傳點）。
        # ⚠ 這一步**沒有經過 Walker**，中間那段路完全是伺服器自己走的 ——
        # 所以除了目標格，直線經過的每一格也都要檢查。
        for _ in range(6):
            near = terrain.random_walkable(random, near=pos, radius=MAX_STEP)
            if near is None or near == pos or self._no_go(near):
                continue
            if any(self._no_go(cell) for cell in line_cells(pos, near)[1:]):
                continue
            self._send_move(*near)
            return

    def _plan_path(
        self, start: tuple[int, int], goal: tuple[int, int]
    ) -> list[tuple[int, int]] | None:
        """算一條路，繞開**傳點與王**（`_walk_block`）—— 踩傳點會被傳走、靠近王會被打。"""
        if self._terrain is None:
            return None
        return self._terrain.find_path(
            start, goal, node_budget=_ROAM_BUDGET, blocked=self._walk_block()
        )

    def _load_warps(self, map_name: str) -> None:
        """記下這張圖上不准踩的格子（傳點與周圍）。

        查不到就是空的 —— 安全退化成「跟以前一樣會踩到」，不會因此不能走路。
        """
        from_data = _warp_cells_of(map_name)     # 取樣點 ＋ 補起來的傳點帶
        learned = self._learned.get(map_name, set())
        cells = set(from_data) | learned
        self._warp_zone = _keep_out(cells)
        # 傳點**本體**那一格。禁區是「不想去」，本體是「踩到就被傳走」——
        # 從禁區裡面往外走時只避開本體，避開整片禁區的話就永遠走不出來。
        self._warp_cells = frozenset(cells)
        # 「不打」的範圍另外一片，而且更大 —— 走路我們自己控得住（A* 繞開就好），
        # 打怪的最後一段是伺服器帶的，控不住，只能離遠一點。
        self._no_fight_zone = _keep_out(cells, radius=_WARP_NO_FIGHT)
        # 換圖了：王的禁區作廢（王是上一張圖的），重新起算。
        self._boss_zone = frozenset()
        self._boss_said.clear()
        log.info(
            "%s 的傳點 %d 格（資料＋帶狀 %d、實際踩過學到 %d）、"
            "走路禁區 %d 格、不打範圍 %d 格",
            map_name, len(self._warp_cells), len(from_data), len(learned),
            len(self._warp_zone), len(self._no_fight_zone),
        )

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
        # ⚠ 只信「確定是在那張圖上讀到的」那幾點（`_recent` 有帶地圖名）。
        points = [cell for where, cell in self._recent if where == old_map]
        if not points:
            log.warning("⚠ 在 %s 被傳走了，但最近幾拍的座標都不屬於那張圖，"
                        "這次不學（學了會擋到沒事的路）", old_map)
            return
        target = self._walker.target
        if target is not None:
            points.append(target)

        # ⚠⚠ **一段一段檢查距離**。相鄰兩拍差超過 `_LEARN_MAX_JUMP` 格的
        # 「一段」不是走出來的 —— 那條線橫跨整張圖，連起來就是幾百格假傳點。
        span: set[tuple[int, int]] = {points[-1]}
        dropped = 0
        for a, b in zip(points, points[1:], strict=False):
            if max(abs(a[0] - b[0]), abs(a[1] - b[1])) > _LEARN_MAX_JUMP:
                dropped += 1
                span.clear()          # 這一點之前的都不可信，重新開始
                span.add(b)
                continue
            span.add(a)
            span.update(line_cells(a, b))
        if dropped:
            log.warning("⚠ %s 的傳點回推丟掉 %d 段（座標跳太遠，多半是換圖那一拍"
                        "讀到新地圖的座標）—— 只學剩下的部分", old_map, dropped)

        learned = self._learned.setdefault(old_map, set())
        before = len(learned)
        if before >= _LEARN_MAX_CELLS:
            log.warning("⚠ %s 已經學到 %d 格傳點（上限 %d），不再學 ——"
                        "再學下去整張圖會算不出路。這通常代表座標讀取有問題",
                        old_map, before, _LEARN_MAX_CELLS)
            return
        learned |= span
        log.warning("⚠ 在 %s 被傳走了（最後看到 %s，正要走去 %s）——"
                    "把這一段 %d 格記成傳點，這次開著的期間都不再踩",
                    old_map, points[-2] if len(points) > 1 else points[-1],
                    target if target is not None else "沒有目標（站著）",
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

    def _wake_position(self, now: float, pos: tuple[int, int] | None) -> bool:
        """座標是即時的嗎。不是的話推一步把移動元件逼出來，並回 False。

        回 False = **這一拍什麼都別做**（除了推的那一步）。
        剛換圖的頭幾拍讀不到是正常的，所以要等 `_STALE_POS_SEC` 才動手。
        """
        reader = self._reader
        if reader is None or reader.position_live:
            self._stale_since = 0.0
            return True
        # ⚠⚠ **座標不是即時的時候，先把「正在脫離傳點」那件事放掉。**
        #   這一支回 False 會讓整拍 `continue`，`_escape_warp()` 根本不會被叫到
        #   —— 它裡面那個「12 秒走不到就換方向」的出口等於不存在，
        #   `_escape_goal` 就永遠留在那裡。實機踩過（2026-08-31）：
        #   `_doing()` 一路回報「正在脫離傳點禁區（往 (153, 20)）」，
        #   45 秒後被當成卡住。座標回來之後本來就要重算，留著沒有意義。
        self._escape_goal = None
        if not self._stale_since:
            self._stale_since = now
            return False
        if now - self._stale_since < _STALE_POS_SEC:
            return False
        if now - self._woke_at < _WAKE_EVERY_SEC:
            return False
        self._woke_at = now
        terrain = self._terrain
        if terrain is None or pos is None:
            return False
        # 往旁邊一格可以站的地方走一步就夠了 —— 走去哪不重要，
        # 重要的是「角色動了」。禁區照樣避開。
        # ⚠ 用 `_nearest_outside()`（它本來就避開傳點禁區）—— 推一步的時候
        # 更不能踩到傳點：那時候我們連自己在哪都還不確定。
        target = self._nearest_outside(pos)
        if target is None or target == pos:
            return False
        log.info("座標還不是即時的（讀到的是進圖座標），往 %s 推一步把它逼出來",
                 target)
        self._send_move(*target)
        return False

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
        now = time.monotonic()
        if self._escape_goal is not None:
            # 已經在往外走了就讓它走完，別每一拍重算一條新路狂送走路封包。
            state = self._walker.update(pos)
            # ⚠⚠ **時間也要看，不能只信走路那一支說的「走不成」。**
            # 實機 2026-08-29：白狐在傳點禁區裡卡了 45 秒，`Walker` 一路回報
            # 「walking」（送得出去、也收得到確認），只是人沒有真的前進 ——
            # 於是這裡永遠等不到 "blocked"，脫離這件事沒有任何出口，
            # 一直到「45 秒毫無進展」把自動打怪關掉。
            too_long = now - self._escape_since > _ESCAPE_GIVE_UP_SEC
            if state == "walking" and not too_long:
                return True
            if state == "blocked" or too_long:
                # ⚠⚠ **走不出去的那一格要記下來。**
                # `_nearest_outside()` 每次都回「最近的那一格」，不記的話
                # 下一拍算出來的還是同一格、同一條路 —— 一直撞同一面牆。
                self._bad_goals.append((self._escape_goal, now + _BAD_GOAL_SEC))
                log.info("%s脫離傳點禁區走不到 %s，換一個方向",
                         "太久：" if too_long else "", self._escape_goal)
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
            # 算不出路也是「這一格不行」—— 不記的話下一拍還是挑它，
            # 每一拍重算一次同一條算不出來的路（跟走不成是同一個坑）。
            self._bad_goals.append((goal, now + _BAD_GOAL_SEC))
            return False
        self._escape_goal = goal
        self._escape_since = now
        # ⚠⚠ **脫離的時候一定要把鎖定的怪放掉。** 連續攻擊還掛在伺服器身上的話，
        # 我們往外走一步、伺服器就把人拉回怪旁邊 —— 兩邊拔河，人出不去，
        # 45 秒之後被當成卡住（那正是使用者看到的「在船點前面自己關掉」）。
        # 不必另外送封包取消：下面 `set_path()` 送出去的移動就是取消。
        if self._aim is not None:
            self._warp_skip[self._aim.gid] = now + _SKIP_SEC
            self._aim = None
        self._walker.set_path(path, avoid=self._warp_cells)
        self._walker.update(pos)
        self._note(f"太靠近傳點，先走開（往 {goal[0]},{goal[1]}）")
        return True

    def _nearest_outside(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        """離 `pos` 最近、又在禁區外面的可走格。由近而遠一圈一圈找。

        ⚠ **跳過走不成的那些**（`_bad_goals`）。地形圖說「可以站」不代表
        「走得過去」—— 中間可能隔著樹或整片走不通的地形。不跳過的話
        每一拍都會挑到同一格、算出同一條走不成的路，一直撞到 45 秒沒進展
        的保護把自動打怪關掉（使用者實測回報「掛機自己停了」）。
        """
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
                    if self._is_bad_goal(cell):
                        continue        # 剛剛走不到，換一個
                    best = cell
                    break
                if best is not None:
                    break
            if best is not None:
                return best
        return None

    def _near_warp(self, cell: tuple[int, int] | None) -> bool:
        """走路禁區：這一格不要踩（`warpzone.KEEP_OUT`）。"""
        return cell is not None and cell in self._warp_zone

    def _crosses_warp(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        """a→b 這條直線會不會經過走路禁區。

        用直線是因為**那一段路不是我們走的**：伺服器帶人（送攻擊、或送一個
        遠一點的移動點）時走的是它自己算的路，直線是最好的近似
        （跟 `Walker._clear_line` 同一個理由）。

        ⚠ 起點自己不算：站在禁區裡的時候由 `_escape_warp()` 負責走出去，
        在那之前把每一個目標都判死刑，只會變成「站在禁區裡把附近的怪全部拉黑」。
        """
        return any(cell in self._warp_zone for cell in line_cells(a, b)[1:])

    def _blind_near_warp(
        self, aim: _Aim, pos: tuple[int, int] | None
    ) -> bool:
        """鎖定的怪座標未知，而我們已經走到傳點附近了嗎？

        ⚠ 開打之後也要問：`_fight()` 只在**送攻擊之前**驗過一次，而
        `0x0437` 是連續攻擊 —— 那隻怪往傳點走、伺服器就把角色一路拉過去，
        座標未知的話 `_no_fight()` 那條（看怪站在哪）永遠是 False，
        擋不住任何東西。這裡改看**自己**走到哪：只要我被拉到禁區
        `_BLIND_REACH` 格內，就當場收手（`_break_off` 會送一步取消攻擊）。
        """
        mob = self._world.get(aim.gid)
        if mob is None or mob.pos is not None:
            return False
        return not self._warp_free_around(pos)

    def _warp_free_around(
        self, pos: tuple[int, int] | None, radius: int = _BLIND_REACH
    ) -> bool:
        """`pos` 周圍 `radius` 格內完全沒有走路禁區嗎？

        給**座標未知的怪**用的（見 `_fight`）。禁區全在這個圓外面的話，
        伺服器不管把角色帶到圓裡哪一格、走哪一條路，都踩不到傳點 ——
        這是沒有對方座標時唯一能證明的事，比「先打了再說」誠實。

        自己的座標也讀不到（`pos is None`）就算**不安全**：
        兩邊都不知道還送連續攻擊，等於閉著眼睛把方向盤交給伺服器。
        """
        if not self._warp_zone:
            return True     # 這張圖沒有傳點，沒有東西可踩
        if pos is None:
            return False
        x, y = pos
        return not any(
            (x + dx, y + dy) in self._warp_zone
            for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1)
        )

    def _no_fight(self, cell: tuple[int, int] | None) -> bool:
        """站在這一格的怪**不打**（`warpzone.NO_FIGHT`，涵蓋走路禁區）。

        ⚠ 三片都問。`_no_fight_zone` 本來就是 `_warp_zone` 的超集，
        但「不打的範圍不准比不踩的範圍窄」是這裡唯一不能出錯的性質 ——
        用 `or` 寫死它，就不必依賴兩片是不是同一次算出來的。
        `_boss_zone` 只有勾了「遠離王」才非空：王旁邊的怪也不打，
        追過去就進了王的自動攻擊範圍。
        """
        return cell is not None and (
            cell in self._no_fight_zone
            or cell in self._warp_zone
            or cell in self._boss_zone
        )

    def _no_go(self, cell: tuple[int, int] | None) -> bool:
        """走路不准踩這一格（傳點禁區 ＋ 王的禁區）。挑漫遊目標／近距走一步時用。"""
        return cell is not None and (
            cell in self._warp_zone or cell in self._boss_zone
        )

    def _walk_block(self) -> frozenset[tuple[int, int]]:
        """走路一律繞開的整片格子：傳點禁區 ＋ 王的禁區。

        沒有王的禁區時**直接回傳點那一份**（不複製）—— 一般情況（沒勾遠離王、
        或這張圖沒王）就跟原本一模一樣，零額外成本。
        """
        if not self._boss_zone:
            return self._warp_zone
        return self._warp_zone | self._boss_zone

    def _refresh_boss(self, pos: tuple[int, int] | None) -> None:
        """每一拍：看視野裡有沒有王（MVP），該講的講、該繞的繞。

        - **知道王是哪隻**：只要看到王就講一次（`_boss_said` 去重），
          不管有沒有勾「遠離王」—— 使用者要能知道這張圖上有王、是哪一隻。
        - **勾了才繞**：`_avoid_boss` 為 True 時，把每一隻王周圍 `_BOSS_KEEP_OUT`
          格圈成禁區（王會移動，所以每拍重算），走路（`_walk_block`）與不打
          （`_no_fight`）都吃這一片。沒勾就維持空集合，完全不影響原本的行為。

        王的座標是即時的（記憶體移動元件），沒座標的王只能講、圈不了 ——
        圈不了也沒關係，`is_farmable` 本來就擋著不會去打牠。
        """
        bosses = [
            m for m in self._world.monsters()
            if is_boss(m.class_id) and m.pos is not None
        ]
        for m in bosses:
            if m.gid in self._boss_said:
                continue
            self._boss_said.add(m.gid)
            tail = "，正在避開" if self._avoid_boss else "（沒有勾「遠離王」，只有不打）"
            log.warning("⚠ 這張圖有王：%s 在 %s%s",
                        mob_name(m.class_id), m.pos, tail)
        if self._avoid_boss and bosses:
            self._boss_zone = _keep_out({m.pos for m in bosses}, radius=_BOSS_KEEP_OUT)
        else:
            self._boss_zone = frozenset()

    def _is_bad_goal(self, cell: tuple[int, int]) -> bool:
        return any(
            max(abs(cell[0] - bad[0]), abs(cell[1] - bad[1])) <= _BAD_GOAL_RADIUS
            for bad, _until in self._bad_goals
        )

    # ---- 雜項 -------------------------------------------------------

    def _send_move(self, x: int, y: int) -> None:
        self._send(build_move(x, y))

    def _client_moving(self) -> bool | None:
        """客戶端認為角色現在正在走嗎？讀不到回 None（**不等於站著**）。

        給 `Walker` 判斷「這一段到底是被打斷了，還是只是我取樣得比較慢」。
        見 `services/player_position.py` 的 `moving()`。
        """
        reader = self._reader
        return reader.position_moving() if reader is not None else None


    def _send(self, data: bytes) -> bool:
        """送封包。失敗代表 socket 已經失效（多半是換頻道），下一拍會重綁。

        回傳「送出去了沒」，讓呼叫端分得出「沒送成功」與「送了沒效果」——
        兩者的處置不一樣（前者等重綁，後者要重試）。
        """
        if not self._link.send(data):
            self._resync_at = 0.0     # 逼下一拍立刻重綁，不要等節流時間
            return False
        return True

    def _cleanup(self) -> None:
        if self._entities is not None:
            self._entities.close()
            self._entities = None
        self._link.close()

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
