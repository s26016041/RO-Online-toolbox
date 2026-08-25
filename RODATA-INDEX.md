# RODATA 索引 — RO 客戶端解包資料

`RODATA/` 是 RO 客戶端 GRF 解包後的完整資料，**245,178 個檔案、約 18 GB**。
已加入 `.gitignore`，不進版控。

這份文件記錄「每個東西是什麼」，做功能時先查這裡再動手。
所有內容都是實際讀檔驗證出來的，不是照 RO 通例猜的。

> 依專案 [CLAUDE.md](CLAUDE.md) 的鐵則：這裡的資料是**第三順位**。
> 動手抄之前，先確認遊戲有沒有把那份資料載進記憶體。

---

## ⚠ 讀取前必知（踩過的坑）

### 1. 編碼不統一，同一個目錄裡混著兩種

| 表的種類 | 編碼 | 例子 |
|---|---|---|
| **顯示名稱／描述**（給玩家看的中文） | `cp950`（Big5） | `idnum2itemdisplaynametable.txt` → `1101#長劍#` |
| **資源檔名**（貼圖／音效檔名） | `euc-kr`（韓文原始） | `idnum2itemresnametable.txt` → `2101#가드#` |

拿 cp950 去讀資源名表會得到「陛萄」這種亂碼 —— 那其實是韓文 `가드`(Guard)。
**38 個 txt 裡有 16 個含非 cp950 位元組**，幾乎都是資源檔名類的表。

實務建議：顯示名用 `cp950`，資源名用 `euc-kr`；不確定就兩種都試，
並一律加 `errors="replace"`，不要假設檔案乾淨。

> 附帶一提：判斷編碼**不要只取檔案開頭幾十個 bytes** 去 decode ——
> 很可能剛好切在多位元組字元中間而誤判成「這個編碼不行」。要整檔試。

### 2. `monsterskillinfo.xml` 被字母鏡射混淆過

檔頭長這樣：`<?cno evihrlm="1.0" vmxlwrmt="vfx-pi"?>`

這是 Atbash（a↔z、b↔y…，大小寫各自鏡射，非字母不動）。還原後就是正常 XML：

```python
def unmirror(data: bytes) -> bytes:
    out = bytearray()
    for ch in data:
        if 97 <= ch <= 122:    # a-z
            out.append(ord('a') + ord('z') - ch)
        elif 65 <= ch <= 90:   # A-Z
            out.append(ord('A') + ord('Z') - ch)
        else:
            out.append(ch)
    return bytes(out)
```

還原結果：`<?xml version="1.0" encoding="euc-kr"?>` + `<Monster_Action_T...`。
其他 xml（`monster_talk_table.xml`、`pettalktable.xml`、`clientinfo.xml`）**沒有**混淆。

### 3. `msgstringtable.csv` 是 Base64，而且比 txt 版好用

每行 `base64(key),base64(UTF-8 值)`：

```
TVNJX0RPX1lPVV9BR1JFRQ==,6KuL5ZWP5piv5ZCm5ZCM5oSP77yf
→ MSI_DO_YOU_AGREE , 請問是否同意？
```

`msgstringtable.txt` 只有值沒有 key（靠行號對應），**csv 版有 key 名稱又是 UTF-8**，
要對照系統訊息優先用 csv。

### 4. `.lub` 是編譯過的 Lua bytecode，不是文字

檔頭 `1B 4C 75 61 51` = `\x1bLuaQ` → **Lua 5.1 bytecode**。
466 個 `.lub` 全部是二進位，直接讀會是亂碼。要用反編譯器（unluac / luadec）
或用 Lua 5.1 直譯器載入後再輸出。純文字的只有一個：
`luafiles514/lua files/effecttool/effecttoolutil.lua`。

---

## 目錄總覽

| 目錄 | 檔數 | 大小 | 內容 |
|---|---:|---:|---|
| `sprite/` | 155,617 | 7.6 GB | 角色／怪物／道具的動畫與圖（`.act` + `.spr`） |
| `texture/` | 68,562 | 4.4 GB | 貼圖（`.bmp` / `.tga`），含地圖材質與 UI 圖 |
| `model/` | 9,058 | 117 MB | 3D 模型（`.rsm` / `.rsm2`），地圖上的建築物件 |
| `palette/` | 4,484 | 4.4 MB | 調色盤（`.pal`），角色染色／職業配色 |
| `wav/` | 3,342 | 199 MB | 音效 |
| **`(data 根目錄)`** | 3,279 | 5.7 GB | **地圖檔 + 38 個資料表**（見下） |
| `luafiles514/` | 467 | 46 MB | Lua 資料（技能、任務、導航…），幾乎全是 bytecode |
| `imf/` | 317 | 3.3 MB | 角色動作組合資訊 |
| `book/` | 48 | 0.2 MB | 遊戲內書籍文字 |
| `contentdata/` | 3 | 0.1 MB | — |
| `simplemsg/` | 1 | — | — |

### 檔案格式對照

