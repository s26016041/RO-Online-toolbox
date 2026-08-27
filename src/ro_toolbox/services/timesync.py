"""跟 NTP 對時，算出本機時鐘偏了多少。

## 為什麼 OTP 一定要管這件事

TOTP 完全靠本機時鐘算。時鐘偏掉的下場**不是報錯**，是每次都算出一個看起來
正常的六位數、然後每次都被伺服器打回票，而錯誤訊息通常跟密碼錯長得一樣。
使用者會去改密碼、重綁 OTP，就是不會想到是電腦時間慢了一分鐘。

所以偏移要當成一等公民顯示出來，超過半個週期就**大聲停用**登入。

純標準函式庫的 SNTP（RFC 4330）客戶端，一個 UDP 來回就結束。
會擋住呼叫端，所以 UI 要丟到背景執行緒跑。
"""

from __future__ import annotations

import logging
import socket
import struct
import time

log = logging.getLogger(__name__)

# NTP 的紀元是 1900-01-01，Unix 是 1970-01-01，差這麼多秒。
_NTP_EPOCH_DELTA = 2_208_988_800

DEFAULT_HOSTS = ("time.windows.com", "pool.ntp.org", "time.google.com")


class TimeSyncError(RuntimeError):
    """對時失敗。訊息是要直接給使用者看的。"""


def _to_unix(raw: bytes) -> float:
    seconds, fraction = struct.unpack("!II", raw)
    return seconds - _NTP_EPOCH_DELTA + fraction / 2**32


def query_offset(host: str, timeout: float = 2.5) -> float:
    """問一台 NTP 伺服器，回**標準時間減本機時間**的秒數。

    正值 = 本機時鐘慢了；負值 = 本機快了。
    用 RFC 4330 的四時間戳公式，順便把網路來回延遲抵銷掉。
    """
    # LI=0（無閏秒警告）、VN=3、Mode=3（client）。其餘 47 個 byte 留白。
    request = b"\x1b" + 47 * b"\0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        t1 = time.time()
        sock.sendto(request, (host, 123))
        data, _ = sock.recvfrom(48)
        t4 = time.time()
    except (TimeoutError, OSError) as exc:
        raise TimeSyncError(f"連不上 {host}：{exc}") from exc
    finally:
        sock.close()

    if len(data) < 48:
        raise TimeSyncError(f"{host} 回的封包只有 {len(data)} bytes，不是合法的 NTP 回應。")

    t2 = _to_unix(data[32:40])  # 伺服器收到的時刻
    t3 = _to_unix(data[40:48])  # 伺服器送出的時刻
    if t2 <= 0 or t3 <= 0:
        raise TimeSyncError(f"{host} 回的時間戳是空的。")
    return ((t2 - t1) + (t3 - t4)) / 2


def query_any(hosts: tuple[str, ...] = DEFAULT_HOSTS, timeout: float = 2.5) -> tuple[float, str]:
    """依序問到有人回為止，回 (偏移秒數, 是誰回的)。

    全部問不到就丟例外 —— 呼叫端要顯示「無法確認時間」，
    但**不該因此停用功能**：沒網路的時候本來就登入不了，
    在這裡多擋一層只會讓錯誤訊息更難懂。
    """
    problems = []
    for host in hosts:
        try:
            return query_offset(host, timeout), host
        except TimeSyncError as exc:
            problems.append(str(exc))
            log.debug("NTP 查詢失敗：%s", exc)
    raise TimeSyncError("所有 NTP 伺服器都問不到：" + "；".join(problems))


def describe(offset: float) -> str:
    """把偏移秒數講成人話。"""
    magnitude = abs(offset)
    if magnitude < 1:
        return "本機時間準確（誤差不到 1 秒）"
    unit = f"{magnitude:.0f} 秒" if magnitude < 90 else f"{magnitude / 60:.1f} 分鐘"
    return f"本機時間比標準時間{'慢' if offset > 0 else '快'} {unit}"
