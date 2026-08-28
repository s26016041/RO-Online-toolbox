"""AOB 特徵登錄表。

依專案 CLAUDE.md：位址一律用特徵掃描定位，不准寫死。
每條特徵都要記錄怎麼生成、拿什麼驗證，對應的 GAMEDATA 條目寫在註解裡。

新增特徵的流程：
  1. 多開／多次重開比對，相同的位元組才留成固定，其餘遮成 ??。
  2. 驗證唯一性：每個行程只能命中預期的那些位址。
  3. 記進 GAMEDATA 的 MEM 條目。
"""

from __future__ import annotations

from dataclasses import dataclass

from ro_toolbox.services.aob import AOBSignature, CodeSignature

# ---------------------------------------------------------------------------
# 角色狀態（HP / SP / 等級）
# ---------------------------------------------------------------------------
# 生成方式：同時開三個角色（Base/Job/HP/SP 各不相同），先用數值掃描找出
# 三者共通的結構位址，再取該位址前 0x20 bytes 的共同位元組樣式當特徵。
#
# 樣式內容是 HP 前方的固定欄位：0, 4, 15, 0, 1, 0, <變動>, 0
# 第 7 個 dword 三個行程都不同（0x53a2591 / 0x53f8c9d / 0x531dbba），已遮成 ??。
#
# 驗證（2026-08-23，見 GAMEDATA [MEM-003]）：
#   三個行程各只命中 1 個位址，都等於預期位址，六個欄位值全部正確。
CHAR_STATUS = AOBSignature(
    pattern=(
        "00 00 00 00  04 00 00 00  0F 00 00 00  00 00 00 00 "
        "01 00 00 00  00 00 00 00  ?? ?? ?? ??  00 00 00 00"
    ),
    value_offset=0x20,
    vt_key="int32",
    label="角色狀態（HP 起點）",
)


@dataclass(frozen=True)
class StatusOffsets:
    """相對於 CHAR_STATUS 命中位址（= HP 所在處）的欄位偏移。

    結構偏移屬於 CLAUDE.md 允許寫死的類別（大更新才會壞），
    但必須留出處：這些偏移是三個角色交叉比對出來的，見 GAMEDATA [MEM-003]。
    """

    hp: int = 0x00
    max_hp: int = 0x04
    sp: int = 0x08
    max_sp: int = 0x0C
    # Base 與 Job 在同一結構的前方，兩者相距 0x08。
    # 三個角色在 HP±0x8000 範圍內各自只有這一個共同偏移。
    base_level: int = -0x3B58
    job_level: int = -0x3B50
    # 經驗值：Base 等級前面連著四個 int64（高 32 位元實測都是 0）。
    # 出處見 GAMEDATA [MEM-015]：先用「畫面百分比 = 目前/所需」在角色結構
    # ±64KB 內找出唯一符合的一對（8961/12986 = 69.01%），
    # 再**邊打怪邊看誰會增加**確定哪個是經驗、哪個是門檻 ——
    # 70 秒內 -0x20 增加 1628、-0x08 增加 1020，另外兩個完全沒動。
    base_exp: int = -0x3B78       # base_level - 0x20
    base_exp_next: int = -0x3B70  # base_level - 0x18
    job_exp_next: int = -0x3B68   # base_level - 0x10
    job_exp: int = -0x3B60        # base_level - 0x08
    # 角色 AID（帳號 ID，int32）：base_level - 0x4C。
    # 出處 GAMEDATA [MEM-017]：狐狐狸讀到 23810315 = 0x016B510B，
    # 與使用者實測 0x00A7 封包裡帶的 AID 完全一致。使用道具封包要帶它。
    aid: int = -0x3BA4
    # 角色名：cp950 編碼、null 結尾。三個行程的名稱字串搜尋結果中，
    # 只有這個位址三者共有（其餘命中是其他玩家名／UI 快取）。
    name: int = 0x2800
    # 當前地圖的檔名（ASCII、null 結尾），例如 moc_fild01 / payon。
    # 三個角色在不同地圖，這是 HP 結構 ±0x8000 內唯一的共同偏移。
    map_name: int = -0x3B9C
    # ⚠ 角色座標**不在這裡**。它以前是 `position: int = -0x3AD5FC`
    #   （相對 HP 全域的固定距離），2026-08-26 改版後那個距離就變了 ——
    #   兩個全域不是同步位移的。改用程式碼特徵定位，見下面的 POSITION_SIGS。


