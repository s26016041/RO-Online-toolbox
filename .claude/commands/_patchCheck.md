# _patchCheck（改版體檢）— RO 更新後，自動找出哪裡壞了並修好

RO 官方改版之後跑這一支。使用者不必再解釋一次背景，照下面做。

## ⛔ 動手前先記住兩件 RO 專屬的事

1. **GameGuard：只讀不寫。** 所有診斷腳本一律唯讀記憶體。
   不准注入、不准寫記憶體、不准改 IAT —— 會當機或封號（[PKT-011]、[PKT-013]）。
   要送動作只能走**封包**（複製遊戲 socket，[PKT-012]）。
2. **記憶體讀得到 ≠ 有連線。** 斷線之後背包與角色狀態照樣讀得出來，
   那是上次登入的殘留（[MEM-029]）。判斷有沒有登入一律用
   `ro_capture.find_server(pid)`，不要看記憶體有沒有值。

## 五層，全綠才算過

| 層 | 工具 | 驗什麼 | 要不要登入 |
|---|---|---|---|
| ① 位址 | **`tools/verify_sigs.py`** | AOB 特徵唯一性、改版模擬 | 不用（開著登入畫面就夠；有快照連開都不用） |
| ② opcode | **`tools/watch_packets.py`**（半自動） | 我們**送**的封包編號有沒有換 | 要 |
| ③ 結構偏移 | **`tools/show_status.py`**（人工對畫面） | 物件版面有沒有搬家 | 要 |
| ④ 查表 | `find_item` / `find_mob` ＋ 背包交叉比對 | 解包資料對不對得上 | 要 |
| ⑤ 動作 | **`tools/farm_test.py`** | 真的走一步／打一隻／喝一瓶 | 要，且有副作用 |

⚠⚠ **①②③④ 全綠 ≠ 功能正常。** 靜態檢查看不到「語意變了」這種壞法
（欄位還在、位址還對，但意思換了）。最後一定要走第 ⑤ 層真的動一次，
或請使用者掛五分鐘回報。

---

## 0. 先確認真的改版了，以及**改了哪一半**

```powershell
Get-Item "D:\ro\RagnarokOnline\Ragexe.exe","D:\ro\RagnarokOnline\data.grf",
         "D:\ro\RagnarokOnline\data0.grf" | Select Name,LastWriteTime,Length
Get-Content "D:\ro\RagnarokOnline\patch3.txt" -Tail 5
```

| 變的是 | 影響 | 要跑哪幾層 |
|---|---|---|
| `Ragexe.exe` | 程式碼重編：特徵、opcode、結構偏移全都可能動 | ①②③⑤ |
| `data.grf` / `data0.grf` | 資源／資料表換了 | ④（要重新解包 RODATA 再重建 assets） |
| 只有 `patch3.txt` | 通常只是補丁清單 | 先跑 ①，沒事就收工 |

⚠ 兩邊都可能同時變 —— 不要看到 exe 沒動就跳過 ④。

## 1. ① 位址層：跑體檢並**存一份快照**

遊戲開著（登入畫面就夠）：

```powershell
.\.venv\Scripts\python.exe tools\verify_sigs.py --save-snapshot reports\ragexe.snap
```

`--save-snapshot` 會把程式碼區段存下來，**之後遊戲關了也能離線重跑**：

```powershell
.\.venv\Scripts\python.exe tools\verify_sigs.py --snapshot reports\ragexe.snap
```

這很重要：診斷常常要來回好幾趟，不要每次都叫使用者開遊戲
（而且 RO 常常在維修）。主控台只印結論，明細在 `reports/verify_sigs-<時間>.md`。

✅ 2026-08-26 已對真的客戶端跑過：27/28 通過（1 項因前提不成立略過）。
live 層現在會檢查角色狀態（驗證後唯一）、**角色座標（x 與 y 必須相鄰）**、
**導航目標全域**、背包容器、封包長度表。

## 2. 讀懂它的三種判定

| 判定 | 意思 | 要做什麼 |
|---|---|---|
| `OK` | 這一項過了 | — |
| `NG` | 不合格 | 一定要修，見下 |
| `--` | 前提不成立而**沒跑到** | 不算過。看它說的理由，補齊前提再跑 |

### NG 的兩類，優先序不同