| 副檔名 | 數量 | 是什麼 |
|---|---:|---|
| `.act` | 115,192 | 動畫定義（每格用哪張 spr、位移、延遲） |
| `.bmp` | 54,792 | 貼圖 |
| `.spr` | 40,122 | 精靈圖（搭配 `.act` 與 `.pal`） |
| `.tga` | 9,664 | 貼圖（含 alpha） |
| `.rsm` / `.rsm2` | 9,027 | 3D 模型 |
| `.pal` | 4,484 | 調色盤 |
| `.wav` | 3,340 | 音效 |
| `.str` | 3,309 | 特效定義 |
| `.gat` | 1,082 | **地形屬性**（每格能不能走、能不能射擊）← 自動走路要用 |
| `.rsw` | 1,081 | 地圖世界檔（光源、物件擺放、水面） |
| `.gnd` | 1,072 | 地面網格（高度、材質） |
| `.lub` | 466 | Lua bytecode |

---

## 已經做好的抽取工具

物品表已經抽成可查詢的檔案，不用每次自己解析：

```powershell
.venv/Scripts/python.exe tools/build_item_table.py   # 重新抽（改版後跑）
.venv/Scripts/python.exe tools/find_item.py 501       # 查 ID
.venv/Scripts/python.exe tools/find_item.py 藥水      # 查名稱
.venv/Scripts/python.exe tools/find_item.py 盾 --equip  # 只列裝備
```

產物 `assets/items.json.gz`（7,911 筆）欄位：
`name` 名稱、`desc` 說明、`res` 資源檔名、`en` 英文代號、
`slots` 插槽數、`equip` 是否裝備、`equip_mask` / `equip_at` 裝備部位。

裝備部位 bitmask 的推導與限制見 GAMEDATA [DAT-007]。

怪物表同樣已抽好：

```powershell
.venv/Scripts/python.exe tools/build_mob_table.py       # 重新抽（改版後跑）
.venv/Scripts/python.exe tools/find_mob.py 1080          # 查 class ID
.venv/Scripts/python.exe tools/find_mob.py 草            # 查名稱
.venv/Scripts/python.exe tools/find_mob.py --map moc_fild01 -v   # 查某張圖出什麼
.venv/Scripts/python.exe tools/find_mob.py --plants      # 只列草
```

產物 `assets/mobs.json.gz`（4,644 隻）欄位：
`name` 中文名、`en` JT 代號、`res` sprite 資源名、`level` 等級、
`race` 種族、`size` 體型、`ele`/`ele_lv` 屬性、`boss` 是否 MVP、
`maps` 出沒地圖與數量、`kind` 分類（`plant` / `plant?` / `mob` / `null`）。

來源與欄位推導見 GAMEDATA [DAT-015]（.lub 解析）、[DAT-016]（欄位意義）、
[DAT-017]（為什麼草只能靠名稱＋等級判）。

## data 根目錄的 38 個資料表

行數為扣掉註解與空行後的實際內容行數。格式除註明外都是 `欄位#欄位#`（`#` 分隔且結尾），CRLF 換行。

### 物品

| 檔案 | 行數 | 內容 | 編碼 |
|---|---:|---|---|
| `idnum2itemdisplaynametable.txt` | 7,927 | 物品 ID → 顯示名（`1101#長劍#`） | cp950 |
| `idnum2itemdesctable.txt` | 55,468 | 物品 ID → 說明文（多行，含 `^RRGGBB` 顏色碼） | cp950 |
| `idnum2itemresnametable.txt` | 16,166 | 物品 ID → 資源檔名（`2101#가드#`） | **euc-kr** |
| `num2itemdisplaynametable.txt` | 8,054 | 同上但用另一組編號 | cp950 |
| `num2itemdesctable.txt` | 37,653 | 同上但用另一組編號 | cp950 |
| `num2itemresnametable.txt` | 16,221 | 同上但用另一組編號 | **euc-kr** |
| `itemslotcounttable.txt` | 2,128 | 物品 ID → 插槽數（`1101#3#`） | cp950 |
| `itemslottable.txt` | 1,800 | 物品插槽資料 | cp950 |
| `itemparamtable.txt` | 1,800 | 物品參數 | cp950 |
| `itemmoveinfov5.txt` | 7,661 | 物品可否交易／倉庫／掉落等旗標，**Tab 分隔**，行尾 `//` 註解為英文名 | 混合 |
| `bookitemnametable.txt` | 45 | 書籍類物品 | 混合 |
| `buyingstoreitemlist.txt` | 3,042 | 可用收購商店買賣的物品 ID | 混合 |
| `metalprocessitemlist.txt` | 291 | 精煉／加工素材清單 | cp950 |
| `metalprocessitemtable.txt` | 15 | 精煉素材對應表 | cp950 |

### 卡片

| 檔案 | 行數 | 內容 | 編碼 |
|---|---:|---|---|
| `cardprefixnametable.txt` | 1,852 | 卡片 ID → 裝備名前綴（`4001#幸運的#`） | cp950（有造字區位元組） |
| `cardpostfixnametable.txt` | 352 | 卡片 ID → 後綴 | 混合 |
| `carditemnametable.txt` | 148 | 卡片物品名 | 混合 |
| `num2cardillustnametable.txt` | 1,415 | 卡片 ID → 卡圖檔名 | **euc-kr** |