STATUS_OFFSETS = StatusOffsets()

# ---- 選角畫面的兩個全域：用**程式碼骨架**定位，不寫死位址 ----------------
#
# 為什麼需要它們：選角畫面上 HP／等級都還沒有值，`CharacterReader` 那條
# 靠數值合理性驗證的 AOB 過不了關。但自動選角就是需要在那個時候知道
# 「游標停在第幾格」與「客戶端最後選了誰」——那是唯一能在按下 Enter
# **之前**確認狀態、按下去之後核對結果的訊號。
#
# 兩組都是「指令骨架當錨、答案從立即值讀出來」（CLAUDE.md 的最高原則）。
# 2026-08-26 在實機上驗證：三組游標特徵、七＋三＋一處命中，
# 讀出來的位址全部一致；名字那條骨架裡本身就有三個立即值互相對照。

#: 選角畫面「游標停在第幾格」（1 byte）。這個數字就是格號 ——
#: 實機驗證：讀到 4 時按 Enter，進到的是伺服器清單裡的第 4 格那隻。
SELECT_CURSOR_SIGS = (
    CodeSignature(
        name="cursor-movzx-push8",
        pattern="0F B6 05 ?? ?? ?? ?? 50 6A 08 FF 15",
        operands=(3,),
        why="movzx eax, byte [游標]; push eax; push 8; call [...]"
            "（把目前格號當參數傳給某個函式）。實機 7 處命中，答案一致。",
    ),
    CodeSignature(
        name="cursor-movzx-cmp",
        pattern="0F B6 0D ?? ?? ?? ?? 8B 87 18 01 00 00 80 3C 01 01",
        operands=(3,),
        why="movzx ecx, byte [游標]; mov eax,[edi+0x118]; cmp byte [ecx+eax],1"
            "（拿格號去查那一格有沒有人）。實機 3 處命中。",
    ),
    CodeSignature(
        name="cursor-store",
        pattern="88 0D ?? ?? ?? ?? 8B 8E 34 01 00 00 85 C9",
        operands=(2,),
        why="mov [游標], cl（寫入端）。實機 1 處命中。",
    ),
)

#: 選角畫面上「客戶端最後選定的角色名字」那個緩衝區。
SELECT_NAME_SIGS = (
    CodeSignature(
        name="name-xor-loop",
        pattern="8A 8C 02 ?? ?? ?? ?? 30 88 ?? ?? ?? ?? 40 83 F8 40 72 ED "
                "B8 ?? ?? ?? ?? C3",
        operands=(3, 9, 20),
        why="一段對名字緩衝區做 0x40 bytes 逐位元組 xor 的迴圈，"
            "最後 mov eax, 緩衝區位址; ret。骨架裡三個立即值指的是同一個位址，"
            "彼此就是驗證。實機 1 處命中。",
    ),
)

