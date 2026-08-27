"""TOTP 核心：算法、三種種子格式、參數實測反查。

`test_rfc6238_vectors` 是這一整支的地基 —— RFC 6238 附錄 B 的官方向量。
它顧的是 HMAC、動態截斷、計數器換算三件事全對；這三件事錯了不會報錯，
只會安靜地產出一個格式正確但永遠登不進去的六位數。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import totp
from ro_toolbox.services.totp import OtpError, OtpSecret

# RFC 6238 Appendix B 的種子：ASCII "12345678901234567890" 重複到需要的長度。
_SHA1_SEED = b"12345678901234567890"
_SEEDS = {"SHA1": _SHA1_SEED, "SHA256": (_SHA1_SEED * 2)[:32], "SHA512": (_SHA1_SEED * 4)[:64]}

_VECTORS = [
    (59, {"SHA1": "94287082", "SHA256": "46119246", "SHA512": "90693936"}),
    (1111111109, {"SHA1": "07081804", "SHA256": "68084774", "SHA512": "25091201"}),
    (1111111111, {"SHA1": "14050471", "SHA256": "67062674", "SHA512": "99943326"}),
    (1234567890, {"SHA1": "89005924", "SHA256": "91819424", "SHA512": "93441116"}),
    (2000000000, {"SHA1": "69279037", "SHA256": "90698825", "SHA512": "38618901"}),
    (20000000000, {"SHA1": "65353130", "SHA256": "77737706", "SHA512": "47863826"}),
]

DEMO_B32 = "JBSWY3DPEHPK3PXP"


def _secret(**kwargs) -> OtpSecret:
    base = {
        "label": "demo",
        "issuer": "GRAVITY",
        "key": totp.decode_base32(DEMO_B32),
        "params_confirmed": True,
    }
    return OtpSecret(**{**base, **kwargs})


@pytest.mark.parametrize(("moment", "expected"), _VECTORS)
def test_rfc6238_vectors(moment, expected):
    """官方測試向量，三種雜湊 × 六個時間點。"""
    for algorithm, want in expected.items():
        secret = OtpSecret("t", "t", _SEEDS[algorithm], algorithm=algorithm, digits=8)
        assert totp.generate(secret, moment) == want


def test_remaining_seconds_spans_full_period():
    """剛跳號時回滿一個週期，不是 0 —— 進度條歸零會讓使用者以為已經過期。

    990 才是 30 秒制的窗邊界（990 % 30 == 0），1000 不是。
    """
    secret = _secret()
    assert totp.remaining_seconds(secret, 990.0) == 30
    assert totp.remaining_seconds(secret, 1019.9) == 1


def test_verify_accepts_neighbouring_window():
    """使用者從看手機到按按鈕會花幾秒，跨過邊界是常態。"""
    secret = _secret()
    previous = totp.generate(secret, 1000.0)
    assert totp.verify(secret, previous, at=1035.0)          # 上一個窗，window=1 收
    assert not totp.verify(secret, previous, at=1035.0, window=0)
    assert not totp.verify(secret, previous, at=1100.0)      # 差太多就不收


def test_verify_ignores_separators_and_rejects_wrong_length():
    secret = _secret()
    code = totp.generate(secret, 1000.0)
    assert totp.verify(secret, f"{code[:3]} {code[3:]}", at=1000.0)
    assert not totp.verify(secret, code[:5], at=1000.0)


# ---- 種子解析 --------------------------------------------------------------


def test_parse_plain_base32_leaves_params_unconfirmed():
    """純字串沒帶參數 —— 預設值只是預設值，不准當成已知。"""
    secret = totp.parse("jbsw y3dp-ehpk 3pxp")[0]
    assert secret.key == totp.decode_base32(DEMO_B32)
    assert secret.params_confirmed is False
    assert secret.source == "base32"


def test_parse_otpauth_uri_takes_params_from_uri():
    secret = totp.parse(
        f"otpauth://totp/GRAVITY:RO1-abc?secret={DEMO_B32}"
        "&issuer=GRAVITY&algorithm=SHA256&digits=8&period=60"
    )[0]
    assert (secret.algorithm, secret.digits, secret.period) == ("SHA256", 8, 60)
    assert secret.params_confirmed is True
    # 標籤前綴跟 issuer 重複時要去掉，不然顯示會變 "GRAVITY / GRAVITY:RO1-abc"
    assert secret.label == "RO1-abc"
    assert secret.display_name == "GRAVITY / RO1-abc"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "https://example.com/login",
        "not!valid!base32",
        f"otpauth://hotp/x?secret={DEMO_B32}",       # 計數器式，會把帳號推歪
        "otpauth://totp/x",                          # 沒有 secret
        f"otpauth://totp/x?secret={DEMO_B32}&algorithm=MD5",
    ],
)
def test_parse_rejects_bad_input(text):
    """壞輸入一律大聲拒絕，不准退回預設值繼續算。"""
    with pytest.raises(OtpError):
        totp.parse(text)


def test_masked_never_shows_the_middle():
    secret = _secret()
    assert secret.masked.startswith("JB")
    assert secret.masked.endswith(secret.base32[-2:])
    assert secret.base32[2:-2] not in secret.masked


# ---- 匯出格式（otpauth-migration） ----------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _int_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _migration_uri(entries: list[bytes], batch_size: int = 1, batch_index: int = 0) -> str:
    import base64
    import urllib.parse

    payload = b"".join(_field(1, e) for e in entries)
    payload += _int_field(2, 2) + _int_field(3, batch_size) + _int_field(4, batch_index)
    data = base64.b64encode(payload).decode()
    return "otpauth-migration://offline?data=" + urllib.parse.quote(data)


def test_parse_migration_reads_raw_key_and_params():
    """匯出格式的 secret 是**原始 bytes 不是 base32**，這裡就是在釘死這一點。"""
    key = totp.decode_base32(DEMO_B32)
    entry = (
        _field(1, key)
        + _field(2, b"RO1-abc")
        + _field(3, b"GRAVITY")
        + _int_field(4, 1)   # SHA1
        + _int_field(5, 1)   # 6 碼
        + _int_field(6, 2)   # TOTP
    )
    secret = totp.parse(_migration_uri([entry]))[0]
    assert secret.key == key
    assert (secret.algorithm, secret.digits, secret.period) == ("SHA1", 6, 30)
    assert secret.params_confirmed is True
    assert secret.issuer == "GRAVITY"


def test_parse_migration_returns_every_account():
    """一張匯出 QR 可以包好幾個帳號，不能只回第一個。"""
    key = totp.decode_base32(DEMO_B32)
    entries = [_field(1, key) + _field(2, f"acc{i}".encode()) for i in range(3)]
    secrets = totp.parse(_migration_uri(entries))
    assert [s.label for s in secrets] == ["acc0", "acc1", "acc2"]


def test_parse_migration_records_batch_number():
    """分批匯出時要看得出來只解到其中一批，否則使用者以為帳號漏了。"""
    entry = _field(1, totp.decode_base32(DEMO_B32)) + _field(2, b"acc")
    secret = totp.parse(_migration_uri([entry], batch_size=3, batch_index=1))[0]
    assert secret.source == "migration 2/3"


def test_parse_migration_rejects_hotp():
    entry = _field(1, totp.decode_base32(DEMO_B32)) + _field(2, b"acc") + _int_field(6, 1)
    with pytest.raises(OtpError, match="HOTP"):
        totp.parse(_migration_uri([entry]))


def test_parse_migration_rejects_truncated_payload():
    """QR 沒拍完整的時候要報錯，不能解出半組垃圾種子。"""
    entry = _field(1, totp.decode_base32(DEMO_B32)) + _field(2, b"acc")
    uri = _migration_uri([entry])
    with pytest.raises(OtpError):
        totp.parse(uri[: len(uri) - 8])


# ---- 參數實測反查 ----------------------------------------------------------


def test_search_params_recovers_unknown_params():
    """只有 base32 時，拿手機上的碼把參數量出來 —— 不是猜。"""
    unknown = totp.parse(DEMO_B32)[0]
    truth = _secret(algorithm="SHA512", digits=6, period=60)
    matches = totp.search_params(unknown, totp.generate(truth, 1700000000), at=1700000000)
    assert [(m.algorithm, m.period) for m in matches] == [("SHA512", 60)]
    assert matches[0].params_confirmed is True


def test_search_params_returns_nothing_when_the_key_is_wrong():
    """種子貼錯時回空，不准硬挑一組參數讓它「看起來成功」。"""
    unknown = totp.parse(DEMO_B32)[0]
    other = OtpSecret("x", "x", totp.decode_base32("GEZDGNBVGY3TQOJQ"), params_confirmed=True)
    assert totp.search_params(unknown, totp.generate(other, 1700000000), at=1700000000) == []


def test_search_params_ignores_impossible_lengths():
    unknown = totp.parse(DEMO_B32)[0]
    assert totp.search_params(unknown, "12345", at=1700000000) == []


def test_upcoming_starts_on_a_window_boundary():
    secret = _secret()
    moments = list(totp.upcoming(secret, count=3, at=1015.0))
    assert [m for m, _ in moments] == [990.0, 1020.0, 1050.0]
    assert moments[0][1] == totp.generate(secret, 1015.0)   # 同一個窗，同一組碼
