"""帳號存檔：DPAPI 加密、兩種失效路徑。

這一支盯的是三件會安靜出事的事：明文外洩、存進沒驗證過的參數、
還有**解不開時回空清單**（畫面顯示「沒有帳號」，使用者一按存檔就全沒了）。
"""

from __future__ import annotations

import sys

import pytest

from ro_toolbox.services import accounts, totp
from ro_toolbox.services.accounts import Account, AccountStore, AccountStoreError

pytestmark = pytest.mark.skipif(
    not accounts.crypto_available(), reason="DPAPI 只有 Windows 有"
)

SECRET_B32 = "JBSWY3DPEHPK3PXP"
PASSWORD = "p@ss w0rd 中文"


@pytest.fixture
def store_file(tmp_path):
    return tmp_path / "accounts.dat"


def _account(name: str = "主帳-騎士") -> Account:
    secret = totp.parse(
        f"otpauth://totp/GRAVITY:RO1-abc?secret={SECRET_B32}&issuer=GRAVITY"
    )[0]
    return Account(name=name, username="demo01", password=PASSWORD, secret=secret)


def test_roundtrip_keeps_every_field(store_file):
    original = _account()
    original.pin = "7342"
    original.server = "波利"
    original.character = "小騎士"
    original.char_slot = 4
    accounts.save(AccountStore([original]), store_file)
    loaded = accounts.load(store_file).accounts[0]

    assert loaded.name == original.name
    assert loaded.username == original.username
    assert loaded.password == PASSWORD
    assert (loaded.pin, loaded.server, loaded.character) == ("7342", "波利", "小騎士")
    assert loaded.char_slot == 4
    assert loaded.secret.key == original.secret.key
    assert loaded.secret.algorithm == original.secret.algorithm
    assert loaded.secret.digits == original.secret.digits
    assert loaded.secret.period == original.secret.period
    assert loaded.secret.params_confirmed is True


def test_nothing_readable_on_disk(store_file):
    """密碼與種子都不准以明文出現在檔案裡。"""
    accounts.save(AccountStore([_account()]), store_file)
    blob = store_file.read_bytes()
    assert PASSWORD.encode("utf-8") not in blob
    assert SECRET_B32.encode() not in blob
    assert b"demo01" not in blob


def test_missing_file_is_an_empty_store(tmp_path):
    assert accounts.load(tmp_path / "nope.dat").accounts == []


def test_corrupt_file_raises_instead_of_looking_empty(store_file):
    """解不開要大聲 —— 回空清單會讓下一次存檔把原本的資料蓋掉。"""
    accounts.save(AccountStore([_account()]), store_file)
    blob = store_file.read_bytes()
    store_file.write_bytes(blob[:-6] + b"XXXXXX")
    with pytest.raises(AccountStoreError):
        accounts.load(store_file)


def test_refuses_unconfirmed_params(store_file):
    """參數還沒實測確認的種子不准落地。"""
    unconfirmed = totp.parse(SECRET_B32)[0]
    assert unconfirmed.params_confirmed is False
    bad = Account("x", "y", "z", unconfirmed)
    with pytest.raises(AccountStoreError, match="還沒驗證"):
        accounts.save(AccountStore([bad]), store_file)
    assert not store_file.exists()


def test_save_is_atomic(store_file):
    """暫存檔不能留在旁邊，也不能取代掉正式檔。"""
    accounts.save(AccountStore([_account("一號")]), store_file)
    accounts.save(AccountStore([_account("二號")]), store_file)
    assert accounts.load(store_file).accounts[0].name == "二號"
    assert not list(store_file.parent.glob("*.tmp"))


def test_one_broken_entry_does_not_hide_the_others(store_file, monkeypatch, caplog):
    """一筆壞掉只略過那一筆，其他帳號要留著，而且要留下紀錄。"""
    good = _account("好的")
    accounts.save(AccountStore([good]), store_file)

    import json

    raw = json.loads(accounts._crypt(store_file.read_bytes(), protect=False).decode("utf-8"))
    raw["accounts"].insert(0, {"name": "壞的", "secret": {"secret": "!!!not base32!!!"}})
    store_file.write_bytes(
        accounts._crypt(json.dumps(raw).encode("utf-8"), protect=True)
    )

    with caplog.at_level("ERROR"):
        loaded = accounts.load(store_file)
    assert [a.name for a in loaded.accounts] == ["好的"]
    assert "壞的" in caplog.text