1. **「該失敗卻回了位址」** —— 最優先。這代表定位器會**安靜地做錯事**：
   骨架已經不對了，它還是回一個看起來合理的位址。
   （例：`魔術乘數改掉 -> 失敗` 這項 NG。）
2. **唯一性 NG（命中多個 / 領先不夠）** —— 特徵不夠獨特，改版隨時會撞到別人。
   往後延伸指令骨架，或改用更穩的錨。

★ **「位移」不是壞掉。** 容器位址、函式位址每次重開都會變，
只要定位器答對就是正常 —— 那正是 AOB 存在的理由。

⛔ **硬規則：不准把要解出來的答案寫進特徵當錨（tautology）。**
模組內的 4-byte 立即值、rel32 一律遮成 `??`，靠指令骨架當錨。
拿答案比對答案的特徵，`.data` 一位移就整段失敗。

### 特徵一律集中在 `services/signatures.py`

| 位置 | 錨 | 出處 |
|---|---|---|
| `CHAR_STATUS` | HP 前方固定欄位的位元組樣式（**資料**樣式） | [MEM-003] |
| `POSITION_X_SIGS` / `POSITION_Y_SIGS` | 座標寫入端 `cmp [x],ecx … mov [x],ecx`，x/y 各兩個立即值互驗，且 `y-x==4` | [MEM-039] |
| `NAVI_DEST_SIGS` | 遊戲尋路目標地圖：CRT 靜態建構鏈（標記值序列 05→01→00→-1） | [MEM-040] |
| `SUBMITTED_ACCOUNT_SIGS` | 送出登入時存帳號那段（`push 2718h` + `mov ecx,[ebx+0B4h]`） | [MEM-032] |
| `SELECT_CURSOR_SIGS` / `SELECT_NAME_SIGS` | 選角畫面的游標與名字 | [MEM-031] |
| `services/bag.py` | `sub ecx,5` + 除以 34 的魔術乘數 → `call` → `mov ecx, imm32` | [MEM-028] |
| `services/packet_table.py` | `mov ecx,esi; call rel32` 最多人呼叫的那個 | [MEM-024] |

### ⚠ 「命中多個」不一定是特徵壞了

兩種原因，處理方式**相反** —— 先把候選攤開來看內容再決定（[MEM-041]）：

- **特徵不夠精確** → 改特徵。
- **環境裡有長得像的雜訊** → 加**驗證**，別動特徵。
  實測：角色特徵命中 6 個，其中 5 個是堆積垃圾（HP 15／maxHP 42 億／名字空白）。
  `verify_sigs` 現在檢查的是「**驗完之後只剩一個**」，不是原始 AOB 唯一。

## 3. ② opcode 層 —— RO 最會咬人的一層

**為什麼是 RO 專屬的重點**：長度表是 AOB 從程式碼抽的，改版會自動跟上
（[MEM-024]）。但我們**主動送出去**的 opcode 是寫死在 `core/ro_protocol.py`
的常數 —— 編號換了不會有任何錯誤：封包送得出去、伺服器直接不理，
症狀是「角色安靜地不動作」，而且離現場很遠。

### 寫死的 opcode 清單（改版後全部要重驗）

送出（`core/ro_protocol.py`）：

| 常數 | 值 | 用途 |
|---|---|---|
| `CZ_REQUEST_MOVE` | 0x035F | 走路 |
| `CZ_REQUEST_ACT` | 0x0437 | 攻擊／坐站（`ACT_ATTACK_CONT = 0x07` 連續攻擊） |
| `CZ_REQNAME` | 0x0368 | 點怪查詢 |
| `CZ_ITEM_PICKUP` | 0x0362 | 撿東西 |
| `CZ_USE_ITEM` | 0x00A7 | 喝水 |
| `CZ_ITEM_THROW` | 0x0363 | 丟東西 |
| `CZ_CHANGE_DIR` / `CZ_REQUEST_TIME` | 0x0361 / 0x0360 | 轉向／心跳 |

接收（`services/world.py`、`farm_bot.py`、`potion.py`）：

| 常數 | 值 | 用途 |
|---|---|---|
| `OP_ENTITY_STAND` / `OP_ENTITY_MOVE` | 0x09FF / 0x09FD | 實體進視野／移動（[PKT-029]） |
| `OP_VANISH` | 0x0080 | 實體消失，`[4]` 是 type（1=死）（[PKT-021]） |
| `OP_ITEM_DROP` | 0x0ADD | 掉落（座標在 `[13:15]`/`[15:17]`，[PKT-031]） |
| `OP_MOB_HP` / `OP_DAMAGE` | 0x0A36 / (0x08C8, 0x02E1) | 怪血／傷害（[PKT-025]、[PKT-027]） |
| `_OP_MOVE_ACK` | 0x0087 | 伺服器確認我要移動 |
| `_OP_USE_ACK` | 0x01C8 | 使用道具回應（[PKT-036]） |

