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

# ---- 角色座標：兩個相鄰的 uint32 全域（x, y）------------------------------
#
# 為什麼要用程式碼特徵而不是「相對 HP 全域的固定距離」（GAMEDATA [MEM-039]）：
# 那個距離在 2026-08-26 的改版就變了（HP 全域移了 +0x60D8、座標只移了 +0x60B8），
# 舊值指到一片 0，而且 (0,0) 通過了當時的合理性檢查 —— 自動打怪安靜地
# 拿 (0,0) 當起點走去地圖角落。距離型的推導只要兩邊移動幅度不同就會斷，
# 而且**斷了不會有任何徵兆**。
#
# [MEM-006] 當初說「座標前 0x30 bytes 全是 0，做不出有辨識度的樣式」——
# 那是指**資料**樣式。程式碼樣式沒這個問題：座標的寫入端骨架很有特徵。
#
# 骨架（ragexe+0xD7B30，實機 1 處命中）：
#
#     8B 4D 0C              mov  ecx, [ebp+0Ch]
#     39 0D <x>             cmp  [x], ecx          ← 座標沒變就不做事
#     75 0C                 jne  short
#     39 3D <y>             cmp  [y], edi
#     0F 84 <rel32>         je   near
#     A1 <全域>             mov  eax, [某全域]
#     89 0D <x>             mov  [x], ecx          ← 寫入端
#     89 3D <y>             mov  [y], edi
#     85 C0 / 74 <rel8>     test eax, eax; je short
#
# x 與 y 各出現**兩次**，四個立即值彼此就是驗證；而且 y 必須等於 x+4
# （由 `CharacterReader` 交叉檢查）。所有立即值與跳躍位移一律遮成 ??。
_POSITION_SKELETON = (
    "8B 4D 0C 39 0D ?? ?? ?? ?? 75 0C 39 3D ?? ?? ?? ?? "
    "0F 84 ?? ?? ?? ?? A1 ?? ?? ?? ?? 89 0D ?? ?? ?? ?? "
    "89 3D ?? ?? ?? ?? 85 C0 74 ??"
)

#: 角色座標的 x。骨架裡兩個 x 立即值必須一致（locate_global 會檢查）。
POSITION_X_SIGS = (
    CodeSignature(
        name="position-store-x",
        pattern=_POSITION_SKELETON,
        operands=(5, 30),
        why="座標寫入端：cmp [x],ecx …… mov [x],ecx。兩個立即值互相驗證。"
            "實機 1 處命中，讀出 0x122A67C。",
    ),
)

#: 角色座標的 y。同一個骨架的另外兩個立即值。
POSITION_Y_SIGS = (
    CodeSignature(
        name="position-store-y",
        pattern=_POSITION_SKELETON,
        operands=(13, 36),
        why="同上骨架的 cmp [y],edi …… mov [y],edi。實機讀出 0x122A680，"
            "剛好是 x+4 —— 這個關係由讀取端再驗一次。",
    ),
)

#: x 與 y 一定相鄰（兩個 uint32）。定位結果不符就是解錯了，要大聲失敗。
POSITION_XY_GAP = 4

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
