"""在目標遊戲行程注入 send 攔截 stub，記錄每個送出的封包。

移植自 `s26016041/Angels-Online-toolbox` 的 `app/core/injector.py`。
組語 stub 原樣保留；針對本專案做了三處調整，因為 RO 的執行檔特性與原專案不同：

1. **IAT 改從執行中的行程記憶體找**。RO 的執行檔有加殼，檔案裡的匯入表是假的
   （37 個 DLL 各只匯入 1 個函式，見 GAMEDATA [PKT-008]），
   真正的 IAT 由殼在執行時還原，只能從記憶體撈。
2. **同時 patch 多個 IAT 項目**。RO 主模組內有 4 處指向 send，
   不知道遊戲實際走哪一個，全部改掉才不會漏攔。
3. **程式碼範圍用整個主模組**（加殼後拿不到可靠的 .text 範圍）。
4. **32 位元檢查改用行程的指標大小**，不信加殼過的檔頭。

原理
----
把目標執行檔匯入表（IAT）裡 send 那一格改指向自寫的機器碼；stub 記錄完後跳回
真正的 send，對遊戲完全透明。純讀寫記憶體、不改遊戲邏輯、不搶焦點。

相依：pymem、keystone-engine、pefile（皆延遲載入，未安裝時 available() 會回報）。
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import time
from ctypes import wintypes
from pathlib import Path

from ro_toolbox.core.intercept import CodeRange, InterceptedPacket

log = logging.getLogger(__name__)

# ⚠ 這幾支一定要宣告型別。不宣告的話 ctypes 預設把 HANDLE 當 32 位元 int 傳、
#   回傳值也當 c_int —— 64 位元 Python 上就是「把控制碼截半」。
_k32 = ctypes.windll.kernel32
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    wintypes.PDWORD,
]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.VirtualProtectEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_size_t,
    wintypes.DWORD,
    wintypes.PDWORD,
]
_k32.VirtualProtectEx.restype = wintypes.BOOL

# 環狀緩衝參數（沿用原專案的實測值）
_N = 128  # 槽數（2 的次方，取模用 and）
_FRAMES = 12  # 沿 EBP 往上走幾層
_FRAME_DWORDS = 6  # 每層記 6 個 dword：返回位址 + 前五個參數
_CAP = 200  # 每筆最多記幾 bytes payload
_PAYLOAD_OFF = 8 + _FRAMES * _FRAME_DWORDS * 4
_SLOT = _PAYLOAD_OFF + _CAP

_MACHINE_I386 = 0x014C
_DYNAMIC_BASE = 0x0040


def available() -> tuple[bool, str]:
    """回傳 (是否可用, 缺套件的安裝提示)。"""
    missing = []
    for module in ("pymem", "keystone", "pefile"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if not missing:
        return True, ""

    display = {"keystone": "keystone-engine"}
    names = " ".join(display.get(m, m) for m in missing)
    return False, (
        f"注入功能需要套件：{names}\n"
        r"請執行： .\.venv\Scripts\python.exe -m pip install -e .[packet]"
    )


def process_path(pid: int) -> str | None:
    """取得指定 PID 的執行檔完整路徑。"""
    handle = _k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        _k32.CloseHandle(handle)


class TargetUnsupportedError(RuntimeError):
    """目標執行檔不符合注入前提（非 32 位元、找不到 send 等）。"""


# ---------------------------------------------------------------------------
# 執行時 IAT 解析
# ---------------------------------------------------------------------------
# 加殼的執行檔（RO 就是，見 GAMEDATA [PKT-008]）檔案裡的匯入表是假的，
# 真正的 IAT 由殼在執行時還原。所以要從**執行中的行程記憶體**找：
#   1. 解析磁碟上 32 位元的 ws2_32.dll，取得目標函式的 RVA
#   2. 查目標行程裡 ws2_32.dll 的載入基址，兩者相加得到函式絕對位址
#   3. 掃描目標行程，找出「存著這個位址」的地方 —— 那些就是 IAT 項目


def _ws2_32_path() -> Path:
    """32 位元的 ws2_32.dll。

    ⚠ 不能用本行程的 ws2_32：開發用的 Python 是 64 位元，
    send 的 RVA 與 32 位元版完全不同（0x10320 vs 0x176F0）。
    """
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return Path(root) / "SysWOW64" / "ws2_32.dll"


def winsock_export_rva(func: str) -> int | None:
    """從 32 位元 ws2_32.dll 的匯出表取得函式 RVA。"""
    import pefile

    path = _ws2_32_path()
    if not path.is_file():
        raise TargetUnsupportedError(f"找不到 32 位元的 ws2_32.dll：{path}")

    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]]
        )
        for export in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if export.name and export.name.decode(errors="replace") == func:
                return export.address
        return None
    finally:
        pe.close()


def find_runtime_iat(scanner, func: str, module_name: str) -> tuple[int, list[int]]:
    """找出目標行程中指向 ws2_32.<func> 的 IAT 項目。

    只回傳落在 module_name（遊戲主模組）裡的項目——其他 DLL 也會匯入同一個
    函式，patch 那些等於去攔別人的網路流量。

    回傳 (函式絕對位址, [IAT 項目位址…])。
    """
    from ro_toolbox.services.memory_scan import VALUE_TYPES

    rva = winsock_export_rva(func)
    if rva is None:
        raise TargetUnsupportedError(f"ws2_32.dll 的匯出表裡找不到 {func}")

    ws2 = next(
        (m for m in scanner.list_modules() if m.name.lower().startswith("ws2_32")),
        None,
    )
    if ws2 is None:
        raise TargetUnsupportedError("目標行程沒有載入 ws2_32.dll")

    target = ws2.base + rva
    scanner.reset()
    scanner.first_scan(VALUE_TYPES["int32"], "exact", target, False, None)
    holders = [addr for addr, _v in scanner.results(limit=200)]
    scanner.reset()

    wanted = module_name.lower()
    entries = []
    for addr in holders:
        info = scanner.module_for_address(addr)
        if info and info[0].name.lower() == wanted:
            entries.append(addr)

    log.info(
        "%s 位於 %#x，%s 內找到 %d 個 IAT 項目：%s",
        func,
        target,
        module_name,
        len(entries),
        [hex(a) for a in entries],
    )
    return target, entries


def inspect_target(exe_path: str) -> dict:
    """解析目標執行檔，回傳注入需要的資訊。

    這裡就把不支援的情況擋下來，不要等到寫記憶體才失敗。
    """
    import pefile

    pe = pefile.PE(exe_path, fast_load=True)
    try:
        if pe.FILE_HEADER.Machine != _MACHINE_I386:
            raise TargetUnsupportedError(
                "這個執行檔不是 32 位元。注入用的 stub 是 x86 組語，"
                "64 位元目標需要改寫成 x64 才能用。"
            )

        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )

        iat = None
        for entry in pe.DIRECTORY_ENTRY_IMPORT or []:
            for imp in entry.imports:
                if imp.name and imp.name.decode(errors="replace") == "send":
                    iat = imp.address
                    break
            if iat:
                break

        if not iat:
            if _looks_packed(pe):
                raise TargetUnsupportedError(
                    "在執行檔的匯入表找不到 send。\n\n"
                    "這個執行檔看起來有加殼：多數 DLL 只匯入一兩個函式，\n"
                    "檔案裡的匯入表是假的，真正的 IAT 由殼在執行時才還原。\n"
                    "要 hook 得改成從執行中的行程記憶體找 IAT。\n"
                    "RO 就屬於這種情況，見 GAMEDATA [PKT-008]。"
                )
            raise TargetUnsupportedError(
                "在執行檔的匯入表找不到 send。\n"
                "可能是遊戲動態載入 ws2_32（GetProcAddress），"
                "或這不是遊戲的主執行檔。"
            )

        image_base = pe.OPTIONAL_HEADER.ImageBase
        aslr = bool(pe.OPTIONAL_HEADER.DllCharacteristics & _DYNAMIC_BASE)

        code_low = code_high = 0
        for section in pe.sections:
            if section.Name.rstrip(b"\x00") == b".text":
                code_low = image_base + section.VirtualAddress
                code_high = code_low + section.Misc_VirtualSize
                break

        return {
            "iat": iat,
            "image_base": image_base,
            "aslr": aslr,
            "code_low": code_low,
            "code_high": code_high,
        }
    finally:
        pe.close()


def _looks_packed(pe) -> bool:
    """加殼的常見跡象：匯入很多 DLL，但每個只匯入一兩個函式。

    RO 的 Ragexe.exe 是 37 個 DLL 各只匯入 1 個函式（見 GAMEDATA [PKT-008]）。
    """
    entries = getattr(pe, "DIRECTORY_ENTRY_IMPORT", None) or []
    if len(entries) < 5:
        return False
    thin = sum(1 for entry in entries if len(entry.imports) <= 2)
    return thin / len(entries) > 0.8


def build_stub_asm(wcnt: int, ring: int, origp: int) -> str:
    """產生 send 攔截 stub 的組合語言（32 位元）。

    ⚠ keystone 把無前綴數字當「十六進位」解析，所以所有數字一律用 0x 十六進位。
    stub 在每次 send 時：把 caller 返回位址、長度、呼叫鏈、payload 記進環狀緩衝，
    再跳回真正的 send（jmp，對遊戲完全透明）。

    ★ 呼叫鏈為什麼要沿 EBP 走，不能直接複製堆疊 ★
    ------------------------------------------------
    送封包的函式開頭可能是 `mov eax, 0xfdf8 ; call __chkstk`（配置 64KB 區域變數），
    這時**真正呼叫它的動作函式的返回位址在數萬 bytes 之外**。直接從 esp 複製一段
    堆疊永遠搆不到那裡，抓到的全是堆疊殘值，結果就是「每種封包看起來都出自同一
    個地方」，完全找不到動作函式。

    函式有標準的 push ebp / mov ebp,esp，所以 send 當下：
        [ebp+4] = 呼叫它的人 = 建構這種封包的函式
        [ebp]   = 上一層的 ebp，可以一直往上走

    ⚠ 走鏈要解參考堆疊上的位址，讀到壞位址 = 存取違規 = 遊戲當場關閉。
    三道保護，任一不過就停止並把剩下的補 0：
      1. 下界：必須大於目前 esp，而且每層都要比前一層高（防止繞回去無限迴圈）
      2. 上界：TEB(fs:[0x18]) 的 StackBase，也就是這條執行緒堆疊真正的頂端
      3. 對齊：必須 4 對齊

    ⚠ 因為會讀到 [ebp+0x18]，上界必須從 StackBase 收緊 0x20，
      否則最後一層可能讀出堆疊頂端 —— 那就是當場崩潰。
    """
    return f"""
    pushad
    mov eax, dword ptr [{wcnt:#x}]
    and eax, {(_N - 1):#x}
    imul eax, eax, {_SLOT:#x}
    add eax, {ring:#x}
    mov ebx, eax
    mov ecx, dword ptr [esp+0x20]
    mov dword ptr [ebx], ecx
    mov ecx, dword ptr [esp+0x2c]
    mov dword ptr [ebx+0x4], ecx

    lea edi, [ebx+0x8]
    mov esi, esp
    mov edx, ebp
    mov eax, dword ptr fs:[0x18]
    mov ebp, dword ptr [eax+0x4]
    sub ebp, 0x20
    mov ecx, {_FRAMES:#x}
    walk:
    cmp edx, esi
    jbe fill
    cmp edx, ebp
    jae fill
    test dl, 0x3
    jnz fill
    mov eax, dword ptr [edx+0x4]
    mov dword ptr [edi], eax
    mov eax, dword ptr [edx+0x8]
    mov dword ptr [edi+0x4], eax
    mov eax, dword ptr [edx+0xc]
    mov dword ptr [edi+0x8], eax
    mov eax, dword ptr [edx+0x10]
    mov dword ptr [edi+0xc], eax
    mov eax, dword ptr [edx+0x14]
    mov dword ptr [edi+0x10], eax
    mov eax, dword ptr [edx+0x18]
    mov dword ptr [edi+0x14], eax
    add edi, 0x18
    mov esi, edx
    mov edx, dword ptr [edx]
    dec ecx
    jnz walk
    jmp payload
    fill:
    mov dword ptr [edi], 0x0
    mov dword ptr [edi+0x4], 0x0
    mov dword ptr [edi+0x8], 0x0
    mov dword ptr [edi+0xc], 0x0
    mov dword ptr [edi+0x10], 0x0
    mov dword ptr [edi+0x14], 0x0
    add edi, 0x18
    dec ecx
    jnz fill

    payload:
    mov ecx, dword ptr [esp+0x2c]
    cmp ecx, {_CAP:#x}
    jbe cap_ok
    mov ecx, {_CAP:#x}
    cap_ok:
    mov esi, dword ptr [esp+0x28]
    lea edi, [ebx+{_PAYLOAD_OFF:#x}]
    cld
    rep movsb
    inc dword ptr [{wcnt:#x}]
    popad
    jmp dword ptr [{origp:#x}]
    """


class SendCapture:
    """掛在單一遊戲行程上的 send 攔截器。

    非執行緒安全，請在同一執行緒使用（UI 執行緒即可，讀取只是讀記憶體）。
    """

    def __init__(self, pid: int, exe_path: str) -> None:
        self._pid = pid
        self._exe = exe_path
        self._pm = None
        # 主模組內可能有多個 IAT 項目指向 send，全部要記著才能還原
        self._iats: list[int] = []
        self._orig = 0
        self._block = 0
        self._read = 0
        self._active = False
        self._code_range: CodeRange | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def code_range(self) -> CodeRange | None:
        return self._code_range

    # ------------------------------------------------------------------

    def start(self) -> None:
        """定位執行時 IAT、寫入 stub、改掉那些項目。失敗會拋例外，呼叫端要接。

        與原專案不同：RO 的執行檔有加殼，檔案裡的匯入表是假的
        （見 GAMEDATA [PKT-008]），所以改成從執行中的行程記憶體找 IAT。
        """
        import keystone
        import pymem

        from ro_toolbox.services.memory_scan import MemoryScanner

        module_name = os.path.basename(self._exe)

        scanner = MemoryScanner()
        scanner.open(self._pid)
        try:
            # stub 是 x86 組語，64 位元目標會直接把遊戲弄掛，先擋下來。
            # 用行程的實際指標大小判斷，比讀檔頭可靠（加殼的檔頭未必可信）。
            if scanner.pointer_size != 4:
                raise TargetUnsupportedError(
                    "這個行程不是 32 位元。注入用的 stub 是 x86 組語，"
                    "64 位元目標需要改寫成 x64 才能用。"
                )
            _send_addr, entries = find_runtime_iat(scanner, "send", module_name)
            if not entries:
                raise TargetUnsupportedError(
                    f"在執行中的 {module_name} 裡找不到指向 ws2_32.send 的 IAT 項目。\n"
                    "遊戲可能還沒建立連線，或是用別的函式送資料（WSASend）。"
                )
            main = next(
                m
                for m in scanner.list_modules()
                if m.name.lower() == module_name.lower()
            )
            # 加殼的執行檔沒辦法從檔案取得可靠的 .text 範圍，
            # 改用整個主模組當呼叫鏈的過濾範圍（寬一點但不會漏）。
            self._code_range = CodeRange(low=main.base, high=main.base + main.size)
        finally:
            scanner.close()

        pm = pymem.Pymem()
        pm.open_process_from_id(self._pid)

        # 所有 IAT 項目都存著同一個 send 位址，取第一個當原值即可
        orig = pm.read_uint(entries[0])

        block = pm.allocate(0x10000)
        wcnt = block  # 寫入計數
        origp = block + 4  # 原 send 位址
        ring = block + 64  # 環狀緩衝起點
        code = ring + _N * _SLOT
        pm.write_uint(wcnt, 0)
        pm.write_uint(origp, orig)

        asm = build_stub_asm(wcnt, ring, origp)
        ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_32)
        ks.syntax = keystone.KS_OPT_SYNTAX_INTEL
        shell, _ = ks.asm(asm, addr=code)
        pm.write_bytes(code, bytes(shell), len(shell))

        # 主模組內可能有多個 IAT 項目指向 send（RO 實測 4 個），
        # 不知道遊戲實際走哪一個，全部改掉才不會漏攔。
        for entry in entries:
            self._patch(pm, entry, code)

        self._pm = pm
        self._iats = list(entries)
        self._orig = orig
        self._block = block
        self._read = 0
        self._active = True
        log.info(
            "已注入 PID %s，%d 個 IAT 項目 → stub %#x，模組範圍 %#x-%#x",
            self._pid,
            len(entries),
            code,
            self._code_range.low,
            self._code_range.high,
        )

    def _aslr_delta(self, pm, info: dict) -> int:
        """有 ASLR 時，pefile 給的靜態位址要加上實際載入基址的偏移。"""
        if not info["aslr"]:
            return 0

        import pymem.process

        name = os.path.basename(self._exe)
        module = pymem.process.module_from_name(pm.process_handle, name)
        if module is None:
            raise TargetUnsupportedError(
                f"這個執行檔啟用了 ASLR，但找不到載入中的模組 {name}，無法修正位址。"
            )
        delta = module.lpBaseOfDll - info["image_base"]
        if delta:
            log.info("目標啟用 ASLR，位址偏移 %#x", delta)
        return delta

    @staticmethod
    def _protect(pm, addr: int, size: int, prot: int) -> int:
        """改頁面保護，回傳**原本的**保護值；失敗回 0。

        ⚠ 回 0 代表「沒改成」—— 0 不是合法的保護值，不能拿去還原。
        """
        old = wintypes.DWORD()
        ok = _k32.VirtualProtectEx(
            pm.process_handle, ctypes.c_void_p(addr), size, prot, ctypes.byref(old)
        )
        return old.value if ok else 0

    def _patch(self, pm, iat: int, target: int) -> None:
        old = self._protect(pm, iat, 8, 0x40)  # PAGE_EXECUTE_READWRITE
        pm.write_uint(iat, target)
        # ⚠ old = 0 代表上面那次就沒改成，拿 0 去還原只是再失敗一次，
        #   而且會把遊戲的 IAT 頁面留在可執行可寫的狀態。
        if old:
            self._protect(pm, iat, 8, old)

    # ------------------------------------------------------------------

    def read_new(self) -> list[InterceptedPacket]:
        """讀出上次之後新攔到的封包。"""
        if not self._active:
            return []

        pm = self._pm
        ring = self._block + 64
        write_count = pm.read_uint(self._block)

        start = self._read
        if write_count - start > _N:  # 溢位，只保留最後 N 筆
            start = write_count - _N

        now = time.time()
        out: list[InterceptedPacket] = []
        for index in range(start, write_count):
            slot = ring + (index % _N) * _SLOT
            head = pm.read_bytes(slot, _PAYLOAD_OFF)
            values = struct.unpack(f"<II{_FRAMES * _FRAME_DWORDS}I", head)
            caller, length = values[0], values[1]
            records = values[2:]

            frames = [records[i * _FRAME_DWORDS] for i in range(_FRAMES)]
            args = [
                tuple(records[i * _FRAME_DWORDS + 1 : (i + 1) * _FRAME_DWORDS])
                for i in range(_FRAMES)
            ]

            size = min(length, _CAP)
            data = bytes(pm.read_bytes(slot + _PAYLOAD_OFF, size)) if size > 0 else b""

            out.append(
                InterceptedPacket(
                    seq=index,
                    timestamp=now,
                    caller=caller,
                    length=length,
                    data=data,
                    frames=frames,
                    args=args,
                    code_range=self._code_range,
                )
            )

        self._read = write_count
        return out

    def stop(self) -> None:
        """把所有被改過的 IAT 項目還原，停止攔截。

        任一項還原失敗都要繼續還原其餘的——留一個指向 stub 的項目，
        遊戲下次送封包就會跳到我們已經不再維護的記憶體。
        """
        if not self._active:
            return
        for entry in self._iats:
            try:
                self._patch(self._pm, entry, self._orig)
            except Exception as exc:  # noqa: BLE001
                log.error("還原 IAT %#x 失敗：%s", entry, exc)
        self._active = False
        log.info("已停止注入 PID %s（還原 %d 個 IAT）", self._pid, len(self._iats))