# ---- 角色座標：**已經沒有特徵了，故意的** --------------------------------
#
# 這裡以前有 `POSITION_X/Y_SIGS`，錨在 `cmp [x],ecx … mov [x],ecx` 的骨架上。
# 骨架本身沒問題（實機 1 處命中、兩個立即值互驗、y 剛好 x+4），
# **錯的是那個全域根本不是角色位置**。
#
# 2026-08-28 實機（GAMEDATA [MEM-047]）：`izlude_in` 上讀到 (112,181) 25 秒不變，
# 那是換圖前 `izlude` 的殘留，而 `position_located` 照樣是 True ——
# 「安靜地做錯事」的教科書案例。
#
# 靜態分析查出來的根因：那個全域在整個 33 MB 模組裡**只被兩條指令碰過**，
# 都在 `ragexe+0xD7440` 這一個函式裡，而它是**小地圖畫標記**用的
# （讀 [eax+0x110]/[eax+0x114] 當地圖寬高把格座標換算成螢幕座標、
# 推顏色 0x77333333、做字串格式化，十個呼叫端各推一個標記種類）。
# 沒有小地圖圖檔的地圖上那段程式碼根本不會跑 —— 而 `texture/…/map/*.bmp`
# 只有 742 張，我們有地形的是 1082 張，**396 張（37%）沒有小地圖**。
#
# ⚠ 教訓：**特徵找得到 ≠ 找到的是對的東西。** 這條特徵每一項技術指標都漂亮，
# 唯獨沒有人問過「這個全域的語意是什麼」。新增特徵時要多做一件事：
# 把命中處的**函式**看一遍，確認它在做的事就是你以為的那件事。
#
# 現在座標分成兩個來源（見 `services/player_position.py`）：
# **移動元件**用 AID 認人，不需要特徵；**進圖座標**是靜態全域，特徵在下面。

# ---- 進圖座標：伺服器 `0x0091` 說「你被移到這裡」------------------------
#
# 為什麼需要它：角色的移動元件要**在這張圖上走過一步**才會有內容，
# 剛傳過來的時候整塊是空的（實機驗證過：換圖後 60+ 個 `GID == AID` 的候選
# 沒有一個通得過驗證）。那一段空窗期只有這個全域知道人在哪。
#
# ⚠ 它**不跟著走路更新**：實機送一段移動、角色走了 9 格，它紋風不動。
# [MEM-006] 當年把它（`HP-0x4290`）當即時座標又推翻，就是因為這個性質。
# 所以它只回答「進這張圖的時候我在哪」，不能拿來當目前位置用。
#
# 怎麼找到的：拿實機的真值（伺服器 `0x0091` 說的 (112,179)）全記憶體搜，
# 模組內只有兩處，其中一處是舊的小地圖全域，另一處就是它；
# 再用位元組搜尋找出所有引用 —— **寫入端 3 處、讀取端 3 處**，
# 挑其中兩處互相獨立的當骨架。
#
# 骨架 A（`ragexe+0x11C400`，`0x0091` 處理函式的尾巴）：
#
#     8B 45 20        mov  eax, [ebp+20h]        ← 參數 x
#     FF 75 28        push dword [ebp+28h]
#     A3 <x>          mov  [進圖x], eax
#     8B 45 24        mov  eax, [ebp+24h]        ← 參數 y
#     A3 <y>          mov  [進圖y], eax
#     FF 15 <ptr>     call dword [...]
#
# 骨架 B（`ragexe+0x8EAD19`）：連續四個 `mov eax,[ebp-disp]; mov [全域],eax`，
# 第一個寫的是地圖名全域（`0x15D2AC8`，`std::string`），
# 後面三個就是進圖座標的 x / y / 方向 —— 版面本身就是驗證。
#
# 所有立即值一律遮成 ??；答案是從命中處讀出來的。
# 實機兩條骨架讀出同一個位址 `0x15D3A08`，而且 y 剛好是 x+4。
_MAP_ENTRY_A = (
    "8B 45 ?? FF 75 ?? A3 ?? ?? ?? ?? 8B 45 ?? A3 ?? ?? ?? ?? FF 15 ?? ?? ?? ??"
)
_MAP_ENTRY_B = (
    "8B 85 ?? ?? ?? ?? B9 ?? ?? ?? ?? A3 ?? ?? ?? ?? "
    "8B 85 ?? ?? ?? ?? A3 ?? ?? ?? ?? 8B 85 ?? ?? ?? ?? A3 ?? ?? ?? ?? "
    "8B 85 ?? ?? ?? ?? A3 ?? ?? ?? ??"
)