### 怎麼驗：讓遊戲自己告訴你新編號

```powershell
.\.venv\Scripts\python.exe tools\watch_packets.py <pid>
```

跑起來後在遊戲裡**手動**做一個動作（走一步、點怪、撿東西、喝一瓶），
看客戶端實際送出什麼。這是最可靠的來源 —— 不必猜、不必查外站。

⚠ **不准從編號規律推**（0x035F 旁邊那個「應該是」某某）。
RO 的 opcode 不照系列連號，猜錯就是「很有自信的錯」（CLAUDE.md 硬規則）。
⚠ 切包一定要有長度表，否則**黏在同一個 TCP 分段後面的封包全部看不到**
（[PKT-043]）——「送了沒回應」有很大機率其實是回了但被切包吃掉。

## 4. ③ 結構偏移層 —— 壞了完全沒有錯誤訊息

症狀是特徵好好的、功能卻不對。官方在物件裡加減成員時，
舊偏移**照樣讀得到**（讀到別的成員），數值看起來也像那麼一回事。

### 寫死的偏移清單（都是 CLAUDE.md 允許寫死的類別，但改版要重驗）

| 在哪 | 偏移 | 出處 |
|---|---|---|
| `signatures.STATUS_OFFSETS` | HP/SP `+0x00`~`+0x0C`、Base `-0x3B58`、AID `-0x3BA4`、名字 `+0x2800`、地圖 `-0x3B9C`、經驗四個 int64 | [MEM-003]～[MEM-005]、[MEM-015]、[MEM-017] |
| `services/bag.py` | 節點 `+0x0C` 格號、`+0x18` 數量、`+0x34` 編號字串 | [MEM-028] |
| `services/entities.py` | class `GID-0x4`、座標 float `GID+0x120`/`+0x124`、存活 `GID-0x24`、繪圖指標 `GID+0x110` | [MEM-014]、[MEM-016] |

### 驗法：用**不變量**，不是「有回值就算過」

```powershell
.\.venv\Scripts\python.exe tools\show_status.py
```

每一項都要有能分辨對錯的證據：

- HP/SP/等級/經驗 → **跟遊戲畫面對得上**（經驗百分比是無條件捨去到小數一位）。
- 角色名 → cp950 解得出來且等於畫面上的名字。
- 地圖名 → ASCII 檔名，且 `mapdata.available_maps()` 裡有這張。
- 座標 → 走一步之後**真的變**（換地圖後會停在上一張圖的舊值，[MEM-022]，
  不能拿剛換圖那一瞬間的值當證據）。
- 背包 → `bag.read_bag()` 的格號／數量要跟遊戲背包視窗一格一格對得上。

⛔ **不准拿某個欄位的位移量去推別的欄位。** 同一個物件裡不同成員的位移量
可以不一樣（姊妹專案 Angels 實際踩過：同一次改版 −0x60/−0x64/−0x68 三種都有）。
一個一個驗。

## 5. ④ 查表層：解包資料要不要重做

`assets/` 底下那幾份都是從 `RODATA/` 抽出來的（來源是客戶端自己的 Lua／地形
資料，不是外站抄的）。**它們是使用者電腦上的唯一來源** —— 別人的機器沒有
`RODATA/`，漏更新就等於那個功能在所有人手上都是舊的
（CLAUDE.md「資料檔也一樣：不准依賴只有開發機有的東西」）。

**先別急著叫使用者重新解包** —— `data.grf` 換了不代表表變了。先對帳：

