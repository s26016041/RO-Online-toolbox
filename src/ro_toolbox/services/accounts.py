"""帳號存檔：帳密與 OTP 種子，用 Windows DPAPI 加密後落地。

## 為什麼不寫進 settings.json

`config/settings.py` 的檔頭自己寫著「刻意讓設定檔可以直接手改、可以進版控」。
帳密跟 OTP 種子放進那種檔案，遲早會有人把它貼進 issue 或推上 GitHub。
所以另開一個檔，而且是加密的。

## 加密：DPAPI，不是自己捲的

用 `CryptProtectData`（綁**目前這個 Windows 使用者**）。金鑰由作業系統管，
程式裡沒有任何硬編的密碼——拷到別台電腦、或同一台電腦的別的使用者，都解不開。

這擋得住的是「檔案被複製走」。擋不住「有人拿到你已登入的帳號執行程式」——
本來就擋不住，別假裝做得到。想要更強就得每次開程式輸入主密碼，那是另一個題目。

**DPAPI 不可用（非 Windows）時一律拒絕存檔**，不准退化成明文。
少一個功能，好過安靜地把種子攤在硬碟上。

## 存什麼

存**身分**：道具用編號、帳號用種子本體。這裡不存任何「位置」性質的東西
（第幾列、上次選哪個），那些屬於 ui_state。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from ro_toolbox.config.paths import user_data_dir
from ro_toolbox.services.totp import OtpError, OtpSecret

log = logging.getLogger(__name__)

STORE_NAME = "accounts.dat"

# 綁在密文上的額外亂數。換掉它等於讓所有舊檔案解不開，所以**不准改**。
_ENTROPY = b"RO-Online-toolbox/accounts/v1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class AccountStoreError(RuntimeError):
    """存檔／讀檔失敗。訊息是要直接給使用者看的。"""


@dataclass(frozen=True, slots=True)
class KnownCharacter:
    """登入時從伺服器的角色清單學到的一格。

    `slot` 是**當下**看到的格號。它會被存下來，但只是快取 ——
    下一次登入會整份重抓覆蓋掉。真的要用時一律拿 `name` 去現查。

    ⚠ **`server` 不能省。** 每一台伺服器的角色是**各自獨立**的 ——
    實測同一個帳號在查爾斯與波利看到完全不同的角色，而且**同一個格號在兩台
    是不同的人**（兩份擷取裡使用者都選格號 3，但那是兩隻不同的角色）。
    混在一起存的話，切台之後會**安靜地選錯角色**。
    """

    name: str
    slot: int
    server: str = ""          # 哪一台（顯示名稱，例如「波利」）；舊資料是空的

    def to_dict(self) -> dict:
        return {"name": self.name, "slot": self.slot, "server": self.server}

    @classmethod
    def from_dict(cls, data: dict) -> KnownCharacter:
        # `server` 是後來才加的欄位 —— 舊的存檔沒有，讀成空字串就好
        # （空字串代表「不知道是哪一台」，查詢時會被當成通吃，不會擋住使用者）。
        return cls(
            name=str(data["name"]),
            slot=int(data["slot"]),
            server=str(data.get("server", "")),
        )


def characters_on(
    entries: Iterable[KnownCharacter], server: str
) -> list[KnownCharacter]:
    """挑出某一台伺服器的角色。`server` 給空字串＝不過濾。

    這是「哪些角色算在這一台」的**唯一**判斷處 —— `Account.characters_on`
    跟編輯對話框都走這裡。兩邊各寫一份的話，總有一天只改到一邊，
    然後下拉選單列出別台的角色，選下去就是安靜地登入到別人。

    條目的 `server` 是空字串時一律留著：那是還沒分台之前存的舊資料，
    擋掉會讓使用者的清單突然變空。
    """
    return [e for e in entries if not server or not e.server or e.server == server]


@dataclass
class Account:
    """一組登入資料，涵蓋整條登入流程要填的每一格。

    `name` 是使用者自己取的顯示名稱（「主帳-騎士」），跟遊戲帳號分開，
    因為多開的時候光看帳號分不出誰是誰。

    登入要走四關（封包結構見 GAMEDATA [PKT-046]）：

        帳密 0x0064 → OTP 0x0A74 → 選伺服器 → 二次密碼 0x08B8 → 選角 0x0066

    ## 角色為什麼存名字不存格號

    專案鐵則：**存身分，不存位置**。`0x0066` 送出去的確實是格號，
    但格號是位置 —— 刪角、換版面之後同一格可能是別隻角色，
    而「登入到錯的角色」是會安靜發生的那種錯。

    所以身分是 `character`（角色名稱）。`char_slot` 只是**上次看到的格號快取**，
    給 UI 顯示用；真的要登入時必須拿名字去角色清單裡現查，
    **查不到就拒絕登入**，不准退回用這個快取值。

    ## 角色清單為什麼綁在帳號上

    每個帳號的角色不一樣，而且客戶端解包資料裡當然沒有（那是伺服器的資料）。
    所以清單存在本機、**跟著這個帳號**，並且**每次自動登入都整份重抓覆蓋** ——
    在遊戲裡刪角、建角、換伺服器之後，下一次登入就會自己修正。
    `known_characters` 空的時候 UI 退回自由輸入，不做一份猜的選單。
    """

    name: str
    username: str
    password: str
    secret: OtpSecret
    pin: str = ""              # 二次密碼（0x08B8，實測四位數）
    #: `0x0064` 裡那 24 bytes 密碼欄的**密文**（十六進位字串）。
    #:
    #: 客戶端送出去的密碼是密文不是明文（[PKT-046]），我們自己組不出來 ——
    #: 但實測那串**每次登入完全一樣**（同帳號、不同行程、相隔數小時），
    #: 所以自動登入時順手從封包抓下來存著，之後就能**不開遊戲**直接跟伺服器
    #: 要角色清單（見 services/login_client）。
    #:
    #: ⚠ 這串等同密碼。它跟密碼一樣只存在 DPAPI 加密過的這個檔案裡。
    password_blob: str = ""
    server: str = ""           # 伺服器名稱，例如「波利」—— 存名字不存索引，同上
    character: str = ""        # 角色名稱＝身分
    char_slot: int | None = None   # 上次看到的格號，僅供顯示，登入時要重查
    #: 上次登入時伺服器給的角色清單。每次登入整份覆蓋，不做增量合併 ——
    #: 合併會把已經刪掉的角色永遠留在選單裡。
    known_characters: list[KnownCharacter] = field(default_factory=list)
    note: str = ""
    #: 這個帳號在帳號頁上有沒有被勾起來（批次登入要登哪幾個）。
    #:
    #: ⚠ **存在帳號自己身上，不是另外存一份名字清單。** 專案鐵則是
    #: 「存身分，不存位置」—— 而另存一份名字清單等於又多了一個要同步的東西：
    #: 改名、刪帳號、換順序都得記得去修它，漏掉就是安靜地勾錯人。
    #: 旗標跟著帳號走就沒有這個問題。
    selected: bool = False

    def slot_of(self, name: str, server: str = "") -> int | None:
        """拿角色名稱查現在的格號。查不到回 None —— 呼叫端要拒絕登入。

        ⚠ 指定 `server` 時只在那一台的清單裡找。**不要跨台找** ——
        同一個格號在兩台是不同的角色，找錯會安靜地登入到別人。
        """
        for entry in self.known_characters:
            if entry.name != name:
                continue
            if server and entry.server and entry.server != server:
                continue
            return entry.slot
        return None

    def characters_on(self, server: str) -> list[KnownCharacter]:
        """某一台伺服器上的角色清單（給 UI 的下拉選單用）。"""
        return characters_on(self.known_characters, server)

    def remember_characters(
        self, entries: list[KnownCharacter], server: str = ""
    ) -> None:
        """把某一台伺服器的角色清單記下來。

        ⚠ **只換掉那一台的**，別台的保留 —— 每台的角色互相獨立（見
        `KnownCharacter`）。早期版本整份覆蓋，切台登入一次就把另一台的清單
        洗掉了，然後用錯的格號選角。
        """
        tagged = [
            KnownCharacter(name=e.name, slot=e.slot, server=server or e.server)
            for e in entries
        ]
        others = [
            e for e in self.known_characters
            if server and e.server and e.server != server
        ]
        self.known_characters = others + tagged
        if self.character:
            self.char_slot = self.slot_of(self.character, server)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "username": self.username,
            "password": self.password,
            "secret": self.secret.to_dict(),
            "pin": self.pin,
            "password_blob": self.password_blob,
            "server": self.server,
            "character": self.character,
            "char_slot": self.char_slot,
            "known_characters": [c.to_dict() for c in self.known_characters],
            "note": self.note,
            "selected": self.selected,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Account:
        slot = data.get("char_slot")
        known = []
        for item in data.get("known_characters", []):
            try:
                known.append(KnownCharacter.from_dict(item))
            except (KeyError, TypeError, ValueError) as exc:
                # 快取壞掉不該讓整個帳號讀不出來 —— 下次登入本來就會重抓。
                log.warning("角色快取有一筆壞掉，已略過：%s", exc)
        return cls(
            name=data.get("name", ""),
            username=data.get("username", ""),
            password=data.get("password", ""),
            secret=OtpSecret.from_dict(data["secret"]),
            pin=data.get("pin", ""),
            password_blob=data.get("password_blob", ""),
            server=data.get("server", ""),
            character=data.get("character", ""),
            char_slot=int(slot) if slot is not None else None,
            known_characters=known,
            note=data.get("note", ""),
            # 後來才加的欄位 —— 舊存檔沒有就是沒勾。
            selected=bool(data.get("selected", False)),
        )

    @property
    def destination(self) -> str:
        """「登入到哪」的一行摘要，給清單顯示用。"""
        parts = [part for part in (self.server, self.character) if part]
        return " / ".join(parts) if parts else "—"


@dataclass
class AccountStore:
    """記憶體裡的帳號清單。UI 改這一份，按存檔才寫回硬碟。"""

    accounts: list[Account] = field(default_factory=list)

    def index_of(self, name: str) -> int:
        for i, account in enumerate(self.accounts):
            if account.name == name:
                return i
        return -1


# ---- DPAPI ----------------------------------------------------------------


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _take(blob: _Blob) -> bytes:
    """把 DPAPI 配的緩衝區複製出來再還給系統，不然每次呼叫都漏一塊記憶體。"""
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def crypto_available() -> bool:
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def _crypt(data: bytes, protect: bool) -> bytes:
    if not crypto_available():
        raise AccountStoreError(
            "這個系統沒有 Windows DPAPI，無法加密帳號檔。"
            "本工具不會把帳密與 OTP 種子以明文寫到硬碟，所以拒絕存檔。"
        )
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.restype = wintypes.BOOL

    source = _blob(data)
    entropy = _blob(_ENTROPY)
    out = _Blob()
    # 兩支的版面一模一樣（7 個參數），差別只在第 2 個：
    # CryptProtectData 是傳入的說明字串，CryptUnprotectData 是傳出的。
    # 兩邊都給 NULL 就好 —— 說明字串我們用不到。
    if not fn(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    ):
        code = ctypes.GetLastError()
        action = "加密" if protect else "解密"
        raise AccountStoreError(f"DPAPI {action}失敗（錯誤碼 {code}）。")
    return _take(out)


# ---- 讀寫 -----------------------------------------------------------------


def store_path() -> Path:
    return user_data_dir() / STORE_NAME


def load(path: Path | None = None) -> AccountStore:
    """讀帳號檔。檔案不存在回空清單；**解不開就大聲丟例外**。

    解不開的時候絕對不能回空清單假裝沒事 —— UI 會顯示「沒有帳號」，
    使用者按下一次存檔就把整份蓋掉了。安靜的資料遺失比報錯難查一百倍。
    """
    target = path or store_path()
    if not target.exists():
        return AccountStore()
    try:
        payload = json.loads(_crypt(target.read_bytes(), protect=False).decode("utf-8"))
    except AccountStoreError:
        raise
    except (OSError, ValueError) as exc:
        raise AccountStoreError(f"帳號檔壞了或不是本工具寫的：{exc}") from exc

    accounts = []
    for item in payload.get("accounts", []):
        try:
            accounts.append(Account.from_dict(item))
        except (KeyError, OtpError, ValueError) as exc:
            # 單一筆壞掉不該讓其他帳號一起消失，但要留下痕跡。
            log.error("帳號「%s」讀取失敗，已略過：%s", item.get("name", "?"), exc)
    return AccountStore(accounts)


def save(store: AccountStore, path: Path | None = None) -> None:
    """寫回硬碟。參數沒被證實的種子一律拒絕，不准存進去。"""
    for account in store.accounts:
        if not account.secret.params_confirmed:
            raise AccountStoreError(
                f"帳號「{account.name}」的 OTP 參數還沒驗證過，不能存。"
                "先用手機上的驗證碼確認一次。"
            )

    target = path or store_path()
    blob = _crypt(
        json.dumps(
            {"version": 1, "accounts": [a.to_dict() for a in store.accounts]},
            ensure_ascii=False,
        ).encode("utf-8"),
        protect=True,
    )
    # 先寫暫存再換過去：中途斷電時舊檔還在，不會變成半份壞檔。
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise AccountStoreError(f"帳號檔寫入失敗：{exc}") from exc
    log.info("已存下 %d 個帳號", len(store.accounts))