#: 進圖座標的 x。兩條互相獨立的骨架必須讀出同一個位址。
MAP_ENTRY_X_SIGS = (
    CodeSignature(
        name="map-entry-x-mapmove",
        pattern=_MAP_ENTRY_A,
        operands=(7,),
        why="0x0091 處理函式：mov [進圖x], eax（參數 x 直接落地）。實機 1 處命中。",
    ),
    CodeSignature(
        name="map-entry-x-spawn",
        pattern=_MAP_ENTRY_B,
        operands=(23,),
        why="連續寫四個全域（地圖名、x、y、方向）的那一段。實機 1 處命中，"
            "與骨架 A 讀出同一個位址 0x15D3A08。",
    ),
)

#: 進圖座標的 y。同兩條骨架的下一個立即值。
MAP_ENTRY_Y_SIGS = (
    CodeSignature(
        name="map-entry-y-mapmove",
        pattern=_MAP_ENTRY_A,
        operands=(15,),
        why="同骨架 A 的 mov [進圖y], eax。實機讀出 0x15D3A0C（= x+4）。",
    ),
    CodeSignature(
        name="map-entry-y-spawn",
        pattern=_MAP_ENTRY_B,
        operands=(34,),
        why="同骨架 B 的第三個全域寫入。",
    ),
)

#: x 與 y 一定相鄰（兩個 int32）。對不上就是解錯了，要大聲失敗。
MAP_ENTRY_XY_GAP = 4

#: 遊戲內建尋路（導航）目標地圖的全域緩衝區。內容是 `char[16]`，
#: 存的是**帶副檔名**的地圖名，例如 `prt_fild08.rsw`。
#:
#: 為什麼需要它：使用者按下遊戲的尋路按鈕時客戶端**一個封包都沒送**
#: （實測 `封包/按下尋路.txt` 只有對時心跳），箭頭是客戶端自己算的。
#: 要知道他想去哪，只能從記憶體讀。
#:
#: 怎麼找到的（GAMEDATA [MEM-040]）：先把導航目標設成 A、搜出所有含 A 的位址，
#: 再把目標改成 B、看哪些位址跟著變成 B。235 個候選只有 6 個真的變，
#: 其中唯一落在模組映像內（＝靜態全域、可以用程式碼特徵定位）的就是這個。
#:
#: ⚠ 這條特徵錨在 CRT 靜態建構鏈上，只有 1 處命中。建構順序在改版時可能變動，
#: 那會讓它安靜地指到**別的**全域。所以讀取端一定要驗內容像不像地圖名，
#: 驗不過就大聲停用（見 `services/navigation.py`）。
NAVI_DEST_SIGS = (
    CodeSignature(
        name="navi-dest-ctor",
        pattern="C6 45 FC 05 B9 ?? ?? ?? ?? E8 ?? ?? ?? ?? "
                "C6 45 FC 01 B9 ?? ?? ?? ?? E8 ?? ?? ?? ?? "
                "C6 45 FC 00 B9 ?? ?? ?? ?? E8 ?? ?? ?? ?? "
                "C7 45 FC FF FF FF FF",
        operands=(19,),
        why="一串靜態物件的建構：mov ecx,<全域>; call <ctor>，中間夾著"
            "mov byte [ebp-4],N 的例外狀態標記。挑這一段是因為標記值的序列"
            "（05 → 01 → 00 → -1）夠特別。四個 mov ecx 的立即值與所有 rel32"
            "全部遮掉，答案是從命中處讀出來的。實機 1 處命中，讀出 0x123CD58。",
    ),
)

#: 導航目標緩衝區讀幾個 bytes。地圖名最長 `xxxxxxxxxxx.rsw`，讀 24 再截到 null。
NAVI_DEST_MAX_BYTES = 24