| 項目 | 過期後果 | 怎麼對帳 |
|---|---|---|
| `items.json.gz` | 選單顯示不出名字／分不出 HP・SP 藥 | `bag.read_bag()` 讀出來的每個 item_id 都要 `find_item.py` 查得到 |
| `mobs.json.gz` | 草／MVP 過濾失效（會去打草或送死） | 場上看到的 class ID 都要 `find_mob.py` 查得到 |
| `warps.json.gz` | 傳點走錯地方、跨圖尋路繞遠路或走不到 | 抽查目前地圖的傳點；`travel.plan_route('prontera','payon')` 算得出來 |
| `mapnames.json.gz` | 自動尋路只顯示得出 `prt_fild08` 這種內部名 | `gamedata.map_display_name('prontera')` 有中文 |
| **`terrain.bin.gz`** | ⛔ **走路類功能全滅**：自動打怪不漫遊、自動尋路直接停用 | `mapdata.available_maps()` 要 **> 900**；新地圖用 `mapdata.has_terrain('<新圖>')` 抽查 |
| `icons.bin` | 道具圖示全空白（外觀降級，功能不受影響） | `icons.available()` 是 True，且 `icons.icon_bytes(501)` 拿得到 BMP |

對不上才請使用者重新解包，然後重建：

```powershell
.\.venv\Scripts\python.exe tools\build_item_table.py
.\.venv\Scripts\python.exe tools\build_mob_table.py
.\.venv\Scripts\python.exe tools\build_warp_table.py
.\.venv\Scripts\python.exe tools\build_map_names.py
.\.venv\Scripts\python.exe tools\build_terrain.py    # 新增地圖一定要重跑
.\.venv\Scripts\python.exe tools\build_icons.py      # 新增道具時
```

⚠ **改版新增地圖時 `build_terrain.py` 一定要重跑**，否則走到新圖就會
「讀不到地形」而停用。`available_maps()` 是最快的體檢：突然變小或變 0 就是有問題
（曾因重新解包後路徑改變而歸零）。
⚠ 重建腳本一律**從資源檔自動抽**，不准手打、不准從編號規律推。
查不到就留空（安全退化），不准填一個猜的值。
⚠ RODATA 的編碼不統一：顯示名 cp950、資源檔名 euc-kr（[DAT-001]）。
⚠ 客戶端**沒有**回血量／效果數值表，已窮舉，別再找（[DAT-021]）。

## 6. ⑤ 動作層 —— 靜態工具的極限，一定要補這一刀

⚠ **會動到使用者的角色，要先有授權。**

每一項都要有**硬證據**，「送得出去」不算：

| 功能 | 送什麼 | 唯一算數的證據 |
|---|---|---|
| 走路 | `0x035F` | 收到 `0x0087`，且記憶體座標真的變（單次上限 17 格，[PKT-030]） |
| 攻擊 | `0x0368` → `0x035F` → `0x0437`(act=7) | 收到傷害封包 `0x08C8`/`0x02E1` 或 `0x0A36`（[PKT-035]） |
| 撿東西 | `0x0362` | `bag.read_bag()` 現查，數量真的 +1 |
| 喝水 | `0x00A7` | 收到 `0x01C8`，且**回包裡的道具編號等於預期的那個** |
| 死亡判定 | — | `0x0080` 的 type 位元組 = 1（不准用等待時間，[PKT-021]） |

一次跑完整輪：

```powershell
.\.venv\Scripts\python.exe tools\farm_test.py <pid> --seconds 70
```

會印走了幾格、卡住幾秒、擊殺／撿取幾個、經驗每小時；軌跡在
`reports/farm_test_<pid>.json`。跟改版前的數字比，差太多就是有東西壞了。

⚠ 攻擊送出後**不准再送移動**（會取消連續攻擊，[PKT-034]）。
⚠ 最省事也最可靠：位址層修好之後**請使用者自己掛五分鐘**回報。
他的回報（「站著不動」「不出手」）比任何自動檢查都準。

## 7. 收尾

- **修好的東西一律進 `GAMEDATA.md`，含走不通的路。**
  新特徵要記成 `MEM` 條目，附**怎麼生成的**與**怎麼驗證唯一性**。
  舊條目失效**改狀態不刪除**，標 `已失效（被 XXX-000 取代）`。
- 這次改版動了什麼，順手更新 `GAMEDATA.md` 開頭的「速查」那一節。
- **重建過的 `assets/*.gz` 一定要一起 commit**（`terrain.bin.gz` 約 1.5 MB）。
  只改程式碼不帶資產＝使用者拿到的還是舊資料，而且完全看不出來。
- commit 訊息寫清楚「哪一段、為什麼壞、怎麼修」—— 改版紀錄很值錢。
- memory 開一份 `patch-YYYY-MM-DD` 記這次的經過。
- 要發版走 `/_pushReleases`。
