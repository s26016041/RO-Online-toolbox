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

from ro_toolbox.services.aob import AOBSignature

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
    # 角色座標：兩個連續的 uint32（x, y），格座標。
    # 位在同模組另一處全域（Ragexe.exe+0xE245C4），與 HP 全域
    # （Ragexe.exe+0x11D1BC0）的距離固定 0x3AD5FC，三個行程皆同。
    #
    # 為什麼不另外做一條 AOB 特徵：座標前 0x30 bytes 全是 0，
    # 做不出有辨識度的樣式。改用「相對已驗證特徵的固定距離」——
    # 這與 base_level 等欄位同性質，屬 CLAUDE.md 允許寫死的結構偏移，
    # 風險等級一樣是「大更新才會壞」。出處見 GAMEDATA [MEM-006]。
    position: int = -0x3AD5FC


STATUS_OFFSETS = StatusOffsets()

# 角色名的編碼與讀取長度。RO 角色名上限 23 bytes，讀 32 再截到 null 綽綽有餘。
NAME_ENCODING = "cp950"
NAME_MAX_BYTES = 32

# 地圖名是 ASCII 檔名，最長的官方地圖名也不到 24 字元。
MAP_NAME_ENCODING = "ascii"
MAP_NAME_MAX_BYTES = 24

# 經驗值欄位以前一直對不上，是因為當成 int32 讀（-0x10 / -0x08 的猜測），
# 而它們其實是 **int64**、而且順序是「Base經驗, Base門檻, Job門檻, Job經驗」。
# 現在四個都已實測確認，見上面的 base_exp 等欄位與 GAMEDATA [MEM-015]。