# ---- 尋路視窗選的「目的地地圖」-------------------------------------------
#
# ⚠ 上面那條 `NAVI_DEST_SIGS` 定位到的全域**不是使用者選的目的地**。
# 實測（2026-08-27，[MEM-046]）：它比較像「最近載入／請求的地圖檔」——
# 人站在 izlu2dun 時裡面是 `izlu2dun.rsw`，而且會被清空。整個程式碼區段裡
# **只有一處**引用它（就是那個靜態建構子），代表沒有任何程式用絕對位址寫它。
#
# 真正的目的地在 `0x15ADD6C`（相對 +0x11ADD6C）：模組內的靜態 `std::string`，
# 使用者在遊戲裡選 iz_dun02 時它就是 `iz_dun02`（size=8、capacity=15）。
# 有 5 處程式碼引用，下面挑兩個互相獨立的骨架。
#
# MSVC 讀 `std::string` 的標準版面（buffer 在 +0、size 在 +0x10、capacity 在 +0x14）：
#
#     83 3d <物件+0x14> 10    cmp dword [capacity], 10h   ← 短字串走內嵌 buffer
#     …
#     b8 <物件>               mov eax, <物件>
#     0f 43 05 <物件>         cmovae eax, [<物件>]        ← capacity>=16 才改用指標
#
# 同一個位址在骨架裡出現兩次（capacity 那個是 +0x14，所以遮掉不當答案），
# 兩個立即值必須相等 —— 那是這條特徵自帶的一致性檢查。
NAVI_GOAL_SIGS = (
    CodeSignature(
        name="navi-goal-read",
        pattern="83 3D ?? ?? ?? ?? 10 53 8B D9 56 57 8B BB F0 00 00 00 "
                "8B 07 8B B0 D8 00 00 00 B8 ?? ?? ?? ?? 0F 43 05 ?? ?? ?? ?? "
                "8B CE 50 FF 15 ?? ?? ?? ??",
        operands=(27, 34),
        why="MSVC 取 std::string 資料指標的標準骨架（cmp capacity,10h → "
            "mov eax,<物件> → cmovae eax,[<物件>]），前面接 mov edi,[ebx+0F0h] / "
            "mov esi,[eax+0D8h]。兩個立即值必須相等。實機 1 處命中，讀出 0x15ADD6C。",
    ),
    CodeSignature(
        name="navi-goal-pass",
        pattern="85 C0 0F 84 ?? ?? ?? ?? 8B 8B E0 00 00 00 E8 ?? ?? ?? ?? 50 "
                "B9 ?? ?? ?? ?? E8 ?? ?? ?? ?? B9 ?? ?? ?? ??",
        operands=(21,),
        why="另一處引用：test eax,eax / jz / mov ecx,[ebx+0E0h] / call / push eax / "
            "mov ecx,<物件>; call。跟上面那條完全獨立，用來互相驗證。"
            "實機 1 處命中，讀出 0x15ADD6C。",
    ),
)

# ---- 「上一次送出去的帳號」-------------------------------------------------
#
# 自動登入用它做**閉環驗證**：按下送出之後，如果這裡不是我們要登入的帳號，
# 就代表字打到別的欄位去了（客戶端記住帳號時焦點會落在密碼欄），下一次先按 Tab。
#
# 以前這裡是寫死的 `SUBMITTED_ACCOUNT_OFFSET = 0x11D2ACC`（GAMEDATA [MEM-032]）——
# 那正是 CLAUDE.md 最高原則禁止的東西。2026-08-26 實測它**碰巧還對**，
# 但同一次改版已經讓角色座標那條固定距離斷掉了（[MEM-039]），這個遲早輪到。
#
# 骨架（ragexe+0x5480B8，實機 1 處命中，讀出 0x15D2ACC）：
#
#     6A 00 6A 00 6A 00 50        push 0 ×3; push eax
#     68 18 27 00 00              push 2718h            ← 訊息／資源編號，很有辨識度
#     8B CE / FF 15 <import>      mov ecx,esi; call [輸入函式]
#     8B CF / FF D6               mov ecx,edi; call esi
#     8B 8B B4 00 00 00           mov ecx,[ebx+0B4h]
#     E8 <rel32> / 50             call; push eax        ← 把剛取到的字串當參數
#     B9 <答案> / E8 <rel32>      mov ecx,<帳號全域>; call  ← 存進去
#     8B 3D <全域>                mov edi,[某全域]
#
# 所有立即值、rel32 與 import 位址一律遮成 ??。
SUBMITTED_ACCOUNT_SIGS = (
    CodeSignature(
        name="submitted-account-store",
        pattern="6A 00 6A 00 6A 00 50 68 18 27 00 00 8B CE FF 15 ?? ?? ?? ?? "
                "8B CF FF D6 8B 8B B4 00 00 00 E8 ?? ?? ?? ?? 50 "
                "B9 ?? ?? ?? ?? E8 ?? ?? ?? ?? 8B 3D ?? ?? ?? ??",
        operands=(37,),
        why="送出登入時把帳號字串存進靜態緩衝的那一段。`push 2718h` 與 "
            "`mov ecx,[ebx+0B4h]` 讓骨架夠獨特。實機 1 處命中，讀出 0x15D2ACC，"
            "內容與使用者當下的帳號一致。",
    ),
)

