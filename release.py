"""一鍵發布：讀 VERSION → 編 .exe → 冒煙測試 → 建 Release 並上傳。

流程
----
1. 讀根目錄的 `VERSION` → tag = `v<版號>`。
2. `git fetch`；origin 已經有這個 tag → 代表發布過了，直接結束（不重複發）。
3. 用 `RO-Online-toolbox.spec` 編成單一 .exe（與 build_local.py 同一份設定）。
4. **跑 `exe --selftest`**：分頁、道具表、怪物表、圖示、樣式表都要在。
   ⚠ 沒過就中止發布 —— 這些東西漏收不會有任何錯誤訊息，
   使用者只會拿到一個選單空白的程式。
5. `gh release create` 標在 origin/main 上，並上傳 .exe。

要換版本 → 改三處版號（`VERSION`、`pyproject.toml`、`ro_toolbox/__init__.py`，
`tests/test_version.py` 會擋住不同步）→ push → 再跑這支。

用法：`.\\.venv\\Scripts\\python.exe release.py`
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# 主控台是 cp950，勾勾叉叉編不進去會拋 UnicodeEncodeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from build_local import EXE_NAME, build, smoke  # noqa: E402


def cap(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)


def die(message: str) -> None:
    print("\n✗ " + message)
    sys.exit(1)


def read_version() -> str:
    path = ROOT / "VERSION"
    if not path.exists():
        die("找不到根目錄的 VERSION 檔。")
    # utf-8-sig：帶 BOM 的 VERSION 也照樣乾淨（否則 tag 會帶一個看不見的 U+FEFF）
    version = path.read_text(encoding="utf-8-sig").strip()
    if not version:
        die("VERSION 檔是空的。")
    return version


def main() -> int:
    for tool in ("git", "gh"):
        if not shutil.which(tool):
            die(f"找不到 {tool}。")
    if cap(["gh", "auth", "status"]).returncode != 0:
        die("gh 尚未登入。請先執行： gh auth login")

    version = read_version()
    tag = f"v{version}"
    print(f"目前版本 = {version}  →  tag = {tag}")

    cap(["git", "fetch", "origin", "--tags", "--quiet"])
    if cap(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"]).stdout.strip():
        print(f"\n✓ GitHub 上已有 {tag}（已發布過）→ 不重複發布。")
        return 0

    target = cap(["git", "rev-parse", "origin/main"]).stdout.strip()
    if not target:
        die("讀不到 origin/main，請先 git push。")
    head = cap(["git", "rev-parse", "HEAD"]).stdout.strip()
    dirty = bool(cap(["git", "status", "--porcelain"]).stdout.strip())
    if head != target or dirty:
        # 不擋，但要講清楚：編出來的 exe 用的是**本機**程式碼，不是 main 上的。
        state = "有未提交的變更" if dirty else "HEAD 不等於 origin/main"
        print(f"\n⚠ 本機與 GitHub main 不一致（{state}）。")
        print("  .exe 會用『本機目前的程式碼』編譯。要編 main 上的版本請先 push。")

    print(f"\n將在 origin/main（{target[:8]}）建立 {tag} 的 Release。")

    exe = build(debug=False)
    if exe is None:
        die("編譯失敗，請看上面 PyInstaller 的訊息。")
    if not smoke(exe):
        die("冒煙測試失敗，發布中止。用 build_local.py --debug 追查。")

    print("\n建立 GitHub Release 並上傳 .exe…")
    notes = (
        f"RO Online 工具箱 {tag}\n\n"
        f"下載 `{EXE_NAME}.exe` 直接執行，不需要裝 Python。\n\n"
        "⚠ 封包擷取需要 [Npcap](https://npcap.com/) 與**系統管理員權限**。\n"
        "⚠ 道具小圖示來自客戶端解包資料（`RODATA/`，不隨版發布），"
        "沒有的話選單只顯示名稱與數量。\n\n"
        "由 release.py 自動編譯發布，發布前已跑過 `--selftest`"
        "（分頁、道具表、怪物表、圖示、樣式表都在）。"
    )
    result = subprocess.run(
        ["gh", "release", "create", tag, "--target", target,
         "--title", tag, "--notes", notes, str(exe)],
        cwd=ROOT, check=False,
    )
    if result.returncode != 0:
        die("建立 Release 失敗，請看上面 gh 的訊息。")
    print(f"\n✅ 已發布 {tag} 並上傳 {EXE_NAME}.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
