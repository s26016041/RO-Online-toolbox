r"""給編出來的 exe 蓋一個 Authenticode 簽章。

## ⚠ 為什麼非簽不可：不簽的 exe 讀不到遊戲記憶體

實測（2026-08-28，GAMEDATA [ENV-006]）：**GameGuard 會擋掉未簽章的執行檔
對遊戲做大量記憶體讀取**，回 `ERROR_ACCESS_DENIED`（錯誤碼 5）。

    動作                      python.exe(已簽)   我們的 exe(未簽)
    讀 0x400000 的 2 bytes     ✅                 ✅
    讀 44.6 MB 的區段          ✅ 錯誤碼 0        ❌ 拿到 0，錯誤碼 5
    連掃三次的命中數           1 / 1 / 1          1 / 0 / 0

小量探測放行、整片掃描擋掉。症狀是程式一直噴「讀不到 ragexe.exe 的程式碼
區段」、補水停用、背包定位不到 —— 而同一份原始碼直接跑完全正常。

**跟打包工具無關**：PyInstaller 與 Nuitka 編出來的都一樣被擋。
簽下去就好了，兩邊都是。

## 自簽就夠

實測 A/B/A/B：同一顆 exe，只差一個**自簽**憑證（信任鏈根本沒過，
`Get-AuthenticodeSignature` 回 `UnknownError`），大區段讀取就從錯誤碼 5
變成正常。GameGuard 只看「有沒有簽章」，不看是不是受信任的。

所以**不必花錢買憑證**。第一次執行會在使用者的「目前使用者」憑證存放區
建一張，之後重複使用。

⚠ 這張憑證只是讓 exe 帶著簽章，**不會**讓 Windows SmartScreen 不再跳
「發行者不明」—— 那本來就會跳（未簽章時也跳），不會變差。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: 憑證的主體名稱。認它就靠這個字串。
SUBJECT = "CN=RO-Online-toolbox"

#: 找不到就建一張新的（有效期一年）。放在「目前使用者」的存放區，
#: 不需要管理員權限。
_PS_SIGN = r"""
$ErrorActionPreference = 'Stop'
$subject = '{subject}'
$cert = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object {{ $_.Subject -eq $subject -and $_.NotAfter -gt (Get-Date) }} |
    Sort-Object NotAfter -Descending | Select-Object -First 1
if (-not $cert) {{
    $cert = New-SelfSignedCertificate -Subject $subject -Type CodeSigningCert `
        -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(5)
    Write-Output "NEWCERT $($cert.Thumbprint)"
}}
$r = Set-AuthenticodeSignature -FilePath '{path}' -Certificate $cert
Write-Output "STATUS $($r.Status)"
"""


def sign(exe: Path) -> bool:
    """簽一顆 exe。成功回 True。

    ⚠ **簽不成要大聲失敗**，不要安靜地放行 —— 沒簽的 exe 讀不到遊戲，
    而那個症狀（一堆「讀不到程式碼區段」）看起來完全不像「忘了簽」。
    """
    if not exe.exists():
        print(f"✗ 要簽的檔案不存在：{exe}")
        return False
    script = _PS_SIGN.format(subject=SUBJECT, path=str(exe).replace("'", "''"))
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"✗ 呼叫 PowerShell 簽章失敗：{exc}")
        return False

    out = (result.stdout or "") + (result.stderr or "")
    if "NEWCERT" in out:
        print(f"  （建立了新的簽章憑證 {SUBJECT}，之後會重複使用）")
    # `UnknownError` = 簽章蓋上去了、但信任鏈沒過。**那就夠了**（見檔頭）。
    if "STATUS Valid" in out or "STATUS UnknownError" in out:
        print(f"✓ 已簽章：{exe.name}")
        return True
    print(f"✗ 簽章失敗：{out.strip()[:400]}")
    return False


def is_signed(exe: Path) -> bool:
    """這顆 exe 帶著簽章嗎？（不管信任鏈過不過）"""
    script = (
        f"(Get-AuthenticodeSignature '{str(exe).replace(chr(39), chr(39) * 2)}')"
        ".Status"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (result.stdout or "").strip() in {"Valid", "UnknownError"}


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python tools/sign_exe.py <exe 路徑>")
        return 2
    return 0 if sign(Path(sys.argv[1])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