#: 帳號緩衝區讀幾個 bytes。RO 帳號上限 23，讀 32 再截到 null 綽綽有餘。
SUBMITTED_ACCOUNT_MAX_BYTES = 32

# 角色名的編碼與讀取長度。RO 角色名上限 23 bytes，讀 32 再截到 null 綽綽有餘。
NAME_ENCODING = "cp950"
NAME_MAX_BYTES = 32

# 地圖名是 ASCII 檔名，最長的官方地圖名也不到 24 字元。
MAP_NAME_ENCODING = "ascii"
MAP_NAME_MAX_BYTES = 24

# 經驗值欄位以前一直對不上，是因為當成 int32 讀（-0x10 / -0x08 的猜測），
# 而它們其實是 **int64**、而且順序是「Base經驗, Base門檻, Job門檻, Job經驗」。
# 現在四個都已實測確認，見上面的 base_exp 等欄位與 GAMEDATA [MEM-015]。


# ---------------------------------------------------------------------------
# 身上的狀態（EFST，畫面上那排狀態圖示）
# ---------------------------------------------------------------------------
# 客戶端把「我身上現在有什麼狀態」放在 ragexe 的一個 **static `std::vector`**
# 裡（三個相鄰欄位 begin／end／capacity）。定位的是 `begin` 的位址，
# end／cap 用 vector 自己的結構偏移 +4／+8 取（同一個物件內部的欄位距離，
# 屬於 CLAUDE.md 允許寫死的「結構偏移」——**不是**兩個獨立全域相減）。
#
# 怎麼找到的（2026-08-29，見 GAMEDATA [MEM-051]）：
#   1. 使用者身上只有「雙手劍加速」一個 buff。全行程掃「像到期時間的 dword」
#      （值落在 [now, now+1小時]）只有 4200 個，請使用者**重放一次**技能，
#      再掃一次找「往前跳一大段」的那個 → 0x166A41D4。
#   2. 掃誰指向那塊 → **只有 3 個指標，而且都在 ragexe 的映像裡、彼此相鄰**
#      → 這是個 static vector。(end-begin)/28 = 1 筆，剛好對上「只有一個 buff」。
#   3. 反組譯引用處：`cmp [eax],esi` / `add eax,0x1c` / `cmp eax,ecx` 的走訪迴圈
#      → 每筆 28 bytes、第一個欄位就是 EFST 編號。
#
# 為什麼相信「這就是身上的狀態」而不是別的清單（[MEM-047] 的教訓：
# 特徵找得到 ≠ 找到的是對的東西）—— 三個行程同時讀，內容各自對上角色身分：
#   獵人 → EFST_FALCON(馴鷹術)＋箭矢；騎士 → EFST_TWOHANDQUICKEN 剩 61 秒；
#   商人 → EFST_ON_PUSH_CART(手推車)。
#   而且盯著看到**到期那一秒筆數從 1 變 0**（03:51:54）。
#
# 驗證（2026-08-29）：四條骨架各自獨立，在三個行程裡全部解出同一個位址
#   0x1357D74（ragexe+0xF57D74）；A/B/C 各命中 1 處，D 命中 3 處但值一致。
_STATUS_VEC_LOOP = (
    "A1 ?? ?? ?? ?? 8B 0D ?? ?? ?? ?? 3B C1 74 ?? 39 30 74 ?? 83 C0 1C 3B C1"
)