### 地圖

| 檔案 | 行數 | 內容 | 編碼 |
|---|---:|---|---|
| `mapnametable.txt` | 1,251 | 地圖檔名 → 中文名（`dicastes01.rsw#邪派國都艾爾迪卡斯特#`） | cp950 |
| `mapinfotable.scp` | — | 地圖資訊（`//` 註解開頭） | cp950 |
| `mappostable.txt` | 139 | 地圖座標對應（`0#hugel.rsw#871#0#927#57#`） | 混合 |
| `mp3nametable.txt` | 865 | 地圖 → 背景音樂（`alberta.rsw#bgm\54.mp3#`） | 混合 |
| `indoorrswtable.txt` | 147 | 室內地圖清單 | 混合 |
| `exceptionminimapnametable.txt` | 145 | 沒有小地圖的地圖（多為副本 `1@air1`） | cp950 |
| `mapobjlighttable.txt` | 309 | 地圖物件光源開關 | cp950 |
| `fogparametertable.txt` | 1,515 | 地圖霧氣參數 | 混合 |
| `viewpointtable.txt` | 25 | 視角參數 | cp950 |
| `resnametable.txt` | 3,429 | 資源檔名替換表（`prt_fild08d.gnd#prt_fild08.gnd#`） | 混合 |

### 任務／技能／訊息

| 檔案 | 行數 | 內容 | 編碼 |
|---|---:|---|---|
| `questid2display.txt` | 16,859 | 任務 ID → 名稱／圖示／描述（`1000#轉生#SG_FEEL#QUE_NOIMAGE#` 後接描述行） | cp950 |
| `leveluseskillspamount.txt` | 2,805 | 技能各等級 SP 消耗（`SM_BASH#` 後接數值行） | cp950 |
| `msgstringtable.txt` | 4,219 | 系統訊息（只有值，靠行號對應） | cp950 |
| `msgstringtable.csv` | — | **同上但有 key，Base64+UTF-8**，優先用這份 | base64/utf-8 |
| `monsterskillinfo.xml` | 20 KB | 怪物技能 —— **字母鏡射混淆**，還原後是 euc-kr XML | 見上 |
| `monster_talk_table.xml` | 123 KB | 怪物對話 | euc-kr |
| `pettalktable.xml` | 573 KB | 寵物對話 | BIG5 |
| `clientinfo.xml` | <1 KB | 客戶端連線設定（伺服器位址等） | euc-kr |

### 其他

| 檔案 | 行數 | 內容 |
|---|---:|---|
| `etcinfo.txt` | 20 | 雜項設定（天氣等） |
| `guildtip.txt` | 45 | 公會系統提示 |
| `tipofday.txt` | 158 | 每日小提示（含指令說明，如 `/savechat`） |
| `manner.txt` | 74 | 不雅字詞過濾表 |
| `ba_frostjoke.txt` | 118 | 吟遊詩人「冷笑話」技能台詞 |
| `dc_scream.txt` | 117 | 舞孃「尖叫」技能台詞 |
| `eventidnum2itemresnametable.txt` | 22 | 活動物品資源名 |
| `eventnum2itemresnametable.txt` | 22 | 同上（另一組編號） |

---

## luafiles514

466 個 `.lub`（Lua 5.1 bytecode）+ 1 個 `.lua`。主要子目錄：

| 子目錄 | 內容 |
|---|---|
| `skillinfoz/` | 技能資料。`skillinfolist.lub`(569 KB)、`skilldescript.lub`(867 KB) |
| `navigation/` | **導航資料** —— NPC 位置、地圖連結、距離表。`navi_npc_tw.lub`(974 KB)、`navi_link_tw.lub`(580 KB)、`navi_npcdistance_tw.lub`(3.6 MB) |
| `worldviewdata/` | 世界地圖檢視用，與 navigation 部分重複 |
| `quest/` | 任務資料，分 `epquest/` 與 `localquest/`，各有 `questinfo/` |
| `equipmentproperties/` | **最大的檔** `equipmentproperties.lub`(9.1 MB)，裝備屬性 |
| `enchant/` | `enchantlist.lub`(2.9 MB)，附魔清單 |
| `itemreform/` | `itemreformsystem.lub`(463 KB)，物品改造 |
| `datainfo/`、`optioninfo/`、`emotion/`、`dressroom/` 等 | 各系統的設定表 |
| `service_taiwan/` | 台版專屬設定 |

`navigation/` 對「自動走路」特別有價值：有 NPC 座標與地圖之間的連結關係。

---

## 待確認

這些還沒驗證，用之前要先查：

- [x] `.lub` **不用外部反編譯器**：自己解 Lua 5.1 bytecode 就夠，見 `tools/lub_parse.py` 與 GAMEDATA [DAT-015]。
- [ ] `idnum2*` 與 `num2*` 兩組編號的差別（推測是新舊編號體系，**尚未驗證**）。
- [ ] `.gat` 的格式細節（自動走路要解析每格的可走屬性）。
- [ ] 這份解包資料是對應哪一版客戶端 —— 改版後要重新解包比對。
