"""TOTP（RFC 6238）：種子解析、產碼、參數實測確認。

**這支刻意只用標準函式庫**，沒有任何第三方相依 —— 它是整條鏈裡最不能出錯的
一段，測試要能在任何環境跑起來。QR 圖片解碼在 `services/qr.py`（那支才有相依）。

## 為什麼參數不能用預設值就算了

RFC 6238 的預設是 SHA1 / 6 碼 / 30 秒，絕大多數服務也照這個走，但**不是保證**。
猜錯的下場不是「報錯」，是**每次都算出一個長得很正常的六位數，然後每次都登入失敗**，
而且伺服器回的錯誤訊息通常跟密碼錯一模一樣。這正是專案鐵則裡「安靜地做錯事」那一類。

所以參數只有兩個合法來源：

1. **`otpauth://` URI 自己帶的**（`algorithm=` / `digits=` / `period=`）——
   authenticator 匯出的 QR 走這條，參數是 app 記著的，不是我們推的。
2. **拿使用者手機上當下那組碼實測比對出來的**（`search_params`）——
   使用者只貼得到一串 base32 時走這條。

兩條都走不通就**拒絕儲存**，不准填預設值蒙混過去。`OtpSecret.params_confirmed`
就是在記「這組參數到底是誰說的」。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass

# RFC 4226 只定義到 SHA1，RFC 6238 擴充到 SHA256/SHA512。
# MD5 只有 Google 的 migration 格式列得出來，實務上沒人用，不支援（要用會大聲拒絕）。
HASHES = {
    "SHA1": hashlib.sha1,
    "SHA256": hashlib.sha256,
    "SHA512": hashlib.sha512,
}

_B32_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")

# search_params 的搜尋空間。碼數不在裡面 —— 它由使用者輸入的碼長度直接決定。
_SEARCH_ALGORITHMS = ("SHA1", "SHA256", "SHA512")
_SEARCH_PERIODS = (30, 60)


class OtpError(ValueError):
    """種子解析失敗。訊息是要直接給使用者看的，寫人話。"""


@dataclass(frozen=True, slots=True)
class OtpSecret:
    """一組 TOTP 種子與它的參數。

    `params_confirmed` 為 False 表示參數是預設值、**還沒被證實**，
    這種狀態不准存檔（`accounts.py` 會擋）。
    """

    label: str          # 帳號標籤，例如 "RO1-s26016041"
    issuer: str         # 發行者，例如 "GRAVITY"
    key: bytes          # 原始金鑰（不是 base32）
    algorithm: str = "SHA1"
    digits: int = 6
    period: int = 30
    source: str = ""            # otpauth / migration / base32，出處存著方便事後追
    params_confirmed: bool = False

    def __post_init__(self) -> None:
        if not self.key:
            raise OtpError("金鑰是空的。")
        if self.algorithm not in HASHES:
            raise OtpError(f"不支援的雜湊演算法：{self.algorithm}")
        if self.digits not in (6, 7, 8):
            raise OtpError(f"碼數超出範圍：{self.digits}")
        if self.period <= 0:
            raise OtpError(f"週期不合理：{self.period}")

    @property
    def base32(self) -> str:
        """去掉 padding 的 base32，給使用者看／存檔用。"""
        return base64.b32encode(self.key).decode("ascii").rstrip("=")

    @property
    def masked(self) -> str:
        """遮蔽後的種子。UI 與日誌**只准顯示這個**，不准印出完整種子。"""
        text = self.base32
        if len(text) <= 6:
            return "*" * len(text)
        return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"

    @property
    def params_text(self) -> str:
        return f"{self.algorithm} / {self.digits} 碼 / {self.period} 秒"

    @property
    def display_name(self) -> str:
        if self.issuer and self.label:
            return f"{self.issuer} / {self.label}"
        return self.label or self.issuer or "(未命名)"

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "issuer": self.issuer,
            "secret": self.base32,
            "algorithm": self.algorithm,
            "digits": self.digits,
            "period": self.period,
            "source": self.source,
            "params_confirmed": self.params_confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> OtpSecret:
        return cls(
            label=data.get("label", ""),
            issuer=data.get("issuer", ""),
            key=decode_base32(data["secret"]),
            algorithm=data.get("algorithm", "SHA1"),
            digits=int(data.get("digits", 6)),
            period=int(data.get("period", 30)),
            source=data.get("source", ""),
            params_confirmed=bool(data.get("params_confirmed", False)),
        )

    def with_params(self, algorithm: str, digits: int, period: int, source: str) -> OtpSecret:
        """換一組（已經證實過的）參數，回新的一份。"""
        return OtpSecret(
            label=self.label,
            issuer=self.issuer,
            key=self.key,
            algorithm=algorithm,
            digits=digits,
            period=period,
            source=source,
            params_confirmed=True,
        )

    def renamed(self, label: str, issuer: str) -> OtpSecret:
        return OtpSecret(
            label=label,
            issuer=issuer,
            key=self.key,
            algorithm=self.algorithm,
            digits=self.digits,
            period=self.period,
            source=self.source,
            params_confirmed=self.params_confirmed,
        )


# ---- 產碼 -----------------------------------------------------------------


def generate(secret: OtpSecret, at: float | None = None) -> str:
    """算出 `at`（Unix 秒，預設現在）那個時間窗的驗證碼。"""
    now = time.time() if at is None else at
    counter = int(now // secret.period)
    mac = hmac.new(secret.key, struct.pack(">Q", counter), HASHES[secret.algorithm]).digest()
    # RFC 4226 5.3 dynamic truncation：用最後一個 byte 的低 4 bit 當偏移。
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**secret.digits).zfill(secret.digits)


def remaining_seconds(secret: OtpSecret, at: float | None = None) -> int:
    """這組碼還有幾秒過期。剛跳號時等於 period，不會回 0。"""
    now = time.time() if at is None else at
    return secret.period - int(now) % secret.period


def verify(secret: OtpSecret, code: str, at: float | None = None, window: int = 1) -> bool:
    """驗證碼對不對。

    `window=1` 表示前後各多收一個時間窗 —— 使用者從看手機到按下按鈕會花幾秒，
    剛好跨過邊界是常態，不放寬的話會有一大堆假失敗。
    比對用 `compare_digest`，別給時間差攻擊留門。
    """
    candidate = "".join(ch for ch in code if ch.isdigit())
    if len(candidate) != secret.digits:
        return False
    now = time.time() if at is None else at
    return any(
        hmac.compare_digest(generate(secret, now + step * secret.period), candidate)
        for step in range(-window, window + 1)
    )


def search_params(
    secret: OtpSecret, code: str, at: float | None = None, window: int = 1
) -> list[OtpSecret]:
    """使用者只給 base32、參數不明時，拿他手機上的碼**實測**出參數。

    回傳所有對得上的組合。這不是猜 —— 是拿真實資料反查，然後把結果記下來。

    - 剛好一組 → 就是它。
    - 超過一組 → **不准挑一個用**。呼叫端要請使用者過一個週期再輸入一組新的碼，
      用第二次結果取交集。單一組六位數撞到兩種參數的機率很低但不是零。
    - 零組 → 種子錯、時間差太多，或碼打錯。拒絕。
    """
    cleaned = "".join(ch for ch in code if ch.isdigit())
    if len(cleaned) not in (6, 7, 8):
        return []
    found = []
    for algorithm in _SEARCH_ALGORITHMS:
        for period in _SEARCH_PERIODS:
            candidate = OtpSecret(
                label=secret.label,
                issuer=secret.issuer,
                key=secret.key,
                algorithm=algorithm,
                digits=len(cleaned),
                period=period,
                source=f"{secret.source}+實測" if secret.source else "實測",
                params_confirmed=True,
            )
            if verify(candidate, cleaned, at=at, window=window):
                found.append(candidate)
    return found


# ---- 種子解析 --------------------------------------------------------------


def decode_base32(text: str) -> bytes:
    """寬鬆地吃 base32：空白、連字號、大小寫、缺 padding 都接受。"""
    cleaned = "".join(text.split()).replace("-", "").upper()
    if not cleaned:
        raise OtpError("種子是空的。")
    bad = sorted(set(cleaned) - _B32_ALPHABET - {"="})
    if bad:
        raise OtpError("這串不是有效的 base32，出現了不該有的字元：" + " ".join(bad))
    cleaned = cleaned.rstrip("=")
    try:
        return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except (binascii.Error, ValueError) as exc:
        raise OtpError(f"base32 解碼失敗：{exc}") from exc


def parse(text: str) -> list[OtpSecret]:
    """吃使用者貼進來的任何東西，回一到多組種子。

    支援三種：`otpauth://totp/...`、`otpauth-migration://offline?data=...`
    （authenticator 的匯出 QR，一張可以包好幾個帳號）、以及純 base32 字串。
    """
    raw = text.strip()
    if not raw:
        raise OtpError("沒有內容。")
    lowered = raw.lower()
    if lowered.startswith("otpauth-migration://"):
        return parse_migration_uri(raw)
    if lowered.startswith("otpauth://"):
        return [parse_otpauth_uri(raw)]
    if lowered.startswith(("http://", "https://")):
        raise OtpError("這是一般網址，不是 OTP 種子。要的是綁定畫面上那串英數字或它的 QR。")
    return [
        OtpSecret(
            label="",
            issuer="",
            key=decode_base32(raw),
            source="base32",
            # 純字串沒有帶參數，預設值還沒被證實 —— 要走 search_params 確認。
            params_confirmed=False,
        )
    ]


def parse_otpauth_uri(uri: str) -> OtpSecret:
    parsed = urllib.parse.urlparse(uri)
    kind = parsed.netloc.lower()
    if kind == "hotp":
        raise OtpError(
            "這是 HOTP（計數器式）種子，不是 TOTP。它每用一次計數器就要加一，"
            "算錯會把帳號的計數器推歪，本工具不處理。"
        )
    if kind != "totp":
        raise OtpError(f"不認得的 otpauth 型別：{parsed.netloc}")

    query = urllib.parse.parse_qs(parsed.query)
    if "secret" not in query:
        raise OtpError("這條 otpauth 網址裡沒有 secret 參數。")

    label = urllib.parse.unquote(parsed.path.lstrip("/"))
    issuer = query.get("issuer", [""])[0]
    # 標籤慣例是「發行者:帳號」，前綴跟 issuer 重複時去掉，
    # 顯示才不會變成 "GRAVITY / GRAVITY:xxx"。
    if issuer and label.startswith(f"{issuer}:"):
        label = label[len(issuer) + 1 :]

    algorithm = query.get("algorithm", ["SHA1"])[0].upper()
    if algorithm not in HASHES:
        raise OtpError(f"不支援的雜湊演算法：{algorithm}")

    return OtpSecret(
        label=label,
        issuer=issuer,
        key=decode_base32(query["secret"][0]),
        algorithm=algorithm,
        digits=int(query.get("digits", ["6"])[0]),
        period=int(query.get("period", ["30"])[0]),
        source="otpauth",
        # 網址明講的參數就是確定的，不用再實測。
        params_confirmed=True,
    )


# ---- authenticator 匯出格式（otpauth-migration） ---------------------------
#
# Google Authenticator「匯出帳戶」產生的 QR。裡面是 base64 過的 protobuf：
#
#   MigrationPayload { repeated OtpParameters otp_parameters = 1; int32 version = 2;
#                      int32 batch_size = 3; int32 batch_index = 4; int32 batch_id = 5; }
#   OtpParameters    { bytes secret = 1; string name = 2; string issuer = 3;
#                      Algorithm algorithm = 4; DigitCount digits = 5;
#                      OtpType type = 6; int64 counter = 7; }
#
# 只用到 varint 與 length-delimited 兩種 wire type，所以**自己解四十行就夠**，
# 不必為了這個拉一整包 protobuf 進來（單一 exe 的體積是要顧的）。
#
# 兩個踩得到的坑：
#   - `secret` 在這個格式裡是**原始 bytes，不是 base32**。
#   - 這個格式**沒有 period 欄位**，Google Authenticator 一律 30 秒。

_MIGRATION_ALGORITHM = {0: "SHA1", 1: "SHA1", 2: "SHA256", 3: "SHA512", 4: "MD5"}
_MIGRATION_DIGITS = {0: 6, 1: 6, 2: 8}
_MIGRATION_TYPE = {0: "未指定", 1: "HOTP", 2: "TOTP"}


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf):
            raise OtpError("匯出資料在 varint 中途就結束了，QR 可能沒拍完整。")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _read_fields(buf: bytes) -> dict[int, list]:
    """解 protobuf，回 {欄位編號: [值]}。值是 int（varint）或 bytes。"""
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, pos = _read_varint(buf, pos)
            else:
                length = 8 if wire == 1 else 4
            value, pos = buf[pos : pos + length], pos + length
            if len(value) != length:
                raise OtpError("匯出資料長度對不上，QR 可能沒拍完整。")
        else:
            raise OtpError(f"匯出資料裡有看不懂的欄位型別（wire type {wire}）。")
        fields.setdefault(number, []).append(value)
    return fields


def parse_migration_uri(uri: str) -> list[OtpSecret]:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
    if "data" not in query:
        raise OtpError("這條匯出網址裡沒有 data 參數。")
    blob = query["data"][0]
    try:
        payload = base64.b64decode(blob + "=" * (-len(blob) % 4))
    except (binascii.Error, ValueError) as exc:
        raise OtpError(f"匯出資料 base64 解碼失敗：{exc}") from exc

    fields = _read_fields(payload)
    entries = fields.get(1, [])
    if not entries:
        raise OtpError("這張匯出 QR 裡沒有任何帳號。")

    # 帳號多的時候 app 會拆成好幾張 QR，一張只解得出自己那一批。
    # 不吭聲的話使用者會以為帳號漏掉了，所以把批次記進 source 顯示出來。
    batch_size = fields.get(3, [1])[0]
    batch_index = fields.get(4, [0])[0]

    results = []
    for raw in entries:
        item = _read_fields(raw)
        name = item.get(2, [b""])[0].decode("utf-8", "replace")
        otp_type = _MIGRATION_TYPE.get(item.get(6, [2])[0], "未知")
        if otp_type == "HOTP":
            raise OtpError(f"「{name}」是 HOTP（計數器式），本工具不處理。")
        algorithm = _MIGRATION_ALGORITHM.get(item.get(4, [1])[0], "?")
        if algorithm not in HASHES:
            raise OtpError(f"「{name}」用的是不支援的演算法：{algorithm}")
        if 1 not in item:
            raise OtpError(f"「{name}」這筆裡面沒有金鑰。")
        results.append(
            OtpSecret(
                label=name,
                issuer=item.get(3, [b""])[0].decode("utf-8", "replace"),
                key=item[1][0],
                algorithm=algorithm,
                digits=_MIGRATION_DIGITS.get(item.get(5, [1])[0], 6),
                period=30,
                source=f"migration {batch_index + 1}/{batch_size}",
                params_confirmed=True,
            )
        )
    return results


def upcoming(
    secret: OtpSecret, count: int = 6, at: float | None = None
) -> Iterator[tuple[float, str]]:
    """接下來幾個時間窗的 (起始時間, 驗證碼)。給對照表用。"""
    now = time.time() if at is None else at
    base = now - now % secret.period
    for step in range(count):
        moment = base + step * secret.period
        yield moment, generate(secret, moment)