STATUS_VEC_SIGS = (
    CodeSignature(
        name="status-vec-find",
        pattern=_STATUS_VEC_LOOP,
        operands=(1,),
        why="「身上有沒有這個狀態」查詢函式：載入 begin／end，逐筆比對第一個"
            "欄位，`add eax,0x1c` 前進。0x1c 就是每筆的大小。實機 1 處命中。",
    ),
    CodeSignature(
        name="status-vec-find-const",
        pattern="A1 ?? ?? ?? ?? 8B 0D ?? ?? ?? ?? 3B C8 74 ?? 81 38 ?? ?? ?? ?? 74",
        operands=(1,),
        why="另一處走訪：拿固定的 EFST 編號（`cmp dword ptr [eax],imm32`）比對。"
            "編號本身遮成 ?? —— 它是那個功能的參數，不是我們要的答案。實機 1 處命中。",
    ),
    CodeSignature(
        name="status-vec-walk",
        pattern="8B 0D ?? ?? ?? ?? 8B 3D ?? ?? ?? ?? C7 85 ?? ?? ?? ?? 01 00 00 00 3B CF",
        operands=(2,),
        why="走訪整串的另一個骨架（mov ecx,[begin] / mov edi,[end] / 旗標設 1）。"
            "實機 1 處命中，與前兩條同值。",
    ),
    CodeSignature(
        name="status-vec-edi",
        pattern="8B 3D ?? ?? ?? ?? A1 ?? ?? ?? ?? 3B F8 74 ?? 8B F0",
        operands=(2,),
        why="edi/eax 版的載入順序（mov edi,[begin] / mov eax,[end] / cmp）。"
            "實機 3 處命中，三處讀出的位址完全一致 —— 這條自帶交叉驗證。",
    ),
)


@dataclass(frozen=True)
class StatusVectorOffsets:
    """`std::vector<狀態>` 與每一筆狀態的結構偏移。

    都是**同一個物件內部**的欄位距離（CLAUDE.md 允許寫死的類別），
    出處是反組譯：走訪迴圈的 `add eax,0x1c` 給了每筆大小，
    欄位意義由三個行程的實機內容對照封包 0x0984／0x043F 的版面確認。
    """

    # vector 本體：三個相鄰指標。
    begin: int = 0x00
    end: int = 0x04
    capacity: int = 0x08

    #: 每一筆的大小（走訪迴圈的 `add eax,0x1c`）。
    stride: int = 0x1C
    #: EFST 編號（走訪迴圈比對的就是這個欄位）。
    efst: int = 0x00
    #: 到期時刻，GetTickCount() 同一個時基。實機：重放技能時整個往前跳。
    expire_tick: int = 0x04
    val1: int = 0x08
    val2: int = 0x0C
    val3: int = 0x10
    #: 總時長（毫秒），對應封包 0x0984 的 total。**9999 = 無時限**
    #: （伺服器的永久旗標，實機在馴鷹術／手推車上都是 9999）。
    total_ms: int = 0x14
    #: 掛上當下的剩餘時間（毫秒），對應封包的 remain。剛放的技能與 total 相同。
    remain_ms: int = 0x18


STATUS_VEC_OFFSETS = StatusVectorOffsets()

#: 伺服器用來表示「這個狀態沒有時限」的 total 值（封包與記憶體都是它）。
STATUS_NO_TIME_LIMIT = 9999

#: 合理性上限：一次身上不可能有這麼多狀態。超過就是解錯了，寧可整批不信。
STATUS_MAX_ENTRIES = 128
