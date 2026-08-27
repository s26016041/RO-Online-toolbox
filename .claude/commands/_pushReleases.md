# _pushReleases — push 並發布最新版 Release

把目前的變更 push 上 GitHub，並確保 GitHub Releases 的最新版跟上。
使用者打這個指令＝已授權建 Release，不必再另外確認。

## 通用流程（任何專案都照這個順序判斷）

1. **盤點現況**（三件事並行查）：
   - 本地版號：找專案的版號來源（`VERSION` 檔、`package.json`、
     `pyproject.toml`、`Cargo.toml`……以專案實際用的為準）。
   - GitHub 最新 Release：`gh release list --limit 5`。
   - `git status` / `git log origin/main..HEAD`：有沒有未 commit、未 push 的東西。
2. **判斷版號要不要改**：
   - 本地版號 ≤ GitHub 最新 Release，且這次有新變更 → **要 bump**
     （沒特別說就 patch +1）。
   - 本地版號已經比 Release 新（上次改了沒發）→ 不用再改，直接用它。
   - 版號變更要 commit（訊息慣例：`chore: 版號 X.Y.Z`）。
3. **驗證**：跑專案的測試／lint。**沒過就停下來回報，不准 push 也不准發 Release。**
4. **先 Push**：`git push origin main`，並確認工作區乾淨、與 origin 同步。
   ⚠ **打包 exe 之前就要推完。** 編一次要一分半，中途發現要改東西的話，
   已經編出來的那顆就跟 repo 對不起來了 —— 發出去的版本要能對回一個 commit。
5. **建置**：編 exe 並跑冒煙測試。**編出來的東西沒過就停，不要發。**
6. **建 Release**：`gh release create v<版號> --title v<版號> --notes <重點變更>`，
   有建置產物就一併上傳。
7. **收尾回報**：Release 網址＋這版包含哪些 commit。

## 本專案（RO-Online-toolbox）的具體指令

remote：`git@github.com:s26016041/RO-Online-toolbox.git`

```
1. 版號改三處（見下方警告）→ git commit -m "chore: 版號 X.Y.Z"
2. .\.venv\Scripts\python.exe -m pytest -q                          # 必須全過
3. .\.venv\Scripts\python.exe -m ruff check src tests tools main.py # 必須全過
4. git push origin main            # ⚠ 一定要在編 exe 之前，且要推乾淨
5. git status --short && git status -sb | head -1   # 確認乾淨且同步再往下
6. .\.venv\Scripts\python.exe release.py                            # 編 exe → 冒煙 → 發布
```

**第 6 步不要自己下 `gh release create`。** `release.py` 會：讀 `VERSION` 決定 tag →
檢查 origin 有沒有這個 tag（發過就不重複發）→ 用 `RO-Online-toolbox.spec` 編 exe →
**跑 `exe --selftest`** → 把 Release 標在 `origin/main` 上並上傳 exe。
少了冒煙那一步，資料檔漏收的 exe 會安靜地發出去（見下方）。

### ⚠ 版號有三處，必須同步

| 檔案 | 位置 | 誰在讀 |
|---|---|---|
| `pyproject.toml` | `version = "X.Y.Z"` | 封裝 |
| `src/ro_toolbox/__init__.py` | `__version__ = "X.Y.Z"` | 視窗標題 |
| `VERSION` | 純文字 `X.Y.Z`（無結尾換行） | 發版流程（人／腳本）；程式本身不讀 |

只改一處會讓視窗標題與封裝版號對不起來。改完用這行確認三邊一致：

```bash
{ grep -m1 '^version = ' pyproject.toml
  grep -m1 '__version__' src/ro_toolbox/__init__.py
  cat VERSION; } | grep -o '[0-9]\+\.[0-9]\+\.[0-9]\+' | sort -u
```

只印出**一行**才算對。⚠ 不要整檔 grep `pyproject.toml` ——
相依套件的版號（`0.9.2`、`2023.2.7`…）會一起被撈進來，看起來像三處不同步。

`tests/test_version.py` 會把這件事釘住（三處一致、格式是 X.Y.Z、
`VERSION` 結尾不能有換行 —— 它會被拼進標籤名）。所以只要 pytest 全過，
版號就不可能不同步；**改版號只改一處的話測試會紅**。

## 注意事項（本專案踩過的坑）

- **一律用 `.\.venv\Scripts\python.exe`，不要用 `python` 或 `py`。**
  這台機器的 `python` 是 Windows 的 App execution alias（見 GAMEDATA [ENV-001]），
  而 `py` 走系統 Python，選用套件都不在裡面（[ENV-004]）。
- 主控台是 cp950，中文會亂碼。跑 Python 一律加 `PYTHONUTF8=1`。
- 建置輸出很長：導到 scratchpad 的 log 檔，只看結尾判定，別整份倒進對話。
- **`RODATA/` 已在 .gitignore**（24 萬檔、18 GB）。若 `git status` 出現它，
  代表 .gitignore 被動過，先停下來查清楚，絕對不要 commit 進去。
- 用 heredoc 寫含 Windows 路徑的檔案時，反斜線會被吃掉（`\.` → `\.`、
  `\a` → BEL）。寫完務必回頭確認，或改用正斜線／`chr(92)` 組字串。

## 打包成 exe

三個檔案，**一份設定**（不要在別處再寫一份 PyInstaller 參數）：

| 檔案 | 做什麼 |
|---|---|
| `RO-Online-toolbox.spec` | 唯一一份 PyInstaller 設定 |
| `build_local.py` | 本機編 ＋ 冒煙測試（不上傳） |
| `release.py` | 編 ＋ 冒煙 ＋ 建 Release 上傳 |

```powershell
.\.venv\Scripts\python.exe build_local.py           # 編 ＋ 冒煙
.\.venv\Scripts\python.exe build_local.py --debug   # 帶主控台版，看得到 traceback
.\.venv\Scripts\python.exe build_local.py --run     # 通過後把 GUI 開起來看
```

實測（2026-08-25，v0.1.2）：**78 MB、編譯約 55 秒、啟動到自檢完成 3.8 秒**。

### ⚠ 打包後最會出事的是資料檔，而且完全沒有徵兆

`assets/*.json.gz` 漏收的話：道具名一律查不到、補水選單整個空白，
程式**不會報錯**。所以 `app.py` 有 `--selftest`，會真的查一筆資料
（`item_name(501)`、`mob_name(1002)`）並確認圖示與樣式表都在；
`release.py` 沒過就中止發布。

路徑要打包後才算得對：`config/paths.py` 的 `_bundle_root()` 讀 `sys._MEIPASS`，
`tests/test_paths.py` 驗它跟 spec 裡的 datas 目的地一致。

圖示：`--icon src\ro_toolbox\ui\resources\icon.ico`（7 種尺寸），
用 `tools\make_icon.py` 從 `assets\icon-source.png` 產 ——
**換圖要重跑那支腳本**，不要手工做 .ico。

⚠ 冒煙測試的輸出一律用 ASCII 記號：主控台是 cp950，`✓` 印下去會拋
`UnicodeEncodeError`（實際踩過 —— exe 自檢**全過**，卻在印成功訊息時掛掉，
看起來像打包失敗）。

## 相關指令

- `/_patchCheck` —— 遊戲改版之後的體檢（位址／opcode／偏移／查表／動作五層）。
  改版後要發版的話，先跑完它再走這裡。