def test_old_files_without_the_new_fields_still_load(store_file):
    """加欄位不能讓既有的帳號檔讀不出來。"""
    import json

    good = _account()
    accounts.save(AccountStore([good]), store_file)
    raw = json.loads(accounts._crypt(store_file.read_bytes(), protect=False).decode("utf-8"))
    for key in ("pin", "server", "character", "char_slot"):
        raw["accounts"][0].pop(key, None)
    store_file.write_bytes(accounts._crypt(json.dumps(raw).encode("utf-8"), protect=True))

    loaded = accounts.load(store_file).accounts[0]
    assert loaded.name == good.name
    assert (loaded.pin, loaded.server, loaded.character, loaded.char_slot) == ("", "", "", None)


def test_pin_is_not_readable_on_disk(store_file):
    """二次密碼跟帳密一樣是機密，不准明文落地。"""
    account = _account()
    account.pin = "7342"
    accounts.save(AccountStore([account]), store_file)
    assert b'"pin"' not in store_file.read_bytes()
    assert b"7342" not in store_file.read_bytes()


def test_index_of_uses_name_not_position():
    store = AccountStore([_account("甲"), _account("乙")])
    assert store.index_of("乙") == 1
    assert store.index_of("丙") == -1


@pytest.mark.skipif(sys.platform == "win32", reason="這條只在沒有 DPAPI 的平台成立")
def test_refuses_to_write_plaintext_without_dpapi(store_file):
    with pytest.raises(AccountStoreError):
        accounts.save(AccountStore([_account()]), store_file)


def test_character_cache_is_replaced_not_merged(store_file):
    """每次登入整份覆蓋。合併的話刪掉的角色會永遠留在選單裡。"""
    from ro_toolbox.services.accounts import KnownCharacter

    account = _account()
    account.character = "小騎士"
    account.remember_characters([KnownCharacter("小騎士", 4), KnownCharacter("小商人", 0)])
    assert account.char_slot == 4

    # 遊戲裡把小騎士刪掉，剩下的角色往前遞補
    account.remember_characters([KnownCharacter("小商人", 0)])
    assert [c.name for c in account.known_characters] == ["小商人"]
    # 選定的角色已經不存在 → 格號要變成 None，不能留著舊的 4
    assert account.char_slot is None


def test_slot_of_returns_none_for_unknown_character():
    """查不到就回 None —— 呼叫端要拒絕登入，不准拿舊格號硬送。"""
    from ro_toolbox.services.accounts import KnownCharacter

    account = _account()
    account.remember_characters([KnownCharacter("小商人", 3)])
    assert account.slot_of("小商人") == 3
    assert account.slot_of("不存在的角色") is None


def test_character_cache_round_trips(store_file):
    from ro_toolbox.services.accounts import KnownCharacter

    account = _account()
    account.remember_characters([KnownCharacter("小騎士", 4), KnownCharacter("小商人", 0)])
    accounts.save(AccountStore([account]), store_file)
    loaded = accounts.load(store_file).accounts[0]
    assert [(c.name, c.slot) for c in loaded.known_characters] == [("小騎士", 4), ("小商人", 0)]


def test_broken_character_cache_entry_does_not_kill_the_account(store_file, caplog):
    """快取壞掉頂多選單少一筆，下次登入本來就會重抓 —— 不該讓整個帳號讀不出來。"""
    import json

    from ro_toolbox.services.accounts import KnownCharacter

    account = _account()
    account.remember_characters([KnownCharacter("小騎士", 4)])
    accounts.save(AccountStore([account]), store_file)

    raw = json.loads(accounts._crypt(store_file.read_bytes(), protect=False).decode("utf-8"))
    raw["accounts"][0]["known_characters"].append({"name": "壞的"})   # 少了 slot
    store_file.write_bytes(accounts._crypt(json.dumps(raw).encode("utf-8"), protect=True))

    with caplog.at_level("WARNING"):
        loaded = accounts.load(store_file).accounts[0]
    assert [c.name for c in loaded.known_characters] == ["小騎士"]
