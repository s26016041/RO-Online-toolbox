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
3. **建置＋驗證**：先跑過專案的測試／建置腳本。
   **驗證沒過就停下來回報，不准 push 也不准發 Release。**
4. **Push**：`git push origin main`。
5. **建 Release**：`gh release create v<版號> --title v<版號> --notes <重點變更>`，
   有建置產物就一併上傳。
6. **收尾回報**：Release 網址＋這版包含哪些 commit。

## 本專案（RO-Online-toolbox）的具體指令

remote：`git@github.com:s26016041/RO-Online-toolbox.git`

```
1. 版號改三處（見下方警告）→ git commit -m "chore: 版號 X.Y.Z"
2. .\.venv\Scripts\python.exe -m pytest -q                          # 必須全過
3. .\.venv\Scripts\python.exe -m ruff check src tests tools main.py # 必須全過
4. git push origin main
5. gh release create vX.Y.Z --title vX.Y.Z --notes "重點變更"
```

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

## 打包成 exe（尚未驗證）

`scripts\build.ps1` 是 PyInstaller 打包腳本，**目前還沒實際跑過**。
第一次要發布含 exe 的 Release 時，先單獨把它跑通、確認產物能開，
再寫進上面的流程，並把結果記進 GAMEDATA。

圖示已經接好：`--icon src\ro_toolbox\ui\resources\icon.ico`（7 種尺寸）。
圖示是用 `tools\make_icon.py` 從 `assets\icon-source.png` 產的 ——
**換圖要重跑那支腳本**，不要手工做 .ico。

## 相關指令

- `/_patchCheck` —— 遊戲改版之後的體檢（位址／opcode／偏移／查表／動作五層）。
  改版後要發版的話，先跑完它再走這裡。
